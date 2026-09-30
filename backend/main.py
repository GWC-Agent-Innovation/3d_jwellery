import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps, UnidentifiedImageError
import httpx

from .generation import GenerationError, generate, provider
from .sketch_generation import generate_sketch_model

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
Image.MAX_IMAGE_PIXELS = 20_000_000
jobs = {}
tasks = set()
generation_lock = asyncio.Lock()

PUBLIC_FIELDS = ("job_id", "status", "created_at", "error", "image_url", "model_url",
                 "stage", "sketch_status", "sketch_error", "sketch_model_url", "sketch_image_url")


def job_dir(job_id):
    return DATA / job_id


def save_meta(job_id):
    public = {key: jobs[job_id][key] for key in PUBLIC_FIELDS if key in jobs[job_id]}
    with (job_dir(job_id) / "meta.json").open("w") as file:
        json.dump(public, file)


def load_jobs():
    for meta_file in sorted(DATA.glob("*/meta.json")):
        try:
            with meta_file.open() as file:
                job = json.load(file)
        except (OSError, ValueError):
            continue
        if isinstance(job, dict) and job.get("job_id") == meta_file.parent.name:
            if job.get("status") in ("queued", "generating"):
                job.update(status="failed", error="Generation was interrupted by a server restart.")
            if job.get("sketch_status") in ("queued", "generating"):
                job.update(sketch_status="failed", sketch_error="Sketch generation was interrupted. Retry the sketch.")
            jobs[job["job_id"]] = job


@asynccontextmanager
async def lifespan(app):
    load_jobs()
    yield
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Jewellery 3D POC", lifespan=lifespan)


def enabled():
    return provider() == "huggingface"


@app.get("/api/health")
def health():
    return {"generation_enabled": enabled(), "provider": provider()}


def update_job(job_id, **fields):
    jobs[job_id].update(fields)
    save_meta(job_id)


async def process(job_id, source, output):
    async with generation_lock:
        update_job(job_id, status="generating", stage="realistic_model")
        try:
            await asyncio.to_thread(generate, source, output)
            with output.open("rb") as file:
                if file.read(4) != b"glTF":
                    raise ValueError("Invalid GLB output")
            update_job(job_id, status="completed", model_url=f"/api/generations/{job_id}/model")
        except GenerationError as error:
            logging.warning("Generation failed: %s: %s", job_id, error)
            update_job(job_id, status="failed", error=str(error))
        except httpx.RequestError:
            logging.warning("Provider network error for job %s", job_id)
            update_job(job_id, status="failed", error="The generation provider connection was interrupted. Check its status before retrying; an accepted job may still be running.")
        except Exception:
            logging.exception("Generation failed: %s", job_id)
            update_job(job_id, status="failed", error="Generation failed. Check the backend log and try again.")
        if jobs[job_id]["status"] == "completed":
            await process_sketch_locked(job_id)
        else:
            update_job(job_id, sketch_status="failed", sketch_error="Generate the realistic model first.")


async def process_sketch_locked(job_id):
    folder = job_dir(job_id)
    output = folder / "sketch.glb"
    update_job(job_id, sketch_status="generating", sketch_error=None)
    loop = asyncio.get_running_loop()
    def progress(stage):
        loop.call_soon_threadsafe(update_sketch_stage, job_id, stage)
    try:
        await asyncio.to_thread(generate_sketch_model, folder / "input.png",
                                folder / "sketch.png", output, progress)
        with output.open("rb") as file:
            if file.read(4) != b"glTF":
                raise ValueError("Invalid sketch GLB")
        update_job(job_id, sketch_status="completed", stage="completed",
                   sketch_model_url=f"/api/generations/{job_id}/sketch-model",
                   sketch_image_url=f"/api/generations/{job_id}/sketch-image")
    except Exception as error:
        hint = str(error) if isinstance(error, GenerationError) else "The free sketch generation failed. Retry the sketch later."
        update_job(job_id, sketch_status="failed", stage="completed", sketch_error=hint)


def update_sketch_stage(job_id, stage):
    update_job(job_id, stage=stage)


async def retry_sketch(job_id):
    async with generation_lock:
        await process_sketch_locked(job_id)


@app.post("/api/generations", status_code=202)
async def create_generation(image: UploadFile):
    if not enabled():
        raise HTTPException(503, "Free APIs only: set GENERATION_PROVIDER=huggingface and restart the server.")
    if active_jobs() >= 3:
        raise HTTPException(429, "The generation queue is full. Try again shortly.")
    raw = await image.read(10 * 1024 * 1024 + 1)
    await image.close()
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, "Choose an image smaller than 10 MB.")
    job_id = uuid4().hex
    folder = job_dir(job_id)
    folder.mkdir()
    source = folder / "input.png"
    try:
        with Image.open(BytesIO(raw)) as original:
            if original.width * original.height > 20_000_000:
                raise ValueError("Image exceeds pixel limit")
            if original.format not in ("PNG", "JPEG", "WEBP"):
                raise ValueError("Unsupported format")
            original.load()
            prepared = ImageOps.exif_transpose(original).convert("RGBA")
            prepared.thumbnail((2048, 2048))
            prepared.save(source)
    except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(400, "Choose a valid JPG, PNG or WebP image under 20 megapixels.")
    jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "sketch_status": "queued",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "image_url": f"/api/generations/{job_id}/image",
    }
    save_meta(job_id)
    task = asyncio.create_task(process(job_id, source, folder / "output.glb"))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return dict(jobs[job_id])


def active_jobs():
    return sum(job["status"] in ("queued", "generating") or
               job.get("sketch_status") in ("queued", "generating") for job in jobs.values())


@app.post("/api/generations/{job_id}/sketch", status_code=202)
async def create_sketch(job_id: str):
    job = status(job_id)
    if not enabled():
        raise HTTPException(503, "Free APIs only: set GENERATION_PROVIDER=huggingface.")
    if job["status"] != "completed":
        raise HTTPException(409, "The realistic model must be ready first.")
    if job.get("sketch_status") in ("queued", "generating", "completed"):
        raise HTTPException(409, "This sketch is already queued, generating or completed.")
    if active_jobs() >= 3:
        raise HTTPException(429, "The generation queue is full. Try again shortly.")
    update_job(job_id, sketch_status="queued", sketch_error=None, stage="queued")
    task = asyncio.create_task(retry_sketch(job_id))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return dict(jobs[job_id])


@app.get("/api/generations")
def list_generations():
    ordered = sorted(jobs.values(), key=lambda job: job.get("created_at", ""), reverse=True)
    return [job for job in ordered if job["status"] == "completed"]


@app.get("/api/generations/{job_id}")
def status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found. The server may have restarted.")
    return jobs[job_id]


@app.get("/api/generations/{job_id}/image")
def image(job_id: str):
    status(job_id)
    return FileResponse(job_dir(job_id) / "input.png", media_type="image/png")


@app.get("/api/generations/{job_id}/model")
def model(job_id: str):
    job = status(job_id)
    if job["status"] != "completed":
        raise HTTPException(409, "The model is not ready yet.")
    return FileResponse(job_dir(job_id) / "output.glb", media_type="model/gltf-binary", filename="jewellery.glb")


@app.get("/api/generations/{job_id}/sketch-model")
def sketch_model(job_id: str):
    if status(job_id).get("sketch_status") != "completed":
        raise HTTPException(409, "The sketch model is not ready yet.")
    return FileResponse(job_dir(job_id) / "sketch.glb", media_type="model/gltf-binary", filename="jewellery-sketch.glb")


@app.get("/api/generations/{job_id}/sketch-image")
def sketch_image(job_id: str):
    if status(job_id).get("sketch_status") != "completed":
        raise HTTPException(409, "The sketch is not ready yet.")
    return FileResponse(job_dir(job_id) / "sketch.png", media_type="image/png", filename="jewellery-sketch.png")


app.mount("/", StaticFiles(directory=ROOT / "dist", html=True), name="frontend")

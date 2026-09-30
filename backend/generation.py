"""Server-side integration with fal.ai's hosted TRELLIS.2 queue API."""
import base64
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

ENDPOINT = "https://queue.fal.run/fal-ai/trellis-2"


class GenerationError(Exception):
    """A safe, actionable error that can be shown in the interface."""


def check_response(response):
    if response.is_success:
        return
    code = response.status_code
    try:
        body = response.json()
        detail = body.get("detail", body.get("message", "")) if isinstance(body, dict) else ""
    except ValueError:
        detail = ""
    if not isinstance(detail, str):
        detail = ""
    key = os.getenv("FAL_KEY", "")
    if key:
        detail = detail.replace(key, "[redacted]")
        for part in key.split(":"):
            if len(part) >= 8:
                detail = detail.replace(part, "[redacted]")
    detail = re.sub(r"https?://\S+|data:\S+", "[omitted]", detail)
    detail = " ".join(detail.split())[:350]
    hints = {
        401: "fal.ai rejected the API key. Check FAL_KEY and restart the server.",
        402: "fal.ai requires credits. Check billing for the account that owns this API key.",
        403: "fal.ai denied generation (403). Check the key's account credits, account restrictions, and access to TRELLIS.2 in the fal dashboard.",
        422: "fal.ai rejected the generation input. Check the image and model settings.",
        429: "fal.ai's rate or concurrency limit was reached. Wait before trying again.",
    }
    hint = hints.get(code, f"fal.ai returned HTTP {code}. Check the provider dashboard before retrying.")
    if detail:
        hint += f" Provider says: {detail}"
    raise GenerationError(hint)


def trusted_queue_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "queue.fal.run":
        raise ValueError("Unexpected provider queue URL")
    return url


def provider():
    return os.getenv("GENERATION_PROVIDER", "huggingface").strip().lower()


def generate(image_path: Path, output_path: Path):
    selected = provider()
    if selected == "huggingface":
        from .huggingface_generation import generate_huggingface
        return generate_huggingface(image_path, output_path)
    if selected == "fal":
        raise GenerationError("Paid generation is disabled. Set GENERATION_PROVIDER=huggingface.")
    raise GenerationError("Unknown generation provider. Set GENERATION_PROVIDER=huggingface.")


def generate_fal(image_path: Path, output_path: Path):
    image = base64.b64encode(image_path.read_bytes()).decode("ascii")
    headers = {"Authorization": f"Key {os.environ['FAL_KEY']}"}
    with httpx.Client(timeout=60) as client:
        response = client.post(ENDPOINT, headers=headers, json={
            "image_url": f"data:image/png;base64,{image}",
            "resolution": 1024, "decimation_target": 100000, "texture_size": 2048,
        })
        check_response(response)
        job = response.json()
        status_url = trusted_queue_url(job["status_url"])
        result_url = trusted_queue_url(job["response_url"])
        deadline = time.monotonic() + 25 * 60
        while time.monotonic() < deadline:
            response = client.get(status_url, headers=headers)
            check_response(response)
            status = response.json()
            if status["status"] == "COMPLETED":
                if status.get("error"):
                    raise GenerationError("fal.ai could not generate this model. Check the failed request in the fal dashboard before retrying.")
                break
            time.sleep(2)
        else:
            raise GenerationError("Generation timed out. It may still be running on fal.ai; check the dashboard before retrying to avoid duplicate charges.")
        response = client.get(result_url, headers=headers)
        check_response(response)
        url = response.json()["model_glb"]["url"]
        parsed = urlparse(url)
        if parsed.scheme != "https" or not (parsed.hostname == "fal.media" or (parsed.hostname or "").endswith(".fal.media")):
            raise ValueError("Unexpected model download host")
        # Never forward the API key to file storage.
        with client.stream("GET", url) as response:
            response.raise_for_status()
            size = 0
            with output_path.open("wb") as output:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 100 * 1024 * 1024:
                        raise ValueError("Model exceeds the POC's 100 MB output limit")
                    output.write(chunk)

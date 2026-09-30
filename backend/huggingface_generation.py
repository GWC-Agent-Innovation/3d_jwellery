"""Microsoft's public Gradio API, with one isolated session per generation."""
import os
import re
import shutil
import tempfile
from concurrent.futures import TimeoutError
from pathlib import Path

from gradio_client import Client, handle_file

from .generation import GenerationError

SPACE = "https://microsoft-trellis-2.hf.space"


def tokens():
    # Comma- or newline-separated so a quota-exhausted account can fall back to the next one.
    raw = os.getenv("HF_TOKEN", "")
    found = [token.strip() for token in re.split(r"[,\n]", raw) if token.strip()]
    # Explicit False prevents accidentally using an unrelated cached HF token.
    return found or [False]


def classify(error, stage):
    detail = str(error).lower()
    if "quota" in detail or "exceeded" in detail:
        return "quota", "The free Hugging Face GPU quota is exhausted. Wait for it to reset, or configure HF_TOKEN for your own account's quota."
    if any(word in detail for word in ("unauthorized", "authentication", "sign in", "sign up", "login", "401", "403")):
        return "auth", "Hugging Face requires account access for this request. Add a Hugging Face token as HF_TOKEN in .env and restart, or try the official demo in your browser."
    return "other", f"The public Hugging Face demo could not {stage}. It may be busy or temporarily unavailable. Try the official demo to check its status."


def attempt(token, image_path, output_path):
    client = None
    stage = "connect"
    with tempfile.TemporaryDirectory(prefix="trellis-hf-") as directory:
        try:
            client = Client(SPACE, token=token, verbose=False, analytics_enabled=False, download_files=directory,
                            httpx_kwargs={"timeout": 60})

            def call(name, **kwargs):
                job = client.submit(api_name=name, **kwargs)
                try:
                    return job.result(timeout=600)
                except TimeoutError:
                    job.cancel()
                    raise GenerationError("The free Hugging Face demo timed out. Try later when its queue is quieter. No paid fallback was used.")

            call("/start_session")
            stage = "prepare image"
            prepared = call("/preprocess_image", input=handle_file(str(image_path)))
            stage = "generate 3D"
            # Same Client retains the hidden gr.State used by the export endpoint.
            call("/image_to_3d", image=handle_file(prepared), seed=0, resolution="512")
            stage = "export GLB"
            result = call("/extract_glb", decimation_target=100000, texture_size=1024)
            path = Path(result[0] if isinstance(result, (list, tuple)) else result)
            if not path.is_file() or path.stat().st_size > 100 * 1024 * 1024:
                raise GenerationError("The demo did not return a usable GLB under 100 MB.")
            with path.open("rb") as file:
                if file.read(4) != b"glTF":
                    raise GenerationError("The demo returned an invalid GLB file.")
            shutil.copyfile(path, output_path)
        except GenerationError:
            raise
        except Exception as error:
            # Do not expose raw provider errors: they may contain tokens or file URLs.
            kind, hint = classify(error, stage)
            raise GenerationError(hint + " No paid fallback was used.", kind) from None
        finally:
            if client is not None:
                client.close()


def generate_huggingface(image_path: Path, output_path: Path):
    candidates = tokens()
    last_error = None
    for index, token in enumerate(candidates):
        try:
            attempt(token, image_path, output_path)
            return
        except GenerationError as error:
            kind = error.args[1] if len(error.args) > 1 else "other"
            last_error = GenerationError(error.args[0])
            if kind not in ("quota", "auth") or index == len(candidates) - 1:
                raise last_error from None
            # Otherwise fall through and retry with the next HF_TOKEN.
    raise last_error

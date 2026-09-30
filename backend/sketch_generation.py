"""Generate a sketch reference with Qwen, then a separate GLB with TRELLIS.2."""
import tempfile
from concurrent.futures import TimeoutError
from pathlib import Path

from gradio_client import Client, handle_file
from PIL import Image

from .generation import GenerationError
from .huggingface_generation import classify, generate_huggingface, tokens

SPACE = "https://qwen-qwen-image-edit.hf.space"
PROMPT = (
    "Redraw this exact jewellery piece as a detailed monochrome graphite pencil sketch. "
    "Preserve its silhouette, proportions, stones, settings, holes and viewing angle. "
    "Use dark pencil contours and fine cross-hatching on the object, with light grey "
    "shading that clearly describes its three-dimensional volume. Show the complete "
    "single object isolated on a plain white background. No text, annotations, "
    "dimensions, extra objects or multiple views."
)


def generate_sketch_image(source: Path, output: Path):
    # Use one configured account; do not rotate accounts to bypass GPU quotas.
    client = None
    with tempfile.TemporaryDirectory(prefix="jewellery-sketch-") as directory:
        try:
            client = Client(SPACE, token=tokens()[0], verbose=False,
                            analytics_enabled=False, download_files=directory,
                            httpx_kwargs={"timeout": 60})
            job = client.submit(image=handle_file(str(source)), prompt=PROMPT,
                                seed=0, randomize_seed=False, true_guidance_scale=4,
                                num_inference_steps=28, rewrite_prompt=False,
                                api_name="/infer")
            try:
                result = job.result(timeout=600)
            except TimeoutError:
                job.cancel()
                raise GenerationError("The free sketch API timed out. Try again later.") from None
            result = result[0] if isinstance(result, (list, tuple)) else result
            path = Path(result["path"] if isinstance(result, dict) else result)
            if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                raise GenerationError("The sketch API did not return a usable image under 20 MB.")
            with Image.open(path) as image:
                if image.width * image.height > 20_000_000:
                    raise GenerationError("The sketch image exceeds the pixel limit.")
                image.load()
                image.convert("RGB").save(output, format="PNG")
        except GenerationError:
            raise
        except Exception as error:
            _, hint = classify(error, "generate the sketch image")
            raise GenerationError(hint) from None
        finally:
            if client is not None:
                client.close()


def generate_sketch_model(source: Path, reference: Path, output: Path, progress):
    if not reference.is_file():
        progress("sketch_image")
        generate_sketch_image(source, reference)
    progress("sketch_model")
    generate_huggingface(reference, output)

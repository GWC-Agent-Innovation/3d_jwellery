import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from backend import main
from backend.generation import GenerationError, generate
from backend.sketch_generation import generate_sketch_image, generate_sketch_model


class SketchTests(unittest.TestCase):
    def test_qwen_api_image_and_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            output = Path(directory) / "sketch.png"
            Image.new("RGB", (12, 12)).save(source)
            client = MagicMock()
            client.submit.return_value.result.return_value = (str(source), 0)
            with patch("backend.sketch_generation.Client", return_value=client):
                generate_sketch_image(source, output)
            with Image.open(output) as image:
                self.assertEqual(image.format, "PNG")
            args = client.submit.call_args.kwargs
            self.assertEqual(args["api_name"], "/infer")
            self.assertFalse(args["rewrite_prompt"])
            self.assertIn("pencil sketch", args["prompt"])
            client.close.assert_called_once()

    def test_sketch_reference_drives_separate_3d_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            source, reference, output = [Path(directory) / name for name in ("input.png", "sketch.png", "sketch.glb")]
            with patch("backend.sketch_generation.generate_sketch_image") as draw, \
                 patch("backend.sketch_generation.generate_huggingface") as build:
                generate_sketch_model(source, reference, output, lambda stage: None)
            draw.assert_called_once_with(source, reference)
            build.assert_called_once_with(reference, output)

    def test_sketch_retry_reuses_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "sketch.png"
            reference.write_bytes(b"existing")
            with patch("backend.sketch_generation.generate_sketch_image") as draw, \
                 patch("backend.sketch_generation.generate_huggingface") as build:
                generate_sketch_model(Path("input.png"), reference, Path("sketch.glb"), lambda stage: None)
            draw.assert_not_called()
            build.assert_called_once()

    def test_partial_success_keeps_realistic_model(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "DATA", Path(directory)), \
             patch.object(main, "jobs", {}), patch.object(main, "generation_lock", asyncio.Lock()):
            folder = Path(directory) / "test"
            folder.mkdir()
            main.jobs["test"] = {"job_id": "test", "status": "queued", "sketch_status": "queued"}
            def fake_model(source, output):
                output.write_bytes(b"glTF" + bytes(16))
            with patch.object(main, "generate", fake_model), \
                 patch.object(main, "generate_sketch_model", side_effect=GenerationError("Free quota exhausted")):
                asyncio.run(main.process("test", folder / "input.png", folder / "output.glb"))
            self.assertEqual(main.jobs["test"]["status"], "completed")
            self.assertEqual(main.jobs["test"]["sketch_status"], "failed")
            self.assertTrue((folder / "output.glb").exists())
            self.assertIn("model_url", main.jobs["test"])

    def test_paid_provider_cannot_run(self):
        with patch.dict(os.environ, {"GENERATION_PROVIDER": "fal", "FAL_KEY": "test-only"}), \
             patch("backend.generation.generate_fal") as paid:
            with self.assertRaisesRegex(GenerationError, "Paid generation is disabled"):
                generate(Path("input.png"), Path("output.glb"))
            paid.assert_not_called()

    def test_sketch_errors_redact_provider_details(self):
        with patch("backend.sketch_generation.Client", side_effect=RuntimeError("secret-token quota exceeded")):
            with self.assertRaises(GenerationError) as caught:
                generate_sketch_image(Path("input.png"), Path("sketch.png"))
        self.assertNotIn("secret-token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

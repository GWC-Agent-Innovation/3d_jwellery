import os
import tempfile
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from backend import main
from backend.generation import GenerationError, check_response
import httpx


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = patch.object(main, "DATA", Path(self.temp.name))
        self.storage.start()
        self.env = patch.dict(os.environ, {"FAL_KEY": "", "GENERATION_PROVIDER": "huggingface"})
        self.env.start()
        def fake_sketch(source, reference, output, progress):
            progress("sketch_image")
            Image.new("RGB", (16, 16), "white").save(reference)
            progress("sketch_model")
            output.write_bytes(b"glTFsketch" + bytes(16))
        self.sketch = patch.object(main, "generate_sketch_model", fake_sketch)
        self.sketch.start()
        main.jobs.clear()
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.sketch.stop()
        self.env.stop()
        self.storage.stop()
        self.temp.cleanup()

    def photo(self):
        buffer = BytesIO()
        Image.new("RGB", (16, 16), "white").save(buffer, format="PNG")
        return {"image": ("photo.png", buffer.getvalue(), "image/png")}

    def test_page_and_paid_provider_blocked(self):
        os.environ["GENERATION_PROVIDER"] = "fal"
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertFalse(self.client.get("/api/health").json()["generation_enabled"])
        self.assertEqual(self.client.post("/api/generations", files=self.photo()).status_code, 503)

    def test_invalid_image(self):
        os.environ["FAL_KEY"] = "test-only"
        response = self.client.post("/api/generations", files={"image": ("fake.png", b"invalid", "image/png")})
        self.assertEqual(response.status_code, 400)

    def test_job_to_download(self):
        os.environ["FAL_KEY"] = "test-only"
        # Test fixture bytes only: no paid generation or sample in the application.
        payload = b"glTF" + bytes(16)
        def fake_generate(source, output):
            self.assertTrue(source.exists())
            output.write_bytes(payload)
        with patch.object(main, "generate", fake_generate):
            response = self.client.post("/api/generations", files=self.photo())
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["job_id"]
            for _ in range(100):
                status = self.client.get(f"/api/generations/{job_id}").json()
                if status["status"] == "failed" or (status["status"] == "completed" and status.get("sketch_status") in ("completed", "failed")):
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "completed")
            model = self.client.get(status["model_url"])
            self.assertEqual(status["sketch_status"], "completed")
            sketch = self.client.get(status["sketch_model_url"])
            self.assertEqual(sketch.content, b"glTFsketch" + bytes(16))
            self.assertEqual(self.client.get(status["sketch_image_url"]).headers["content-type"], "image/png")
            self.assertEqual(self.client.post(f"/api/generations/{job_id}/sketch").status_code, 409)
            self.assertEqual(model.content, payload)
            self.assertEqual(model.headers["content-type"], "model/gltf-binary")

    def test_unknown_job(self):
        self.assertEqual(self.client.get("/api/generations/missing").status_code, 404)

    def test_add_sketch_to_existing_project_without_regenerating_realistic(self):
        job_id = "a" * 32
        folder = main.job_dir(job_id)
        folder.mkdir()
        Image.new("RGB", (16, 16)).save(folder / "input.png")
        (folder / "output.glb").write_bytes(b"glTF-realistic")
        main.jobs[job_id] = {"job_id": job_id, "status": "completed", "model_url": f"/api/generations/{job_id}/model"}
        main.save_meta(job_id)
        with patch.object(main, "generate") as realistic:
            response = self.client.post(f"/api/generations/{job_id}/sketch")
            self.assertEqual(response.status_code, 202)
            for _ in range(100):
                job = self.client.get(f"/api/generations/{job_id}").json()
                if job.get("sketch_status") in ("completed", "failed"):
                    break
                time.sleep(0.01)
            self.assertEqual(job["sketch_status"], "completed")
            realistic.assert_not_called()
        self.assertEqual(self.client.get(job["model_url"]).content, b"glTF-realistic")
        self.assertEqual(self.client.get(job["sketch_model_url"]).status_code, 200)

    def test_unready_sketch_download_and_busy_retry(self):
        job_id = "b" * 32
        main.jobs[job_id] = {"job_id": job_id, "status": "completed", "sketch_status": "queued"}
        self.assertEqual(self.client.get(f"/api/generations/{job_id}/sketch-model").status_code, 409)
        self.assertEqual(self.client.post(f"/api/generations/{job_id}/sketch").status_code, 409)

    def test_past_projects_list_and_input_image(self):
        os.environ["FAL_KEY"] = "test-only"
        payload = b"glTF" + bytes(16)
        def fake_generate(source, output):
            output.write_bytes(payload)
        with patch.object(main, "generate", fake_generate):
            response = self.client.post("/api/generations", files=self.photo())
            job_id = response.json()["job_id"]
            for _ in range(100):
                status = self.client.get(f"/api/generations/{job_id}").json()
                if status["status"] == "failed" or (status["status"] == "completed" and status.get("sketch_status") in ("completed", "failed")):
                    break
                time.sleep(0.01)
            self.assertEqual(status["status"], "completed")
            listing = self.client.get("/api/generations").json()
            self.assertEqual([job["job_id"] for job in listing], [job_id])
            image_response = self.client.get(status["image_url"])
            self.assertEqual(image_response.status_code, 200)
            self.assertEqual(image_response.headers["content-type"], "image/png")

    def test_jobs_reload_from_disk_after_restart(self):
        os.environ["FAL_KEY"] = "test-only"
        payload = b"glTF" + bytes(16)
        def fake_generate(source, output):
            output.write_bytes(payload)
        with patch.object(main, "generate", fake_generate):
            response = self.client.post("/api/generations", files=self.photo())
            job_id = response.json()["job_id"]
            for _ in range(100):
                status = self.client.get(f"/api/generations/{job_id}").json()
                if status["status"] == "failed" or (status["status"] == "completed" and status.get("sketch_status") in ("completed", "failed")):
                    break
                time.sleep(0.01)
        main.jobs.clear()
        main.load_jobs()
        self.assertEqual(main.jobs[job_id]["status"], "completed")

    def test_provider_denial_is_actionable_and_redacted(self):
        os.environ["FAL_KEY"] = "test-secret-value"
        response = httpx.Response(403, json={"detail": "Account balance exhausted. test-secret-value"})
        with self.assertRaises(GenerationError) as caught:
            check_response(response)
        self.assertIn("Account balance exhausted", str(caught.exception))
        self.assertNotIn("test-secret-value", str(caught.exception))

    def test_provider_denial_reaches_frontend(self):
        os.environ["FAL_KEY"] = "test-only"
        with patch.object(main, "generate", side_effect=GenerationError("fal.ai denied generation (403). Check account credits.")):
            response = self.client.post("/api/generations", files=self.photo())
            job_id = response.json()["job_id"]
            for _ in range(100):
                job = self.client.get(f"/api/generations/{job_id}").json()
                if job["status"] == "failed":
                    break
                time.sleep(0.01)
            self.assertEqual(job["status"], "failed")
            self.assertIn("Check account credits", job["error"])


if __name__ == "__main__":
    unittest.main()

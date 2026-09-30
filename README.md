# Jewellery: two generated 3D models

One uploaded photo produces two independent, rotatable GLB files through free public Hugging Face demo APIs:

1. TRELLIS.2 generates the realistic model from the photo.
2. Qwen Image Edit redraws the photo as a pencil sketch.
3. TRELLIS.2 generates a separate 3D model using that sketch image.

The viewer loads each GLB with its generated materials. It does not apply a sketch filter. Both files have separate download links. The sketch appearance is experimental: TRELLIS.2 can reinterpret the drawing, and the two meshes may have different shapes. No live sketch result has been verified yet.

## Run

Run `./start.ps1`, then open http://127.0.0.1:8017. Restart an already-running server after backend changes.

For a fresh installation, install Python 3.11+, create `.venv`, install `requirements.txt`, copy `.env.example` to `.env`, and run:

```powershell
.venv/Scripts/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8017
```

Configuration:

```dotenv
GENERATION_PROVIDER=huggingface
HF_TOKEN=
```

An optional server-side Hugging Face token is used for access to the free demos. The Qwen adapter uses the first configured token. Never commit `.env`. Paid generation is disabled even if a FAL_KEY is present; there is no paid fallback.

## Free service limits

Generate sends the uploaded image to Microsoft's TRELLIS.2 Space and Qwen's Image Edit Space. The generated sketch is then sent to TRELLIS.2. Public demos have shared queues, account quotas and availability limits; there is no guaranteed free capacity. Each new project requires two 3D generations and one image edit.

- https://huggingface.co/spaces/microsoft/TRELLIS.2
- https://huggingface.co/spaces/Qwen/Qwen-Image-Edit

Qwen uses `/infer`, 28 steps, and disables prompt rewriting. TRELLIS uses the same session for preprocessing, generation and export, at 512 generation resolution, 100,000 target faces and 1024px textures.

## Partial results and saved projects

A successful realistic model remains downloadable if sketch generation fails. Retry sketch only retries the sketch branch, reusing the saved sketch image when available. Old projects can generate their missing sketch without regenerating the realistic model. Jobs and URLs are persisted under `data/<job_id>/`; interrupted jobs are marked as failed on restart.

API endpoints:

- `POST /api/generations`: upload image and queue both outputs.
- `GET /api/generations/{id}`: realistic `status`, `sketch_status`, `stage`, result URLs and safe errors. Overall `status=completed` means the realistic model is ready; clients must also inspect `sketch_status`.
- `POST /api/generations/{id}/sketch`: queue a missing/failed sketch.
- `GET /api/generations/{id}/model`: realistic GLB.
- `GET /api/generations/{id}/sketch-model`: sketch GLB.
- `GET /api/generations/{id}/sketch-image`: generated reference PNG.

## Validation

Run `.venv/Scripts/python -m unittest discover -s tests -v` and `node --check dist/app.js`.
Tests mock external generation and cover separate output downloads, sketch inputs, partial failure, retries, legacy projects, persistence, redacted errors and disabled paid generation. The live Qwen API schema was checked; an end-to-end image transmission test is pending user approval. Browser automation is unavailable in this session, so interactive visual QA remains pending.

## POC limits

Local single-user app without authentication; use one Uvicorn worker and do not expose publicly. Files remain in `data/`. Uploads support JPG/PNG/WebP up to 10 MB and 20 megapixels. GLBs are limited to 100 MB. Three.js loads from jsDelivr. Model accuracy, hidden geometry and retention of pencil strokes are not guaranteed. Timeouts may leave remote work running.

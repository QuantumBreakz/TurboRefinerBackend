# Backend (FastAPI)

FastAPI service powering multi-pass refinement, real-time progress (SSE/WS), diffs, analytics, and job management.

## Stack
- FastAPI, Pydantic
- sse-starlette for SSE
- Uvicorn (dev)

## Environment
- `OPENAI_API_KEY` (if using OpenAI in pipeline)
- `BACKEND_API_KEY` (optional; enable request auth)
- `REFINER_OUTPUT_DIR` (optional; where to write outputs)

## Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (Dev)
```bash
python -m backend.api.main
```
Default: `http://0.0.0.0:8000`.

## Key Endpoints
- POST `/refine/run` — starts refinement; streams progress via SSE
- GET `/ws/progress/{job_id}` — WebSocket broadcast for job events (optional)
- GET `/jobs` `/jobs/{job_id}/status` — job listing/status
- GET `/analytics/summary` — summary of usage and job metrics
- GET `/refine/diff?fileId=...&fromPass=...&toPass=...` — diff across passes
- GET `/style/templates` — lists .docx style templates from `./templates`
- POST `/strategy/feedback` — record strategy feedback (stored under `strategy_feedback/`)

## Streaming (SSE)
- Uses `EventSourceResponse`. Anti-buffering headers are set; per-pass events are yielded and followed by a keepalive to flush.
- Terminal markers: `done` and `error` events are sent explicitly; a `stream_end` JSON event is also emitted.

## File Versions for Diff
- Stored by `backend/core/file_versions.py`. Each pass content and metadata saved for diff retrieval.

## Strategy Feedback
- Managed by `backend/core/strategy_feedback.py`.
- Folder `strategy_feedback/` is created only to persist feedback when `/strategy/feedback` is called.

## Templates
- `./templates` is read-only; place `.docx` files there to surface in `/style/templates`.

## Security
- Optionally enforce `BACKEND_API_KEY` in request headers (implement check in endpoints if required).
- Sanitize paths for download endpoints; never trust user-provided paths.

## Deployment
- Uvicorn/Gunicorn: `uvicorn backend.api.main:app --host 0.0.0.0 --port 8000`
- Reverse proxy: ensure SSE buffering disabled (`X-Accel-Buffering: no` for Nginx); set `Cache-Control: no-transform`.

## Troubleshooting
- No `pass_complete` events: confirm generator yields per pass and keepalive; verify proxy isn’t buffering.
- Only `stream_end`: inspect Next.js proxy and headers; ensure Node runtime.
- Context length errors: backend truncates and retries; consider reducing input size.

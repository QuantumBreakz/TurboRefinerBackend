# Defensive import to surface errors cleanly in serverless
try:
    from backend.api.main import app  # FastAPI ASGI app
except Exception as e:
    # Fallback minimal app to expose error at runtime instead of process exit
    import traceback as _tb
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, PlainTextResponse
    _err = f"{type(e).__name__}: {e}\n{_tb.format_exc()}"
    print(_err)  # surface in platform logs
    app = FastAPI(title="Turbo Refiner - Fallback")
    @app.get("/", response_class=JSONResponse)
    async def _root():
        return {"error": "startup_failure", "detail": _err}
    @app.get("/__error", response_class=PlainTextResponse)
    async def _error_text():
        return _err
    @app.get("/{path:path}", response_class=JSONResponse)
    async def _fallback(path: str):
        return JSONResponse({"error": "startup_failure", "path": path, "detail": _err}, status_code=500)



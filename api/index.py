# Defensive import to surface errors cleanly in serverless
try:
    from backend.api.main import app  # FastAPI ASGI app
except Exception as e:
    # Fallback minimal app to expose error at runtime instead of process exit
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    app = FastAPI(title="Turbo Refiner - Fallback")
    @app.get("/{path:path}")
    async def _fallback(path: str):
        return JSONResponse({"error": "startup_failure", "detail": str(e)}, status_code=500)



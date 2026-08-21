import logging
import contextlib
from typing import Dict, Any
from fastapi import FastAPI, Depends, Request
from pydantic_settings import BaseSettings

# Structured logging setup
logger = logging.getLogger("app")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}')
handler.setFormatter(formatter)
logger.addHandler(handler)

class Settings(BaseSettings):
    port: int = 3000
    host: str = "0.0.0.0"

settings = Settings()

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize resources
    logger.info("Service starting up")
    app.state.cleanup_counter = 0
    yield
    # Shutdown: Clean up resources demonstrably
    app.state.cleanup_counter += 1
    logger.info("Service shutting down, cleaning up resources [SHUTDOWN_MARKER]")

app = FastAPI(
    title="Python Service",
    description="Minimal FastAPI service",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None, # Disable Swagger UI by default for bare APIs if desired
    redoc_url=None,
)

# Authentik header helper
def get_user_from_headers(request: Request) -> Dict[str, str]:
    """
    Extracts Authentik ForwardAuth trusted identity headers.
    Never logs these values.
    """
    return {
        "username": request.headers.get("x-authentik-username", ""),
        "email": request.headers.get("x-authentik-email", ""),
        "name": request.headers.get("x-authentik-name", ""),
        "uid": request.headers.get("x-authentik-uid", ""),
        "groups": request.headers.get("x-authentik-groups", ""),
    }

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/api/info")
async def api_info(user: Dict[str, str] = Depends(get_user_from_headers)):
    logger.info("Processed /api/info request")
    # Return neutral typed endpoint info, optionally including if a user is authenticated
    is_authenticated = bool(user["username"])
    return {
        "service": "python-service",
        "status": "operational",
        "authenticated": is_authenticated
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)

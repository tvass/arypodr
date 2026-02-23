import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import api.models  # noqa: F401 — register all ORM models on Base.metadata
from api.config import settings
from api.database import Base, engine
from api.routers import admin, feed
from api.services import worker
from api.version import VERSION

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(levelname)s:     %(name)s:%(message)s",
)

_logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables, then start the background download worker.
    _logger.info("Arypodr v%s starting", VERSION)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    worker_task = asyncio.create_task(worker.start())
    yield

    # Shutdown: cancel the worker, then close the DB.
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Arypodr",
        description="Self-hosted podcast ownership platform",
        version=VERSION,
        debug=settings.DEBUG,
        lifespan=lifespan,
    )

    app.include_router(admin.router, prefix="/admin", tags=["admin"])
    app.include_router(feed.router, prefix="/feed", tags=["feed"])

    # Serve archived audio files.  Both the worker and AntennaPod use this path.
    media_path = Path(settings.MEDIA_PATH)
    media_path.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=media_path), name="media")

    @app.get("/healthz", tags=["health"])
    async def healthz():
        return {"status": "ok"}

    return app


app = create_app()

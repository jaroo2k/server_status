from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import events, servers
from app.core.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Server Monitor", lifespan=lifespan)
app.include_router(servers.router)
app.include_router(events.router)
app.mount(
    "/admin",
    StaticFiles(directory=Path(__file__).parent / "static" / "admin", html=True),
    name="admin",
)


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/admin/")


@app.get("/healthz")
async def healthz():
    return {"ok": True}

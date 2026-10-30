from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import events, servers
from app.core.db import init_db


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Server Monitor", lifespan=lifespan)
app.include_router(servers.router)
app.include_router(events.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}

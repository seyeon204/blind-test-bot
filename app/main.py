import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.v1.router import router
from app.utils.claude_client import close_client
from app.services.test_orchestrator import gc_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(gc_loop())
    yield
    await close_client()


app = FastAPI(
    title="Blind Test Bot",
    description="Generates and runs API test cases from a spec file.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)

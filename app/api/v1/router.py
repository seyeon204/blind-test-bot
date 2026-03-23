from fastapi import APIRouter
from app.api.v1.endpoints import test_runs, health, spec

router = APIRouter(prefix="/api/v1")
router.include_router(test_runs.router)
router.include_router(spec.router)
router.include_router(health.router)

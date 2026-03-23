from fastapi import APIRouter
from app.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": settings.claude_model}

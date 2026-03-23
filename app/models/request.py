from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class TestStrategy(str, Enum):
    minimal = "minimal"        # ~2 TCs per endpoint
    standard = "standard"      # ~5 TCs per endpoint
    exhaustive = "exhaustive"  # ~10+ TCs per endpoint


class GeneratorType(str, Enum):
    local = "local"    # rule-based, free, no API call
    claude = "claude"  # Claude AI, smarter but costs tokens


class GenerateConfig(BaseModel):
    generator: GeneratorType = GeneratorType.local
    strategy: TestStrategy = TestStrategy.standard
    auth_headers: dict[str, str] = Field(default_factory=dict)
    max_tc_per_endpoint: int | None = Field(default=None, ge=1, le=100)
    enable_rate_limit_tests: bool = False


class ExecuteConfig(BaseModel):
    target_base_url: str
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    webhook_url: Optional[str] = None

    @field_validator("target_base_url")
    @classmethod
    def strip_url(cls, v: str) -> str:
        return v.strip()

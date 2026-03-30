from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class TestStrategy(str, Enum):
    minimal = "minimal"        # ~2 TCs per endpoint
    standard = "standard"      # ~5 TCs per endpoint
    exhaustive = "exhaustive"  # ~10+ TCs per endpoint


class LLMProvider(str, Enum):
    local      = "local"       # rule-based, free, no AI
    ai_recom   = "ai-recom"    # AI-recommended preset (best free CLI combo per phase)
    claude_api = "claude-api"  # Anthropic API (Claude)
    claude_cli = "claude-cli"  # Claude CLI subprocess
    gemini_api = "gemini-api"  # Google Gemini API
    gemini_cli = "gemini-cli"  # Gemini CLI subprocess
    codex_api  = "codex-api"   # OpenAI API (GPT / Codex)
    codex_cli  = "codex-cli"   # Codex CLI subprocess


class GenerateConfig(BaseModel):
    strategy: TestStrategy = TestStrategy.standard
    auth_headers: dict[str, str] = Field(default_factory=dict)
    max_tc_per_endpoint: int | None = Field(default=None, ge=1, le=100)
    enable_rate_limit_tests: bool = False
    # Provider for each pipeline phase — defaults to "local" (rule-based, free)
    phase1_provider: LLMProvider = LLMProvider.local  # test plan (tc_planner); local = skip
    phase2_provider: LLMProvider = LLMProvider.local  # TC generation
    phase3_provider: LLMProvider = LLMProvider.local  # AI validation; local = heuristic


class ExecuteConfig(BaseModel):
    target_base_url: str
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    webhook_url: Optional[str] = None

    @field_validator("target_base_url")
    @classmethod
    def strip_url(cls, v: str) -> str:
        return v.strip()

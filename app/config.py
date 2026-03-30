from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: str
    max_concurrent_requests: int = 10
    request_timeout_seconds: int = 30
    claude_model: str = "claude-sonnet-4-6"       # spec parsing / document understanding
    claude_tc_model: str = "claude-haiku-4-5-20251001"  # TC generation (faster, cheaper)
    anthropic_rpm: int = 40  # requests-per-minute cap (0 = disabled)
    tc_batch_delay_seconds: int = 10  # delay between TC generation batches
    # Memory TTL
    run_ttl_hours: int = 24
    # AI validator timeout
    ai_validate_timeout_seconds: int = 120
    # Response body truncation
    max_response_body_bytes: int = 10240
    # Gemini API (used when provider = "gemini-api")
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    # OpenAI / Codex API (used when provider = "codex-api")
    codex_api_key: str = ""
    codex_model: str = "gpt-4o-mini"
    # LLM provider — "anthropic"|"claude-api" | "claude-cli" | "gemini-api" | "gemini-cli" | "codex-api" | "codex-cli"
    # "anthropic" and "claude-api" are equivalent (both use the Anthropic API).
    llm_provider: str = "anthropic"
    # Per-phase provider overrides (empty string = inherit from llm_provider)
    # Valid values: "" | "anthropic" | "claude-api" | "claude-cli" | "gemini-api" | "gemini-cli" | "codex-api" | "codex-cli"
    phase0_provider: str = ""   # Phase 0: document/PDF spec parsing
    phase1_provider: str = ""   # Phase 1: test plan (tc_planner)
    phase2a_provider: str = ""  # Phase 2a: individual TC generation
    phase2b_provider: str = ""  # Phase 2b: scenario TC generation
    phase3_provider: str = ""   # Phase 3: AI validation
    # Cost tracking — Haiku pricing (per 1M tokens, USD)
    model_input_price_per_mtok: float = 0.80
    model_output_price_per_mtok: float = 4.00
    model_cache_creation_price_per_mtok: float = 1.00
    model_cache_read_price_per_mtok: float = 0.08


settings = Settings()

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
    # Cost tracking — Haiku pricing (per 1M tokens, USD)
    model_input_price_per_mtok: float = 0.80
    model_output_price_per_mtok: float = 4.00
    model_cache_creation_price_per_mtok: float = 1.00
    model_cache_read_price_per_mtok: float = 0.08


settings = Settings()

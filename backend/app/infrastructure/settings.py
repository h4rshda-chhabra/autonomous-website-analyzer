from __future__ import annotations

from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_name: str = "autonomous-website-analyzer"
    debug: bool = False
    log_level: str = "INFO"

    # ── Anthropic ─────────────────────────────────────────────────────────────
    anthropic_api_key: str = Field(default="", description="Required for AI tool calls")
    anthropic_model: str = "claude-sonnet-4-6"
    anthropic_max_tokens: int = 4096
    anthropic_classification_max_tokens: int = 1024
    anthropic_planning_max_tokens: int = 2048
    anthropic_synthesis_max_tokens: int = 8192

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_state_db: int = 0
    redis_pubsub_db: int = 1
    redis_ttl_seconds: int = 7_200  # 2 hours; any audit must complete within this

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/auditor"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30
    db_echo: bool = False

    # ── Celery ────────────────────────────────────────────────────────────────
    celery_broker_url: str = "redis://localhost:6379/2"
    celery_result_backend: str = "redis://localhost:6379/3"
    celery_task_soft_time_limit: int = 420   # 7 min: agent signals graceful stop
    celery_task_time_limit: int = 480        # 8 min: Celery kills worker

    # ── Playwright ────────────────────────────────────────────────────────────
    playwright_headless: bool = True
    playwright_slow_mo_ms: int = 0
    playwright_default_timeout_ms: int = 30_000

    # ── Lighthouse ────────────────────────────────────────────────────────────
    lighthouse_executable: str = "lighthouse"
    lighthouse_chrome_flags: str = "--headless --no-sandbox --disable-gpu --disable-dev-shm-usage"
    lighthouse_timeout_ms: int = 120_000

    # ── Screenshots ───────────────────────────────────────────────────────────
    screenshot_storage_dir: str = "/tmp/auditor/screenshots"

    # ── Audit Limits ──────────────────────────────────────────────────────────
    max_concurrent_audits: int = 5
    audit_hard_timeout_seconds: int = 360
    max_broken_link_checks: int = 150
    max_html_chars_for_ai: int = 60_000

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: List[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    # ── Security ──────────────────────────────────────────────────────────────
    secret_key: str = Field(default="change-me-in-production-use-32-random-bytes")


settings = Settings()

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def mock_history_path(value: str | None) -> Path | None:
    """Resolve an explicit local path relative to agent/, independent of cwd.

    abspath normalizes ../ without following links; the resetter still checks
    and rejects symlinks/junctions before touching the database.
    """
    if not value or not value.strip():
        return None
    path = Path(value.strip())
    return Path(os.path.abspath(path if path.is_absolute() else BACKEND_ROOT / path))


@dataclass(frozen=True)
class Settings:
    data_root: Path = Path(os.getenv("AUTOHUNT_DATA_ROOT", str(BACKEND_ROOT / "data"))).resolve()
    api_key: str | None = os.getenv("AUTOHUNT_API_KEY")
    data_key: str | None = os.getenv("AUTOHUNT_DATA_KEY")
    allowed_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv(
            "AUTOHUNT_ALLOWED_ORIGINS",
            "http://127.0.0.1:3000,http://localhost:3000",
        ).split(",")
        if origin.strip()
    )
    rate_limit: int = int(os.getenv("AUTOHUNT_RATE_LIMIT", "60"))
    read_rate_limit: int = int(os.getenv("AUTOHUNT_READ_RATE_LIMIT", "600"))
    max_body_bytes: int = 2 * 1024 * 1024
    log_level: str = os.getenv("AUTOHUNT_LOG_LEVEL", "INFO")
    llm_provider: str = os.getenv("AUTOHUNT_LLM_PROVIDER", "mock")
    llm_model: str = os.getenv("AUTOHUNT_LLM_MODEL", "mock-autohunt-v1")
    deepseek_api_key: str | None = os.getenv("DEEPSEEK_API_KEY")
    browser_headless: bool = os.getenv("AUTOHUNT_BROWSER_HEADLESS", "false").lower() == "true"
    browser_cdp_url: str = os.getenv("AUTOHUNT_BROWSER_CDP_URL", "http://127.0.0.1:9223")
    # Intent-routing small model (Layer 1). Placeholder: empty key/model keeps the
    # layer disabled and routing degrades to rules + LLM fallback automatically.
    router_small_api_key: str | None = os.getenv("AUTOHUNT_ROUTER_SMALL_API_KEY")
    router_small_base_url: str = os.getenv("AUTOHUNT_ROUTER_SMALL_BASE_URL", "https://api.siliconflow.cn/v1")
    router_small_model: str = os.getenv("AUTOHUNT_ROUTER_SMALL_MODEL", "")
    # Temporary, authenticated local maintenance. No path is accepted from UI.
    history_reset_enabled: bool = os.getenv("AUTOHUNT_HISTORY_RESET_ENABLED", "false").lower() == "true"
    mock_history_database: Path | None = mock_history_path(os.getenv("AUTOHUNT_MOCK_HISTORY_DATABASE"))
    confirmation_policy: str = os.getenv("AUTOHUNT_APPROVAL_POLICY", "risk")
    confirmation_threshold: str = os.getenv("AUTOHUNT_APPROVAL_THRESHOLD", "HIGH")


settings = Settings()

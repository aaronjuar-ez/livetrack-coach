"""Settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines, no interpolation, never overrides."""
    path = path or Path(".env")
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("LTC_DATA_DIR", str(Path.home() / ".livetrack-coach"))))

    # Poller
    poll_interval_s: float = field(default_factory=lambda: _env_float("LTC_POLL_INTERVAL", 60))
    poll_max_backoff_s: float = field(default_factory=lambda: _env_float("LTC_POLL_MAX_BACKOFF", 600))
    session_idle_timeout_s: float = field(default_factory=lambda: _env_float("LTC_IDLE_TIMEOUT", 1800))

    # Gmail
    gmail_client_json: str | None = field(default_factory=lambda: _env("GMAIL_OAUTH_CLIENT_JSON"))
    gmail_poll_interval_s: float = field(default_factory=lambda: _env_float("LTC_GMAIL_POLL_INTERVAL", 30))
    gmail_sender: str = field(default_factory=lambda: _env("LTC_GMAIL_SENDER", "garmin.com"))

    # Garmin history (garmin-connect-mcp over streamable HTTP)
    garmin_mcp_url: str | None = field(default_factory=lambda: _env("GARMIN_MCP_URL"))
    garmin_mcp_token: str | None = field(default_factory=lambda: _env("GARMIN_MCP_TOKEN"))
    history_days: int = field(default_factory=lambda: _env_int("LTC_HISTORY_DAYS", 120))

    # Narrator
    narrator: str = field(default_factory=lambda: _env("LTC_NARRATOR", "auto"))  # auto|mistral|openai|vibe|template
    mistral_api_key: str | None = field(default_factory=lambda: _env("MISTRAL_API_KEY"))
    mistral_model: str = field(default_factory=lambda: _env("LTC_MISTRAL_MODEL", "mistral-small-latest"))
    openai_base_url: str | None = field(default_factory=lambda: _env("LTC_OPENAI_BASE_URL"))
    openai_api_key: str | None = field(default_factory=lambda: _env("LTC_OPENAI_API_KEY") or _env("OPENAI_API_KEY"))
    openai_model: str | None = field(default_factory=lambda: _env("LTC_OPENAI_MODEL"))
    vibe_bin: str = field(default_factory=lambda: _env("LTC_VIBE_BIN", "vibe"))
    vibe_model: str | None = field(default_factory=lambda: _env("LTC_VIBE_MODEL"))
    max_callouts_per_run: int = field(default_factory=lambda: _env_int("LTC_MAX_CALLOUTS", 6))
    callout_cooldown_s: float = field(default_factory=lambda: _env_float("LTC_CALLOUT_COOLDOWN", 240))

    # Delivery
    delivery: str = field(default_factory=lambda: _env("LTC_DELIVERY", "stdout"))  # comma list: imessage,sms,stdout,webhook
    recipient: str = field(default_factory=lambda: _env("LTC_RECIPIENT", "+19515601355"))
    webhook_url: str | None = field(default_factory=lambda: _env("LTC_WEBHOOK_URL"))

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir).expanduser()

    @property
    def athlete_path(self) -> Path:
        return self.data_dir / "athlete.json"

    @property
    def gmail_token_path(self) -> Path:
        return self.data_dir / "gmail_token.json"

    @property
    def gmail_state_path(self) -> Path:
        return self.data_dir / "gmail_state.json"


def load_settings() -> Settings:
    load_dotenv()
    return Settings()

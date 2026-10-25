from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cities import CANONICAL_CITIES, canonical_city_set


DEFAULT_APP_TITLE = "OpportunityRadar"
DEFAULT_RANK_MODEL = "openai/gpt-4.1-mini"
DEFAULT_TIMEZONE = "America/Edmonton"
DEFAULT_SEND_DOW = "MON"
DEFAULT_SEND_HOUR = 9
DEFAULT_MAX_JOBS = 30
DEFAULT_MIN_JOBS = 15
DEFAULT_MAX_JOBS_PER_COMPANY = 2
DEFAULT_SEND_PROVIDER = "twilio_whatsapp"
DEFAULT_EMAIL_SUBJECT = "OpportunityRadar weekly jobs"
DEFAULT_GOOGLE_RECIPIENTS_RANGE = "Recipients!A:C"


def load_env(root: Path) -> None:
    env_path = root / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value not in {"0", "false", "no", "off"}


def env_csv(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


@dataclass(frozen=True)
class RuntimeConfig:
    timezone: str
    send_dow: str
    send_hour: int
    send_provider: str
    recipients: list[str]
    max_jobs: int
    min_jobs: int
    max_jobs_per_company: int
    allowed_cities: set[str]
    allow_global_remote: bool
    deterministic_fallback: bool
    rank_model: str
    api_token: str
    twilio_content_sid: str
    twilio_messaging_service_sid: str
    send_empty_digest: bool
    email_subject: str
    google_gmail_from: str
    google_recipients_sheet_id: str
    google_recipients_range: str
    send_frequency: str = "weekly"


def load_runtime_config(root: Path) -> RuntimeConfig:
    load_env(root)
    frequency = os.environ.get("OPPORTUNITY_SEND_FREQUENCY", "weekly").strip().lower() or "weekly"
    if frequency not in {"daily", "weekly"}:
        raise ValueError("OPPORTUNITY_SEND_FREQUENCY must be daily or weekly")
    cities = canonical_city_set(env_csv("OPPORTUNITY_CITIES") or list(CANONICAL_CITIES))
    return RuntimeConfig(
        send_frequency=frequency,
        timezone=os.environ.get("OPPORTUNITY_TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE,
        send_dow=os.environ.get("OPPORTUNITY_SEND_DOW", DEFAULT_SEND_DOW).strip().upper() or DEFAULT_SEND_DOW,
        send_hour=int(os.environ.get("OPPORTUNITY_SEND_HOUR", str(DEFAULT_SEND_HOUR))),
        send_provider=os.environ.get("OPPORTUNITY_SEND_PROVIDER", DEFAULT_SEND_PROVIDER).strip() or DEFAULT_SEND_PROVIDER,
        recipients=env_csv("OPPORTUNITY_RECIPIENTS"),
        max_jobs=max(1, int(os.environ.get("OPPORTUNITY_MAX_JOBS", str(DEFAULT_MAX_JOBS)))),
        min_jobs=max(0, int(os.environ.get("OPPORTUNITY_MIN_JOBS", str(DEFAULT_MIN_JOBS)))),
        max_jobs_per_company=max(1, int(os.environ.get("OPPORTUNITY_MAX_JOBS_PER_COMPANY", str(DEFAULT_MAX_JOBS_PER_COMPANY)))),
        allowed_cities=cities,
        allow_global_remote=env_bool("OPPORTUNITY_ALLOW_GLOBAL_REMOTE", False),
        deterministic_fallback=env_bool("OPPORTUNITY_DETERMINISTIC_FALLBACK", False),
        rank_model=os.environ.get("OPENROUTER_RANK_MODEL", DEFAULT_RANK_MODEL).strip() or DEFAULT_RANK_MODEL,
        api_token=os.environ.get("OPPORTUNITY_API_TOKEN", "").strip(),
        twilio_content_sid=os.environ.get("TWILIO_WHATSAPP_CONTENT_SID", "").strip(),
        twilio_messaging_service_sid=os.environ.get("TWILIO_MESSAGING_SERVICE_SID", "").strip(),
        send_empty_digest=env_bool("OPPORTUNITY_SEND_EMPTY_DIGEST", False),
        email_subject=os.environ.get("OPPORTUNITY_EMAIL_SUBJECT", DEFAULT_EMAIL_SUBJECT).strip() or DEFAULT_EMAIL_SUBJECT,
        google_gmail_from=os.environ.get("GOOGLE_GMAIL_FROM", os.environ.get("OPPORTUNITY_EMAIL_FROM", "")).strip(),
        google_recipients_sheet_id=os.environ.get("GOOGLE_RECIPIENTS_SHEET_ID", "").strip(),
        google_recipients_range=os.environ.get("GOOGLE_RECIPIENTS_RANGE", DEFAULT_GOOGLE_RECIPIENTS_RANGE).strip() or DEFAULT_GOOGLE_RECIPIENTS_RANGE,
    )


def default_sources_path(root: Path) -> Path:
    configured = os.environ.get("OPPORTUNITY_SOURCES_PATH", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else root / path
    local_path = root / "data" / "config" / "sources.local.json"
    if local_path.exists():
        return local_path
    return root / "data" / "config" / "sources.example.json"

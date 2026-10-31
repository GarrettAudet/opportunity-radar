from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .cities import canonical_city_set
from .config import read_json
from .filtering import MAX_REQUIRED_YEARS, role_focus_allows, years_experience_allowed
from .models import JobPosting
from .state import load_json_from_github


@dataclass(frozen=True)
class ConditionMatch:
    allowed: bool
    city: str
    role_group_ids: list[str]
    role_group_labels: list[str]
    matched_terms: list[str]
    excluded_terms: list[str]
    rejection_reason: str = ""
    posted_at: str = ""
    posting_age_days: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "city": self.city,
            "role_group_ids": list(self.role_group_ids),
            "role_group_labels": list(self.role_group_labels),
            "matched_terms": list(self.matched_terms),
            "excluded_terms": list(self.excluded_terms),
            "rejection_reason": self.rejection_reason,
            "posted_at": self.posted_at,
            "posting_age_days": self.posting_age_days,
        }


def default_conditions_path(root: Path) -> Path:
    configured = os.environ.get("OPPORTUNITY_CONDITIONS_PATH", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else root / path
    local_path = root / "data" / "config" / "conditions.local.json"
    if local_path.exists():
        return local_path
    return root / "data" / "config" / "conditions.example.json"


def load_conditions(root: Path, explicit_path: str = "") -> dict[str, Any]:
    config_repo = os.environ.get("GITHUB_CONFIG_REPO", os.environ.get("GITHUB_STATE_REPO", "")).strip()
    config_token = os.environ.get("GITHUB_CONFIG_TOKEN", os.environ.get("GITHUB_STATE_TOKEN", "")).strip()
    github_path = os.environ.get("GITHUB_CONDITIONS_PATH", "").strip()
    if not explicit_path and config_repo and config_token and github_path:
        ref = os.environ.get("GITHUB_CONFIG_REF", os.environ.get("GITHUB_STATE_REF", "main"))
        return load_json_from_github(config_repo, github_path, config_token, ref)
    path = Path(explicit_path) if explicit_path else default_conditions_path(root)
    if not path.is_absolute():
        path = root / path
    return read_json(path)


def conditions_allowed_cities(conditions: dict[str, Any]) -> set[str]:
    values = conditions.get("locations") or conditions.get("cities") or []
    return canonical_city_set(values) if values else set()


def parse_condition_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def posting_age_days(job: JobPosting, *, now: datetime | None = None) -> float | None:
    posted = parse_condition_datetime(job.posted_at)
    if posted is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return (current - posted).total_seconds() / 86400


def recency_allowed(job: JobPosting, conditions: dict[str, Any], *, now: datetime | None = None) -> tuple[bool, str, float | None]:
    raw_days = conditions.get("posted_within_days", conditions.get("max_posting_age_days", 0))
    try:
        max_age_days = float(raw_days or 0)
    except (TypeError, ValueError):
        max_age_days = 0
    posted_at = job.posted_at
    age_days = posting_age_days(job, now=now)
    if max_age_days <= 0:
        return True, posted_at, age_days
    if age_days is None:
        if bool(conditions.get("allow_missing_posted_date", False)):
            return True, posted_at, age_days
        return False, posted_at, age_days
    if age_days < -1:
        return False, posted_at, age_days
    return age_days <= max_age_days, posted_at, age_days


def keyword_pattern(term: str) -> str:
    normalized = re.sub(r"\s+", " ", term.lower()).strip()
    if not normalized:
        return ""
    return r"(?<![a-z0-9])" + re.escape(normalized) + r"(?![a-z0-9])"


def matched_keywords(text: str, terms: Iterable[object]) -> list[str]:
    lowered = text.lower()
    matches: list[str] = []
    for raw_term in terms:
        term = re.sub(r"\s+", " ", str(raw_term or "").lower()).strip()
        pattern = keyword_pattern(term)
        if pattern and re.search(pattern, lowered) and term not in matches:
            matches.append(term)
    return matches


def condition_metadata_text(job: JobPosting) -> str:
    return " ".join(
        [
            job.title,
            job.company,
            job.city,
            job.location_text,
            job.department,
            job.employment_type,
            job.posted_at,
            " ".join(job.tags),
        ]
    )


def condition_text(job: JobPosting, *, description_chars: int) -> str:
    return " ".join([condition_metadata_text(job), job.description_text[:description_chars]])


def full_condition_text(job: JobPosting) -> str:
    return " ".join([condition_metadata_text(job), job.description_text])


def condition_role_text(job: JobPosting, *, description_chars: int) -> str:
    return " ".join(
        [
            job.title,
            job.department,
            job.employment_type,
            " ".join(job.tags),
            job.description_text[:description_chars] if description_chars > 0 else "",
        ]
    )


def group_matches(text: str, group: dict[str, Any]) -> tuple[bool, list[str]]:
    include_any = list(group.get("include_any", []) or [])
    include_all = list(group.get("include_all", []) or [])
    exclude_any = list(group.get("exclude_any", []) or [])
    if exclude_any and matched_keywords(text, exclude_any):
        return False, []
    matched_any = matched_keywords(text, include_any)
    matched_all = matched_keywords(text, include_all)
    if include_all and len(matched_all) != len([item for item in include_all if str(item).strip()]):
        return False, []
    if include_any and not matched_any:
        return False, []
    if not include_any and not include_all:
        return False, []
    return True, [*matched_any, *[term for term in matched_all if term not in matched_any]]


def rejected_match(job: JobPosting, reason: str, *, posted_at: str, age_days: float | None, excluded_terms: list[str] | None = None) -> ConditionMatch:
    return ConditionMatch(False, job.city, [], [], [], excluded_terms or [], reason, posted_at, age_days)


def match_job_conditions(
    job: JobPosting,
    conditions: dict[str, Any],
    *,
    description_chars: int = 1200,
    now: datetime | None = None,
) -> ConditionMatch:
    recent, posted_at, age_days = recency_allowed(job, conditions, now=now)
    if not recent:
        reason = "missing_posted_date" if age_days is None else "not_recent"
        return rejected_match(job, reason, posted_at=posted_at, age_days=age_days)

    metadata_text = condition_metadata_text(job)
    full_text = full_condition_text(job)
    role_description_chars = int(conditions.get("role_description_chars", description_chars) or 0)
    role_text = condition_role_text(job, description_chars=role_description_chars)
    max_years = int(conditions.get("max_years_experience", MAX_REQUIRED_YEARS) or MAX_REQUIRED_YEARS)
    title_excluded_terms = matched_keywords(job.title, conditions.get("exclude_title_any", []) or [])
    if title_excluded_terms:
        return rejected_match(job, "excluded_title_keyword", posted_at=posted_at, age_days=age_days, excluded_terms=title_excluded_terms)
    excluded_terms = matched_keywords(metadata_text, conditions.get("exclude_any", []) or [])
    if excluded_terms:
        return rejected_match(job, "excluded_keyword", posted_at=posted_at, age_days=age_days, excluded_terms=excluded_terms)
    full_text_excluded_terms = matched_keywords(
        full_text,
        conditions.get("exclude_full_text_any", []) or conditions.get("exclude_description_any", []) or [],
    )
    if full_text_excluded_terms:
        return rejected_match(
            job,
            "excluded_full_text_keyword",
            posted_at=posted_at,
            age_days=age_days,
            excluded_terms=full_text_excluded_terms,
        )
    if not years_experience_allowed(full_text, max_required_years=max_years):
        return rejected_match(job, "years_experience", posted_at=posted_at, age_days=age_days)

    role_group_ids: list[str] = []
    role_group_labels: list[str] = []
    matched_terms: list[str] = []
    role_groups = [group for group in conditions.get("role_groups", []) if isinstance(group, dict)]
    for group in role_groups:
        ok, group_terms = group_matches(role_text, group)
        if not ok:
            continue
        group_id = str(group.get("id") or group.get("label") or "role_group").strip()
        label = str(group.get("label") or group_id).strip()
        if group_id and group_id not in role_group_ids:
            role_group_ids.append(group_id)
            role_group_labels.append(label)
        for term in group_terms:
            if term not in matched_terms:
                matched_terms.append(term)

    if not role_focus_allows(job) or (role_groups and not role_group_ids):
        return rejected_match(job, "no_role_group", posted_at=posted_at, age_days=age_days)
    return ConditionMatch(True, job.city, role_group_ids, role_group_labels, matched_terms, [], "", posted_at, age_days)


def role_group_counts(matches: Iterable[ConditionMatch | dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in matches:
        if isinstance(match, ConditionMatch):
            group_ids = match.role_group_ids
        else:
            group_ids = [str(item) for item in match.get("role_group_ids", [])]
        for group_id in group_ids:
            counts[group_id] = counts.get(group_id, 0) + 1
    return dict(sorted(counts.items()))

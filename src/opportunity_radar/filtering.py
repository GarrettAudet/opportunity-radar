from __future__ import annotations

from collections import OrderedDict
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import JobPosting


MAX_REQUIRED_YEARS = 5


def _consulting_alternative(text: str, *, pm_only: bool = False) -> bool:
    bridge = r"(?:(?!\b(?:required|require|requires|must|preferred|bonus|welcome|accept|consider)\b)[^.;,\n])"
    subject = r"product management|pm" if pm_only else r"product management|product|strategy|business operations"
    return bool(re.search(
        rf"\b(?:{subject})\b{bridge}{{0,70}}\bor\b{bridge}{{0,35}}\b(?:management |strategy )?consulting\b"
        rf"|\b(?:management |strategy )?consulting\b{bridge}{{0,70}}\bor\b{bridge}{{0,35}}\b(?:{subject})\b",
        text,
    ))


def _mandatory_pm_experience(text: str) -> bool:
    preferred_section = False
    for clause in re.split(r"[.;,\n]+|\band\b", text):
        if re.search(r"\b(?:required|requirements|minimum qualifications|must have)\s*:", clause):
            preferred_section = False
        if re.search(r"\b(?:preferred qualifications|nice to have)\s*:", clause):
            preferred_section = True
        if not re.search(r"\b(?:product management|pm)\b", clause):
            continue
        if _consulting_alternative(clause, pm_only=True):
            continue
        if re.search(r"\bnot required\b|\bno (?:prior )?(?:product management|pm) experience\b", clause):
            continue
        optional = preferred_section or re.search(r"\b(?:preferred|bonus|optional|nice to have)\b", clause)
        explicit = re.search(r"\b(?:required|require|requires|must|minimum|at least)\b", clause)
        years = re.search(r"\b\d+\s*\+?\s*(?:years?|yrs?)\b", clause)
        if explicit or (years and not optional):
            return True
    return False


def _pm_open_to_consultants(text: str) -> bool:
    term = r"(?:product management|pm|product|strategy|business operations|(?:management |strategy )?consulting)"
    alternative_list = rf"\b{term}(?:\s*,\s*{term})+\s*,?\s+or\s+{term}\b"
    # Join only recognized coordinated alternatives, never a separate mandatory
    # PM requirement and a consulting preference in the following clause.
    text = re.sub(alternative_list, lambda match: re.sub(r",\s*", " ", match.group()), text)
    if _mandatory_pm_experience(text):
        return False
    if re.search(r"\b(?:no|not|without)\b[^.;\n]{0,35}\bconsulting\b|\bconsulting\b[^.;\n]{0,35}\b(?:not accepted|not considered|not eligible)\b", text):
        return False
    welcome = re.search(
        r"\b(?:welcome|accept|consider|open to)\b[^.;\n]{0,65}\bconsulting (?:backgrounds?|experience)\b"
        r"|\bconsulting (?:backgrounds?|experience)\b[^.;\n]{0,40}\b(?:welcome|accepted|eligible)\b",
        text,
    )
    return _consulting_alternative(text) or bool(welcome)


def role_focus_allows(job: JobPosting) -> bool:
    """Require role-level functional evidence; company boilerplate cannot qualify."""
    title = re.sub(r"[-–—/]+", " ", job.title.lower()).replace("&", " and ")
    title = re.sub(r"\s+", " ", title).strip()
    if re.search(r"\b(?:legal|counsel|lawyer|engineering|recruitment|recruiting|investment|investor|compliance|accountant|accounting|policy)\b", title):
        return False
    if contains_any(title, ["intern", "internship", "engineer", "developer", "designer", "recruiter", "consultant", "retail", "warehouse", "clinical", "office manager", "customer support"]):
        return False
    if re.search(r"\bproduct (?:manager|management)\b", title):
        return _pm_open_to_consultants(job.description_text.lower())
    functional = r"\b(?:gtm|go to market|business operations|bizops|revenue operations|revops|sales (?:strategy|operations)|strategy and (?:operations|ops)|deployment strategist|forward deployed strategist|chief of staff|corporate strategy|business strategy|growth strategy)\b"
    if re.search(functional, title):
        return True
    if re.search(r"^(?:(?:ai|product|commercial) )?(?:strategy|strategic) (?:associate|analyst|manager|lead|operations)\b", title):
        return True
    # Vague ATS titles need explicit duties rather than a company's interests.
    if re.fullmatch(r"(?:associate|analyst|manager)(?: [ivx]+)?", title):
        duties = re.sub(r"[-–—/]+", " ", job.description_text.lower()).replace("&", " and ")
        return bool(re.search(r"\b(?:work on|own|drive|lead|responsible for)\s+(?:product strategy|business strategy|business operations|strategy and operations|gtm|go to market)\b", duties))
    return False


def canonical_job_url(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.lower()
        if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
            host = "boards.greenhouse.io"
        path = re.sub(r"/+", "/", parsed.path).rstrip("/")
        if host in {"boards.greenhouse.io", "jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"}:
            path = re.sub(r"/(?:apply|application)$", "", path)
        tracking = {"gh_src", "source", "src", "ref", "referrer", "tracking", "lever-source", "lever-origin"}
        query = sorted((key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in tracking)
        port = parsed.port
        authority = host if port in {None, 80, 443} else f"{host}:{port}"
        return urlunsplit(("https", authority, path, urlencode(query), ""))
    except ValueError:
        return ""


def job_identity_keys(job: JobPosting) -> set[str]:
    keys = {job.stable_key}
    for value in (job.canonical_url, job.apply_url):
        url = canonical_job_url(value)
        if url:
            keys.add(f"canonical:{url}")
    return keys


def delivered_job_identities(state: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for collection in ("sent_jobs", "seen_jobs"):
        for key, entry in state.get(collection, {}).items():
            if not isinstance(entry, dict):
                continue
            if collection == "seen_jobs" and not (entry.get("first_sent_at") or entry.get("sent_at")):
                continue
            keys.add(key)
            keys.update(str(value) for value in entry.get("identity_keys", []))
            for field in ("url", "canonical_url", "apply_url"):
                url = canonical_job_url(entry.get(field))
                if url:
                    keys.add(f"canonical:{url}")
    keys.update(_pending_job_identities(state))
    return keys


def _pending_job_identities(state: dict[str, Any]) -> set[str]:
    """Unresolved provider outcomes remain held until receipt reconciliation."""
    keys: set[str] = set()
    for attempt in state.get("delivery_attempts", {}).values():
        if not isinstance(attempt, dict):
            continue
        for entry in attempt.get("jobs", []):
            if isinstance(entry, dict):
                keys.update(str(value) for value in entry.get("identity_keys", []))
    return keys


def contains_any(text: str, needles: list[str]) -> bool:
    lowered = text.lower()
    for needle in needles:
        normalized = re.sub(r"\s+", " ", needle.lower()).strip()
        if not normalized:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(normalized) + r"(?![a-z0-9])"
        if re.search(pattern, lowered):
            return True
    return False


def explicit_year_requirements(text: str) -> list[tuple[int, int | None]]:
    lowered = re.sub(r"\s+", " ", text.lower())
    requirements: list[tuple[int, int | None]] = []
    range_pattern = re.compile(
        r"(?<![\d$])(\d{1,2})\s*(?:-|to)\s*(\d{1,2})\s*(?:\+?\s*)?(?:years?|yrs?)\b"
        r"(?:\s+of)?(?:\s+(?:relevant|professional|work|industry))?\s+experience"
    )
    for match in range_pattern.finditer(lowered):
        requirements.append((int(match.group(1)), int(match.group(2))))

    min_pattern = re.compile(
        r"(?:minimum|min\.?|at least|requires?|requirement:)?\s*"
        r"(?<![\d$])(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b"
        r"(?:\s+of)?(?:\s+(?:relevant|professional|work|industry))?\s+experience"
    )
    for match in min_pattern.finditer(lowered):
        requirements.append((int(match.group(1)), None))

    experience_after_pattern = re.compile(
        r"(?<![\d$])(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b"
        r"(?=[^.;\n]{0,120}\bexperience\b)"
    )
    for match in experience_after_pattern.finditer(lowered):
        requirements.append((int(match.group(1)), None))

    experience_before_pattern = re.compile(
        r"\bexperience\b[^.;\n]{0,80}?"
        r"(?<![\d$])(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b"
    )
    for match in experience_before_pattern.finditer(lowered):
        requirements.append((int(match.group(1)), None))

    requirement_section_pattern = re.compile(
        r"\b(?:requirements?|qualifications?|preferred qualifications?|who you are|you have|you bring|must have)\b"
        r"[^.;\n]{0,140}?"
        r"(?<![\d$])(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b"
    )
    for match in requirement_section_pattern.finditer(lowered):
        requirements.append((int(match.group(1)), None))
    return requirements


def years_experience_allowed(text: str, max_required_years: int = MAX_REQUIRED_YEARS) -> bool:
    for minimum, maximum in explicit_year_requirements(text):
        if minimum > max_required_years:
            return False
        if maximum is not None and maximum > max_required_years:
            return False
    return True


def source_filter_allows(job: JobPosting, source: dict[str, Any]) -> bool:
    if not role_focus_allows(job):
        return False
    haystack = " ".join(
        [
            job.title,
            job.company,
            job.city,
            job.location_text,
            job.department,
            job.employment_type,
            job.description_text[:8000],
            " ".join(job.tags),
        ]
    )
    years_haystack = " ".join([haystack, job.description_text])
    if not years_experience_allowed(years_haystack):
        return False
    exclude = list(source.get("exclude_keywords", []) or [])
    if exclude and contains_any(haystack, exclude):
        return False
    include = list(source.get("include_keywords", []) or [])
    if include and not contains_any(haystack, include):
        return False
    return True


def dedupe_jobs(jobs: list[JobPosting]) -> list[JobPosting]:
    by_key: OrderedDict[str, JobPosting] = OrderedDict()
    identities: set[str] = set()
    for job in jobs:
        keys = job_identity_keys(job)
        duplicate = bool(keys & identities)
        identities.update(keys)
        if not duplicate:
            by_key[job.stable_key] = job
    return list(by_key.values())


def remove_seen_jobs(jobs: list[JobPosting], state: dict[str, Any], *, include_seen: bool = False) -> list[JobPosting]:
    if include_seen:
        return jobs
    seen = delivered_job_identities(state)
    return [job for job in jobs if not job_identity_keys(job) & seen]

from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters import fetch_source
from .config import default_sources_path, load_runtime_config, read_json
from .criteria import load_criteria
from .digest import format_digest
from .filtering import dedupe_jobs, delivered_job_identities, job_identity_keys, remove_seen_jobs, role_focus_allows, source_filter_allows
from .models import DigestRun, JobPosting, RankedOpportunity, RecipientResult, SourceResult, now_iso
from .ranker import rank_deterministically, rank_with_llm
from .scheduling import day_key_for, should_send_now, week_key_for
from .sender import DryRunSender, GmailEmailSender, GmailSmtpSender, MicrosoftGraphEmailSender, Sender, TwilioWhatsAppSender, normalize_send_provider
from .twilio_whatsapp import twilio_send_config_errors
from .microsoft_graph import microsoft_graph_send_config_errors
from .google_workspace import gmail_send_config_errors, load_google_sheet_recipients
from .smtp_email import smtp_send_config_errors
from .state import JsonStore, load_json_from_github, state_store_from_env


def load_sources(root: Path, explicit_path: str = "") -> dict[str, Any]:
    config_repo = os.environ.get("GITHUB_CONFIG_REPO", os.environ.get("GITHUB_STATE_REPO", "")).strip()
    config_token = os.environ.get("GITHUB_CONFIG_TOKEN", os.environ.get("GITHUB_STATE_TOKEN", "")).strip()
    github_path = os.environ.get("GITHUB_SOURCES_PATH", "").strip()
    if not explicit_path and config_repo and config_token and github_path:
        return load_json_from_github(config_repo, github_path, config_token, os.environ.get("GITHUB_CONFIG_REF", os.environ.get("GITHUB_STATE_REF", "main")))
    path = Path(explicit_path) if explicit_path else default_sources_path(root)
    if not path.is_absolute():
        path = root / path
    return read_json(path)


def enabled_sources(sources_config: dict[str, Any]) -> list[dict[str, Any]]:
    return [source for source in sources_config.get("sources", []) if source.get("enabled", True)]


def apply_source_filters(jobs: list[JobPosting], source_by_id: dict[str, dict[str, Any]]) -> list[JobPosting]:
    filtered = []
    for job in jobs:
        source = source_by_id.get(job.source_id, {})
        if source_filter_allows(job, source):
            filtered.append(job)
    return filtered


def diversify_ranked(
    ranked: list[RankedOpportunity],
    *,
    max_jobs: int,
    max_jobs_per_company: int,
) -> list[RankedOpportunity]:
    counts: dict[str, int] = {}
    selected: list[RankedOpportunity] = []
    for item in ranked:
        company_key = item.job.company.strip().lower() or item.job.source_id
        if counts.get(company_key, 0) >= max_jobs_per_company:
            continue
        selected.append(item)
        counts[company_key] = counts.get(company_key, 0) + 1
        if len(selected) >= max_jobs:
            break
    return [RankedOpportunity(**{**item.__dict__, "rank": index}) for index, item in enumerate(selected, start=1)]


def ranked_from_evaluated_state(
    state: dict[str, Any],
    *,
    max_jobs: int,
    max_jobs_per_company: int,
    include_seen: bool = False,
) -> tuple[list[RankedOpportunity], int]:
    sent_identities = set() if include_seen else delivered_job_identities(state)
    selected_identities: set[str] = set()
    ranked: list[RankedOpportunity] = []
    for key, entry in state.get("evaluated_jobs", {}).items():
        if not isinstance(entry, dict) or entry.get("status") != "included":
            continue
        job_payload = entry.get("job", {})
        if not isinstance(job_payload, dict):
            continue
        try:
            job = JobPosting(**job_payload)
        except TypeError:
            continue
        identities = job_identity_keys(job)
        if not role_focus_allows(job) or identities & sent_identities or identities & selected_identities:
            continue
        selected_identities.update(identities)
        ranked.append(
            RankedOpportunity(
                job=job,
                score=float(entry.get("score", 0) or 0),
                include=True,
                scholar_fit_reason=str(entry.get("scholar_fit_reason", ""))[:400],
                why_cool=str(entry.get("why_cool", ""))[:400],
                risk_flags=[str(flag)[:120] for flag in entry.get("risk_flags", []) if str(flag).strip()],
            )
        )
    ranked.sort(key=lambda item: (item.score, item.job.fetched_at), reverse=True)
    selected = diversify_ranked(ranked, max_jobs=max_jobs, max_jobs_per_company=max_jobs_per_company)
    return selected, len(ranked)


def already_sent_on_day(state: dict[str, Any], day_key: str, timezone_name: str) -> bool:
    if day_key in state.get("sent_days", {}):
        return True
    # Older deployments have only weekly markers. Honor a delivery made today
    # when switching cadence, without blocking the rest of that week.
    for entry in state.get("sent_weeks", {}).values():
        try:
            sent_at = datetime.fromisoformat(entry["sent_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if day_key_for(sent_at, timezone_name) == day_key:
            return True
    return False


def update_state_after_send(state: dict[str, Any], *, week_key: str, day_key: str, selected: list[RankedOpportunity], run_payload: dict[str, Any]) -> dict[str, Any]:
    sent_at = now_iso()
    seen_jobs = state.setdefault("seen_jobs", {})
    sent_jobs = state.setdefault("sent_jobs", {})
    for item in selected:
        seen_jobs[item.job.stable_key] = {
            "first_sent_at": sent_at,
            "company": item.job.company,
            "title": item.job.title,
            "city": item.job.city,
            "url": item.job.canonical_url,
            "content_hash": item.job.content_hash,
            "identity_keys": sorted(job_identity_keys(item.job)),
        }
        sent_jobs[item.job.stable_key] = {
            "sent_at": sent_at,
            "week_key": week_key,
            "run_id": run_payload.get("run_id", ""),
            "company": item.job.company,
            "title": item.job.title,
            "city": item.job.city,
            "url": item.job.canonical_url,
            "identity_keys": sorted(job_identity_keys(item.job)),
        }
    state.setdefault("sent_weeks", {})[week_key] = {"sent_at": sent_at, "run_id": run_payload.get("run_id", "")}
    state.setdefault("sent_days", {})[day_key] = {"sent_at": sent_at, "run_id": run_payload.get("run_id", "")}
    runs = state.setdefault("runs", [])
    runs.append(run_payload)
    state["runs"] = runs[-50:]
    state["updated_at"] = now_iso()
    return state


def sender_for_run(config: Any, *, dry_run: bool, sender: Sender | None = None) -> Sender:
    if sender is not None:
        return sender
    if dry_run:
        return DryRunSender()
    provider = normalize_send_provider(str(config.send_provider))
    if provider == "gmail_smtp":
        return GmailSmtpSender(subject=config.email_subject, from_address=config.google_gmail_from)
    if provider == "gmail_email":
        return GmailEmailSender(subject=config.email_subject, from_address=config.google_gmail_from)
    if provider == "microsoft_graph_email":
        return MicrosoftGraphEmailSender(subject=config.email_subject)
    if provider == "twilio_whatsapp":
        return TwilioWhatsAppSender(content_sid=config.twilio_content_sid, messaging_service_sid=config.twilio_messaging_service_sid)
    raise ValueError(f"unsupported_send_provider:{config.send_provider}")


def load_delivery_recipients(config: Any) -> list[str]:
    provider = normalize_send_provider(str(config.send_provider))
    if provider == "gmail_email" and config.google_recipients_sheet_id:
        return load_google_sheet_recipients(
            spreadsheet_id=config.google_recipients_sheet_id,
            range_name=config.google_recipients_range,
        )
    return config.recipients


def send_config_errors(config: Any, *, recipients: list[str]) -> list[str]:
    provider = normalize_send_provider(str(config.send_provider))
    if provider == "gmail_smtp":
        return smtp_send_config_errors(recipients=recipients)
    if provider == "gmail_email":
        return gmail_send_config_errors(recipients=recipients, allow_sheet=bool(config.google_recipients_sheet_id))
    if provider == "microsoft_graph_email":
        return microsoft_graph_send_config_errors(recipients=recipients)
    if provider == "twilio_whatsapp":
        return twilio_send_config_errors(
            recipients=recipients,
            content_sid=config.twilio_content_sid,
            messaging_service_sid=config.twilio_messaging_service_sid,
        )
    return [f"unsupported_send_provider:{config.send_provider}"]


def claim_selected_digest(state_store, state, run_id, selected, errors):
    """Reserve selected identities atomically before any provider call."""
    attempt = {
        "status": "pending", "started_at": now_iso(),
        "jobs": [{"key": item.job.stable_key, "url": item.job.canonical_url, "identity_keys": sorted(job_identity_keys(item.job))} for item in selected],
        "recipient_results": [],
    }
    claim_delivery = getattr(state_store, "claim_delivery", None)
    if not callable(claim_delivery):
        errors.append("delivery_claim_unsupported")
        return None
    try:
        claimed = claim_delivery(state, run_id, attempt)
    except Exception as exc:
        errors.append(f"delivery_journal_failed:{type(exc).__name__}")
        return None
    if claimed is None:
        errors.append("delivery_claim_conflict")
        return None
    state.clear()
    state.update(claimed)
    attempt = state["delivery_attempts"][run_id]
    return attempt


def send_selected_digest(
    *,
    config: Any,
    send: bool,
    selected: list[RankedOpportunity],
    digest_text: str,
    errors: list[str],
    sender: Sender | None,
    state: dict[str, Any],
    state_store: JsonStore,
    run_id: str,
) -> list[Any]:
    recipient_results = []
    ranker_failed = any(error.startswith("ranker_failed") for error in errors)
    if send and not ranker_failed and (selected or config.send_empty_digest):
        try:
            recipients = load_delivery_recipients(config)
        except Exception as exc:
            errors.append(f"recipient_load_failed:{type(exc).__name__}: {exc}")
            return recipient_results
        if sender is None:
            config_errors = send_config_errors(config, recipients=recipients)
            if config_errors:
                errors.extend(error for error in config_errors if error not in errors)
                return recipient_results
        elif not recipients:
            errors.append("no_recipients_configured")
            return recipient_results
        active_sender = sender_for_run(config, dry_run=False, sender=sender)
        attempt = claim_selected_digest(state_store, state, run_id, selected, errors)
        if attempt is None:
            return recipient_results
        for recipient in recipients:
            try:
                result = active_sender.send(recipient, digest_text)
            except Exception as exc:
                result = RecipientResult(recipient=recipient, ok=False, provider=str(config.send_provider), error=f"delivery_exception:{type(exc).__name__}")
            recipient_results.append(result)
        attempt["recipient_results"] = [result.to_dict() for result in recipient_results]
        attempt["status"] = "sent" if all(result.ok for result in recipient_results) else "needs_review"
        if attempt["status"] == "needs_review":
            errors.append("delivery_needs_review")
    return recipient_results


def persist_digest_delivery(
    digest_run: DigestRun, state: dict[str, Any], state_store: JsonStore, *, day_key: str,
) -> DigestRun:
    if not digest_run.recipient_results:
        return digest_run
    all_ok = all(result.ok for result in digest_run.recipient_results)
    if all_ok:
        update_state_after_send(state, week_key=digest_run.week_key, day_key=day_key, selected=digest_run.selected_jobs, run_payload=digest_run.to_dict())
    try:
        state_store.save(state)
    except Exception as exc:
        # The pre-send hold is durable even if final receipt persistence fails.
        digest_run.errors.append(f"delivery_receipt_save_failed:{type(exc).__name__}")
    return DigestRun(**{**digest_run.__dict__, "state_summary": {
        "mutated": True,
        "evaluated_jobs": len(state.get("evaluated_jobs", {})),
        "sent_jobs": len(state.get("sent_jobs", {})),
        "seen_jobs": len(state.get("seen_jobs", {})),
        "delivery_attempts": len(state.get("delivery_attempts", {})),
    }})


def collect_digest_sources(root, config, state, sources_path, errors):
    """Collect configured public feeds and observed source/cache outcomes."""
    sources_config = load_sources(root, sources_path)
    default_cities = set(config.allowed_cities)
    source_by_id = {str(source.get("id")): source for source in enabled_sources(sources_config)}
    all_jobs: list[JobPosting] = []
    source_results: list[SourceResult] = []
    source_cache_updates: dict[str, Any] = {}
    state_source_cache = state.get("source_cache", {})
    defaults = sources_config.get("defaults", {}) if isinstance(sources_config.get("defaults", {}), dict) else {}
    allow_global_remote = bool(defaults.get("allow_global_remote", config.allow_global_remote))
    if defaults.get("cities"):
        from .cities import canonical_city_set
        default_cities = canonical_city_set(defaults.get("cities"))

    for source in enabled_sources(sources_config):
        source_id = str(source.get("id") or source.get("name") or "source")
        jobs, result, cache_update = fetch_source(
            source,
            default_cities=default_cities,
            allow_global_remote=allow_global_remote,
            cache=state_source_cache.get(source_id, {}),
        )
        source_results.append(result)
        all_jobs.extend(jobs)
        if cache_update:
            source_cache_updates[source_id] = cache_update
        if not result.ok:
            errors.append(f"source_failed:{source_id}:{result.error}")

    return all_jobs, source_results, source_by_id, source_cache_updates


def run_digest(
    root: Path,
    *,
    send: bool = False,
    force: bool = False,
    respect_schedule: bool = False,
    sources_path: str = "",
    deterministic_fallback: bool | None = None,
    include_seen: bool = False,
    from_state: bool = False,
    now: datetime | None = None,
    state_store: JsonStore | None = None,
    sender: Sender | None = None,
) -> DigestRun:
    root = root.resolve()
    started_at = now_iso()
    run_id = secrets.token_hex(8)
    config = load_runtime_config(root)
    week_key = week_key_for(now, config.timezone)
    day_key = day_key_for(now, config.timezone)
    daily = config.send_frequency == "daily"
    errors: list[str] = []
    dry_run = not send
    state_store = state_store or state_store_from_env(root)
    state = state_store.load()

    if respect_schedule and send and not should_send_now(now, timezone_name=config.timezone, send_dow=config.send_dow, send_hour=config.send_hour, frequency=config.send_frequency):
        errors.append("outside_configured_send_window")
        return DigestRun(
            run_id=run_id,
            week_key=week_key,
            dry_run=dry_run,
            send_requested=send,
            started_at=started_at,
            finished_at=now_iso(),
            source_results=[],
            candidate_count=0,
            selected_jobs=[],
            errors=errors,
            state_summary={"mutated": False},
        )
    already_sent = already_sent_on_day(state, day_key, config.timezone) if daily else week_key in state.get("sent_weeks", {})
    if send and not force and already_sent:
        errors.append("day_already_sent" if daily else "week_already_sent")
        return DigestRun(
            run_id=run_id,
            week_key=week_key,
            dry_run=dry_run,
            send_requested=send,
            started_at=started_at,
            finished_at=now_iso(),
            source_results=[],
            candidate_count=0,
            selected_jobs=[],
            errors=errors,
            state_summary={"mutated": False},
        )

    if from_state:
        selected, candidate_count = ranked_from_evaluated_state(
            state,
            max_jobs=config.max_jobs,
            max_jobs_per_company=config.max_jobs_per_company,
            include_seen=(include_seen or force) and not send,
        )
        if not state.get("evaluated_jobs"):
            errors.append("no_evaluated_jobs")
        digest_text = format_digest(selected, week_key=week_key, day_key=day_key if daily else "", errors=errors)
        recipient_results = send_selected_digest(
            config=config,
            send=send,
            selected=selected,
            digest_text=digest_text,
            errors=errors,
            sender=sender,
            state=state,
            state_store=state_store,
            run_id=run_id,
        )
        finished_at = now_iso()
        digest_run = DigestRun(
            run_id=run_id,
            week_key=week_key,
            dry_run=dry_run,
            send_requested=send,
            started_at=started_at,
            finished_at=finished_at,
            source_results=[],
            candidate_count=candidate_count,
            selected_jobs=selected,
            recipient_results=recipient_results,
            errors=errors,
            state_summary={
                "mutated": False,
                "evaluated_jobs": len(state.get("evaluated_jobs", {})),
                "sent_jobs": len(state.get("sent_jobs", {})),
            },
            digest_text=digest_text,
        )
        return persist_digest_delivery(digest_run, state, state_store, day_key=day_key)

    all_jobs, source_results, source_by_id, source_cache_updates = collect_digest_sources(root, config, state, sources_path, errors)

    filtered = apply_source_filters(all_jobs, source_by_id)
    candidates = remove_seen_jobs(dedupe_jobs(filtered), state, include_seen=(include_seen or force) and not send)
    criteria_text = load_criteria(root)
    allow_fallback = config.deterministic_fallback if deterministic_fallback is None else deterministic_fallback
    try:
        selected = rank_with_llm(
            candidates,
            criteria_text=criteria_text,
            model=config.rank_model,
            max_selected=config.max_jobs * max(1, config.max_jobs_per_company),
            min_selected=min(config.min_jobs, config.max_jobs),
        )
    except Exception as exc:
        if allow_fallback:
            errors.append(f"ranker_fallback:{type(exc).__name__}")
            selected = rank_deterministically(candidates, max_selected=config.max_jobs * max(1, config.max_jobs_per_company), min_selected=min(config.min_jobs, config.max_jobs))
        else:
            errors.append(f"ranker_failed:{type(exc).__name__}")
            selected = []

    selected = diversify_ranked(selected, max_jobs=config.max_jobs, max_jobs_per_company=config.max_jobs_per_company)
    digest_text = format_digest(selected, week_key=week_key, day_key=day_key if daily else "", errors=errors)
    recipient_results = send_selected_digest(
        config=config,
        send=send,
        selected=selected,
        digest_text=digest_text,
        errors=errors,
        sender=sender,
        state=state,
        state_store=state_store,
        run_id=run_id,
    )

    finished_at = now_iso()
    digest_run = DigestRun(
        run_id=run_id,
        week_key=week_key,
        dry_run=dry_run,
        send_requested=send,
        started_at=started_at,
        finished_at=finished_at,
        source_results=source_results,
        candidate_count=len(candidates),
        selected_jobs=selected,
        recipient_results=recipient_results,
        errors=errors,
        state_summary={"mutated": False, "seen_jobs": len(state.get("seen_jobs", {}))},
        digest_text=digest_text,
    )
    if source_cache_updates:
        state.setdefault("source_cache", {}).update(source_cache_updates)

    return persist_digest_delivery(digest_run, state, state_store, day_key=day_key)

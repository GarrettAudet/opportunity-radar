from __future__ import annotations

import json
import re
from typing import Any

from .models import JobPosting, RankedOpportunity
from .filtering import role_focus_allows
from .openrouter_client import OpenRouterClient, parse_json_object


class RankingError(RuntimeError):
    pass


COOL_TERMS = {
    "gtm": 18,
    "go-to-market": 18,
    "deployment strategist": 20,
    "forward deployed strategist": 20,
    "bizops": 18,
    "revenue operations": 18,
    "revops": 18,
    "ai": 18,
    "artificial intelligence": 18,
    "machine learning": 16,
    "strategy": 14,
    "strategic": 14,
    "venture": 18,
    "investment": 16,
    "investor": 16,
    "policy": 14,
    "public policy": 16,
    "global": 12,
    "chief of staff": 18,
    "founder": 14,
    "startup": 12,
    "product": 10,
    "operations": 14,
    "operator": 10,
    "ops": 8,
    "growth": 12,
    "partnership": 12,
    "partnerships": 12,
    "business development": 12,
    "climate": 10,
    "fintech": 10,
    "crypto": 8,
    "frontier": 12,
    "leadership": 10,
}

LOW_SIGNAL_TITLE_TERMS = {
    "administrative": 35,
    "executive assistant": 35,
    "office manager": 30,
    "recruiter": 32,
    "recruiting": 32,
    "talent": 26,
    "human resources": 30,
    "hrbp": 30,
    "customer success": 28,
    "account manager": 26,
    "sales engineer": 24,
    "brand designer": 30,
    "designer": 24,
    "digital marketing": 24,
    "crm manager": 24,
    "member services": 24,
    "engineering manager": 34,
    "account executive": 30,
    "sales development": 30,
    "account associate": 26,
    "marketer": 24,
    "content editor": 24,
    "storytelling": 24,
    "contract": 20,
    "customer engineer": 24,
    "solutions engineer": 24,
}

SENIOR_TITLE_TERMS = {
    "senior": 42,
    "staff engineer": 42,
    "staff ai engineer": 42,
    "staff ml engineer": 42,
    "staff machine learning engineer": 42,
    "principal": 42,
    "director": 42,
    "vice president": 42,
    "vp": 42,
    "head of": 42,
    "lead": 28,
}


def keyword_present(term: str, text: str) -> bool:
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text))


def deterministic_signal(job: JobPosting) -> tuple[float, list[str]]:
    role_haystack = " ".join([job.title, job.department, job.employment_type, " ".join(job.tags)]).lower()
    context_haystack = " ".join([job.company, job.description_text[:900]]).lower()
    company_haystack = job.company.lower()
    title_haystack = job.title.lower()
    score = 45.0
    reasons: list[str] = []
    for term, points in COOL_TERMS.items():
        if keyword_present(term, role_haystack):
            score += points
            reasons.append(term)
        elif keyword_present(term, context_haystack):
            score += max(4, points * 0.4)
            reasons.append(term)
    for term, points in BRAND_TERMS.items():
        if term in company_haystack or term in context_haystack:
            score += points
            reasons.append(term)
    for term, penalty in LOW_SIGNAL_TITLE_TERMS.items():
        if keyword_present(term, title_haystack):
            score -= penalty
            reasons.append(f"low-signal:{term}")
    for term, penalty in SENIOR_TITLE_TERMS.items():
        if keyword_present(term, title_haystack):
            score -= penalty
            reasons.append(f"seniority:{term}")
    if job.city in {"Beijing", "Dubai", "Shenzhen", "New York", "San Francisco", "Sydney"}:
        score += 8
    return score, reasons


BRAND_TERMS = {
    "openai": 24,
    "anthropic": 24,
    "google": 22,
    "deepmind": 22,
    "microsoft": 20,
    "meta": 20,
    "apple": 18,
    "stripe": 18,
    "airbnb": 16,
    "mckinsey": 20,
    "bain": 18,
    "bcg": 18,
    "blackstone": 18,
    "goldman": 16,
    "world bank": 18,
    "united nations": 18,
}


def rank_with_llm(
    jobs: list[JobPosting],
    *,
    criteria_text: str,
    model: str,
    max_selected: int,
    min_selected: int = 0,
    client: OpenRouterClient | None = None,
) -> list[RankedOpportunity]:
    jobs = [job for job in jobs if role_focus_allows(job)]
    if not jobs:
        return []
    client = client or OpenRouterClient()
    candidate_jobs = sorted(jobs, key=lambda item: deterministic_signal(item)[0], reverse=True)[:60]
    payload = [job.compact_for_llm() for job in candidate_jobs]
    prompt = f"""
Return JSON only.

You select a weekly OpportunityRadar job digest for Schwarzman Scholars.

Criteria markdown:
{criteria_text}

Candidate jobs:
{json.dumps(payload, ensure_ascii=False)}

Return:
{{
  "opportunities": [
    {{
      "key": "exact candidate key",
      "score": 0,
      "include": true,
      "scholar_fit_reason": "short reason this fits Schwarzman Scholars",
      "why_cool": "one concise digest-ready line",
      "risk_flags": []
    }}
  ]
}}

Return one row for every candidate key. Aim for {min(max(0, min_selected), max_selected)}-{max_selected} digest roles when enough candidates score 70 or higher. Include only jobs that are genuinely high-signal; do not include jobs below 70.
"""
    text = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": "You rank jobs for a curated weekly opportunities digest. Return strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=8000,
    )
    data = parse_json_object(text)
    rows = data.get("opportunities", [])
    if not isinstance(rows, list):
        raise RankingError("LLM response missing opportunities list")
    jobs_by_key = {job.stable_key: job for job in jobs}
    ranked: list[RankedOpportunity] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", "")).strip()
        job = jobs_by_key.get(key)
        if job is None:
            continue
        score = float(row.get("score", 0) or 0)
        include = bool(row.get("include", False)) and score >= 70
        ranked.append(
            RankedOpportunity(
                job=job,
                score=score,
                include=include,
                scholar_fit_reason=str(row.get("scholar_fit_reason", "")).strip()[:400],
                why_cool=str(row.get("why_cool", "")).strip()[:400],
                risk_flags=[str(flag).strip()[:120] for flag in row.get("risk_flags", []) if str(flag).strip()],
            )
        )
    return select_ranked(ranked, max_selected=max_selected, min_selected=min_selected)


def select_ranked(ranked: list[RankedOpportunity], *, max_selected: int, min_selected: int = 0) -> list[RankedOpportunity]:
    ranked = [item for item in ranked if role_focus_allows(item.job)]
    ranked.sort(key=lambda item: item.score, reverse=True)
    selected = [item for item in ranked if item.include]
    target = min(max(0, min_selected), max_selected, len(ranked))
    if len(selected) < target:
        selected_keys = {item.job.stable_key for item in selected}
        for item in ranked:
            if len(selected) >= target:
                break
            if item.job.stable_key in selected_keys or item.score < 70:
                continue
            flags = list(item.risk_flags)
            if "score_threshold_fill" not in flags:
                flags.append("score_threshold_fill")
            selected.append(RankedOpportunity(**{**item.__dict__, "include": True, "risk_flags": flags}))
            selected_keys.add(item.job.stable_key)
    selected = selected[:max_selected]
    return [RankedOpportunity(**{**item.__dict__, "rank": index}) for index, item in enumerate(selected, start=1)]


def rank_deterministically(jobs: list[JobPosting], *, max_selected: int, min_selected: int = 0) -> list[RankedOpportunity]:
    ranked: list[RankedOpportunity] = []
    for job in jobs:
        score, reasons = deterministic_signal(job)
        include = score >= 70
        why = f"{job.company} role in {job.city}: {job.title}"
        if reasons:
            why += f" ({', '.join(reasons[:3])})"
        ranked.append(
            RankedOpportunity(
                job=job,
                score=min(score, 100.0),
                include=include,
                scholar_fit_reason="Deterministic fallback matched curated opportunity signals.",
                why_cool=why,
                risk_flags=["deterministic_fallback"],
            )
        )
    return select_ranked(ranked, max_selected=max_selected, min_selected=min_selected)

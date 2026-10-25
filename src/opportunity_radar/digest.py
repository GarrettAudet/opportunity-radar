from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta

from .models import RankedOpportunity


GROUP_ORDER = [
    ("strategy_operations", "Strategy / Operations"),
    ("legal_regulatory", "Legal / Regulatory"),
    ("policy_ai", "Policy / AI Governance"),
    ("investing_venture", "Investing / Venture"),
    ("product_growth_partnerships", "Product / Growth / Partnerships"),
    ("frontier_tech_startups", "Frontier Tech / Startups"),
]
GROUP_LABELS = dict(GROUP_ORDER)
OTHER_GROUP = "Other Strong Matches"
THEME_KEYWORDS = [
    ("AI and frontier-tech", ["ai", "artificial intelligence", "machine learning", "agent", "automation", "frontier"]),
    ("fintech and financial infrastructure", ["fintech", "finance", "financial", "accounting", "payments", "banking"]),
    ("product and go-to-market", ["product", "growth", "gtm", "go-to-market", "partnership", "enablement"]),
    ("strategy and operations", ["strategy", "operations", "bizops", "business operations", "special projects"]),
    ("healthcare and life sciences", ["health", "healthcare", "clinical", "cancer", "therapeutics", "pediatric"]),
    ("climate and infrastructure", ["climate", "energy", "infrastructure", "construction", "robotics"]),
]


def week_label(week_key: str) -> str:
    return week_key.replace("-W", " week ")


def week_date_range(week_key: str) -> str:
    try:
        year_text, week_text = week_key.split("-W", 1)
        start = date.fromisocalendar(int(year_text), int(week_text), 1)
    except (TypeError, ValueError):
        return week_label(week_key)
    end = start + timedelta(days=6)
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%B')} {start.day}-{end.day}, {start.year}"
    if start.year == end.year:
        return f"{start.strftime('%B')} {start.day}-{end.strftime('%B')} {end.day}, {start.year}"
    return f"{start.strftime('%B')} {start.day}, {start.year}-{end.strftime('%B')} {end.day}, {end.year}"


def condition_group_ids(item: RankedOpportunity) -> list[str]:
    ids: list[str] = []
    for tag in item.job.tags:
        if not tag.startswith("condition:"):
            continue
        group_id = tag.split(":", 1)[1].strip()
        if group_id and group_id not in ids:
            ids.append(group_id)
    return ids


def digest_group_label(item: RankedOpportunity) -> str:
    ids = condition_group_ids(item)
    for group_id, label in GROUP_ORDER:
        if group_id in ids:
            return label
    if ids:
        return GROUP_LABELS.get(ids[0], ids[0].replace("_", " ").title())
    return OTHER_GROUP


def grouped_opportunities(selected: list[RankedOpportunity]) -> list[tuple[str, list[RankedOpportunity]]]:
    buckets: dict[str, list[RankedOpportunity]] = {}
    for item in selected:
        buckets.setdefault(digest_group_label(item), []).append(item)
    labels = [label for _group_id, label in GROUP_ORDER if label in buckets]
    labels.extend(label for label in buckets if label not in labels)
    return [(label, buckets[label]) for label in labels]


def trend_sentence(selected: list[RankedOpportunity], *, daily: bool = False) -> str:
    prefix = "Today's list" if daily else "This week's list"
    city_counts = Counter(item.job.city for item in selected if item.job.city)
    group_counts = Counter(digest_group_label(item) for item in selected)
    theme_counts: Counter[str] = Counter()
    for item in selected:
        text = " ".join(
            [
                item.job.company,
                item.job.title,
                item.job.department,
                item.job.description_text[:600],
                item.why_cool,
                item.scholar_fit_reason,
            ]
        ).lower()
        for label, terms in THEME_KEYWORDS:
            if any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) for term in terms):
                theme_counts[label] += 1
    city_part = ""
    top_cities = [city for city, count in city_counts.most_common(3) if count >= 2]
    if top_cities:
        city_part = "roles clustered around " + human_join(top_cities)
    top_themes = [theme for theme, count in theme_counts.most_common(3) if count >= 3]
    if not top_themes:
        top_themes = [group.lower() for group, count in group_counts.most_common(2) if count >= 2]
    theme_part = ""
    if top_themes:
        theme_part = "a noticeable tilt toward " + human_join(top_themes)
    if city_part and theme_part:
        return f"{prefix} has {city_part}, with {theme_part}."
    if city_part:
        return f"{prefix} has {city_part}, with a broad mix of role types."
    if theme_part:
        return f"{prefix} has {theme_part}."
    return f"{prefix} is a mixed set of strategy, product, policy, investing, and frontier-tech roles."


def human_join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def format_digest(selected: list[RankedOpportunity], *, week_key: str, day_key: str = "", errors: list[str] | None = None) -> str:
    if day_key:
        day = date.fromisoformat(day_key)
        period = f"{day.strftime('%B')} {day.day}, {day.year}"
    else:
        period = f"the week of {week_date_range(week_key)}"
    if not selected:
        return "\n".join(
            [
                "Hello everyone,",
                "",
                f"No new high-signal roles matched the criteria for {period}.",
                "",
                "Best,",
                "Garrett",
            ]
        )
    lines = [
        "Hello everyone,",
        "",
        f"Here is {'today' if day_key else 'this week'}'s curated list of interesting roles for Schwarzman Scholars for {period}.",
        trend_sentence(selected, daily=bool(day_key)),
        "",
    ]
    display_rank = 1
    for group_label, items in grouped_opportunities(selected):
        lines.append(group_label)
        for item in items:
            job = item.job
            url = job.apply_url or job.canonical_url
            reason = item.why_cool or item.scholar_fit_reason
            lines.append(f"{display_rank}. {job.company} - {job.title}")
            lines.append(f"   Location: {job.city or job.location_text}")
            if reason:
                lines.append(f"   Why it is interesting: {reason}")
            if url:
                lines.append(f"   Apply: {url}")
            lines.append("")
            display_rank += 1
    if errors and any(error.startswith("source_failed") for error in errors):
        lines.append("Note: a few sources failed this run, so the digest uses the sources that were available.")
        lines.append("")
    lines.extend(["Best,", "Garrett"])
    return "\n".join(lines).strip()


def digest_preview_title() -> str:
    return datetime.now().strftime("OpportunityRadar preview %Y-%m-%d %H:%M")

from __future__ import annotations

import os
from pathlib import Path


DEFAULT_CRITERIA = """# OpportunityRadar Criteria

Audience: Schwarzman Scholars pursuing GTM, strategy/business operations, deployment strategy, and Product Manager roles explicitly open to consulting backgrounds.

Hard eligibility: only these focused functions qualify. PM postings must explicitly accept consulting as an alternative; a consulting preference alongside required PM experience is insufficient. General consulting, engineering-only, generic operations, policy, legal and investing roles do not qualify through company prestige or broad context. Never widen eligibility to reach the minimum count.

Prefer roles that have at least one of these signals:
- Hot startup or unusually fast-growing company.
- Big brand, elite institution, or respected global platform.
- Strong strategy, business operations, GTM, deployment or product ownership within the focused functions.
- Role gives meaningful ownership, access to senior decision-making, or a differentiated learning curve.
- Explicit years-of-experience requirements are 0-5 years; reject roles requiring more than 5 years.
- Location is one of the configured target cities.

Avoid:
- Generic local roles with no strategic, leadership, brand, mission, or learning signal.
- Roles that look seniority-mismatched for early-career candidates unless they are fellowships, rotational programs, or clearly accessible.
- Roles explicitly requiring more than 5 years of experience.
- Spammy, vague, commission-only, unpaid, or low-information postings.
"""


def load_criteria(root: Path) -> str:
    configured = os.environ.get("OPPORTUNITY_CRITERIA_PATH", "").strip()
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = root / path
        if path.exists():
            return path.read_text(encoding="utf-8")
    path = root / "docs" / "opportunity-criteria.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return DEFAULT_CRITERIA

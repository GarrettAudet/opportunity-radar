from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from opportunity_radar.conditions import match_job_conditions, role_group_counts
from opportunity_radar.models import JobPosting


CONDITIONS = {
    "locations": ["New York", "San Francisco"],
    "max_years_experience": 5,
    "exclude_any": ["intern"],
    "role_groups": [
        {"id": "strategy_operations", "label": "Strategy / Operations", "include_any": ["strategy", "operations", "chief of staff"]},
        {"id": "policy_ai", "label": "Policy / AI", "include_any": ["policy", "ai governance"]},
    ],
}


def job(title: str, description: str = "", posted_at: str = "") -> JobPosting:
    return JobPosting(
        source_id="fixture",
        source_name="Fixture",
        external_id=title.lower().replace(" ", "-"),
        title=title,
        company="Example",
        location_text="New York, NY",
        city="New York",
        canonical_url=f"https://example.com/{title.lower().replace(' ', '-')}",
        posted_at=posted_at,
        description_text=description,
    )


class ConditionTests(unittest.TestCase):
    def test_matches_role_group(self) -> None:
        match = match_job_conditions(job("Chief of Staff, AI Strategy"), CONDITIONS)
        self.assertTrue(match.allowed)
        self.assertIn("strategy_operations", match.role_group_ids)
        self.assertIn("strategy", match.matched_terms)

    def test_rejects_no_role_group(self) -> None:
        match = match_job_conditions(job("Account Executive"), CONDITIONS)
        self.assertFalse(match.allowed)
        self.assertEqual(match.rejection_reason, "no_role_group")

    def test_rejects_excluded_keyword_with_boundaries(self) -> None:
        self.assertTrue(match_job_conditions(job("Internal Business Operations Associate"), CONDITIONS).allowed)
        match = match_job_conditions(job("Operations Intern"), CONDITIONS)
        self.assertFalse(match.allowed)
        self.assertEqual(match.rejection_reason, "excluded_keyword")

    def test_excluded_seniority_uses_metadata_not_full_description(self) -> None:
        conditions = {**CONDITIONS, "exclude_any": ["director", "senior director"]}
        allowed = match_job_conditions(job("Business Operations Coordinator", "This role reports to the Senior Director of Operations."), conditions)
        self.assertTrue(allowed.allowed)
        rejected = match_job_conditions(job("Director, Operations"), conditions)
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.rejection_reason, "excluded_keyword")

    def test_rejects_more_than_five_years(self) -> None:
        match = match_job_conditions(job("Operations Lead", "Requires 8+ years of operations experience."), CONDITIONS)
        self.assertFalse(match.allowed)
        self.assertEqual(match.rejection_reason, "years_experience")

    def test_rejects_more_than_five_years_after_description_prefix(self) -> None:
        description = ("context " * 400) + "Who You Are 6+ years in client marketing, communications, or events."
        match = match_job_conditions(job("Strategic Events Program Manager", description), CONDITIONS)
        self.assertFalse(match.allowed)
        self.assertEqual(match.rejection_reason, "years_experience")

    def test_rejects_low_signal_full_text_keywords(self) -> None:
        conditions = {**CONDITIONS, "exclude_full_text_any": ["retail store", "sales floor"]}
        description = "This operations role supports a retail store and sales floor execution."
        match = match_job_conditions(job("Operations Lead", description), conditions)
        self.assertFalse(match.allowed)
        self.assertEqual(match.rejection_reason, "excluded_full_text_keyword")
        self.assertIn("retail store", match.excluded_terms)

    def test_excludes_title_keywords_before_role_matching(self) -> None:
        conditions = {**CONDITIONS, "exclude_title_any": ["recruiter", "senior"]}
        match = match_job_conditions(job("Senior Operations Recruiter"), conditions)
        self.assertFalse(match.allowed)
        self.assertEqual(match.rejection_reason, "excluded_title_keyword")

    def test_role_description_chars_can_ignore_company_blurbs(self) -> None:
        conditions = {**CONDITIONS, "role_description_chars": 0}
        description = "We are an AI company with product strategy and venture backing."
        rejected = match_job_conditions(job("Brand Designer", description), conditions)
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.rejection_reason, "no_role_group")

        allowed = match_job_conditions(job("Strategy Associate", description), conditions)
        self.assertTrue(allowed.allowed)

    def test_rejects_stale_postings_when_recency_enabled(self) -> None:
        now = datetime(2026, 6, 20, tzinfo=timezone.utc)
        conditions = {**CONDITIONS, "posted_within_days": 8}
        stale = job("Strategy Associate", posted_at=(now - timedelta(days=14)).isoformat())
        match = match_job_conditions(stale, conditions, now=now)
        self.assertFalse(match.allowed)
        self.assertEqual(match.rejection_reason, "not_recent")

    def test_allows_recent_postings_when_recency_enabled(self) -> None:
        now = datetime(2026, 6, 20, tzinfo=timezone.utc)
        conditions = {**CONDITIONS, "posted_within_days": 8}
        recent = job("Strategy Associate", posted_at=(now - timedelta(days=2)).isoformat())
        match = match_job_conditions(recent, conditions, now=now)
        self.assertTrue(match.allowed)
        self.assertLess(match.posting_age_days or 0, 8)

    def test_role_group_counts(self) -> None:
        counts = role_group_counts([
            {"role_group_ids": ["strategy_operations", "policy_ai"]},
            {"role_group_ids": ["strategy_operations"]},
        ])
        self.assertEqual(counts, {"policy_ai": 1, "strategy_operations": 2})


if __name__ == "__main__":
    unittest.main()

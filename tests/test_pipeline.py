from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from opportunity_radar.models import JobPosting, RankedOpportunity, RecipientResult
from opportunity_radar.pipeline import diversify_ranked, run_digest
from opportunity_radar.state import FileJsonStore


ROOT = Path(__file__).resolve().parents[1]


class FakeSender:
    provider = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[str, str]] = []

    def send(self, recipient: str, message: str) -> RecipientResult:
        self.sent.append((recipient, message))
        return RecipientResult(recipient=recipient, ok=not self.fail, provider=self.provider, error="failed" if self.fail else "")


class PipelineTests(unittest.TestCase):
    def test_delayed_scheduled_digest_sends_from_state_only_once(self) -> None:
        env = {
            "OPPORTUNITY_RECIPIENTS": "group@example.com",
            "OPPORTUNITY_TIMEZONE": "Asia/Shanghai",
            "OPPORTUNITY_SEND_DOW": "MON",
            "OPPORTUNITY_SEND_HOUR": "9",
        }
        job = JobPosting(
            source_id="fixture", source_name="Fixture", external_id="late-send",
            title="Strategy Associate", company="Example", location_text="Beijing",
            city="Beijing", canonical_url="https://example.com/late-send",
        )
        with patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            store.save({"evaluated_jobs": {
                job.stable_key: {"status": "included", "score": 90, "job": job.to_dict()},
            }})
            sender = FakeSender()
            def scheduled_run(timestamp: str):
                return run_digest(
                    ROOT, send=True, respect_schedule=True, from_state=True,
                    now=datetime.fromisoformat(timestamp), state_store=store, sender=sender,
                )

            early = scheduled_run("2026-10-19T00:59:59+00:00")
            self.assertIn("outside_configured_send_window", early.errors)
            self.assertEqual(sender.sent, [])

            delayed = scheduled_run("2026-10-19T03:43:18+00:00")
            self.assertEqual(delayed.errors, [])
            self.assertTrue(delayed.state_summary["mutated"])
            self.assertEqual(len(sender.sent), 1)
            self.assertIn(delayed.week_key, store.load()["sent_weeks"])
            self.assertIn(job.stable_key, store.load()["sent_jobs"])

            later = scheduled_run("2026-10-19T09:41:24+00:00")
            self.assertIn("week_already_sent", later.errors)
            self.assertFalse(later.state_summary["mutated"])
            self.assertEqual(len(sender.sent), 1)

    def test_dry_run_does_not_mutate_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            result = run_digest(
                ROOT,
                sources_path="tests/fixtures/sources.fixture.json",
                deterministic_fallback=True,
                include_seen=True,
                state_store=store,
            )
            self.assertGreaterEqual(result.candidate_count, 3)
            self.assertGreaterEqual(len(result.selected_jobs), 1)
            self.assertFalse((Path(tmp) / "state.json").exists())

    def test_send_mutates_state_once(self) -> None:
        with patch.dict(os.environ, {"OPPORTUNITY_RECIPIENTS": "whatsapp:+15550001111"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                sender = FakeSender()
                result = run_digest(
                    ROOT,
                    send=True,
                    force=True,
                    sources_path="tests/fixtures/sources.fixture.json",
                    deterministic_fallback=True,
                    include_seen=True,
                    state_store=store,
                    sender=sender,
                )
                self.assertTrue(result.state_summary["mutated"])
                state = store.load()
                self.assertIn(result.week_key, state["sent_weeks"])
                self.assertGreaterEqual(len(state["seen_jobs"]), 1)

                second = run_digest(
                    ROOT,
                    send=True,
                    force=False,
                    sources_path="tests/fixtures/sources.fixture.json",
                    deterministic_fallback=True,
                    include_seen=True,
                    state_store=store,
                    sender=sender,
                )
                self.assertIn("week_already_sent", second.errors)
                self.assertFalse(second.state_summary["mutated"])

    def test_ranker_failure_does_not_send_without_fallback(self) -> None:
        with patch.dict(os.environ, {"OPPORTUNITY_RECIPIENTS": "whatsapp:+15550001111", "OPENROUTER_API_KEY": ""}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                sender = FakeSender()
                result = run_digest(
                    ROOT,
                    send=True,
                    force=True,
                    sources_path="tests/fixtures/sources.fixture.json",
                    deterministic_fallback=False,
                    include_seen=True,
                    state_store=store,
                    sender=sender,
                )
                self.assertIn("ranker_failed:OpenRouterError", result.errors)
                self.assertEqual(sender.sent, [])
                self.assertFalse(result.state_summary["mutated"])
                self.assertFalse((Path(tmp) / "state.json").exists())
    def test_partial_failure_does_not_mark_sent(self) -> None:
        with patch.dict(os.environ, {"OPPORTUNITY_RECIPIENTS": "whatsapp:+15550001111"}, clear=False):
            with tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                result = run_digest(
                    ROOT,
                    send=True,
                    force=True,
                    sources_path="tests/fixtures/sources.fixture.json",
                    deterministic_fallback=True,
                    include_seen=True,
                    state_store=store,
                    sender=FakeSender(fail=True),
                )
                self.assertFalse(result.state_summary["mutated"])
                self.assertFalse((Path(tmp) / "state.json").exists())

    def test_real_twilio_send_preflights_missing_credentials(self) -> None:
        env = {"OPPORTUNITY_RECIPIENTS": "whatsapp:+15550001111"}
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                result = run_digest(
                    ROOT,
                    send=True,
                    force=True,
                    sources_path="tests/fixtures/sources.fixture.json",
                    deterministic_fallback=True,
                    include_seen=True,
                    state_store=store,
                )
                self.assertIn("TWILIO_ACCOUNT_SID is not set", result.errors)
                self.assertIn("TWILIO_AUTH_TOKEN is not set", result.errors)
                self.assertIn("TWILIO_WHATSAPP_FROM is not set", result.errors)
                self.assertEqual(result.recipient_results, [])
                self.assertFalse(result.state_summary["mutated"])

    @patch("opportunity_radar.pipeline.load_google_sheet_recipients")
    def test_send_uses_google_sheet_recipients(self, load_google_sheet_recipients) -> None:  # type: ignore[no-untyped-def]
        load_google_sheet_recipients.return_value = ["one@example.com", "two@example.com"]
        env = {
            "OPPORTUNITY_SEND_PROVIDER": "gmail_email",
            "GOOGLE_RECIPIENTS_SHEET_ID": "sheet-id",
        }
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                sender = FakeSender()
                result = run_digest(
                    ROOT,
                    send=True,
                    force=True,
                    sources_path="tests/fixtures/sources.fixture.json",
                    deterministic_fallback=True,
                    include_seen=True,
                    state_store=store,
                    sender=sender,
                )
        self.assertTrue(result.state_summary["mutated"])
        self.assertEqual([recipient for recipient, _message in sender.sent], ["one@example.com", "two@example.com"])

    def test_empty_state_send_does_not_email_by_default(self) -> None:
        env = {"OPPORTUNITY_RECIPIENTS": "group@example.com"}
        with patch.dict(os.environ, env, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                sender = FakeSender()
                result = run_digest(
                    ROOT,
                    send=True,
                    force=True,
                    from_state=True,
                    state_store=store,
                    sender=sender,
                )
        self.assertIn("no_evaluated_jobs", result.errors)
        self.assertEqual(sender.sent, [])
        self.assertFalse(result.state_summary["mutated"])


class PipelineDiversityTests(unittest.TestCase):
    def test_diversify_ranked_caps_company_repeats(self) -> None:
        ranked = []
        for index, company in enumerate(["Ampersand", "Ampersand", "Ampersand", "Atlas", "Braintrust"], start=1):
            job = JobPosting(
                source_id="fixture",
                source_name="Fixture",
                external_id=str(index),
                title=f"Strategy Role {index}",
                company=company,
                location_text="New York",
                city="New York",
                canonical_url=f"https://example.com/{index}",
            )
            ranked.append(RankedOpportunity(job, 100 - index, True, "", ""))
        selected = diversify_ranked(ranked, max_jobs=4, max_jobs_per_company=2)
        self.assertEqual([item.job.company for item in selected], ["Ampersand", "Ampersand", "Atlas", "Braintrust"])
        self.assertEqual([item.rank for item in selected], [1, 2, 3, 4])


if __name__ == "__main__":
    unittest.main()

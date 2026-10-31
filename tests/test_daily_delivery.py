from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from opportunity_radar.models import JobPosting
from opportunity_radar.pipeline import run_digest
from opportunity_radar.state import FileJsonStore
from tests.test_pipeline import FakeSender


ROOT = Path(__file__).resolve().parents[1]
ENV = {
    "OPPORTUNITY_RECIPIENTS": "group@example.com",
    "OPPORTUNITY_TIMEZONE": "Asia/Shanghai",
    "OPPORTUNITY_SEND_FREQUENCY": "daily",
    "OPPORTUNITY_SEND_DOW": "MON",
    "OPPORTUNITY_SEND_HOUR": "9",
    "OPPORTUNITY_MAX_JOBS": "1",
}


def seed(store: FileJsonStore) -> None:
    jobs = {}
    for index in range(3):
        job = JobPosting(
            source_id="fixture", source_name="Fixture", external_id=str(index),
            title=f"Strategy Associate {index}", company=f"Example {index}",
            location_text="Beijing", city="Beijing", canonical_url=f"https://example.com/{index}",
        )
        jobs[job.stable_key] = {"status": "included", "score": 90 - index, "job": job.to_dict()}
    store.save({"evaluated_jobs": jobs})


def run(store: FileJsonStore, sender: FakeSender, timestamp: str, *, scheduled: bool = True):
    # Delivery receipts must use the same simulated clock as schedule decisions.
    with patch("opportunity_radar.pipeline.now_iso", return_value=timestamp):
        return run_digest(
            ROOT, send=True, from_state=True, respect_schedule=scheduled,
            now=datetime.fromisoformat(timestamp), state_store=store, sender=sender,
        )


class DailyDeliveryTests(unittest.TestCase):
    def test_daily_delivery_sends_new_jobs_on_consecutive_days_only_once_per_day(self) -> None:
        with patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            seed(store)
            sender = FakeSender()
            first = run(store, sender, "2026-10-26T03:43:00+00:00")
            self.assertEqual(first.errors, [])
            again = run(store, sender, "2026-10-26T10:00:00+00:00")
            self.assertIn("day_already_sent", again.errors)
            next_day = run(store, sender, "2026-10-27T03:43:00+00:00")
            self.assertEqual(next_day.errors, [])
            self.assertEqual(len(sender.sent), 2)
            self.assertNotEqual(first.selected_jobs[0].job.stable_key, next_day.selected_jobs[0].job.stable_key)
            self.assertEqual(set(store.load()["sent_days"]), {"2026-10-26", "2026-10-27"})

    def test_every_local_day_is_eligible_after_send_hour(self) -> None:
        for day in range(26, 33):
            timestamp = f"2026-10-{day:02d}T03:43:00+00:00" if day <= 31 else "2026-11-01T03:43:00+00:00"
            with self.subTest(timestamp=timestamp), patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
                store = FileJsonStore(Path(tmp) / "state.json")
                seed(store)
                sender = FakeSender()
                result = run(store, sender, timestamp)
                self.assertEqual(result.errors, [])
                self.assertEqual(len(sender.sent), 1)

    def test_local_date_boundary_and_manual_send_share_daily_lock(self) -> None:
        with patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            seed(store)
            sender = FakeSender()
            first = run(store, sender, "2026-10-26T15:59:59+00:00")
            self.assertEqual(first.errors, [])
            early = run(store, sender, "2026-10-26T16:00:00+00:00")
            self.assertIn("outside_configured_send_window", early.errors)
            self.assertEqual(len(sender.sent), 1)
            # Manual delivery after local midnight is a new day, despite same UTC date.
            manual = run(store, sender, "2026-10-26T16:01:00+00:00", scheduled=False)
            self.assertEqual(manual.errors, [])
            scheduled = run(store, sender, "2026-10-27T01:17:00+00:00")
            self.assertIn("day_already_sent", scheduled.errors)
            self.assertEqual(len(sender.sent), 2)

    def test_legacy_weekly_send_blocks_only_its_local_day(self) -> None:
        with patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            seed(store)
            state = store.load()
            state["sent_weeks"] = {"2026-W44": {"sent_at": "2026-10-25T17:00:00Z", "run_id": "old"}}
            store.save(state)
            sender = FakeSender()
            today = run(store, sender, "2026-10-26T03:43:00+00:00")
            self.assertIn("day_already_sent", today.errors)
            tomorrow = run(store, sender, "2026-10-27T03:43:00+00:00")
            self.assertEqual(tomorrow.errors, [])
            self.assertEqual(len(sender.sent), 1)

    def test_daily_email_uses_local_date_and_daily_wording(self) -> None:
        with patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            seed(store)
            result = run(store, FakeSender(), "2026-10-26T03:43:00+00:00")
            self.assertIn("October 26, 2026", result.digest_text)
            self.assertIn("Today's list", result.digest_text)
            self.assertNotIn("week", result.digest_text)

    def test_failed_daily_send_does_not_lock_the_day(self) -> None:
        with patch.dict(os.environ, ENV, clear=True), tempfile.TemporaryDirectory() as tmp:
            store = FileJsonStore(Path(tmp) / "state.json")
            seed(store)
            failed = run(store, FakeSender(fail=True), "2026-10-27T03:43:00+00:00")
            self.assertTrue(failed.recipient_results)
            self.assertTrue(failed.state_summary["mutated"])
            self.assertFalse(store.load().get("sent_days"))
            self.assertTrue(store.load().get("delivery_attempts"))
            succeeded = run(store, FakeSender(), "2026-10-27T04:43:00+00:00")
            self.assertTrue(succeeded.state_summary["mutated"])
            self.assertNotEqual(failed.selected_jobs[0].job.stable_key, succeeded.selected_jobs[0].job.stable_key)


if __name__ == "__main__":
    unittest.main()

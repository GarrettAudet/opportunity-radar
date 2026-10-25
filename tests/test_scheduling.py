from __future__ import annotations

import unittest
from datetime import datetime

from opportunity_radar.scheduling import should_send_now


class SchedulingTests(unittest.TestCase):
    def test_delayed_monday_runs_remain_eligible(self) -> None:
        # Actual first Monday runs that previously missed the Shanghai send hour.
        for timestamp in ("2026-10-12T04:52:39+00:00", "2026-10-19T03:43:18+00:00"):
            with self.subTest(timestamp=timestamp):
                self.assertTrue(should_send_now(
                    datetime.fromisoformat(timestamp),
                    timezone_name="Asia/Shanghai", send_dow="MON", send_hour=9,
                ))

    def test_window_opens_at_send_hour_and_closes_at_local_midnight(self) -> None:
        cases = (
            ("2026-10-19T00:59:59+00:00", False),
            ("2026-10-19T01:00:00+00:00", True),
            ("2026-10-19T15:59:59+00:00", True),
            ("2026-10-19T16:00:00+00:00", False),
            ("2026-10-18T01:00:00+00:00", False),
        )
        for timestamp, expected in cases:
            with self.subTest(timestamp=timestamp):
                self.assertEqual(should_send_now(
                    datetime.fromisoformat(timestamp),
                    timezone_name="Asia/Shanghai", send_dow="MON", send_hour=9,
                ), expected)

    def test_uses_local_weekday_when_utc_day_differs(self) -> None:
        self.assertTrue(should_send_now(
            datetime.fromisoformat("2026-10-20T01:00:00+00:00"),
            timezone_name="America/Edmonton", send_dow="MON", send_hour=9,
        ))

    def test_other_configured_weekday(self) -> None:
        self.assertTrue(should_send_now(
            datetime.fromisoformat("2026-10-21T04:00:00+00:00"),
            timezone_name="Asia/Shanghai", send_dow="WED", send_hour=9,
        ))

    def test_invalid_schedule_cannot_send(self) -> None:
        for day, hour in (("invalid", 9), ("MON", -1), ("MON", 24)):
            with self.subTest(day=day, hour=hour):
                self.assertFalse(should_send_now(
                    datetime.fromisoformat("2026-10-19T03:43:18+00:00"),
                    timezone_name="Asia/Shanghai", send_dow=day, send_hour=hour,
                ))


if __name__ == "__main__":
    unittest.main()

import unittest
from datetime import datetime, timezone

from maintenance import (
    evaluate_maintenance_window,
    parse_data_uri_maintenance_windows,
    parse_notice_maintenance_window,
)


class MaintenanceTests(unittest.TestCase):
    def test_filters_global_android_full_service_maintenance(self):
        windows = parse_data_uri_maintenance_windows(
            [
                {
                    "MaintenanceServerType": 0,
                    "StartTimeFixJST": "2026-08-31 14:30:00+00:00",
                    "EndTimeFixJST": "2026-08-31 16:30:00+00:00",
                    "MaintenancePlatformTypes": [0],
                    "MaintenanceAreaType": 0,
                    "AreaIds": [],
                    "MaintenanceFunctionTypes": [],
                },
                {
                    "MaintenanceServerType": 2,
                    "StartTimeFixJST": "2026-08-31 12:00:00+00:00",
                    "EndTimeFixJST": "2026-08-31 16:30:00+00:00",
                    "MaintenancePlatformTypes": [0],
                    "MaintenanceAreaType": 1,
                    "AreaIds": [50, 60, 61],
                    "MaintenanceFunctionTypes": [15],
                },
            ]
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual(
            windows[0].start_at_utc,
            datetime(2026, 8, 31, 5, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            windows[0].end_at_utc,
            datetime(2026, 8, 31, 7, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(windows[0].source, "data-uri")

    def test_parses_anonymous_english_maintenance_notice(self):
        window = parse_notice_maintenance_window(
            """
            2026/8/30 2:30 (UTC-7)<br><br>
            We will perform maintenance at the time below.<br><br>
            ◆Maintenance Duration<br>
            2026/8/30 22:30~2026/8/31 0:30 (UTC-7)<br><br>
            ◆Maintenance Details
            """,
            notice_id=158003,
        )

        self.assertIsNotNone(window)
        assert window is not None
        self.assertEqual(
            window.start_at_utc,
            datetime(2026, 8, 31, 5, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(
            window.end_at_utc,
            datetime(2026, 8, 31, 7, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(window.notice_id, 158003)

    def test_runs_from_fifteen_minutes_after_start_through_end_grace(self):
        window = parse_notice_maintenance_window(
            "Maintenance Duration<br>"
            "2026/8/30 22:30~2026/8/31 0:30 (UTC-7)"
        )
        assert window is not None

        before = evaluate_maintenance_window(
            [window], datetime(2026, 8, 31, 5, 44, tzinfo=timezone.utc)
        )
        at_start = evaluate_maintenance_window(
            [window], datetime(2026, 8, 31, 5, 45, tzinfo=timezone.utc)
        )
        at_end = evaluate_maintenance_window(
            [window], datetime(2026, 8, 31, 7, 45, tzinfo=timezone.utc)
        )
        after = evaluate_maintenance_window(
            [window], datetime(2026, 8, 31, 7, 46, tzinfo=timezone.utc)
        )

        self.assertFalse(before.should_run)
        self.assertTrue(at_start.should_run)
        self.assertTrue(at_end.should_run)
        self.assertFalse(after.should_run)
        self.assertEqual(at_start.reason, "maintenance-window")
        self.assertEqual(
            at_start.effective_start_at_utc,
            datetime(2026, 8, 31, 5, 45, tzinfo=timezone.utc),
        )
        self.assertEqual(
            at_start.effective_end_at_utc,
            datetime(2026, 8, 31, 7, 45, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()

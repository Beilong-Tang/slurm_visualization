"""Times are shown in the viewer's time zone (from the slurmviz_tz cookie)."""

import unittest
from datetime import datetime, timezone

from slurm_viz import render as r
from slurm_viz import views
from slurm_viz.slurm import Job


def server_naive(utc: datetime) -> datetime:
    """How Slurm would print this instant on this server: naive, in the server's own zone."""
    return utc.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


class ViewerZone:
    def __init__(self, name):
        self.zone = r.parse_tz(name)

    def __enter__(self):
        self.token = r.DISPLAY_TZ.set(self.zone)

    def __exit__(self, *exc):
        r.DISPLAY_TZ.reset(self.token)


class TestTimezone(unittest.TestCase):
    def test_daylight_saving_offsets(self):
        before = server_naive(datetime(2026, 10, 28, 14, 0))  # EDT, UTC-4
        after = server_naive(datetime(2026, 11, 3, 2, 58))    # EST, UTC-5
        with ViewerZone("America/New_York"):
            self.assertEqual(r.to_display(before).strftime("%m-%d %H:%M %Z"), "10-28 10:00 EDT")
            self.assertEqual(r.to_display(after).strftime("%m-%d %H:%M %Z"), "11-02 21:58 EST")
            html = r.fmt_dt(after, now=after)
            self.assertIn(">11-02 21:58</time>", html)
            self.assertIn("server 11-03 02:58", html)  # tooltip keeps Slurm's own time (server is UTC)

    def test_unknown_or_missing_zone_uses_server_time(self):
        self.assertIsNone(r.parse_tz("Not/AZone"))
        self.assertIsNone(r.parse_tz(""))
        self.assertIsNone(r.parse_tz("../../etc/passwd"))
        t = server_naive(datetime(2026, 11, 3, 2, 58))
        with ViewerZone(None):
            self.assertIn(f">{t:%m-%d %H:%M}</time>", r.fmt_dt(t, now=t))

    def test_date_filter_uses_viewer_days(self):
        # submitted Nov 3 02:10 UTC = Nov 2 21:10 in New York
        job = Job("1", submit=server_naive(datetime(2026, 11, 3, 2, 10)))
        with ViewerZone("America/New_York"):
            self.assertEqual(len(views.apply_filters([job], {"from": "2026-11-02", "to": "2026-11-02"})), 1)
            self.assertEqual(len(views.apply_filters([job], {"from": "2026-11-03"})), 0)
        with ViewerZone("UTC"):
            self.assertEqual(len(views.apply_filters([job], {"from": "2026-11-03"})), 1)

    def test_layout_announces_zone(self):
        with ViewerZone("America/New_York"):
            html = r.layout("t", "/", "", {}, "me", fetched_at=1_793_000_000)
        self.assertIn('data-tz="America/New_York"', html)
        self.assertRegex(html, r"times in E[SD]T")


if __name__ == "__main__":
    unittest.main()

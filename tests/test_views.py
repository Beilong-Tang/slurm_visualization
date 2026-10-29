"""Tests for pagination, filters, rendering and the HTTP server (using saved fixtures, no Slurm needed)."""

import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from slurm_viz import server, views
from slurm_viz.render import esc, fmt_td, pagination
from slurm_viz.slurm import SEP, SACCT_FIELDS, Job, SlurmClient

FIX = Path(__file__).parent / "fixtures"


def fake_runner(args):
    if args[0] == "squeue":
        return (FIX / "squeue.txt").read_text()
    if args[0] == "sacct" and "-j" in args:
        return (FIX / "sacct_steps.txt").read_text() if "-X" not in args else ""
    if args[0] == "sacct":
        # one extra job with HTML in its name to check escaping
        evil = dict.fromkeys(SACCT_FIELDS, "")
        evil.update(JobID="1", JobIDRaw="1", JobName="<script>alert(1)</script>", State="COMPLETED",
                    Partition="gpu", Submit="2026-10-01T00:00:00")
        return (FIX / "sacct_history.txt").read_text() + SEP.join(evil[f] for f in SACCT_FIELDS) + "\n"
    if args[0] == "scontrol":
        return (FIX / "scontrol_pending.txt").read_text()
    raise AssertionError(args)


def make_app(page_size=20):
    return views.App(client=SlurmClient(user="btang1", runner=fake_runner), page_size=page_size)


class TestPaginate(unittest.TestCase):
    def test_pages(self):
        items = list(range(45))
        self.assertEqual(views.paginate(items, 1, 20), (list(range(20)), 1, 3))
        self.assertEqual(views.paginate(items, 3, 20), (list(range(40, 45)), 3, 3))
        self.assertEqual(views.paginate(items, 99, 20)[1:], (3, 3))  # clamped
        self.assertEqual(views.paginate(items, -5, 20)[1:], (1, 3))
        self.assertEqual(views.paginate([], 1, 20), ([], 1, 1))
        self.assertEqual(views.parse_page("abc"), 1)

    def test_pager_html(self):
        html = pagination("/", {"q": "x"}, 6, 20, 400, 20)
        self.assertIn("101–120 of 400", html)
        self.assertIn('href="/?q=x&amp;page=7"', html)
        self.assertIn("…", html)
        self.assertNotIn("<a", pagination("/", {}, 1, 1, 5, 20))


class TestFilterSort(unittest.TestCase):
    def setUp(self):
        t = datetime(2026, 10, 20)
        self.jobs = [
            Job("10", name="train-a", state="COMPLETED", partition="gpu", submit=t, elapsed=timedelta(hours=1)),
            Job("11", name="eval", state="TIMEOUT", partition="gpu-hp", submit=t + timedelta(days=2)),
            Job("12", name="train-b", state="PENDING", partition="gpu", submit=t + timedelta(days=5)),
            Job("13", name="bash", state="CANCELLED", partition="common", submit=None),
        ]

    def ids(self, jobs):
        return [j.job_id for j in jobs]

    def test_filters(self):
        f = views.apply_filters
        self.assertEqual(self.ids(f(self.jobs, {"q": "TRAIN"})), ["10", "12"])
        self.assertEqual(self.ids(f(self.jobs, {"q": "11"})), ["11"])
        self.assertEqual(self.ids(f(self.jobs, {"state": "failed"})), ["11"])  # group name
        self.assertEqual(self.ids(f(self.jobs, {"state": "TIMEOUT"})), ["11"])
        self.assertEqual(self.ids(f(self.jobs, {"partition": "gpu"})), ["10", "12"])
        self.assertEqual(self.ids(f(self.jobs, {"from": "2026-10-21", "to": "2026-10-22"})), ["11"])
        self.assertEqual(len(f(self.jobs, {"from": "garbage"})), 4)

    def test_sort_puts_missing_last(self):
        p = {"sort": "submit", "order": "asc"}
        self.assertEqual(self.ids(views.sort_jobs(self.jobs, p, "submit")), ["10", "11", "12", "13"])
        p = {"sort": "submit", "order": "desc"}
        self.assertEqual(self.ids(views.sort_jobs(self.jobs, p, "submit")), ["12", "11", "10", "13"])
        p = {"sort": "bogus"}
        views.sort_jobs(self.jobs, p, "id")
        self.assertEqual(p["sort"], "id")

    def test_fmt_td(self):
        self.assertEqual(fmt_td(timedelta(days=1, hours=22, minutes=5)), "1d 22h")
        self.assertEqual(fmt_td(timedelta(seconds=334)), "5m 34s")
        self.assertEqual(fmt_td(timedelta(0)), "0s")
        self.assertEqual(fmt_td(None), "–")


class TestPages(unittest.TestCase):
    def test_list_pages(self):
        app = make_app()
        for view in (views.overview, views.running, views.pending, views.completed):
            page = view(app, {})
            self.assertEqual(page.status, 200)
            self.assertLessEqual(page.html.count('<tr class="row-'), 20)
        page = views.overview(app, {})
        self.assertEqual(page.html.count('<tr class="row-'), 20)
        self.assertNotIn("<script>alert", page.html)  # escaped everywhere

    def test_escaping_on_search(self):
        page = views.overview(make_app(), {"q": "<script>alert(1)</script>"})
        self.assertNotIn("<script>alert", page.html)
        self.assertIn(esc("<script>alert(1)</script>"), page.html)

    def test_running_only_running(self):
        app = make_app()
        page = views.running(app, {})
        self.assertNotIn("row-pending", page.html)
        self.assertNotIn("row-completed", page.html)

    def test_detail(self):
        app = make_app()
        self.assertEqual(views.job_detail(app, {}, "777824").status, 200)
        self.assertEqual(views.job_detail(app, {}, "777852").status, 200)
        self.assertEqual(views.job_detail(app, {}, "bad;id").status, 404)
        self.assertEqual(views.job_detail(app, {}, "123").status, 404)


class TestServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        handler = type("H", (server.Handler,), {"app": make_app(), "token": "secret", "cookie_name": "c"})
        handler.log_message = lambda *a: None
        cls.httpd = server.bind("127.0.0.1", 18765, handler)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path, cookie=None):
        req = urllib.request.Request(self.base + path, headers={"Cookie": cookie} if cookie else {})
        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req) as resp:
                return resp.status, dict(resp.headers), resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode()

    def test_auth(self):
        self.assertEqual(self.get("/")[0], 403)
        self.assertEqual(self.get("/?token=wrong")[0], 403)
        self.assertEqual(self.get("/", cookie="c=wrong")[0], 403)
        status, headers, _ = self.get("/running?token=secret&page=2")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/running?page=2")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertEqual(self.get("/", cookie="c=secret")[0], 200)

    def test_fresh_bypasses_cache(self):
        client = self.httpd.RequestHandlerClass.app.client
        self.get("/", cookie="c=secret")
        fetched = client.cache._entries["squeue"].fetched_at
        self.get("/", cookie="c=secret")
        self.assertEqual(client.cache._entries["squeue"].fetched_at, fetched)  # cached
        import time; time.sleep(0.01)
        status, _, html = self.get("/?fresh=1", cookie="c=secret")
        self.assertEqual(status, 200)
        self.assertGreater(client.cache._entries["squeue"].fetched_at, fetched)
        self.assertNotIn("fresh=1", html)  # not copied into sort/page links

    def test_swap_regions(self):
        _, _, html = self.get("/completed?state=FAILED", cookie="c=secret")
        for region in ('id="results" data-swap', 'id="tabs" data-swap', 'id="banner" data-swap',
                       'id="updated" data-swap', 'id="refresh-btn"', 'id="loadbar"', 'id="filters"'):
            self.assertIn(region, html)
        _, _, html = self.get("/", cookie="c=secret")
        self.assertIn('id="cards" data-swap', html)

    def test_live_log_endpoint(self):
        import json
        status, headers, body = self.get("/job/777852/log", cookie="c=secret")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["Content-Type"])
        data = json.loads(body)
        self.assertEqual(data["state"], "PENDING")
        self.assertEqual([l["kind"] for l in data["logs"]], ["stdout", "stderr"])
        self.assertEqual(self.get("/job/777852/log")[0], 403)  # token still required
        self.assertEqual(self.get("/job/bad;id/log", cookie="c=secret")[0], 404)
        status, _, body = self.get("/job/123_%5B5-10%5D", cookie="c=secret")
        self.assertEqual(status, 404)
        self.assertIn("not found", body)  # URL-decoded into a valid array id, not rejected as invalid

    def test_favicon(self):
        self.assertEqual(self.get("/favicon.ico")[0], 200)

    def test_static_and_traversal(self):
        self.assertEqual(self.get("/static/style.css")[0], 200)
        self.assertEqual(self.get("/static/../server.py")[0], 404)
        self.assertEqual(self.get("/static/%2e%2e/server.py")[0], 404)

    def test_routes(self):
        for path in ("/", "/running", "/pending", "/completed?page=2", "/job/777824"):
            self.assertEqual(self.get(path, cookie="c=secret")[0], 200, path)
        self.assertEqual(self.get("/nope", cookie="c=secret")[0], 404)
        _, headers, _ = self.get("/", cookie="c=secret")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


if __name__ == "__main__":
    unittest.main()

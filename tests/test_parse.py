"""Parser tests using real squeue/sacct output saved from the NCShare cluster.

Run with: python3 -m unittest discover -s tests -v
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from slurm_viz.cache import TTLCache
from slurm_viz.slurm import (
    SACCT_FIELDS, SEP, Job, SlurmClient, SlurmError, check_job_id, expand_log_path,
    merge_jobs, normalize_state, parse_duration, parse_gpus, parse_sacct, parse_scontrol,
    parse_squeue, parse_steps, read_log_tail,
)

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


class TestHelpers(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(parse_duration("5:34"), timedelta(minutes=5, seconds=34))
        self.assertEqual(parse_duration("1:11:05"), timedelta(hours=1, minutes=11, seconds=5))
        self.assertEqual(parse_duration("1-22:22:50"), timedelta(days=1, hours=22, minutes=22, seconds=50))
        self.assertEqual(parse_duration("2-00:00:00"), timedelta(days=2))
        self.assertEqual(parse_duration("00:00:02.123"), timedelta(seconds=2))
        for empty in ("UNLIMITED", "Partition_Limit", "INVALID", "", "N/A"):
            self.assertIsNone(parse_duration(empty))

    def test_gpus(self):
        self.assertEqual(parse_gpus("billing=24,cpu=24,gres/gpu:h200=1,gres/gpu=1,mem=128G,node=1"), (1, "h200"))
        self.assertEqual(parse_gpus("cpu=24,mem=128G,node=1,billing=24,gres/gpu=4,gres/gpu:h200=4"), (4, "h200"))
        self.assertEqual(parse_gpus("gres/gpu:h200:2"), (2, "h200"))
        self.assertEqual(parse_gpus("gpu:3"), (3, ""))
        self.assertEqual(parse_gpus("billing=2,cpu=2,mem=4G,node=1"), (0, ""))
        self.assertEqual(parse_gpus("N/A"), (0, ""))

    def test_states(self):
        self.assertEqual(normalize_state("CANCELLED by 3000426"), "CANCELLED")
        self.assertEqual(normalize_state("CANCELLED+"), "CANCELLED")
        self.assertEqual(normalize_state("RUNNING"), "RUNNING")

    def test_job_id_validation(self):
        for good in ("777852", "123_4", "123_[5-10]", "123_[1,3,5-7%2]", "55+1"):
            check_job_id(good)
        for bad in ("", "-a", "123;rm", "abc", "123 456"):
            with self.assertRaises(ValueError):
                check_job_id(bad)


class TestRealOutput(unittest.TestCase):
    def test_squeue(self):
        jobs = parse_squeue(fixture("squeue.txt"))
        self.assertTrue(jobs)
        for j in jobs:
            self.assertIn(j.category, ("running", "pending"))
            self.assertIsNotNone(j.submit)
            self.assertNotIn("(", j.reason)  # %r, not %R
        pending = [j for j in jobs if j.category == "pending"]
        for j in pending:
            self.assertIsNone(j.time_left)

    def test_sacct(self):
        jobs = parse_sacct(fixture("sacct_history.txt"))
        self.assertEqual(len(jobs), len(fixture("sacct_history.txt").splitlines()))
        states = {j.state for j in jobs}
        self.assertTrue(states <= {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "RUNNING", "PENDING",
                                   "OUT_OF_MEMORY", "PREEMPTED", "NODE_FAIL", "REQUEUED"}, states)
        cancelled = [j for j in jobs if j.state == "CANCELLED"]
        self.assertTrue(all(j.state_group == "cancelled" for j in cancelled))
        self.assertTrue(any(j.gpus for j in jobs), "expected at least one GPU job")
        for j in jobs:
            self.assertIsNotNone(j.submit, j.job_id)
            if j.category == "finished" and j.start:
                self.assertIsNotNone(j.end, j.job_id)

    def test_wrong_field_count_is_an_error(self):
        with self.assertRaises(SlurmError):
            parse_sacct("1|2|3")

    def test_steps(self):
        steps = parse_steps(fixture("sacct_steps.txt"))
        self.assertEqual([s.step_id for s in steps], ["777824", "777824.batch", "777824.extern"])
        self.assertEqual(steps[1].max_rss, "16531800K")

    def test_scontrol(self):
        info = parse_scontrol(fixture("scontrol_pending.txt"))
        self.assertEqual(info["JobState"], "PENDING")
        self.assertTrue(info["StdOut"].endswith(".out"))
        self.assertNotIn("%", info["StdOut"])

    def test_merge_prefers_live_data(self):
        hist = parse_sacct(fixture("sacct_history.txt"))
        act = parse_squeue(fixture("squeue.txt"))
        merged = merge_jobs(hist, act)
        ids = [j.job_id for j in merged]
        self.assertEqual(len(ids), len(set(ids)))
        live = {j.job_id for j in act}
        for j in merged:
            if j.job_id in live:
                self.assertEqual(j.source, "squeue")
        submits = [j.submit for j in merged if j.submit]
        self.assertEqual(submits, sorted(submits, reverse=True))


class TestLogs(unittest.TestCase):
    def test_expand_patterns(self):
        job = Job(job_id="777824", job_id_raw="777824", name="sphere-prep", workdir="/w", nodelist="gpu-[01-02]")
        self.assertEqual(expand_log_path("/logs/%x_%j.out", job, "me"), "/logs/sphere-prep_777824.out")
        self.assertEqual(expand_log_path("out-%u-%N-%%.txt", job, "me"), "/w/out-me-gpu-01-%.txt")
        arr = Job(job_id="900_7", job_id_raw="907", name="a", workdir="/w")
        self.assertEqual(expand_log_path("slurm-%A_%a.out", arr, "me"), "/w/slurm-900_7.out")
        self.assertEqual(expand_log_path("x-%3a-%j", arr, "me"), "/w/x-007-907")
        self.assertEqual(expand_log_path("", job, "me"), "")

    def test_read_tail(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "log.out")
            Path(p).write_bytes(b"first\nprogress 10%\rprogress 100%\nwin\r\n" + b"x" * 10)
            text, msg = read_log_tail(p)
            self.assertEqual(text, "first\nprogress 100%\nwin\n" + "x" * 10)
            text, _ = read_log_tail(p, max_bytes=12)
            self.assertEqual(text, "x" * 10)  # partial first line dropped
            Path(p).write_bytes(b"\x1b[34m2026-10-29\x1b[0m (step=1) loss \x1b[1;31mERR\x1b[0m\n")
            self.assertEqual(read_log_tail(p)[0], "2026-10-29 (step=1) loss ERR\n")
            self.assertEqual(read_log_tail(os.path.join(d, "missing"))[0], None)
            self.assertEqual(read_log_tail("/dev/null")[0], None)
            self.assertEqual(read_log_tail(d)[1], "not a regular file")


class TestClientAndCache(unittest.TestCase):
    def test_cache_ttl_and_stale(self):
        now = [0.0]
        cache = TTLCache(ttl=10, clock=lambda: now[0])
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) == 2:
                raise SlurmError("slurmctld down")
            return len(calls)

        self.assertEqual(cache.get("k", fetch).value, 1)
        self.assertEqual(cache.get("k", fetch).value, 1)  # cached
        now[0] = 11
        r = cache.get("k", fetch)  # refresh fails -> stale old value
        self.assertEqual((r.value, r.stale), (1, True))
        now[0] = 22
        r = cache.get("k", fetch)
        self.assertEqual((r.value, r.stale), (3, False))

    def test_client_uses_runner(self):
        seen = []

        def runner(args):
            seen.append(args[0])
            return fixture("squeue.txt") if args[0] == "squeue" else fixture("sacct_history.txt")

        c = SlurmClient(user="btang1", runner=runner)
        jobs = c.all_jobs().value
        self.assertTrue(jobs)
        c.all_jobs()
        self.assertEqual(seen, ["squeue", "sacct"])  # second call served from cache
        args = c.sacct_args()
        self.assertIn("-X", args)
        self.assertIn("now-30days", args)
        self.assertIn(f"--delimiter={SEP}", args)
        self.assertTrue(args[-1].endswith(",".join(SACCT_FIELDS)))
        with self.assertRaises(ValueError):
            c.steps("--help")


if __name__ == "__main__":
    unittest.main()

"""GPU utilization / memory efficiency: sacct parsing, slurm-gpu parsing, source selection."""

import unittest
from pathlib import Path

from slurm_viz.gpu import (
    GPU_MEM_MIB, SEP, GpuStatsSource, parse_mem_mib, parse_sacct_gpu, parse_slurm_gpu,
)
from slurm_viz.slurm import SlurmClient

FIX = Path(__file__).parent / "fixtures"

# Shape of a polars DataFrame print, which is what the ┆ separators suggest.
# NOT real slurm-gpu output yet: replace with a saved copy once it has been run on a login node.
SLURM_GPU_SAMPLE = """\
shape: (3, 5)
┌─────────┬──────────────┬───────────┬─────────┬───────────┐
│ JobID   ┆ JobName      ┆ Partition ┆ GPUUtil ┆ GPUMemEff │
│ ---     ┆ ---          ┆ ---       ┆ ---     ┆ ---       │
│ str     ┆ str          ┆ str       ┆ f64     ┆ f64       │
╞═════════╪══════════════╪═══════════╪═════════╪═══════════╡
│ 774246  ┆ train        ┆ gpu-hp    ┆ 87.5    ┆ 61.2      │
│ 782942  ┆ eval         ┆ gpu-hp    ┆ 42.0%   ┆ 12.3%     │
│ …       ┆ …            ┆ …         ┆ …       ┆ …         │
└─────────┴──────────────┴───────────┴─────────┴───────────┘
"""


class TestSacctGpu(unittest.TestCase):
    def setUp(self):
        self.stats = parse_sacct_gpu((FIX / "sacct_gpu.txt").read_text())

    def test_mem_units(self):
        self.assertEqual(parse_mem_mib("366786M"), 366786)
        self.assertEqual(parse_mem_mib("12G"), 12288)
        self.assertEqual(parse_mem_mib("0"), 0)
        self.assertIsNone(parse_mem_mib(""))

    def test_multi_gpu_job_is_per_gpu(self):
        # 774246: 3x H200, gpuutil=297 and gpumem=343720M summed over the GPUs
        st = self.stats["774246"]
        self.assertAlmostEqual(st.util, 99.0)
        self.assertAlmostEqual(st.mem_eff, 343720 / (3 * GPU_MEM_MIB["h200"]) * 100)
        self.assertEqual(st.mem_total_mib, 3 * GPU_MEM_MIB["h200"])
        self.assertEqual(st.source, "sacct")

    def test_busiest_step_wins(self):
        # 773765: batch step 99% / 130896M, step 1 4% / 10882M
        st = self.stats["773765"]
        self.assertAlmostEqual(st.util, 99.0)
        self.assertAlmostEqual(st.mem_used_mib, 130896)

    def test_only_gpu_jobs(self):
        for st in self.stats.values():
            self.assertTrue(0 <= st.util <= 100)
            self.assertTrue(0 <= st.mem_eff <= 100)
        cpu_job = "778390"  # cpu-btang1 on common
        self.assertNotIn(cpu_job, self.stats)


class TestSlurmGpu(unittest.TestCase):
    def test_parse_table(self):
        stats = parse_slurm_gpu(SLURM_GPU_SAMPLE)
        self.assertEqual(set(stats), {"774246", "782942"})  # header, ---, dtype and … rows skipped
        self.assertEqual((stats["774246"].util, stats["774246"].mem_eff), (87.5, 61.2))
        self.assertEqual((stats["782942"].util, stats["782942"].mem_eff), (42.0, 12.3))
        self.assertEqual(stats["774246"].source, "slurm-gpu")

    def test_missing_columns(self):
        with self.assertRaises(ValueError):
            parse_slurm_gpu("│ JobID ┆ Other │\n│ 1 ┆ 2 │")
        with self.assertRaises(ValueError):
            parse_slurm_gpu("no table here")


class TestSource(unittest.TestCase):
    def runner(self, slurm_gpu_output=SLURM_GPU_SAMPLE, fail=False):
        calls = []

        def run(args):
            calls.append(args)
            if args[0] == "slurm-gpu":
                if fail:
                    raise RuntimeError("slurm-gpu exploded")
                return slurm_gpu_output
            return (FIX / "sacct_gpu.txt").read_text()
        return run, calls

    def test_auto_detects(self):
        run, _ = self.runner()
        src = GpuStatsSource("btang1", 30, run, "auto", which=lambda name: "/usr/bin/slurm-gpu")
        self.assertEqual(src.resolved_mode, "slurm-gpu")
        src = GpuStatsSource("btang1", 30, run, "auto", which=lambda name: None)
        self.assertEqual(src.resolved_mode, "sacct")

    def test_slurm_gpu_overrides_sacct_and_keeps_memory(self):
        run, calls = self.runner()
        stats, warnings = GpuStatsSource("btang1", 30, run, "slurm-gpu").fetch()
        self.assertEqual(warnings, [])
        self.assertEqual(stats["774246"].source, "slurm-gpu")
        self.assertEqual(stats["774246"].util, 87.5)
        self.assertAlmostEqual(stats["774246"].mem_used_mib, 343720)  # from sacct
        self.assertEqual(stats["773765"].source, "sacct")  # not listed by slurm-gpu: fallback
        partitions = [a[a.index("-r") + 1] for a in calls if a[0] == "slurm-gpu"]
        self.assertEqual(partitions, ["gpu", "gpu-hp"])
        first = next(a for a in calls if a[0] == "slurm-gpu")
        self.assertEqual(first[first.index("-u") + 1], "btang1")

    def test_slurm_gpu_failure_falls_back(self):
        run, _ = self.runner(fail=True)
        stats, warnings = GpuStatsSource("btang1", 30, run, "slurm-gpu").fetch()
        self.assertEqual(len(warnings), 2)
        self.assertEqual(stats["774246"].source, "sacct")

    def test_off(self):
        run, calls = self.runner()
        self.assertEqual(GpuStatsSource("btang1", 30, run, "off").fetch(), ({}, []))
        self.assertEqual(calls, [])

    def test_dates(self):
        from datetime import date
        src = GpuStatsSource("me", 30, None, "slurm-gpu")
        args = src.slurm_gpu_args("gpu-hp", today=date(2026, 11, 3))
        self.assertEqual(args, ["slurm-gpu", "report", "-r", "gpu-hp", "-u", "me",
                                "-S", "2026-10-04", "-E", "2026-11-04"])


class TestAttach(unittest.TestCase):
    def test_finished_gpu_jobs_get_stats(self):
        def run(args):
            if args[0] == "squeue":
                return (FIX / "squeue.txt").read_text()
            if any(a.startswith("--format=JobID,AllocTRES") for a in args):
                return (FIX / "sacct_gpu.txt").read_text()
            return (FIX / "sacct_history.txt").read_text()

        client = SlurmClient(user="btang1", runner=run, gpu_mode="sacct")
        jobs = {j.job_id: j for j in client.all_jobs().value}
        self.assertAlmostEqual(jobs["777824"].gpu_util, 100.0)
        self.assertEqual(jobs["777824"].gpu_stats_source, "sacct")
        for j in jobs.values():
            if j.category != "finished" or not j.gpus:
                self.assertIsNone(j.gpu_util, j.job_id)

    def test_gpu_failure_does_not_break_pages(self):
        def run(args):
            if args[0] == "squeue":
                return (FIX / "squeue.txt").read_text()
            if any(a.startswith("--format=JobID,AllocTRES") for a in args):
                raise RuntimeError("sacct GPU query failed")
            return (FIX / "sacct_history.txt").read_text()

        client = SlurmClient(user="btang1", runner=run, gpu_mode="sacct")
        self.assertTrue(client.all_jobs().value)
        self.assertIn("sacct GPU query failed", client.gpu_warnings[0])


if __name__ == "__main__":
    unittest.main()

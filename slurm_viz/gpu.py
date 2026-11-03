"""GPU utilization and GPU memory efficiency for finished jobs.

Two sources:
- "sacct": always available. Slurm's NVML plugin stores gres/gpuutil and
  gres/gpumem per job step. These are one sample (Ave == Max), summed over the
  job's GPUs, so they are a rough guide, not a time-weighted average.
- "slurm-gpu": NCShare's `slurm-gpu report` (installed on the login nodes),
  which gives GPUUtil and GPUMemEff per job. Used for the partitions it covers;
  jobs it does not list fall back to sacct.
"""

import re
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from .slurm import SEP, parse_gpus

# Total memory per GPU in MiB, as nvidia-smi reports it.
GPU_MEM_MIB = {"h200": 143771, "h100": 81559, "a100": 81920, "a6000": 49140, "l40s": 46068}
DEFAULT_GPU = "h200"  # NCShare's GPU nodes are all H200

SLURM_GPU_PARTITIONS = ("gpu", "gpu-hp")
GPU_SACCT_FIELDS = ["JobID", "AllocTRES", "TRESUsageInMax"]

MODES = ("auto", "slurm-gpu", "sacct", "off")


@dataclass
class GpuStats:
    util: float | None  # percent per GPU, 0-100
    mem_eff: float | None  # percent of allocated GPU memory used, 0-100
    mem_used_mib: float | None  # summed over the job's GPUs
    mem_total_mib: float | None
    source: str  # "sacct" or "slurm-gpu"


def tres_value(tres: str, key: str) -> str:
    m = re.search(rf"(?:^|,){re.escape(key)}=([^,]+)", tres)
    return m.group(1) if m else ""


def parse_mem_mib(value: str) -> float | None:
    """'366786M' -> 366786.0, '12G' -> 12288.0, '0' -> 0.0."""
    m = re.fullmatch(r"([\d.]+)([KMGTP]?)", value.strip())
    if not m:
        return None
    scale = {"": 1 / 2**20, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024**2, "P": 1024**3}[m.group(2)]
    return float(m.group(1)) * scale


def parse_sacct_gpu(text: str) -> dict[str, GpuStats]:
    """Aggregate per-step gres/gpuutil and gres/gpumem into one GpuStats per job.

    Steps are separate srun calls, so take the busiest step (max), not the sum.
    """
    alloc: dict[str, tuple[int, str]] = {}
    util: dict[str, float] = {}
    mem: dict[str, float] = {}
    for line in text.splitlines():
        values = line.split(SEP)
        if len(values) != len(GPU_SACCT_FIELDS):
            continue
        job_step, alloc_tres, usage = values
        job_id, _, step = job_step.partition(".")
        if not step:
            alloc[job_id] = parse_gpus(alloc_tres)
            continue
        if step == "extern":
            continue
        u, m = tres_value(usage, "gres/gpuutil"), parse_mem_mib(tres_value(usage, "gres/gpumem"))
        if u:
            util[job_id] = max(util.get(job_id, 0.0), float(u))
        if m is not None:
            mem[job_id] = max(mem.get(job_id, 0.0), m)

    out = {}
    for job_id, (n_gpus, gtype) in alloc.items():
        if not n_gpus or (job_id not in util and job_id not in mem):
            continue
        per_gpu = GPU_MEM_MIB.get((gtype or DEFAULT_GPU).lower())
        total = per_gpu * n_gpus if per_gpu else None
        used = mem.get(job_id)
        out[job_id] = GpuStats(
            util=min(util[job_id] / n_gpus, 100.0) if job_id in util else None,
            mem_eff=min(used / total * 100, 100.0) if used is not None and total else None,
            mem_used_mib=used, mem_total_mib=total, source="sacct",
        )
    return out


def parse_percent(value: str) -> float | None:
    value = value.strip().rstrip("%").strip()
    try:
        return float(value)
    except ValueError:
        return None


def parse_slurm_gpu(text: str) -> dict[str, GpuStats]:
    """Parse `slurm-gpu report` table output (box-drawing table with ┆/│ separators).

    Looks for a job id column and the GPUUtil / GPUMemEff columns by name, so
    extra or reordered columns are fine. Raises ValueError if they are missing.
    """
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] not in "│┃|":
            continue
        cells = [c.strip() for c in re.split(r"[┆│┃|]", s.strip("│┃|"))]
        rows.append(cells)
    if not rows:
        raise ValueError("no table found in slurm-gpu output")

    def norm(h: str) -> str:
        return re.sub(r"[\s_\-]", "", h).lower()

    header = [norm(h) for h in rows[0]]
    try:
        i_job = next(i for i, h in enumerate(header) if h in ("jobid", "jobidraw", "job"))
        i_util = header.index("gpuutil")
        i_mem = header.index("gpumemeff")
    except (StopIteration, ValueError):
        raise ValueError(f"slurm-gpu output has no JobID/GPUUtil/GPUMemEff columns: {rows[0]}")

    out = {}
    for cells in rows[1:]:
        if len(cells) <= max(i_job, i_util, i_mem):
            continue
        job_id = cells[i_job].strip('"')
        if not re.match(r"^\d", job_id):  # '---', dtype rows like 'str', '…'
            continue
        out[job_id.split(".")[0]] = GpuStats(
            util=parse_percent(cells[i_util]), mem_eff=parse_percent(cells[i_mem]),
            mem_used_mib=None, mem_total_mib=None, source="slurm-gpu",
        )
    return out


@dataclass
class GpuStatsSource:
    """Fetches GpuStats for the user's jobs in the history window using the chosen mode."""
    user: str
    days: int
    runner: Callable[[list[str]], str]
    mode: str = "auto"
    which: Callable[[str], str | None] = shutil.which

    @property
    def resolved_mode(self) -> str:
        if self.mode == "auto":
            return "slurm-gpu" if self.which("slurm-gpu") else "sacct"
        return self.mode

    def sacct_args(self) -> list[str]:
        return ["sacct", "-u", self.user, "-S", f"now-{self.days}days", "-E", "now",
                "--parsable2", "--noheader", f"--delimiter={SEP}",
                "--format=" + ",".join(GPU_SACCT_FIELDS)]

    def slurm_gpu_args(self, partition: str, today: date | None = None) -> list[str]:
        today = today or date.today()
        start = today - timedelta(days=self.days)
        end = today + timedelta(days=1)
        return ["slurm-gpu", "report", "-r", partition, "-u", self.user,
                "-S", start.isoformat(), "-E", end.isoformat()]

    def fetch(self) -> tuple[dict[str, GpuStats], list[str]]:
        """Return (stats by job id, warnings). sacct is the base; slurm-gpu overrides it."""
        mode = self.resolved_mode
        if mode == "off":
            return {}, []
        stats = parse_sacct_gpu(self.runner(self.sacct_args()))
        warnings = []
        if mode == "slurm-gpu":
            for partition in SLURM_GPU_PARTITIONS:
                try:
                    for job_id, st in parse_slurm_gpu(self.runner(self.slurm_gpu_args(partition))).items():
                        old = stats.get(job_id)
                        if old:  # slurm-gpu has no absolute memory numbers; keep sacct's
                            st.mem_used_mib, st.mem_total_mib = old.mem_used_mib, old.mem_total_mib
                        stats[job_id] = st
                except Exception as exc:  # keep the sacct numbers for this partition
                    warnings.append(f"slurm-gpu ({partition}): {exc}")
        return stats, warnings

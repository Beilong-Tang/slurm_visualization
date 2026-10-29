"""Read job data from Slurm (squeue, sacct, scontrol) and turn it into Job objects."""

import getpass
import os
import re
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Callable

from .cache import CacheResult, TTLCache

SEP = "\x1f"  # ASCII unit separator: never appears in job names or paths
COMMAND_TIMEOUT = 10

SQUEUE_FIELDS = [
    ("job_id", "%i"), ("name", "%j"), ("partition", "%P"), ("qos", "%q"),
    ("account", "%a"), ("state", "%T"), ("elapsed", "%M"), ("time_limit", "%l"),
    ("time_left", "%L"), ("nodes", "%D"), ("cpus", "%C"), ("mem", "%m"),
    ("gres", "%b"), ("reason", "%r"), ("nodelist", "%N"), ("submit", "%V"),
    ("start", "%S"), ("priority", "%Q"), ("workdir", "%Z"),
]

SACCT_FIELDS = [
    "JobID", "JobIDRaw", "JobName", "Partition", "QOS", "Account", "State",
    "ExitCode", "Submit", "Start", "End", "Elapsed", "Timelimit", "NNodes",
    "NCPUS", "ReqMem", "AllocTRES", "ReqTRES", "NodeList", "Reason", "WorkDir",
    "StdOut", "StdErr",
]

STEP_FIELDS = [
    "JobID", "JobName", "State", "ExitCode", "Start", "End", "Elapsed",
    "NCPUS", "MaxRSS", "AllocTRES",
]

RUNNING_STATES = {"RUNNING", "COMPLETING", "CONFIGURING", "SUSPENDED", "STOPPED", "SIGNALING", "STAGE_OUT"}
PENDING_STATES = {"PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESIZING", "RESV_DEL_HOLD", "SPECIAL_EXIT"}
FAILED_STATES = {"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "BOOT_FAIL", "DEADLINE"}
CANCELLED_STATES = {"CANCELLED", "PREEMPTED", "REVOKED"}

JOB_ID_RE = re.compile(r"^\d+(_\d+|_\[[\d,\-%]+\])?(\+\d+)?$")
ANSI_RE = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[@-Z\\-_])")
EMPTY = {"", "Unknown", "None", "N/A", "(null)", "None assigned", "INVALID", "NOT_SET"}


class SlurmError(RuntimeError):
    pass


# ---------------------------------------------------------------- parsing helpers

def parse_time(value: str) -> datetime | None:
    if value in EMPTY:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def parse_duration(value: str) -> timedelta | None:
    """Parse Slurm durations: '5:34', '1:11:05', '1-22:22:50', '2-00:00:00', '00:00:02.123'."""
    if value in EMPTY or value in ("UNLIMITED", "Partition_Limit"):
        return None
    days = 0
    if "-" in value:
        d, value = value.split("-", 1)
        days = int(d)
    parts = value.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.insert(0, 0.0)
    h, m, s = nums[-3:]
    return timedelta(days=days, hours=h, minutes=m, seconds=int(s))


def parse_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


def parse_gpus(tres: str) -> tuple[int, str]:
    """Return (gpu_count, gpu_type) from a TRES or GRES string.

    Handles sacct 'gres/gpu:h200=1,gres/gpu=1' and squeue 'gres/gpu:h200:1' / 'gpu:2'.
    """
    if not tres or tres in EMPTY:
        return 0, ""
    count, gtype = 0, ""
    for item in tres.split(","):
        item = item.strip().removeprefix("gres/")
        if not item.startswith("gpu"):
            continue
        if "=" in item:  # TRES form: gpu=1 or gpu:h200=1
            key, num = item.split("=", 1)
            n = parse_int(num)
            if ":" in key:
                gtype = key.split(":", 1)[1]
                count = count or n
            else:
                count = n  # the untyped total wins
        else:  # GRES form: gpu, gpu:2, gpu:h200:1
            parts = item.split(":")
            n = parse_int(parts[-1]) if len(parts) > 1 and parts[-1].isdigit() else 1
            if len(parts) == 3 or (len(parts) == 2 and not parts[1].isdigit()):
                gtype = parts[1]
            count += n
    return count, gtype


def normalize_state(raw: str) -> str:
    """'CANCELLED by 3000426' -> 'CANCELLED', 'CANCELLED+' -> 'CANCELLED'."""
    return raw.split()[0].rstrip("+") if raw.strip() else "UNKNOWN"


def clean(value: str) -> str:
    return "" if value in EMPTY else value


# ---------------------------------------------------------------- Job model

@dataclass
class Job:
    job_id: str
    name: str = ""
    partition: str = ""
    qos: str = ""
    account: str = ""
    state: str = "UNKNOWN"
    state_raw: str = ""
    exit_code: str = ""
    submit: datetime | None = None
    start: datetime | None = None
    end: datetime | None = None
    elapsed: timedelta | None = None
    time_limit: timedelta | None = None
    time_left: timedelta | None = None
    nodes: int = 0
    cpus: int = 0
    mem: str = ""
    gpus: int = 0
    gpu_type: str = ""
    nodelist: str = ""
    reason: str = ""
    priority: int = 0
    workdir: str = ""
    stdout: str = ""  # may still contain %j-style patterns (from sacct)
    stderr: str = ""
    job_id_raw: str = ""
    source: str = ""  # "squeue" or "sacct"

    @property
    def category(self) -> str:
        """One of 'running', 'pending', 'finished'."""
        if self.state in RUNNING_STATES:
            return "running"
        if self.state in PENDING_STATES:
            return "pending"
        return "finished"

    @property
    def is_active(self) -> bool:
        return self.category != "finished"

    @property
    def state_group(self) -> str:
        """Color group for the UI: running, pending, completed, failed, cancelled."""
        if self.category in ("running", "pending"):
            return self.category
        if self.state == "COMPLETED":
            return "completed"
        if self.state in CANCELLED_STATES:
            return "cancelled"
        return "failed"

    @property
    def is_array(self) -> bool:
        return "_" in self.job_id

    @property
    def sort_key_id(self) -> tuple:
        base, _, rest = self.job_id.partition("_")
        return (parse_int(base), rest)


def parse_squeue(text: str) -> list[Job]:
    jobs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        values = line.split(SEP)
        if len(values) != len(SQUEUE_FIELDS):
            raise SlurmError(f"squeue: expected {len(SQUEUE_FIELDS)} fields, got {len(values)}: {line!r}")
        r = dict(zip((k for k, _ in SQUEUE_FIELDS), values))
        gpus, gtype = parse_gpus(r["gres"])
        state = normalize_state(r["state"])
        jobs.append(Job(
            job_id=r["job_id"], name=r["name"], partition=r["partition"], qos=r["qos"],
            account=r["account"], state=state, state_raw=r["state"],
            submit=parse_time(r["submit"]), start=parse_time(r["start"]),
            elapsed=parse_duration(r["elapsed"]), time_limit=parse_duration(r["time_limit"]),
            time_left=parse_duration(r["time_left"]) if state in RUNNING_STATES else None,
            nodes=parse_int(r["nodes"]), cpus=parse_int(r["cpus"]), mem=clean(r["mem"]),
            gpus=gpus, gpu_type=gtype, nodelist=clean(r["nodelist"]),
            reason=clean(r["reason"]), priority=parse_int(r["priority"]),
            workdir=clean(r["workdir"]), source="squeue",
        ))
    return jobs


def parse_sacct(text: str) -> list[Job]:
    jobs = []
    for line in text.splitlines():
        if not line.strip():
            continue
        values = line.split(SEP)
        if len(values) != len(SACCT_FIELDS):
            raise SlurmError(f"sacct: expected {len(SACCT_FIELDS)} fields, got {len(values)}: {line!r}")
        r = dict(zip(SACCT_FIELDS, values))
        state = normalize_state(r["State"])
        gpus, gtype = parse_gpus(r["AllocTRES"])
        if not gpus:  # pending/never-started jobs have no AllocTRES
            gpus, gtype = parse_gpus(r["ReqTRES"])
        jobs.append(Job(
            job_id=r["JobID"], job_id_raw=r["JobIDRaw"], name=r["JobName"],
            partition=r["Partition"], qos=r["QOS"], account=r["Account"],
            state=state, state_raw=r["State"], exit_code=r["ExitCode"],
            submit=parse_time(r["Submit"]), start=parse_time(r["Start"]), end=parse_time(r["End"]),
            elapsed=parse_duration(r["Elapsed"]), time_limit=parse_duration(r["Timelimit"]),
            nodes=parse_int(r["NNodes"]), cpus=parse_int(r["NCPUS"]), mem=clean(r["ReqMem"]),
            gpus=gpus, gpu_type=gtype, nodelist=clean(r["NodeList"]),
            reason=clean(r["Reason"]), workdir=clean(r["WorkDir"]),
            stdout=clean(r["StdOut"]), stderr=clean(r["StdErr"]), source="sacct",
        ))
    return jobs


@dataclass
class Step:
    step_id: str
    name: str
    state: str
    exit_code: str
    start: datetime | None
    end: datetime | None
    elapsed: timedelta | None
    cpus: int
    max_rss: str
    alloc_tres: str


def parse_steps(text: str) -> list[Step]:
    steps = []
    for line in text.splitlines():
        if not line.strip():
            continue
        values = line.split(SEP)
        if len(values) != len(STEP_FIELDS):
            raise SlurmError(f"sacct steps: expected {len(STEP_FIELDS)} fields, got {len(values)}: {line!r}")
        r = dict(zip(STEP_FIELDS, values))
        steps.append(Step(
            step_id=r["JobID"], name=r["JobName"], state=r["State"], exit_code=r["ExitCode"],
            start=parse_time(r["Start"]), end=parse_time(r["End"]),
            elapsed=parse_duration(r["Elapsed"]), cpus=parse_int(r["NCPUS"]),
            max_rss=r["MaxRSS"], alloc_tres=r["AllocTRES"],
        ))
    return steps


def parse_scontrol(text: str) -> dict[str, str]:
    """Parse one-line `scontrol show job -o` output into a dict.

    Values may contain spaces (e.g. Command=), so split on ' Key=' boundaries.
    """
    text = text.strip()
    out: dict[str, str] = {}
    matches = list(re.finditer(r"(?:^|\s)([A-Za-z][\w/:.\-]*)=", text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1)] = text[m.end():end]
    return out


def expand_log_path(pattern: str, job: Job, user: str) -> str:
    """Fill in Slurm filename patterns (%j, %x, %A, %a, %u, %N, %%) the way sbatch does."""
    if not pattern:
        return ""
    base, _, task = job.job_id.partition("_")
    raw = job.job_id_raw or base
    # first host of a nodelist: "gpu-[01-02],x" -> "gpu-01"
    m = re.match(r"([^\[,]*)(?:\[([^\],\-]+))?", job.nodelist)
    first_node = m.group(1) + (m.group(2) or "")
    values = {"j": raw, "J": raw, "A": base, "a": task, "u": user, "x": job.name,
              "N": first_node, "n": "0", "s": "batch", "t": "0"}

    def sub(m: re.Match) -> str:
        width, key = m.group(1), m.group(2)
        if key == "%":
            return "%"
        val = values.get(key, m.group(0))
        return val.zfill(int(width)) if width and val.isdigit() else val

    path = re.sub(r"%(\d*)([%AaJjNnstux])", sub, pattern)
    if not os.path.isabs(path) and job.workdir:
        path = os.path.join(job.workdir, path)
    return path


def merge_jobs(history: list[Job], active: list[Job]) -> list[Job]:
    """sacct rows ∪ squeue rows by job id; live squeue data wins for active jobs."""
    by_id = {j.job_id: j for j in history}
    for a in active:
        old = by_id.get(a.job_id)
        if old is not None:
            # keep sacct-only fields (exit code, stdout patterns, raw id, end) under the live values
            a = replace(a, exit_code=old.exit_code, stdout=old.stdout, stderr=old.stderr,
                        job_id_raw=old.job_id_raw, end=old.end)
        by_id[a.job_id] = a
    return sorted(by_id.values(), key=lambda j: (j.submit or datetime.min, j.sort_key_id), reverse=True)


# ---------------------------------------------------------------- running commands

def run_command(args: list[str], timeout: float = COMMAND_TIMEOUT) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              errors="replace")
    except FileNotFoundError:
        raise SlurmError(f"{args[0]} not found on PATH")
    except subprocess.TimeoutExpired:
        raise SlurmError(f"{args[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        raise SlurmError(f"{args[0]} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class SlurmClient:
    user: str = field(default_factory=getpass.getuser)
    days: int = 30
    ttl: float = 15.0
    runner: Callable[[list[str]], str] = run_command

    def __post_init__(self):
        self.cache = TTLCache(self.ttl)

    # command lines are methods so --check can print exactly what runs
    def squeue_args(self) -> list[str]:
        fmt = SEP.join(code for _, code in SQUEUE_FIELDS)
        return ["squeue", "-u", self.user, "-h", "-o", fmt]

    def sacct_args(self) -> list[str]:
        return ["sacct", "-u", self.user, "-X", "-S", f"now-{self.days}days", "-E", "now",
                "--parsable2", "--noheader", f"--delimiter={SEP}",
                "--format=" + ",".join(SACCT_FIELDS)]

    def active(self) -> CacheResult:
        return self.cache.get("squeue", lambda: parse_squeue(self.runner(self.squeue_args())))

    def history(self) -> CacheResult:
        return self.cache.get("sacct", lambda: parse_sacct(self.runner(self.sacct_args())))

    def all_jobs(self) -> CacheResult:
        """Merged list plus the oldest fetch time and any error from either source."""
        act, hist = self.active(), self.history()
        jobs = merge_jobs(hist.value, act.value)
        error = "; ".join(e for e in (act.error, hist.error) if e) or None
        return CacheResult(jobs, min(act.fetched_at, hist.fetched_at), error)

    def job(self, job_id: str) -> Job | None:
        check_job_id(job_id)
        for j in self.all_jobs().value:
            if j.job_id == job_id:
                return j
        # older than the history window: ask sacct for just this job
        text = self.runner(["sacct", "-j", job_id, "-X", "--parsable2", "--noheader",
                            f"--delimiter={SEP}", "--format=" + ",".join(SACCT_FIELDS)])
        jobs = parse_sacct(text)
        return jobs[0] if jobs else None

    def steps(self, job_id: str) -> list[Step]:
        check_job_id(job_id)
        text = self.runner(["sacct", "-j", job_id, "--parsable2", "--noheader",
                            f"--delimiter={SEP}", "--format=" + ",".join(STEP_FIELDS)])
        return parse_steps(text)

    def log_paths(self, job: Job) -> tuple[str, str]:
        """Resolved (stdout, stderr) paths. Active jobs ask scontrol, which expands them."""
        if job.is_active:
            try:
                info = parse_scontrol(self.runner(["scontrol", "show", "job", "-o", job.job_id]))
                return info.get("StdOut", ""), info.get("StdErr", "")
            except SlurmError:
                pass
        out = expand_log_path(job.stdout, job, self.user)
        err = expand_log_path(job.stderr, job, self.user)
        return out, err


def check_job_id(job_id: str) -> None:
    if not JOB_ID_RE.match(job_id):
        raise ValueError(f"invalid job id: {job_id!r}")


def read_log_tail(path: str, max_bytes: int = 64 * 1024) -> tuple[str | None, str]:
    """Return (text, message). Only reads regular files owned by the current user."""
    if not path or path == "/dev/null":
        return None, "no log file"
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, "file not found"
    except OSError as exc:
        return None, f"cannot open: {exc.strerror}"
    if not os.path.isfile(path):
        return None, "not a regular file"
    if st.st_uid != os.getuid():
        return None, "not owned by you"
    with open(path, "rb") as f:
        if st.st_size > max_bytes:
            f.seek(st.st_size - max_bytes)
        data = f.read(max_bytes)
    text = data.decode("utf-8", errors="replace")
    if st.st_size > max_bytes:
        text = text.split("\n", 1)[-1]  # drop the partial first line
    text = ANSI_RE.sub("", text)  # terminal colors from loggers like rich/loguru
    # progress bars write '\r'; keep only the last redraw of each line
    text = "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))
    return text, f"{st.st_size:,} bytes"

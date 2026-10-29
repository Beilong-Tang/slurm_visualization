"""Command line entry point: `python3 -m slurm_viz [--check] ...`."""

import argparse
import getpass
import sys
import time
from collections import Counter

from .slurm import SlurmClient, SlurmError, read_log_tail


def check(client: SlurmClient) -> int:
    """Run every Slurm command the app uses and report whether the data looks right."""
    ok = True

    def step(label, fn):
        nonlocal ok
        t0 = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:
            ok = False
            print(f"  FAIL  {label}: {exc}")
            return None
        print(f"  ok    {label} ({(time.perf_counter() - t0) * 1000:.0f} ms)")
        return result

    print(f"Checking Slurm access for user {client.user!r}, history window {client.days} days\n")
    active = step("squeue (running + pending)", lambda: client.active().value)
    history = step(f"sacct  (last {client.days} days)", lambda: client.history().value)
    if active is None or history is None:
        return 1

    jobs = client.all_jobs().value
    print(f"\n  squeue: {len(active)} active jobs   sacct: {len(history)} jobs   merged: {len(jobs)}")
    print("  by category:", dict(Counter(j.category for j in jobs)))
    print("  by state:   ", dict(Counter(j.state for j in jobs).most_common()))
    print("  partitions: ", dict(Counter(j.partition for j in jobs).most_common()))
    gpu_jobs = [j for j in jobs if j.gpus]
    in_use = sum(j.gpus for j in jobs if j.category == "running")
    print(f"  GPU jobs:    {len(gpu_jobs)}   GPUs in use now: {in_use}")

    print("\n  newest 5 jobs:")
    for j in jobs[:5]:
        extra = j.reason if j.category == "pending" else j.nodelist
        print(f"    {j.job_id:>10}  {j.state:<11} {j.partition:<12} gpus={j.gpus:<2} {j.name[:30]:<30} {extra}")

    sample = next((j for j in jobs if j.category == "finished" and j.stdout not in ("", "/dev/null")), None) \
        or (jobs[0] if jobs else None)
    if sample:
        print(f"\n  detail check on job {sample.job_id} ({sample.state}):")
        steps = step("sacct -j (job steps)", lambda: client.steps(sample.job_id))
        if steps is not None:
            print("        steps:", ", ".join(s.step_id for s in steps))
        paths = step("log paths", lambda: client.log_paths(sample))
        if paths:
            for kind, path in zip(("stdout", "stderr"), paths):
                text, msg = read_log_tail(path)
                print(f"        {kind}: {path or '(none)'} -> {msg}")
    running = next((j for j in jobs if j.category in ("running", "pending")), None)
    if running:
        paths = step(f"scontrol show job {running.job_id} (active job log paths)",
                     lambda: client.log_paths(running))
        if paths:
            print(f"        stdout: {paths[0] or '(none)'}")

    print("\nAll checks passed." if ok else "\nSome checks FAILED.")
    return 0 if ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="slurm_viz", description="Web dashboard for your Slurm jobs")
    p.add_argument("--check", action="store_true", help="test Slurm access and exit")
    p.add_argument("--user", default=getpass.getuser())
    p.add_argument("--days", type=int, default=30, help="history window for sacct (default 30)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--refresh", type=int, default=30, help="auto-refresh seconds (0 = off)")
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--ttl", type=float, default=15.0, help="seconds to cache Slurm output")
    args = p.parse_args(argv)

    client = SlurmClient(user=args.user, days=args.days, ttl=args.ttl)
    if args.check:
        return check(client)
    try:
        from .server import serve
    except ImportError:
        print("The web server is not built yet; run with --check.", file=sys.stderr)
        return 1
    return serve(client, args)


if __name__ == "__main__":
    sys.exit(main())

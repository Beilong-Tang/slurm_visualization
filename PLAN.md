# Slurm Job Visualizer — Plan

A small web dashboard that runs on the NCShare login node, shows your Slurm jobs
(running, pending, finished), and is reached from your laptop with `ssh -L`.

## 1. Goals

| Page | What it shows |
|---|---|
| **Overview** (`/`) | Summary cards (running / pending / completed / failed counts, GPUs in use), then **all** jobs in one table (active and finished), 20 per page |
| **Running** (`/running`) | Running jobs: elapsed vs. time limit, time left, node, GPUs |
| **Queue** (`/pending`) | Pending jobs: pending reason (`Priority`, `Resources`, `QOSMaxGRESPerUser`, …), submit time, how long it has waited, requested GPUs |
| **Completed** (`/completed`) | Finished jobs (COMPLETED, FAILED, CANCELLED, TIMEOUT, OUT_OF_MEMORY, PREEMPTED, NODE_FAIL), 20 per page |
| **Job detail** (`/job/<id>`) | All fields for one job, its job steps, and the last ~100 lines of its stdout/stderr files when they can be read |

Every list page:
- 20 jobs per page, with First / Prev / page numbers / Next / Last links (`?page=N`).
- Filters in the query string: state, partition, name search, date range (`?state=FAILED&partition=gpu-hp&q=train&page=2`).
- Sortable columns (`?sort=submit&order=desc`); newest first by default.
- Colored state badges (green running, yellow pending, grey completed, red failed or timed out, purple cancelled or preempted).
- Auto-refresh every 30 s on the overview, running and queue pages (with a toggle).

## 2. Cluster facts this is built on

Checked on this login node:
- Slurm **24.11.5**; `squeue`, `sacct` and `sinfo` are in `/usr/bin`.
- Partitions: `common`, `common-lm`, `interactive`, `gpu`, `interactive-gpu`, `gpu-hp`, `osg`, `osg-gpu`.
- GPUs show up in TRES as `gres/gpu:h200=N` (AllocTRES in sacct) and `gres/gpu:h200:N` (TRES_PER_NODE in squeue).
- From the [NCShare GPU guide](https://userguide.ncshare.org/guides/gpu/):
  - `gpu` jobs can be **preempted** (cancelled and requeued), so PREEMPTED and requeued jobs get their own badge.
  - `gpu-hp` needs an institution QoS (`ncsu_h200_hp`), so QoS is shown as a column.
  - The per-user limit is 8 GPUs, so the overview shows "GPUs in use: X / 8".
- System Python is **3.12 with no `pip`, `ensurepip`, conda or Django installed**. That decides the tech stack below.

## 3. Tech stack: Python standard library only

Since nothing can be `pip install`ed without first setting up an environment, the
app uses only the standard library:

| Need | Tool |
|---|---|
| HTTP server | `http.server.ThreadingHTTPServer` |
| Routing | A small dict of path regex → handler function |
| HTML | Python functions that build HTML strings, with every value passed through `html.escape` |
| Slurm data | `subprocess.run(["squeue", ...])` and `subprocess.run(["sacct", ...])` with arguments as lists (no shell) |
| Styling | One hand-written CSS file (light and dark mode); no CDN, so it works with no internet on the node |
| JS | A few lines of vanilla JS for auto-refresh and sorting links |

So `python3 -m slurm_viz` works on any login node with nothing to install.
Django would be heavy for 5 read-only pages and needs a virtualenv. If you want
it later, the parsing layer (`slurm.py`) is separate and can be reused from
Flask or Django unchanged.

## 4. Getting the data

### Active jobs: `squeue`
```
squeue -u $USER -h -o "<fields joined by \x1f>"
```
Fields: `%i` JobID, `%j` name, `%P` partition, `%q` QoS, `%T` state, `%M` elapsed,
`%l` time limit, `%L` time left, `%D` nodes, `%C` CPUs, `%m` memory, `%b` GRES,
`%R` reason/nodelist, `%V` submit time, `%S` (expected) start time, `%Q` priority, `%Z` workdir.

Fields are separated by the ASCII unit separator (`\x1f`) instead of `|`, so job
names that contain `|` or spaces don't break parsing.

### History: `sacct`
```
sacct -u $USER -X -S <now - N days> --parsable2 --noheader --delimiter=$'\x1f' \
  --format=JobID,JobName,Partition,QOS,Account,State,ExitCode,Submit,Start,End,
           Elapsed,Timelimit,NNodes,NCPUS,ReqMem,AllocTRES,ReqTRES,NodeList,
           Reason,WorkDir,StdOut,StdErr
```
- `-X` keeps one row per job allocation. The detail page runs it again without `-X` to list the steps (`.batch`, `.extern`, `.0`, …).
- The history window defaults to **30 days** (`--days` flag). Last week you had 54 jobs, so 30 days is a few hundred rows, which takes well under a second.
- `State` comes back as `CANCELLED by 3000426`, so only the first word is used for the badge and the full text goes in a tooltip.

### Merging
- Overview = sacct rows ∪ squeue rows, deduplicated by JobID. For active jobs the squeue values win (live reason, time left, priority).
- Array jobs: `123_4` is shown as-is. A pending range such as `123_[5-10]` is one row with an "array" tag.
- GPU count is parsed from `gres/gpu[:type]=N` in AllocTRES, or from ReqTRES/`%b` for pending jobs.

### Caching
- A TTL cache in memory (default 15 s) per command, behind a lock, so many browser
  tabs or quick paging don't hit `slurmctld`/`slurmdbd` repeatedly.
- Each `subprocess` call has a timeout (10 s). If Slurm is slow or down, the page shows the last good data with a "stale since HH:MM:SS" banner instead of an error page.

### Log tail (detail page)
- The stdout/stderr paths come from sacct `StdOut`/`StdErr` (or `scontrol show job` for active jobs), with `%j`/`%x`/`%u`/`%A`/`%a` filled in.
- Only files owned by the user running the app are read, only the last 64 KB, and they are shown HTML-escaped.

## 5. Project layout

```
slurm_visualization/
├── PLAN.md
├── README.md               # quick start and ssh -L instructions
├── run.sh                  # starts the app; prints the URL and token
├── slurm_viz/
│   ├── __init__.py
│   ├── __main__.py         # CLI: --port --host --days --user --refresh --page-size
│   ├── slurm.py            # squeue/sacct calls, parsing → Job dataclass, merging
│   ├── cache.py            # TTL cache
│   ├── server.py           # HTTP handler, routing, token check
│   ├── views.py            # page handlers: filter → sort → paginate → render
│   ├── render.py           # HTML helpers: layout, table, badges, pagination
│   └── static/
│       ├── style.css
│       └── app.js
└── tests/
    ├── fixtures/           # saved squeue/sacct output from this cluster
    ├── test_parse.py       # parsing, state normalization, GPU extraction, durations
    └── test_views.py       # pagination edge cases, filters, HTML escaping
```

`Job` dataclass (shared by every page):
```python
@dataclass
class Job:
    job_id: str; name: str; partition: str; qos: str; account: str
    state: str            # normalized: RUNNING, PENDING, COMPLETED, FAILED, ...
    state_raw: str        # e.g. "CANCELLED by 3000426"
    exit_code: str
    submit: datetime | None; start: datetime | None; end: datetime | None
    elapsed: timedelta | None; time_limit: timedelta | None; time_left: timedelta | None
    nodes: int; cpus: int; mem: str; gpus: int; gpu_type: str
    nodelist: str; reason: str; workdir: str; stdout: str; stderr: str
```

Pagination is a plain function: `paginate(items, page, size=20) -> (slice, page, n_pages)`, which clamps out-of-range page numbers.

## 6. Access and security (shared login node)

The login node is shared, and **any user on it can connect to `127.0.0.1:<port>`**,
so binding to localhost is not enough to keep your job list private.

1. Bind to `127.0.0.1` only (never `0.0.0.0`).
2. Generate a random token at startup, like Jupyter does, and print the URL with it:
   `http://localhost:8765/?token=abc123…`. The first visit sets an HttpOnly cookie, and requests without a valid token or cookie get 403.
3. The app only runs `squeue`/`sacct` for `$USER` (the `--user` flag is just for convenience; Slurm's own permissions still apply).
4. Read-only: no cancel or submit buttons in v1.

Usage:
```bash
# on the login node (inside tmux/screen so it survives logout)
./run.sh --port 8765
#  -> Open: http://localhost:8765/?token=XXXX

# on your laptop
ssh -N -L 8765:localhost:8765 btang1@<ncshare-login-host>
# then open the printed URL in a browser
```
If the port is already taken by someone else, `run.sh` tries the next free port and prints which one it used.

## 7. Milestones

1. **Data layer**: `slurm.py` and `cache.py`, plus tests using fixtures saved from real `squeue`/`sacct` output on this cluster.
2. **Server skeleton**: routing, token auth, static files, base layout with nav (Overview · Running · Queue · Completed).
3. **List pages**: overview cards and the all-jobs table, running, queue, completed; 20 per page with pagination, filters and sorting.
4. **Job detail**: fields, steps, log tail.
5. **Polish**: auto-refresh, stale-data banner, dark mode, relative times ("3h ago"), progress bar for elapsed vs. limit.
6. **Docs**: README with quick start, flags, ssh -L instructions, tmux tip.

## 8. Possible extras (after v1)

- Charts: jobs per day by final state, GPU-hours per partition (inline SVG, still no dependencies).
- Cluster panel from `sinfo`: idle/alloc GPUs per node on `gpu`/`gpu-hp`.
- `gpu-hp` quota from `get_gpu_quota_account.sh` / `get_gpu_quota_user.sh` when they are installed.
- GPU efficiency per job (`slurm-report`, when available).
- An optional "Cancel job" button (`scancel`) with a confirmation step.
- CSV export of the filtered table.

## 9. Open questions

- History window: is 30 days by default enough, or do you want longer (e.g. 180 days, with sacct's `-S` loading more as you page)? - Yes, 30 days is enough.
- Only your own jobs, or also all jobs in an account or partition (e.g. a lab-wide view of the `gpu-hp` queue)? - Just my own job

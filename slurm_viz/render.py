"""HTML building blocks. Every dynamic value goes through esc()."""

from datetime import datetime, timedelta
from html import escape
from typing import Callable
from urllib.parse import urlencode

from .slurm import Job

PENDING_REASONS = {
    "Priority": "Other jobs have higher priority",
    "Resources": "Waiting for nodes/GPUs to free up; this job is next",
    "Dependency": "Waiting for a job it depends on to finish",
    "DependencyNeverSatisfied": "The job it depends on failed; this job will never start",
    "BeginTime": "Its --begin time has not been reached",
    "JobHeldUser": "Held by you (scontrol release <id>)",
    "JobHeldAdmin": "Held by an administrator",
    "QOSMaxGRESPerUser": "You already use the max GPUs allowed for this QoS",
    "QOSMaxJobsPerUserLimit": "You already run the max jobs allowed for this QoS",
    "QOSGrpGRESMinutes": "The group's GPU-minute quota is used up",
    "QOSGrpGRES": "The group's GPU limit is reached",
    "AssocMaxJobsLimit": "Your account's job limit is reached",
    "ReqNodeNotAvail": "Requested nodes are down, drained or reserved",
    "PartitionTimeLimit": "Requested time is over the partition limit",
    "Reservation": "Waiting for a reservation",
    "launch failed requeued held": "Launch failed; job requeued and held",
}


def esc(value) -> str:
    return escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------- formatting

def fmt_dt(dt: datetime | None, now: datetime | None = None) -> str:
    """'10-29 14:05' with the full timestamp and a relative time as tooltip."""
    if dt is None:
        return '<span class="muted">–</span>'
    now = now or datetime.now()
    short = dt.strftime("%m-%d %H:%M") if dt.year == now.year else dt.strftime("%Y-%m-%d")
    return f'<time datetime="{dt.isoformat()}" title="{dt:%Y-%m-%d %H:%M:%S} ({esc(fmt_rel(dt, now))})">{short}</time>'


def fmt_rel(dt: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    delta = now - dt
    if delta.total_seconds() < 0:
        return "in " + fmt_td(-delta, parts=1)
    return fmt_td(delta, parts=1) + " ago"


def fmt_td(td: timedelta | None, parts: int = 2) -> str:
    """'1d 22h', '1h 37m', '5m 34s', '12s'."""
    if td is None:
        return "–"
    secs = int(td.total_seconds())
    units = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    out = []
    for name, size in units:
        n, secs = divmod(secs, size)
        if n or out:
            out.append(f"{n}{name}")
    return " ".join(out[:parts]) if out else "0s"


def fmt_mem(value: str) -> str:
    """Slurm memory like '16531800K' -> '15.8G'."""
    units = "KMGTP"
    if not value or value[-1] not in units:
        return value or "–"
    try:
        n = float(value[:-1])
    except ValueError:
        return value
    i = units.index(value[-1])
    while n >= 1024 and i < len(units) - 1:
        n, i = n / 1024, i + 1
    return f"{n:.1f}{units[i]}" if n < 10 else f"{n:.0f}{units[i]}"


def query_url(path: str, params: dict, **changes) -> str:
    merged = {**params, **changes}
    merged = {k: v for k, v in merged.items() if v not in (None, "")}
    return path + ("?" + urlencode(merged) if merged else "")


# ---------------------------------------------------------------- components

def badge(job: Job) -> str:
    title = job.state_raw if job.state_raw != job.state else ""
    return f'<span class="badge s-{job.state_group}" title="{esc(title)}">{esc(job.state)}</span>'


def gpu_cell(job: Job) -> str:
    if not job.gpus:
        return '<span class="muted">–</span>'
    return f'<span class="gpu" title="{esc(job.gpu_type)}">{job.gpus}× {esc(job.gpu_type or "GPU")}</span>'


def reason_cell(job: Job) -> str:
    if not job.reason:
        return '<span class="muted">–</span>'
    hint = PENDING_REASONS.get(job.reason, "")
    return f'<span class="reason" title="{esc(hint)}">{esc(job.reason)}</span>'


def progress(job: Job) -> str:
    """Elapsed vs. time limit bar for running jobs."""
    if not job.elapsed:
        return '<span class="muted">–</span>'
    if not job.time_limit:
        return esc(fmt_td(job.elapsed))
    frac = min(job.elapsed / job.time_limit, 1.0)
    level = "hi" if frac > 0.9 else "mid" if frac > 0.7 else ""
    return (f'<div class="progress {level}" title="{frac:.0%} of time limit">'
            f'<div style="width:{frac * 100:.1f}%"></div></div>'
            f'<small>{esc(fmt_td(job.elapsed))} / {esc(fmt_td(job.time_limit))}</small>')


def job_link(job: Job) -> str:
    arr = ' <span class="tag">array</span>' if job.is_array else ""
    return f'<a class="mono" href="/job/{esc(job.job_id)}">{esc(job.job_id)}</a>{arr}'


Column = tuple[str, str, Callable[[Job], str]]  # (sort key or "", label, cell renderer)


def table(columns: list[Column], jobs: list[Job], path: str, params: dict) -> str:
    if not jobs:
        return '<p class="empty">No jobs match.</p>'
    sort, order = params.get("sort", ""), params.get("order", "desc")
    head = []
    for key, label, _ in columns:
        if not key:
            head.append(f"<th>{esc(label)}</th>")
            continue
        new_order = "asc" if key == sort and order == "desc" else "desc"
        arrow = (" ▼" if order == "desc" else " ▲") if key == sort else ""
        url = query_url(path, params, sort=key, order=new_order, page=None)
        head.append(f'<th><a href="{esc(url)}">{esc(label)}{arrow}</a></th>')
    rows = []
    for job in jobs:
        cells = "".join(f"<td>{fn(job)}</td>" for _, _, fn in columns)
        rows.append(f'<tr class="row-{job.state_group}">{cells}</tr>')
    return (f'<div class="table-wrap"><table><thead><tr>{"".join(head)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def pagination(path: str, params: dict, page: int, n_pages: int, total: int, page_size: int) -> str:
    first = (page - 1) * page_size + 1 if total else 0
    last = min(page * page_size, total)
    info = f'<span class="muted">{first}–{last} of {total}</span>'
    if n_pages <= 1:
        return f'<nav class="pager">{info}</nav>'

    def link(p: int, label: str, disabled: bool = False, current: bool = False) -> str:
        if current:
            return f'<span class="current">{label}</span>'
        if disabled:
            return f'<span class="disabled">{label}</span>'
        return f'<a href="{esc(query_url(path, params, page=p))}">{label}</a>'

    # 1 … 4 5 [6] 7 8 … 20
    shown = sorted({1, n_pages, *range(max(1, page - 2), min(n_pages, page + 2) + 1)})
    nums, prev = [], 0
    for p in shown:
        if p - prev > 1:
            nums.append('<span class="gap">…</span>')
        nums.append(link(p, str(p), current=p == page))
        prev = p
    return (f'<nav class="pager">{link(1, "« First", page == 1)}{link(page - 1, "‹ Prev", page == 1)}'
            f'{"".join(nums)}{link(page + 1, "Next ›", page == n_pages)}'
            f'{link(n_pages, "Last »", page == n_pages)}{info}</nav>')


def filter_form(path: str, params: dict, states: list[str], partitions: list[str]) -> str:
    def options(values, selected, blank):
        opts = [f'<option value="">{esc(blank)}</option>']
        for v in values:
            sel = " selected" if v == selected else ""
            opts.append(f'<option value="{esc(v)}"{sel}>{esc(v)}</option>')
        return "".join(opts)

    keep = "".join(f'<input type="hidden" name="{esc(k)}" value="{esc(params[k])}">'
                   for k in ("sort", "order") if params.get(k))
    state_sel = (f'<select name="state" aria-label="State">{options(states, params.get("state", ""), "All states")}</select>'
                 if states else "")
    # app.js applies changes immediately; the button is only for browsers without JS
    return f'''<form class="filters" id="filters" method="get" action="{esc(path)}">
  <input type="search" name="q" value="{esc(params.get("q", ""))}" placeholder="Search name or job ID" aria-label="Search">
  {state_sel}
  <select name="partition" aria-label="Partition">{options(partitions, params.get("partition", ""), "All partitions")}</select>
  <label>Submitted from <input type="date" name="from" value="{esc(params.get("from", ""))}"></label>
  <label>to <input type="date" name="to" value="{esc(params.get("to", ""))}"></label>
  {keep}
  <button type="submit" class="nojs">Filter</button>
  <a class="reset" href="{esc(path)}">Reset</a>
</form>'''


NAV = [("/", "Overview", "all"), ("/running", "Running", "running"),
       ("/pending", "Queue", "pending"), ("/completed", "Completed", "finished")]


def layout(title: str, active: str, body: str, counts: dict, user: str,
           fetched_at: float | None = None, error: str | None = None, refresh: int = 0) -> str:
    nav = []
    for href, label, key in NAV:
        cls = ' class="active"' if href == active else ""
        n = counts.get(key)
        count = f' <span class="count">{n}</span>' if n is not None else ""
        nav.append(f'<a href="{href}"{cls}>{esc(label)}{count}</a>')
    banner = '<div id="banner" data-swap>'
    if error:
        when = datetime.fromtimestamp(fetched_at).strftime("%H:%M:%S") if fetched_at else "?"
        banner += f'<div class="banner">Slurm did not answer; showing data from {when}. <small>{esc(error)}</small></div>'
    banner += "</div>"
    updated = (f'<span id="updated" data-swap data-ts="{fetched_at:.0f}">updated '
               f'{datetime.fromtimestamp(fetched_at):%H:%M:%S}</span>') if fetched_at else ""
    toggle = (f'<label class="auto"><input type="checkbox" id="autorefresh" data-seconds="{refresh}"> '
              f'auto-refresh {refresh}s</label>') if refresh else ""
    return f'''<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · Slurm Jobs</title>
<link rel="stylesheet" href="/static/style.css">
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<script src="/static/app.js" defer></script>
</head><body>
<div id="loadbar" aria-hidden="true"></div>
<header><div class="brand">Slurm Jobs <span class="muted">· {esc(user)}</span></div>
<nav class="tabs" id="tabs" data-swap>{"".join(nav)}</nav>
<div class="status">{updated}<button type="button" id="refresh-btn" class="js-only"
  title="Fetch the latest data from Slurm now">↻ Refresh</button>{toggle}</div></header>
{banner}
<main>{body}</main>
</body></html>'''


def error_page(status: int, message: str) -> str:
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{status} · Slurm Jobs</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main><h1>{status}</h1><p>{esc(message)}</p><p><a href="/">Back to overview</a></p></main></body></html>'''

"""Page handlers: filter -> sort -> paginate -> render."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from . import render as r
from .render import esc
from .slurm import Job, SlurmClient, SlurmError, read_log_tail

LOG_LINES = 300
LOG_POLL = 5  # seconds between live log updates on the job page

SORT_KEYS: dict[str, Callable[[Job], object]] = {
    "id": lambda j: j.sort_key_id,
    "name": lambda j: j.name.lower(),
    "state": lambda j: j.state,
    "partition": lambda j: j.partition,
    "gpus": lambda j: j.gpus,
    "submit": lambda j: j.submit,
    "start": lambda j: j.start,
    "end": lambda j: j.end,
    "elapsed": lambda j: j.elapsed,
    "left": lambda j: j.time_left,
    "reason": lambda j: j.reason,
    "exit": lambda j: j.exit_code,
}


@dataclass
class App:
    client: SlurmClient
    page_size: int = 20
    refresh: int = 30


@dataclass
class Page:
    status: int
    html: str
    content_type: str = "text/html; charset=utf-8"


# ---------------------------------------------------------------- list helpers

def paginate(items: list, page: int, size: int) -> tuple[list, int, int]:
    """Return (items on page, clamped page number, number of pages)."""
    n_pages = max(1, -(-len(items) // size))
    page = min(max(1, page), n_pages)
    return items[(page - 1) * size: page * size], page, n_pages


def parse_page(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def apply_filters(jobs: list[Job], params: dict) -> list[Job]:
    q = params.get("q", "").strip().lower()
    state = params.get("state", "")
    partition = params.get("partition", "")
    d_from, d_to = parse_date(params.get("from", "")), parse_date(params.get("to", ""))
    out = []
    for j in jobs:
        if q and q not in j.name.lower() and q not in j.job_id:
            continue
        if state and state not in (j.state, j.state_group):
            continue
        if partition and j.partition != partition:
            continue
        if d_from and (not j.submit or j.submit.date() < d_from):
            continue
        if d_to and (not j.submit or j.submit.date() > d_to):
            continue
        out.append(j)
    return out


def sort_jobs(jobs: list[Job], params: dict, default: str) -> list[Job]:
    key = params.get("sort") if params.get("sort") in SORT_KEYS else default
    params["sort"] = key
    reverse = params.get("order", "desc") != "asc"
    params["order"] = "desc" if reverse else "asc"
    fn = SORT_KEYS[key]
    present = [j for j in jobs if fn(j) is not None]
    missing = [j for j in jobs if fn(j) is None]  # always last, whatever the order
    return sorted(present, key=fn, reverse=reverse) + missing


def counts(jobs: list[Job]) -> dict:
    c = Counter(j.category for j in jobs)
    return {"all": len(jobs), "running": c["running"], "pending": c["pending"], "finished": c["finished"]}


def list_page(app: App, params: dict, path: str, title: str, heading: str, select: Callable[[Job], bool],
              columns: list[r.Column], default_sort: str, intro: str = "", refresh: bool = True) -> Page:
    result = app.client.all_jobs()
    jobs = result.value
    subset = [j for j in jobs if select(j)]
    states = sorted({j.state for j in subset})
    if params.get("state") and params["state"] not in states:
        states.append(params["state"])
    partitions = sorted({j.partition for j in subset if j.partition})
    filtered = sort_jobs(apply_filters(subset, params), params, default_sort)
    shown, page, n_pages = paginate(filtered, parse_page(params.get("page")), app.page_size)
    params["page"] = str(page)
    show_states = len(states) > 1 or bool(params.get("state"))
    body = (f'<h1>{esc(heading)}</h1>{intro}'
            + r.filter_form(path, params, states if show_states else [], partitions)
            + '<div id="results" data-swap>'
            + r.table(columns, shown, path, params)
            + r.pagination(path, params, page, n_pages, len(filtered), app.page_size)
            + "</div>")
    html = r.layout(title, path, body, counts(jobs), app.client.user, result.fetched_at, result.error,
                    app.refresh if refresh else 0)
    return Page(200, html)


# ---------------------------------------------------------------- columns

def node_or_reason(j: Job) -> str:
    return r.reason_cell(j) if j.category == "pending" else esc(j.nodelist) or '<span class="muted">–</span>'


def name_cell(j: Job) -> str:
    return f'<span class="name" title="{esc(j.name)}">{esc(j.name)}</span>'


def waited(j: Job) -> str:
    return esc(r.fmt_td(datetime.now() - j.submit)) if j.submit else "–"


def exit_cell(j: Job) -> str:
    cls = "muted" if j.exit_code in ("0:0", "") else "bad"
    return f'<span class="mono {cls}">{esc(j.exit_code or "–")}</span>'


COL_ID = ("id", "Job ID", r.job_link)
COL_NAME = ("name", "Name", name_cell)
COL_STATE = ("state", "State", r.badge)
COL_PART = ("partition", "Partition", lambda j: esc(j.partition))
COL_GPUS = ("gpus", "GPUs", r.gpu_cell)
COL_SUBMIT = ("submit", "Submitted", lambda j: r.fmt_dt(j.submit))
COL_START = ("start", "Started", lambda j: r.fmt_dt(j.start) if j.category != "pending" or not j.start
             else f'<span class="muted" title="Slurm\'s estimated start">~{r.fmt_dt(j.start)} (est.)</span>')
COL_ELAPSED = ("elapsed", "Elapsed", lambda j: esc(r.fmt_td(j.elapsed)) if j.category != "pending" else '<span class="muted">–</span>')


# ---------------------------------------------------------------- pages

def overview(app: App, params: dict) -> Page:
    result = app.client.all_jobs()
    jobs = result.value
    groups = Counter(j.state_group for j in jobs)
    gpus_running = sum(j.gpus for j in jobs if j.category == "running")
    gpus_queued = sum(j.gpus for j in jobs if j.category == "pending")
    gpu_hours = sum(j.gpus * j.elapsed.total_seconds() for j in jobs if j.gpus and j.elapsed) / 3600
    cards = [
        ("running", "Running", groups["running"], "/running"),
        ("pending", "In queue", groups["pending"], "/pending"),
        ("completed", "Completed", groups["completed"], "/completed?state=COMPLETED"),
        ("failed", "Failed / timeout", groups["failed"], "/completed?state=failed"),
        ("cancelled", "Cancelled", groups["cancelled"], "/completed?state=cancelled"),
    ]
    html_cards = "".join(
        f'<a class="card c-{key}" href="{href}"><span class="num">{n}</span><span>{label}</span></a>'
        for key, label, n, href in cards)
    html_cards += (f'<div class="card c-gpu"><span class="num">{gpus_running}</span>'
                   f'<span>GPUs in use · {gpus_queued} queued</span></div>'
                   f'<div class="card c-gpu"><span class="num">{gpu_hours:,.0f}</span>'
                   f'<span>GPU-hours, last {app.client.days} days</span></div>')
    intro = (f'<p class="muted">Every job from the last {app.client.days} days plus everything '
             f'running or queued now.</p><div class="cards" id="cards" data-swap>{html_cards}</div><h2>All jobs</h2>')
    columns = [COL_ID, COL_NAME, COL_STATE, COL_PART, COL_GPUS, COL_SUBMIT, COL_START, COL_ELAPSED,
               ("", "Node / reason", node_or_reason)]
    return list_page(app, params, "/", "Overview", "Overview", lambda j: True, columns, "submit", intro)


def running(app: App, params: dict) -> Page:
    columns = [COL_ID, COL_NAME, COL_PART, COL_GPUS, ("", "Node", lambda j: esc(j.nodelist)), COL_START,
               ("elapsed", "Elapsed / limit", r.progress),
               ("left", "Time left", lambda j: esc(r.fmt_td(j.time_left)))]
    return list_page(app, params, "/running", "Running", "Running jobs",
                     lambda j: j.category == "running", columns, "start")


def pending(app: App, params: dict) -> Page:
    columns = [COL_ID, COL_NAME, COL_PART, COL_GPUS, ("", "CPUs", lambda j: str(j.cpus)),
               ("reason", "Reason", r.reason_cell), COL_SUBMIT, ("", "Waiting", waited),
               ("start", "Est. start", lambda j: r.fmt_dt(j.start)),
               ("", "Time limit", lambda j: esc(r.fmt_td(j.time_limit)))]
    intro = '<p class="muted">Hover a reason to see what it means. Est. start is Slurm\'s guess and changes often.</p>'
    return list_page(app, params, "/pending", "Queue", "Jobs in queue",
                     lambda j: j.category == "pending", columns, "submit", intro)


def completed(app: App, params: dict) -> Page:
    columns = [COL_ID, COL_NAME, COL_STATE, ("exit", "Exit", exit_cell), COL_PART, COL_GPUS, COL_SUBMIT,
               ("end", "Ended", lambda j: r.fmt_dt(j.end)), COL_ELAPSED,
               ("", "Node", lambda j: esc(j.nodelist) or '<span class="muted">–</span>')]
    return list_page(app, params, "/completed", "Completed", "Finished jobs",
                     lambda j: j.category == "finished", columns, "end", refresh=False)


def job_detail(app: App, params: dict, job_id: str) -> Page:
    client = app.client
    result = client.all_jobs()
    try:
        job = client.job(job_id)
    except ValueError:
        return Page(404, r.error_page(404, f"Invalid job id {job_id!r}"))
    if job is None:
        return Page(404, r.error_page(404, f"Job {job_id} not found"))

    def row(label, value_html):
        return f"<dt>{esc(label)}</dt><dd>{value_html}</dd>"

    wait = (job.start - job.submit) if job.start and job.submit and job.category != "pending" else None
    reason = r.reason_cell(job)
    if job.reason in r.PENDING_REASONS:
        reason += f' <small class="muted">{esc(r.PENDING_REASONS[job.reason])}</small>'
    fields = [
        row("State", r.badge(job) + (f' <span class="muted">{esc(job.state_raw)}</span>'
                                    if job.state_raw != job.state else "")),
        row("Exit code", exit_cell(job)) if job.category == "finished" else "",
        # sacct keeps the last pending reason on finished jobs; it is only meaningful while queued
        row("Reason", reason) if job.reason and job.category == "pending" else "",
        row("Partition", esc(job.partition)), row("QoS", esc(job.qos)), row("Account", esc(job.account)),
        row("Submitted", r.fmt_dt(job.submit)),
        row("Started" if job.category != "pending" else "Est. start", r.fmt_dt(job.start)),
        row("Ended", r.fmt_dt(job.end)) if job.category == "finished" else "",
        row("Waited in queue", esc(r.fmt_td(wait))) if wait is not None else "",
        row("Elapsed / limit", r.progress(job)) if job.category == "running"
        else row("Elapsed", esc(r.fmt_td(job.elapsed))) + row("Time limit", esc(r.fmt_td(job.time_limit))),
        row("Time left", esc(r.fmt_td(job.time_left))) if job.time_left else "",
        row("GPUs", r.gpu_cell(job)), row("CPUs", str(job.cpus)), row("Memory", esc(job.mem or "–")),
        row("Nodes", f"{job.nodes} · {esc(job.nodelist or '–')}"),
        row("Priority", str(job.priority)) if job.priority else "",
        row("Work dir", f'<span class="mono">{esc(job.workdir or "–")}</span>'),
    ]
    body = [f'<p class="crumb"><a href="/">Overview</a> › job <span class="mono">{esc(job.job_id)}</span></p>',
            f'<h1>{esc(job.name)} {r.badge(job)}</h1>',
            f'<dl class="fields">{"".join(fields)}</dl>']

    try:
        steps = client.steps(job.job_id) if job.category != "pending" else []
        rows = "".join(
            f'<tr><td class="mono">{esc(s.step_id)}</td><td>{esc(s.name)}</td><td>{esc(s.state)}</td>'
            f'<td class="mono">{esc(s.exit_code)}</td><td>{r.fmt_dt(s.start)}</td><td>{r.fmt_dt(s.end)}</td>'
            f'<td>{esc(r.fmt_td(s.elapsed))}</td><td>{s.cpus}</td><td class="mono">{esc(r.fmt_mem(s.max_rss))}</td></tr>'
            for s in steps)
        body.append('<h2>Steps</h2><div class="table-wrap"><table><thead><tr><th>Step</th><th>Name</th>'
                    '<th>State</th><th>Exit</th><th>Start</th><th>End</th><th>Elapsed</th><th>CPUs</th>'
                    f'<th>Max RSS</th></tr></thead><tbody>{rows}</tbody></table></div>' if steps
                    else '<h2>Steps</h2><p class="empty">No steps yet.</p>')
    except SlurmError as exc:
        body.append(f'<h2>Steps</h2><p class="banner">{esc(exc)}</p>')

    live = "checked" if job.is_active else ""
    body.append(f'<div class="logs-head" id="logs" data-job="{esc(job.job_id)}" data-state="{esc(job.state)}" '
                f'data-poll="{LOG_POLL}">'
                f'<h2>Logs</h2><label class="auto"><input type="checkbox" id="log-live" {live}> '
                f'live (every {LOG_POLL}s)</label>'
                f'<button type="button" class="log-refresh js-only">↻ Refresh logs</button></div>')
    for log in log_data(client, job):
        if log["same_as_stdout"]:
            body.append('<p class="muted">stderr goes to the same file as stdout.</p>')
            continue
        hidden = "" if log["text"] is not None else " hidden"
        body.append(
            f'<div class="log" data-kind="{log["kind"]}"><div class="log-head"><strong>{log["kind"]}</strong> '
            f'<span class="mono">{esc(log["path"] or "(not set)")}</span> '
            f'<span class="muted log-msg">{esc(log["msg"])}</span></div>'
            f'<div class="muted log-note">{esc(log["note"])}</div>'
            f'<pre class="logtext"{hidden}>{esc(log["text"] or "")}</pre></div>')

    # logs update themselves, so no full-page auto-refresh here (it would reset the log scroll)
    html = r.layout(f"Job {job.job_id}", "", "".join(body), counts(result.value), client.user,
                    result.fetched_at, result.error, 0)
    return Page(200, html)


def log_data(client: SlurmClient, job: Job) -> list[dict]:
    """stdout/stderr tails for a job, shared by the job page and the live-log endpoint."""
    paths = client.log_paths(job)
    out = []
    for kind, path in zip(("stdout", "stderr"), paths):
        same = kind == "stderr" and bool(path) and path == paths[0]
        text, msg, note = None, "", ""
        if not same:
            text, msg = read_log_tail(path)
            if text is not None:
                lines = text.splitlines()
                if len(lines) > LOG_LINES:
                    note = f"showing last {LOG_LINES} of {len(lines)} lines read"
                text = "\n".join(lines[-LOG_LINES:])
        out.append({"kind": kind, "path": path, "msg": msg, "note": note, "text": text, "same_as_stdout": same})
    return out


def job_log(app: App, params: dict, job_id: str) -> Page:
    """JSON for live log updates: {"state": ..., "logs": [...]}."""
    try:
        job = app.client.job(job_id)
    except ValueError:
        job = None
    if job is None:
        return Page(404, json.dumps({"error": "job not found"}), "application/json")
    data = {"state": job.state, "logs": log_data(app.client, job)}
    return Page(200, json.dumps(data), "application/json")

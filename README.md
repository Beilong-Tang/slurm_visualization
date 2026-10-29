# slurm_visualization

A small web dashboard for your own Slurm jobs: what is running, what is queued
(and why), and everything that finished in the last 30 days, 20 jobs per page.
It only uses the Python 3.10+ standard library, so there is nothing to install.

| Page | Shows |
|---|---|
| **Overview** `/` | Summary cards (running, queued, completed, failed, cancelled, GPUs in use, GPU-hours) and all jobs |
| **Running** `/running` | Node, elapsed vs. time limit bar, time left |
| **Queue** `/pending` | Pending reason (hover for an explanation), how long it has waited, Slurm's estimated start |
| **Completed** `/completed` | Final state, exit code, end time, elapsed |
| **Job** `/job/<id>` | All fields, job steps with max memory, and the last 300 lines of stdout/stderr |

Every list can be filtered (name or ID search, state, partition, submit date) and sorted by clicking a column header.

## Quick start

**Step 1: start the server on the login node** (terminal 1, your normal ssh session)

```bash
ssh btang1@login.ncshare.org
hostname                       # note it, e.g. login-01
cd ~/work/software/slurm_visualization
tmux new -s slurmviz           # keeps the server running after you disconnect
./run.sh --check               # optional: confirm Slurm data can be read
./run.sh                       # default port 8765
```

It prints:

```
  1. On your laptop:  ssh -N -L 8765:localhost:8765 btang1@login.ncshare.org   (the login node is login-01)
  2. Open:            http://localhost:8765/?token=Xq3...
```

Copy the URL, then detach from tmux with `Ctrl+b` then `d`. The server keeps running.

**Step 2: open the tunnel on your laptop** (terminal 2, a new local terminal, not on the cluster)

```bash
ssh -N -L 8765:localhost:8765 btang1@login.ncshare.org
```

Log in as usual. `-N` means no shell opens, so the terminal just sits there;
that is expected. Leave it open while you use the dashboard, and press `Ctrl+C` to close the tunnel.
This works the same in macOS/Linux terminals and Windows PowerShell.

**Step 3: open the URL in your laptop's browser**

Paste the full `http://localhost:8765/?token=...` URL. After the first visit the
token is saved as a cookie, so later `http://localhost:8765/` is enough (until the server restarts,
which creates a new token).

**Later:** `ssh` back in, run `tmux attach -t slurmviz` to see the server (and the URL again),
or `Ctrl+C` to stop it.

### Troubleshooting

- **"Connection refused" in the browser:** the tunnel landed on a different login node from the
  one running the server. Run `hostname` after `ssh btang1@login.ncshare.org`; if it is not the
  node from step 1, start the server again on that node.
- **Port already in use on your laptop:** pick another local port, e.g.
  `ssh -N -L 9000:localhost:8765 ...`, then open `http://localhost:9000/?token=...`.
- **Server says port 8765 was busy:** someone else on the login node is using it, so the
  server took the next free port (e.g. 8766). Use that number in the `ssh -L` command.
- **403 Missing token:** open the full URL with `?token=...` from the server output.
- **Running the server on a compute node** (e.g. inside `srun --pty bash`) is possible but the
  server stops when that job ends. The tunnel then needs a jump:
  `ssh -N -J btang1@login.ncshare.org -L 8765:localhost:8765 btang1@compute-05`
  (this only works if NCShare allows ssh to a compute node where you have a running job).

## Options

| Flag | Default | |
|---|---|---|
| `--port` | 8765 | Uses the next free port if it is taken |
| `--days` | 30 | History window for `sacct` |
| `--refresh` | 30 | Auto-refresh seconds on Overview/Running/Queue (0 = off); can also be switched off in the page header |
| `--page-size` | 20 | Jobs per page |
| `--ttl` | 15 | Seconds to reuse Slurm output between requests |
| `--user` | `$USER` | Whose jobs to show |
| `--host` | 127.0.0.1 | Leave this alone (see Security) |

## Security

The login node is shared, and anyone on it can connect to `localhost:<port>`.
So the server binds to `127.0.0.1` only and requires the random token that it
prints at startup (a new one every start). Requests without it get 403. The app
is read-only: it only runs `squeue`, `sacct` and `scontrol show job`, and it only
shows log files that you own.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use `squeue`/`sacct` output saved in `tests/fixtures/`, so they run without Slurm.

See [PLAN.md](PLAN.md) for the design.

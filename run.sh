#!/usr/bin/env bash
# Start the Slurm job dashboard. Extra flags are passed through, e.g.:
#   ./run.sh --port 8765 --days 30 --refresh 30
#   ./run.sh --check
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m slurm_viz "$@"

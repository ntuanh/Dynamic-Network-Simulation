#!/usr/bin/env bash
# Start the controller with the project venv. Ctrl+C stops it and restores the network.
#
#   ./run.sh                      # run with config/config.yaml until duration_sec elapses
#   ./run.sh --duration 60        # stop automatically after 60 s
#   ./run.sh --dry-run -v         # print the commands, touch nothing, no root needed
#
# Any extra arguments are passed straight through to "main.py start".
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

PY="$PWD/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
    echo "error: $PY not found - create it with:" >&2
    echo "    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# --dry-run needs no privileges; a real run programs tc/iptables and needs root.
if [[ " $* " == *" --dry-run "* ]]; then
    exec "$PY" main.py start --config config/config.yaml "$@"
fi

# exec so sudo replaces this shell: Ctrl+C then reaches main.py directly and
# triggers the same verified restoration as "main.py stop".
exec sudo "$PY" main.py start --config config/config.yaml "$@"

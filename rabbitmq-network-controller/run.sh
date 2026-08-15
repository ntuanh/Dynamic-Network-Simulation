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

# Non-interactive sudo: take the password from $RMQNC_SUDO_PASS, or the first
# line of a local ".sudo_pass" file next to this script. Both are gitignored and
# stay on the host they run on - never commit them. Leave both unset to be
# prompted interactively instead.
SUDO_PASS="${RMQNC_SUDO_PASS:-}"
if [[ -z "$SUDO_PASS" && -f "$PWD/.sudo_pass" ]]; then
    SUDO_PASS="$(head -n1 "$PWD/.sudo_pass")"
fi

# exec so sudo replaces this shell: Ctrl+C then reaches main.py directly and
# triggers the same verified restoration as "main.py stop". The PTY delivers
# SIGINT to the process group regardless of where stdin points, so feeding the
# password on stdin (sudo -S) does not interfere with Ctrl+C.
if [[ -n "$SUDO_PASS" ]]; then
    exec sudo -S -p '' "$PY" main.py start --config config/config.yaml "$@" <<<"$SUDO_PASS"
fi

exec sudo "$PY" main.py start --config config/config.yaml "$@"

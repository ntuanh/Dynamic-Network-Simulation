#!/usr/bin/env bash
# Last-resort cleanup: remove every kernel object this project can create,
# without needing Python, the config file or the rollback journal.
#
#   sudo ./scripts/emergency_restore.sh [interface] [ifb-device]
#
# Prefer `sudo python3 main.py restore` - it also verifies the result against the
# backup taken before the run.  Use this script when Python is unavailable or the
# project directory is gone.
set -uo pipefail

IFACE="${1:-$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')}"
IFB="${2:-ifb-rmq0}"
CHAIN="RMQNC"

if [[ -z "${IFACE}" ]]; then
  echo "usage: $0 <interface> [ifb-device]" >&2
  exit 2
fi
if [[ "$(id -u)" != "0" ]]; then
  echo "error: run me as root (sudo $0 ...)" >&2
  exit 1
fi

echo "cleaning traffic control state on ${IFACE}..."

# Deleting the root qdisc removes every class, leaf qdisc and filter under it.
tc qdisc del dev "${IFACE}" root    2>/dev/null && echo "  removed root qdisc"
tc qdisc del dev "${IFACE}" ingress 2>/dev/null && echo "  removed ingress qdisc"

for BIN in iptables ip6tables; do
  command -v "${BIN}" >/dev/null || continue
  while "${BIN}" -t mangle -D POSTROUTING -o "${IFACE}" -j "${CHAIN}" 2>/dev/null; do
    echo "  removed ${BIN} POSTROUTING jump"
  done
  "${BIN}" -t mangle -F "${CHAIN}" 2>/dev/null && echo "  flushed ${BIN} chain ${CHAIN}"
  "${BIN}" -t mangle -X "${CHAIN}" 2>/dev/null && echo "  deleted ${BIN} chain ${CHAIN}"
done

if ip link show "${IFB}" &>/dev/null; then
  ip link del "${IFB}" && echo "  deleted ${IFB}"
fi

echo
echo "current state:"
tc qdisc show dev "${IFACE}"
echo
echo "done - ${IFACE} is back to the kernel default queueing discipline."

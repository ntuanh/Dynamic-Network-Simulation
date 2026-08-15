#!/usr/bin/env bash
# Show every kernel object this project can create, so you can confirm that
#   (a) RabbitMQ traffic is shaped, and
#   (b) nothing else is.
#
#   sudo ./scripts/verify_shaping.sh [interface] [ifb-device]
#
# Without arguments the interface is taken from the default route.
set -uo pipefail

IFACE="${1:-$(ip -o route show default 2>/dev/null | awk '{print $5; exit}')}"
IFB="${2:-ifb-rmq0}"
CHAIN="RMQNC"

if [[ -z "${IFACE}" ]]; then
  echo "usage: $0 <interface> [ifb-device]" >&2
  exit 2
fi

hr() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

hr "queueing disciplines on ${IFACE}"
tc -s qdisc show dev "${IFACE}"

hr "classes on ${IFACE} (1:10 = RabbitMQ, 1:99 = everything else)"
tc -s class show dev "${IFACE}" || true

hr "filters on ${IFACE}"
tc filter show dev "${IFACE}" || true

hr "ingress filters on ${IFACE}"
tc filter show dev "${IFACE}" parent ffff: 2>/dev/null || echo "<no ingress qdisc>"

if ip link show "${IFB}" &>/dev/null; then
  hr "ingress shaping device ${IFB}"
  tc -s qdisc show dev "${IFB}"
  tc -s class show dev "${IFB}" || true
else
  hr "ingress shaping device ${IFB}"
  echo "<not present - egress-only shaping>"
fi

hr "netfilter marking (mangle table)"
if command -v iptables >/dev/null; then
  iptables -t mangle -S "${CHAIN}" 2>/dev/null || echo "<chain ${CHAIN} not present>"
  echo
  iptables -t mangle -L POSTROUTING -v -n --line-numbers | head -n 15
else
  echo "<iptables not installed>"
fi

hr "live RabbitMQ connections"
ss -ntp 2>/dev/null | grep -E ':(5672|15672)\b' || echo "<no established connections on 5672/15672>"

hr "verdict"
if tc class show dev "${IFACE}" 2>/dev/null | grep -q 'class htb 1:10'; then
  RATE=$(tc class show dev "${IFACE}" | awk '/class htb 1:10/{for(i=1;i<=NF;i++) if($i=="rate") print $(i+1)}')
  echo "RabbitMQ traffic on ${IFACE} is shaped to ${RATE}"
elif tc qdisc show dev "${IFACE}" 2>/dev/null | grep -q 'qdisc tbf 40:'; then
  RATE=$(tc qdisc show dev "${IFACE}" | awk '/qdisc tbf 40:/{for(i=1;i<=NF;i++) if($i=="rate") print $(i+1)}')
  echo "RabbitMQ traffic on ${IFACE} is shaped to ${RATE} (tbf backend)"
else
  echo "no RabbitMQ shaping is currently installed on ${IFACE}"
fi

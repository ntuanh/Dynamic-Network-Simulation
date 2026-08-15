#!/usr/bin/env bash
# Drive the network controller on the RabbitMQ broker host from here (server dai).
#
# tc/iptables program the LOCAL kernel, so the controller must run ON the broker
# (machine-1) to shape the broker's NIC and thus every AMQP client. This wrapper
# runs it there over SSH while you keep typing one command on dai. Ctrl+C is
# forwarded to the remote controller (ssh -t), triggering the same verified
# restoration as a local run.
#
#   ./run-remote.sh                  # run until config duration_sec elapses
#   ./run-remote.sh --duration 60    # stop automatically after 60 s
#   ./run-remote.sh --dry-run -v     # print the commands, touch nothing
#
# Extra arguments pass straight through to run.sh on the remote host.
set -euo pipefail

# SSH alias defined in ~/.ssh/config (Host machine-1). Override with TARGET=...
TARGET="${TARGET:-machine-1}"
# Where the repo lives on the broker host.
REMOTE_DIR="${REMOTE_DIR:-~/Dynamic-Network-Simulation/rabbitmq-network-controller}"

# -t: allocate a PTY so Ctrl+C reaches the remote controller as SIGINT.
exec ssh -t "$TARGET" "cd $REMOTE_DIR && ./run.sh $*"

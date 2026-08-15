# rabbitmq-network-controller

Dynamically control the **real** network bandwidth available to RabbitMQ traffic on a Linux host.

The controller shapes every packet on the RabbitMQ TCP ports (5672 / 15672 by default) using the Linux
traffic-control subsystem, re-drawing the limit from a configurable random process every second. It never
touches the broker: no queue names, no exchange names, no vhosts, no credentials, no RabbitMQ configuration
changes, no producer/consumer changes. When it stops — cleanly, on `Ctrl+C`, on `SIGTERM`, on a crash — the
host networking is restored to exactly the state it was in beforehand, and the restoration is verified and
logged.

```
producer ──┐                      ┌── consumer
           │   ┌──────────────┐   │
           ├──►│   RabbitMQ   │◄──┤        the broker is untouched
           │   └──────────────┘   │
           │          ▲           │
           └──────────┼───────────┘
                      │
        ┌─────────────┴──────────────┐
        │  Linux kernel: tc + netfilter │  ◄── this project only ever talks to the kernel
        │  htb 1:10 @ 20.4 Mbit/s      │
        └───────────────────────────────┘
```

---

## Table of contents

1. [5-Minute Quick Start](#5-minute-quick-start)
2. [A. Overview](#a-overview)
3. [B. Installation](#b-installation)
4. [C. Configuration](#c-configuration)
5. [D. Running](#d-running)
6. [E. Verification](#e-verification)
7. [F. Logs](#f-logs)
8. [G. Shutdown and restoration](#g-shutdown-and-restoration)
9. [H. Troubleshooting](#h-troubleshooting)
10. [I. Example workflow](#i-example-workflow)
11. [J. Safety notes](#j-safety-notes)
12. [Project structure](#project-structure)
13. [Extending the bandwidth model](#extending-the-bandwidth-model)

---

## 5-Minute Quick Start

Exact commands, from a fresh Ubuntu 22.04/24.04 machine to a running experiment.

```bash
# ── 0. (00:00) system packages ──────────────────────────────────────────────
sudo apt update
sudo apt install -y iproute2 iptables python3 python3-pip git

# ── 1. (00:40) get the project ──────────────────────────────────────────────
cd ~
git clone <your-repo-url> rabbitmq-network-controller     # or copy the folder here
cd rabbitmq-network-controller
pip install -r requirements.txt                            # PyYAML + matplotlib

# ── 2. (01:20) sanity-check the tooling ─────────────────────────────────────
tc -V                     # iproute2-6.x
iptables --version        # iptables v1.8.x (nf_tables) is fine
ip -brief link show       # <-- note YOUR interface name (eth0, ens33, enp0s3, ...)

# ── 3. (02:00) start RabbitMQ (skip if you already have a broker) ───────────
sudo apt install -y rabbitmq-server
sudo systemctl enable --now rabbitmq-server
ss -ltn | grep 5672        # broker is listening

# ── 4. (03:00) point the config at your interface ───────────────────────────
sed -i 's/^  interface: .*/  interface: "auto"/' config/config.yaml
#   "auto" resolves to the interface holding the default route.
#   Prefer to be explicit?  ->  sed -i 's/^  interface: .*/  interface: "ens33"/' config/config.yaml

# ── 5. (03:30) preview what will be executed (no root, no changes) ──────────
python3 main.py start --config config/config.yaml --dry-run --duration 3 -v

# ── 6. (04:00) run the real thing ───────────────────────────────────────────
sudo python3 main.py start --config config/config.yaml
#   Leave this running.  Every second it prints a new limit:
#   bandwidth #2 -> 17.412 Mbit/s (1.3 ms)

# ── 7. (04:30) in a SECOND terminal: watch it work ──────────────────────────
cd ~/rabbitmq-network-controller
sudo python3 main.py status          # bandwidth, tc rules, uptime, restoration state
sudo tc -s class show dev $(ip -o route show default | awk '{print $5; exit}')
#   ... now run your producers/consumers and watch throughput follow the limit

# ── 8. (05:00) stop and verify ──────────────────────────────────────────────
#   press Ctrl+C in the first terminal (or: sudo python3 main.py stop)
#   -> "Restoration: SUCCESS", every check [ok]
python3 scripts/plot_bandwidth.py    # -> results/bandwidth_over_time.png
tc qdisc show                        # back to the kernel default everywhere
```

Nothing to uninstall: the controller only ever adds kernel objects it removes again, and `tc` rules never
survive a reboot.

---

## A. Overview

### What the project does

* Generates a bandwidth value every `update_interval_sec` from a configurable stochastic process
  (gaussian by default, plus uniform / constant / log-normal / Markov / CSV-trace-replay / your own Python).
* Programs that value into the kernel as a hard rate limit **for RabbitMQ traffic only**.
* Logs every value to CSV + JSON, and every action to a rotating application log.
* Backs up the pre-run networking state, journals an undo command for every change it makes, and restores +
  verifies on every exit path.

### How bandwidth control works

Linux can only queue packets it is about to *send*. The controller therefore builds a small queueing
hierarchy on the interface and steers RabbitMQ packets into a rate-limited class.

With the default `shaper: htb`:

```
                 root qdisc  htb 1:      (default 99)
                       │
        ┌──────────────┴───────────────┐
   class 1:10                     class 1:99
   RabbitMQ                       EVERYTHING ELSE
   rate = ceil = <dynamic>        rate = ceil = link speed   ← ssh, git, apt, docker, VSCode
        │                              │                        are never delayed
   qdisc 10: fq_codel             qdisc 99: fq_codel
   (or netem, when enabled)
```

Every second the controller issues a single, atomic, sub-millisecond netlink update:

```bash
tc class change dev eth0 parent 1: classid 1:10 htb rate 17.412mbit ceil 17.412mbit burst 8706b ...
```

Existing TCP connections are **not** reset — the new limit simply applies to the packets that follow, and
TCP congestion control adapts within a few RTTs. That is what makes second-by-second variation realistic.

With `shaper: tbf` the same idea is implemented as a `prio` qdisc with 4 bands; the kernel's default
`priomap` only ever selects bands 1–3, so band 4 is reachable exclusively through our filters and carries a
`tbf` qdisc at the dynamic rate.

**Ingress.** Inbound traffic (producers → broker) cannot be queued on arrival, so when
`network.shape_ingress: true` the controller creates an IFB (Intermediate Functional Block) device, mirrors
**only RabbitMQ packets** onto it with `act_mirred`, and applies the same limit there. The bits still arrive
on the wire, but they are queued/dropped before the application sees them, which makes the sender's TCP back
off — the standard way to emulate a slow downlink on Linux.

### How RabbitMQ traffic is identified

No queue names, no exchange names, no vhosts, no logins, no broker API calls. Identification is purely at
the TCP layer, so it covers **all** RabbitMQ traffic on the host — every connection, every channel, every
producer and consumer, including the management UI.

Two mechanisms are available:

| `network.classification` | mechanism | what it does |
|---|---|---|
| `fwmark` *(default)* | `iptables -t mangle` + `tc … fw` | a dedicated chain `RMQNC` marks packets whose source **or** destination TCP port is a RabbitMQ port with `--set-xmark 0x2a/0xff`; a `fw` filter steers marked packets into class `1:10` |
| `u32` | pure `tc` | `u32` filters match the TCP port fields directly in the IP header — no netfilter involved at all |

```bash
# what the fwmark path installs (exactly these five rules)
iptables -t mangle -N RMQNC
iptables -t mangle -A RMQNC -p tcp -m multiport --dports 5672,15672 -j MARK --set-xmark 0x2a/0xff
iptables -t mangle -A RMQNC -p tcp -m multiport --sports 5672,15672 -j MARK --set-xmark 0x2a/0xff
iptables -t mangle -I POSTROUTING 1 -o eth0 -j RMQNC
tc filter add dev eth0 parent 1: protocol ip prio 10 handle 0x2a/0xff fw flowid 1:10
```

`--set-xmark MARK/MASK` only touches the mask's bits, so marks used by Docker, Kubernetes, WireGuard or
policy routing survive untouched. The jump is scoped to `-o <interface>`, so nothing else on the host is
even evaluated. The ingress path uses `u32` instead of marks, because `tc` ingress hooks run *before*
netfilter.

**Anything that is not on a RabbitMQ port lands in the default class and runs at line rate.** SSH, git,
VSCode Remote, `apt`, unrelated Docker traffic and system updates are never shaped.

### Limitations and assumptions

* **Linux only, root required.** `tc` and netfilter are kernel features; there is no user-space fallback.
  Kernel modules used: `sch_htb`/`sch_prio`/`sch_tbf`, `sch_fq_codel`, optionally `sch_netem`, `act_mirred`
  and `ifb`, plus `xt_multiport`/`xt_mark`. All are present in stock Ubuntu/Debian kernels.
* **Traffic must actually traverse the configured interface.** If the broker and the client run on the same
  host, their traffic goes over the loopback device — set `interface: "lo"`. If they run in Docker
  containers on the same bridge, shape the bridge (`docker0`, `br-…`) or the container's `veth`, not `eth0`.
* **Egress is a true shaper; ingress is an emulation.** Outbound bytes are genuinely paced. Inbound bytes
  have already crossed the wire by the time Linux sees them; queueing them on an IFB device makes the
  *sender* slow down, which is the standard, but not bit-exact, way to model a slow downlink.
* **Ports, not protocols.** Traffic is selected by TCP port. TLS (5671), STOMP (61613), MQTT (1883) or
  clustering (25672) are only shaped if you list them in `rabbitmq.ports`.
* **One controller per host.** A PID file with an exclusive `flock` prevents two instances from fighting
  over the same qdisc tree.
* **Rates below ~8 kbit/s are not meaningful** and `min_mbps` must be > 0 — `tc` cannot express a rate of
  zero. Use a very small `min_mbps` (e.g. `0.05`) to emulate an outage.
* **The controller does not persist across reboots** — by design. `tc` rules live in kernel memory only, so
  a reboot is itself a complete restoration.

---

## B. Installation

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y iproute2 iptables python3 python3-pip

pip install -r requirements.txt
```

### RHEL / Fedora / Rocky

```bash
sudo dnf install -y iproute iptables python3 python3-pip
pip install -r requirements.txt
```

### Verify the required tools

```bash
tc -h                 # usage text from iproute2
tc -V                 # tc utility, iproute2-6.1.0
iptables --version    # iptables v1.8.9 (nf_tables)
python3 --version     # Python 3.10 or newer
ip -brief link show   # your interface names
```

Optional, for ingress shaping:

```bash
sudo modprobe ifb act_mirred    # both are usually auto-loaded on demand
lsmod | grep -E 'ifb|act_mirred'
```

Optional, for the plots:

```bash
python3 -c "import matplotlib; print(matplotlib.__version__)"
```

If the helper scripts were copied from a filesystem that does not carry the executable bit (Windows share,
zip archive), restore it once:

```bash
chmod +x scripts/*.sh
```

### Permissions

Everything except `--dry-run`, `status` and the plotting script needs root, because programming `tc` and
netfilter requires `CAP_NET_ADMIN`:

```bash
sudo python3 main.py start --config config/config.yaml
```

No other privilege is required, and the controller never opens a network socket of its own.

---

## C. Configuration

The shipped `config/config.yaml` is deliberately short; `example_config.yaml` documents **every** field with
its default. Relative paths are resolved against the project root, so `sudo` from any directory behaves
identically.

### `network`

| Key | Default | Meaning |
|---|---|---|
| `interface` | `auto` | Interface carrying RabbitMQ traffic. `auto` = the default-route interface. Use `lo` for same-host traffic, `docker0`/`br-…` for container bridges. |
| `shape_ingress` | `false` | Also limit inbound RabbitMQ traffic by mirroring it onto an IFB device. |
| `ifb_device` | `ifb-rmq0` | Name of that IFB device; created at start, deleted at shutdown. |
| `classification` | `fwmark` | `fwmark` (iptables marks, recommended) or `u32` (pure tc port matching). |
| `fwmark` / `fwmask` | `0x2a` / `0xff` | Mark applied to RabbitMQ packets. Only the mask's bits are written. Change if it clashes with other software. |
| `shaper` | `htb` | `htb` (classful, recommended) or `tbf` (token bucket on a `prio` band). |
| `latency_ms` | `50` | Queue latency budget for `tbf`; also sizes the `htb` burst. |
| `leaf_qdisc` | `fq_codel` | Leaf qdisc under the shaped class. Ignored when `netem.enabled` is true. |
| `unshaped_ceil_mbps` | `0` (auto) | Ceiling of the *unshaped* class. `0` reads `/sys/class/net/<if>/speed`, falling back to 10 Gbit/s. |
| `enable_ipv6` | `true` | Also install `ip6tables` marking and `protocol ipv6` filters. |
| `protect_existing_qdisc` | `true` | Refuse to start if the interface already has a non-default root qdisc (someone else is shaping it). Override with `--force`. |

### `rabbitmq`

| Key | Default | Meaning |
|---|---|---|
| `ports` | `[5672, 15672]` | **All** TCP traffic on these ports is shaped. Add `5671` (AMQPS), `61613` (STOMP), `1883` (MQTT), `25672` (clustering) as needed. |
| `match_direction` | `both` | `both` matches source *or* destination port; `dst`/`src` restrict it. |

### `bandwidth`

| Key | Default | Meaning |
|---|---|---|
| `distribution` | `gaussian` | `constant`, `gaussian` (alias `normal`), `uniform`, `lognormal`, `markov`, `trace`, `custom`. |
| `mean_mbps` | `20` | Mean of the gaussian / value of the constant model. |
| `std_mbps` | `5` | Standard deviation. |
| `min_mbps` / `max_mbps` | `1` / `100` | Hard clamp applied to every generated sample. `min_mbps` must be > 0. |
| `update_interval_sec` | `1` | How often a new value is generated and applied. |
| `round_digits` | `3` | Rounding applied before the value reaches `tc`. |
| `low_mbps` / `high_mbps` | — | Bounds for `uniform` (default: `min`/`max`). |
| `markov.*` | — | `states_mbps`, `transition` (rows sum to 1), `start_state`, `jitter_std_mbps`. |
| `trace.*` | — | `file`, `column`, `loop`, `scale`. |
| `custom.*` | — | `module` (dotted path or `.py` file), `callable`. |

### `netem` (optional impairment, applied only to RabbitMQ traffic)

`enabled`, `delay_ms`, `jitter_ms`, `loss_pct`, `duplicate_pct`, `reorder_pct`, `distribution`.

### `logging`

`csv_file`, `json_file`, `app_log_file`, `restoration_report_file`, `level`, `console`, `max_bytes`,
`backup_count`, `json_flush_every`.

### `runtime`

`duration_sec` (`0` = until stopped), `random_seed` (`null` = non-reproducible), `backup_dir`, `state_file`,
`pid_file`, `journal_file`, `verify_restore`, `restore_timeout_sec`.

### Example configurations

Ready to run, in [`config/examples/`](config/examples):

**1. Stable 20 Mbit/s** — [`01_stable_20mbps.yaml`](config/examples/01_stable_20mbps.yaml)

```yaml
bandwidth:
  distribution: "constant"
  mean_mbps: 20
  min_mbps: 1
  max_mbps: 100
  update_interval_sec: 5
```

**2. Dynamic 20 ± 5 Mbit/s** — [`02_dynamic_20_5.yaml`](config/examples/02_dynamic_20_5.yaml)

```yaml
bandwidth:
  distribution: "gaussian"
  mean_mbps: 20
  std_mbps: 5
  min_mbps: 1
  max_mbps: 100
  update_interval_sec: 1
```

**3. Dynamic 100 ± 20 Mbit/s (with 5 ms ± 2 ms latency)** — [`03_dynamic_100_20.yaml`](config/examples/03_dynamic_100_20.yaml)

```yaml
bandwidth:
  distribution: "gaussian"
  mean_mbps: 100
  std_mbps: 20
  min_mbps: 10
  max_mbps: 200
  update_interval_sec: 1
netem:
  enabled: true
  delay_ms: 5
  jitter_ms: 2
  distribution: "normal"
```

**4. Trace replay** — [`04_trace_replay.yaml`](config/examples/04_trace_replay.yaml)

```yaml
bandwidth:
  distribution: "trace"
  update_interval_sec: 1
  trace:
    file: "config/examples/trace_example.csv"   # timestamp,bandwidth_mbps
    column: "bandwidth_mbps"
    loop: true
    scale: 1.0
```

Any CSV works, including a previous run's `logs/bandwidth_history.csv` — so an experiment can be replayed
byte-for-byte against a different broker configuration.

**5. Markov link states** — [`05_markov_states.yaml`](config/examples/05_markov_states.yaml) ·
**6. Custom Python model** — [`06_custom_model.yaml`](config/examples/06_custom_model.yaml)

---

## D. Running

```bash
# start (foreground; Ctrl+C restores the network)
sudo python3 main.py start --config config/config.yaml

# start in the background
sudo python3 main.py start --config config/config.yaml --daemon

# check status
sudo python3 main.py status

# stop and restore
sudo python3 main.py stop

# force restore (after a crash, a SIGKILL or a reboot)
sudo python3 main.py restore
```

### Useful flags

| Command | Flag | Effect |
|---|---|---|
| `start` | `--dry-run` | Print every command instead of executing it. No root needed. |
| `start` | `--interface eth1` | Override `network.interface`. |
| `start` | `--duration 300` | Override `runtime.duration_sec` (`0` = unlimited). |
| `start` | `--daemon` | Detach; logs keep flowing to `logs/controller.log`. |
| `start` | `--force` | Take over an existing root qdisc / ignore a non-empty rollback journal. |
| `start` | `-v` / `-q` | Debug logging / no console logging. |
| `stop` | `--timeout 30` | Seconds to wait for a graceful exit (default 20). |
| `stop` | `--force` | `SIGKILL` after the timeout, then restore from the journal. |
| `status` | `--json` | Machine readable output. |
| `status` | `--no-rules` | Skip the live `tc`/`iptables` dump. |
| `status` | `--history 20` | Show the last 20 samples. |
| `restore` | `--force` | Also sweep for leftovers and ignore a still-running controller. |
| `restore` | `--backup DIR` | Verify against a specific backup instead of `runtime_backup/latest`. |

### What `status` shows

```text
RabbitMQ Network Controller - status

  controller     : RUNNING (pid 4711)
  phase          : running
  uptime         : 12m 04s
  started at     : 2026-08-15T12:00:00
  config         : /home/ubuntu/rabbitmq-network-controller/config/config.yaml
  interface      : eth0 (+ ingress via ifb-rmq0)
  shaper         : htb / classification: fwmark
  rabbitmq ports : 5672, 15672
  model          : gaussian mean=20.0 std=5.0 clamped to [1.0, 100.0] Mbit/s
  bandwidth      : 17.412 Mbit/s
  last update    : 2026-08-15T12:12:04 (724 applied, 0 failed)
  dry run        : no
  backup         : /home/ubuntu/rabbitmq-network-controller/runtime_backup/backup-20260815-120000
  restoration    : pending (rules are active)

Live traffic-control rules
  $ tc qdisc show dev eth0
      qdisc htb 1: root refcnt 2 r2q 10 default 0x99 direct_packets_stat 0 direct_qlen 1000
      qdisc fq_codel 10: parent 1:10 limit 10240p flows 1024 quantum 1514 ...
  $ tc class show dev eth0
      class htb 1:10 root leaf 10: prio 1 rate 17412Kbit ceil 17412Kbit burst 8706b cburst 8706b
      class htb 1:99 root leaf 99: prio 0 rate 1Gbit ceil 1Gbit burst 1Mb cburst 1600b
  ...

Last 5 bandwidth samples (logs/bandwidth_history.csv)
  2026-08-15T12:12:00        21.043 Mbit/s  eth0       applied
  ...
```

### Running as a systemd service

```ini
# /etc/systemd/system/rabbitmq-network-controller.service
[Unit]
Description=RabbitMQ dynamic bandwidth controller
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/rabbitmq-network-controller
ExecStart=/usr/bin/python3 main.py start --config config/config.yaml
ExecStop=/usr/bin/python3 main.py stop
Restart=no
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

`systemctl stop` sends `SIGTERM`, which triggers the same verified restoration as `Ctrl+C`.

---

## E. Verification

### Are the rules installed?

```bash
tc qdisc show                        # every interface
tc qdisc show dev eth0               # root htb 1: + leaf qdiscs
tc class show dev eth0               # 1:10 = RabbitMQ, 1:99 = everything else
tc filter show dev eth0              # the fw / u32 classifier
tc filter show dev eth0 parent ffff: # ingress redirect filters (if enabled)
iptables -t mangle -L -v             # the RMQNC chain and its POSTROUTING jump
iptables -t mangle -S RMQNC          # exact rule text
```

One command that prints all of the above plus a verdict:

```bash
sudo ./scripts/verify_shaping.sh              # uses the default-route interface
sudo ./scripts/verify_shaping.sh eth0         # explicit
```

### Is RabbitMQ traffic actually being shaped?

```bash
# 1. the packet counters on the RabbitMQ class must be climbing
watch -n1 'tc -s class show dev eth0 | grep -A3 "class htb 1:10"'

# 2. the mangle rules must be matching packets (non-zero pkts/bytes)
sudo iptables -t mangle -L RMQNC -v -n

# 3. which processes hold RabbitMQ connections
ss -ntp | grep 5672
ss -tni  '( sport = :5672 or dport = :5672 )'   # cwnd/rtt per connection

# 4. live throughput
sudo iftop -i eth0 -f 'port 5672'
nload eth0
sudo tcpdump -i eth0 -nn 'tcp port 5672' -c 20

# 5. end-to-end proof: throughput follows the configured limit
sudo tc class change dev eth0 parent 1: classid 1:10 htb rate 1mbit ceil 1mbit  # temporary manual test
#    ... push messages, observe the drop, then let the controller resume
```

### Is unrelated traffic untouched?

```bash
# All non-RabbitMQ traffic lands in class 1:99, which runs at line rate.
tc -s class show dev eth0 | grep -A3 'class htb 1:99'

# Prove it: this should be as fast as it was before the controller started.
curl -o /dev/null -w '%{speed_download}\n' https://speed.hetzner.de/100MB.bin
```

---

## F. Logs

### `logs/bandwidth_history.csv`

Fixed four-column schema — safe to parse with pandas, awk or a spreadsheet.

```csv
timestamp,bandwidth_mbps,interface,status
2026-08-15T12:00:01,15.2,eth0,applied
2026-08-15T12:00:02,22.874,eth0,applied
2026-08-15T12:00:03,18.301,eth0,applied
2026-08-15T12:00:04,19.44,eth0,failed
2026-08-15T12:00:05,17.412,eth0,applied
2026-08-15T12:59:59,17.412,eth0,shutdown
```

`status` is one of `applied` (programmed into the kernel), `dry-run`, `failed` (the `tc` call errored — the
previous limit stayed in force) or `shutdown` (final row written during teardown).

The CSV is **appended across runs**, so a single file can hold several experiments. Point each experiment at
its own `logging.csv_file` (as the shipped examples do), or move the file aside between runs, if you want one
plot per experiment.

### `logs/bandwidth_history.json`

Same history plus metadata and a rolling summary:

```json
{
  "schema": 1,
  "generated_at": "2026-08-15T13:00:00",
  "record_count": 3600,
  "summary": {
    "count": 3599, "min_mbps": 4.117, "max_mbps": 38.02,
    "mean_mbps": 20.014, "std_mbps": 4.982, "median_mbps": 20.06, "failures": 0
  },
  "records": [
    {
      "timestamp": "2026-08-15T12:00:01", "bandwidth_mbps": 15.2, "interface": "eth0",
      "status": "applied", "epoch": 1786780801.004, "update_index": 1,
      "model": "gaussian", "direction": "both", "apply_duration_ms": 1.284, "error": ""
    }
  ]
}
```

### `logs/controller.log`

Rotating application log (10 MiB × 5 by default):

```text
2026-08-15T12:00:00+0000 INFO     [rmqnc.network] network state backed up to runtime_backup/backup-20260815-120000
2026-08-15T12:00:00+0000 INFO     [rmqnc.network] installing shaping on eth0 (shaper=htb, classification=fwmark, ports=5672,15672, ingress=True)
2026-08-15T12:00:00+0000 INFO     [rmqnc.network] shaping active: RabbitMQ traffic limited to 15.200 Mbit/s
2026-08-15T12:00:01+0000 INFO     [rmqnc.loop] bandwidth #2 -> 22.874 Mbit/s (1.1 ms)
2026-08-15T12:59:59+0000 WARNING  [rmqnc.cleanup] received SIGINT - shutting down and restoring the network...
2026-08-15T12:59:59+0000 INFO     [rmqnc] Restoration: SUCCESS (reason: signal:SIGINT)
```

### `logs/restoration_report.json`

Written on every teardown — the machine-readable proof that the host was put back:

```json
{
  "reason": "signal:SIGINT", "interface": "eth0",
  "started_at": "2026-08-15T12:59:59", "finished_at": "2026-08-15T12:59:59",
  "ops_executed": 7, "success": true, "verified": true, "errors": [],
  "checks": [
    {"name": "qdisc state",        "passed": true, "detail": ""},
    {"name": "class state",        "passed": true, "detail": ""},
    {"name": "filter state",       "passed": true, "detail": ""},
    {"name": "ingress filters",    "passed": true, "detail": ""},
    {"name": "iptables mangle",    "passed": true, "detail": ""},
    {"name": "ip6tables mangle",   "passed": true, "detail": ""},
    {"name": "RMQNC chain removed","passed": true, "detail": ""},
    {"name": "ifb device removed", "passed": true, "detail": ""}
  ]
}
```

### Plots

```bash
python3 scripts/plot_bandwidth.py                                  # -> results/bandwidth_over_time.png
python3 scripts/plot_bandwidth.py --csv logs/dynamic_20_5.csv \
    --output results/experiment_a.png --title "Experiment A"
python3 scripts/plot_bandwidth.py --theme dark                     # dark background
```

The figure shows the applied rate as a step chart (the limit is piecewise-constant between updates), the
mean, any failed updates, and the distribution of the applied values.

---

## G. Shutdown and restoration

### What happens when…

| Event | What the controller does |
|---|---|
| **`Ctrl+C` (SIGINT)** | The signal handler asks the loop to stop, the `finally` block runs the teardown, every undo command is replayed in reverse order, the state is verified against the backup, the result is written to `logs/restoration_report.json` and printed. Exit code 0. |
| **A second `Ctrl+C`** | Cleanup runs immediately instead of waiting for the loop, then the process exits with 130. |
| **`SIGTERM` / `SIGHUP` / `SIGQUIT`** (`kill`, `systemctl stop`, `main.py stop`) | Identical to `Ctrl+C`. |
| **Uncaught exception** | `sys.excepthook` logs the traceback, runs the full teardown, then re-raises. Background-thread exceptions go through `threading.excepthook`. |
| **Normal exit / duration elapsed** | The `atexit` hook runs the same teardown; it is idempotent, so nothing runs twice. |
| **`SIGKILL` / OOM killer / power loss** | Not catchable by *any* process. Every mutating command was journalled to `runtime_backup/rollback.json` **before** it was executed, so `sudo python3 main.py restore` replays the exact undo sequence. `main.py start` refuses to run while a non-empty journal exists. |
| **Machine reboot** | `tc` rules and `iptables` chains live in kernel memory only and are never persisted, so a reboot is itself a complete restoration. Run `sudo python3 main.py restore` afterwards only to clear the stale journal file. |

### Teardown order

1. Remove the ingress filters and the ingress qdisc.
2. Remove the IFB device's qdiscs and delete the device.
3. Delete the root qdisc — which removes every class, leaf qdisc and filter beneath it in one operation.
4. Remove the `POSTROUTING` jump, flush and delete the `RMQNC` chain (v4 and v6).
5. Re-install the original root qdisc if it was a non-default one.
6. Re-read the whole state and compare it, line by line, with the backup taken before the run.

### Manual restoration

```bash
sudo python3 main.py restore            # replay the journal + verify
sudo python3 main.py restore --force    # also sweep for anything left behind
sudo ./scripts/emergency_restore.sh     # pure shell, no Python or config needed
```

If everything else fails, these five commands remove every object this project can create:

```bash
sudo tc qdisc del dev eth0 root
sudo tc qdisc del dev eth0 ingress
sudo iptables -t mangle -D POSTROUTING -o eth0 -j RMQNC
sudo iptables -t mangle -F RMQNC && sudo iptables -t mangle -X RMQNC
sudo ip link del ifb-rmq0
```

`tc qdisc del dev eth0 root` is safe: the kernel immediately reinstates the default queueing discipline
(`pfifo_fast`/`fq_codel`/`mq`).

---

## H. Troubleshooting

### `error: root privileges are required to change traffic control rules`

```bash
sudo python3 main.py start --config config/config.yaml
python3 main.py start --config config/config.yaml --dry-run   # preview without root
```

### `required tool 'tc' was not found in PATH`

`sudo` strips `/sbin` from `PATH` on some systems. The controller already searches `/sbin`, `/usr/sbin` and
`/usr/local/sbin`, so this really means the package is missing:

```bash
sudo apt install -y iproute2 iptables
which tc || ls -l /sbin/tc
sudo env "PATH=$PATH:/sbin:/usr/sbin" python3 main.py start --config config/config.yaml
```

### `network interface 'eth0' does not exist`

Modern distributions use predictable names (`ens33`, `enp0s3`, `wlp2s0`):

```bash
ip -brief link show
ip route show default            # the interface that carries your traffic
sed -i 's/^  interface: .*/  interface: "auto"/' config/config.yaml
sudo python3 main.py start --config config/config.yaml --interface ens33
```

### RabbitMQ traffic is not detected / the limit has no effect

Diagnose in this order:

```bash
# 1. Is traffic really crossing the shaped interface?
ss -ntp | grep 5672                    # look at the peer addresses
ip route get <peer-ip>                 # -> "dev X"  : X is the interface to shape
#    127.0.0.1 => set interface: "lo"
#    172.17.x  => Docker: shape docker0 / br-… instead

# 2. Are the marking rules matching packets?
sudo iptables -t mangle -L RMQNC -v -n # pkts column must be > 0
#    all zeros with fwmark? -> try classification: "u32"

# 3. Is the class receiving packets?
sudo tc -s class show dev eth0 | grep -A3 'class htb 1:10'   # Sent ... must grow

# 4. Is the port right? (TLS uses 5671, clustering 25672)
sudo ss -ltnp | grep beam              # what the broker actually listens on

# 5. Is the limit simply higher than the offered load?
#    A 20 Mbit/s limit does nothing to a workload that only sends 2 Mbit/s.
sudo tc class change dev eth0 parent 1: classid 1:10 htb rate 1mbit ceil 1mbit
```

### Bandwidth changes are not applied

```bash
sudo python3 main.py status                   # "last update ... (N applied, M failed)"
grep -E 'ERROR|failed' logs/controller.log | tail -20
grep ',failed' logs/bandwidth_history.csv | tail
```

Common causes: the root qdisc was deleted by another tool (restart the controller); `max_mbps` exceeds what
`tc` can express on the device; the interface went down (`ip link show dev eth0`).

### `Error: Exclusivity flag on, cannot modify` / `RTNETLINK answers: File exists`

Another qdisc is already installed — usually a previous run that was `SIGKILL`ed, or another shaping tool:

```bash
tc qdisc show dev eth0
sudo python3 main.py restore --force
```

### `another controller instance is already running`

```bash
sudo python3 main.py status
sudo python3 main.py stop
cat runtime_backup/controller.pid       # stale? the lock is released when the process dies
```

### `a previous run did not finish its cleanup`

```bash
sudo python3 main.py restore            # replays runtime_backup/rollback.json
sudo python3 main.py start --config config/config.yaml --force   # or start anyway
```

### Restoration failure

```bash
cat logs/restoration_report.json                 # which check failed and why
sudo python3 main.py restore --force             # sweep everything
sudo ./scripts/emergency_restore.sh eth0         # nuclear option
tc qdisc show dev eth0                           # must show the kernel default
sudo iptables -t mangle -S | grep RMQNC          # must print nothing
```

A `qdisc state` mismatch immediately after a `--force` takeover of a foreign qdisc is expected: the
controller only best-effort re-creates root qdiscs it did not install itself.

### Ingress shaping does nothing

```bash
sudo modprobe ifb act_mirred
ip link show ifb-rmq0                            # must exist while running
tc filter show dev eth0 parent ffff:             # must list the redirect filters
tc -s qdisc show dev ifb-rmq0                    # "Sent" must grow
```

If the modules are unavailable the controller logs a warning and continues with egress-only shaping.

### `iptables` on an nftables host

`iptables-nft` (the default on Debian 11+/Ubuntu 22.04+) is fully supported — the rules simply show up in
`nft list ruleset` as well. If your host has no `iptables` binary at all, set `classification: "u32"`; the
controller also falls back to it automatically.

---

## I. Example workflow

A complete experiment, start to finish.

```bash
# 1. Start RabbitMQ
sudo systemctl start rabbitmq-server
sudo rabbitmqctl status | head -20

# 2. Start the bandwidth controller (terminal 1)
cd ~/rabbitmq-network-controller
sudo python3 main.py start --config config/examples/02_dynamic_20_5.yaml

# 3. Run producers and consumers (terminal 2) - completely unmodified
python3 my_producer.py --messages 100000 --size 64KB &
python3 my_consumer.py &
#    or use the official perf tool:
#    rabbitmq-perf-test --uri amqp://guest:guest@localhost:5672 --producers 2 --consumers 2 --size 65536

# 4. Observe the dynamic bandwidth (terminal 3)
sudo python3 main.py status
watch -n1 'tc -s class show dev eth0 | grep -A3 "class htb 1:10"'
sudo iftop -i eth0 -f 'port 5672'

# 5. Collect the logs
tail -f logs/bandwidth_history.csv
cp logs/dynamic_20_5.csv results/run-$(date +%F-%H%M).csv

# 6. Generate the plots
python3 scripts/plot_bandwidth.py --csv logs/dynamic_20_5.csv \
    --output results/experiment_20_5.png --title "RabbitMQ under 20 ± 5 Mbit/s"

# 7. Stop the controller
#    Ctrl+C in terminal 1, or from anywhere:
sudo python3 main.py stop

# 8. Verify that networking is restored
cat logs/restoration_report.json | python3 -m json.tool | head -20
tc qdisc show dev eth0            # kernel default only
tc class show dev eth0            # empty
sudo iptables -t mangle -S | grep RMQNC || echo "no leftovers"
ip link show ifb-rmq0 2>/dev/null || echo "ifb device removed"
ping -c3 8.8.8.8                  # normal latency
```

Correlate the two data sets afterwards by joining your broker-side throughput metrics with
`logs/bandwidth_history.csv` on the `timestamp` column.

---

## J. Safety notes

* **Root/sudo is required.** Programming `tc` and netfilter needs `CAP_NET_ADMIN`. That is the *only*
  privilege the controller needs — it opens no sockets, contacts no network service and reads no secrets.
* **The application modifies Linux traffic control rules** on exactly one interface (plus one IFB device
  when ingress shaping is enabled). It creates: one root qdisc, two classes, two leaf qdiscs, one to four
  filters, one `iptables` mangle chain with two rules, and one `POSTROUTING` jump scoped to that interface.
* **Only RabbitMQ traffic is shaped.** Everything else lands in the default class at line rate. SSH, git,
  VSCode Remote, unrelated Docker traffic and system updates are never delayed — which also means you cannot
  lock yourself out of a remote host with this tool.
* **Cleanup is automatic on every catchable exit path** and is verified against a snapshot taken before the
  run. The only uncatchable case is `SIGKILL`, which the on-disk rollback journal covers.
* **Always verify after an experiment.** `sudo python3 main.py status`, `tc qdisc show dev <iface>` and
  `logs/restoration_report.json` are the three places to look. If a check ever fails,
  `sudo python3 main.py restore --force` and `sudo ./scripts/emergency_restore.sh` will clean up.
* **Do not run two controllers on one host.** The PID lock prevents it; do not defeat it.
* **Test on a lab machine first.** This is real kernel networking, not a simulation.
* **`--dry-run` prints every command it would execute** and needs no privileges — use it to review exactly
  what will happen before touching a shared machine.

---

## Project structure

```
rabbitmq-network-controller/
├── config/
│   ├── config.yaml                  # working configuration
│   └── examples/                    # six ready-to-run experiment configs + a sample trace
├── controller/
│   ├── config_loader.py             # YAML -> validated, typed dataclasses
│   ├── bandwidth_model.py           # gaussian / uniform / markov / trace / custom generators
│   ├── network_controller.py        # tc + netfilter: build, update, tear down, verify
│   ├── logger.py                    # app log + CSV/JSON bandwidth history
│   ├── cleanup.py                   # signal / atexit / excepthook teardown guarantees
│   ├── status.py                    # state file, PID lock, status rendering
│   └── shell.py                     # safe subprocess wrapper (no shell, ever)
├── logs/                            # controller.log, bandwidth_history.{csv,json}, restoration_report.json
├── results/                         # generated plots
├── runtime_backup/                  # pre-run snapshots, rollback journal, state file, PID file
├── scripts/
│   ├── plot_bandwidth.py            # matplotlib visualisation
│   ├── custom_model_example.py      # example custom bandwidth generator
│   ├── verify_shaping.sh            # print every rule + a verdict
│   └── emergency_restore.sh         # pure-shell last-resort cleanup
├── main.py                          # CLI: start / stop / status / restore
├── example_config.yaml              # fully annotated reference configuration
├── requirements.txt
└── README.md
```

### Runtime state files

| File | Purpose |
|---|---|
| `runtime_backup/backup-<timestamp>/` | Full pre-run snapshot: `tc` qdisc/class/filter output, `iptables-save`/`ip6tables-save` of the mangle table, `nft list ruleset`, `ip link` and the effective configuration. |
| `runtime_backup/latest` | Symlink to the most recent backup. |
| `runtime_backup/rollback.json` | Crash-safe undo journal — written *before* each change is made. |
| `runtime_backup/state.json` | What the controller is doing right now (read by `status`). |
| `runtime_backup/controller.pid` | PID + exclusive `flock`; guarantees a single instance. |

---

## Extending the bandwidth model

Every generator subclasses `BandwidthModel` and is registered by name:

```python
# controller/bandwidth_model.py
@register_model
class SawtoothModel(BandwidthModel):
    name = "sawtooth"

    def setup(self) -> None:
        self.tick = 0

    def sample(self) -> float:
        self.tick += 1
        span = self.config.max_mbps - self.config.min_mbps
        return self.config.min_mbps + span * (self.tick % 60) / 60
```

```yaml
bandwidth:
  distribution: "sawtooth"
```

Clamping to `[min_mbps, max_mbps]`, rounding, logging and error handling are provided by the base class.

Without touching the package at all, point the `custom` model at your own file — see
[`scripts/custom_model_example.py`](scripts/custom_model_example.py) and
[`config/examples/06_custom_model.yaml`](config/examples/06_custom_model.yaml):

```yaml
bandwidth:
  distribution: "custom"
  custom:
    module: "scripts/custom_model_example.py"   # or a dotted import path
    callable: "generate"                        # f(config, rng) -> float | Iterator[float]
```

Already supported out of the box: **trace replay from CSV** (including real network traces and previous
runs), **uniform**, **constant**, **log-normal**, **Markov processes** and **custom Python generators**.

---

## Requirements

* Linux with `iproute2` and (optionally) `iptables`
* Python 3.10 or newer
* `PyYAML` (controller) and `matplotlib` (plots) — `pip install -r requirements.txt`
* root / `sudo` for everything except `--dry-run`, `status` and plotting

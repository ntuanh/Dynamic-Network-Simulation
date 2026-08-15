#!/usr/bin/env python3
"""RabbitMQ Network Controller - command line entry point.

    sudo python3 main.py start --config config/config.yaml
    sudo python3 main.py status
    sudo python3 main.py stop
    sudo python3 main.py restore

The ``start`` command runs in the foreground by default (Ctrl+C restores the
network); pass ``--daemon`` to detach it into the background.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Final, Sequence

from controller import __version__
from controller.bandwidth_model import BandwidthModel, BandwidthModelError, create_model
from controller.cleanup import CleanupManager
from controller.config_loader import AppConfig, ConfigError, PROJECT_ROOT, load_config
from controller.logger import (
    BandwidthHistoryLogger,
    atomic_write_json,
    get_logger,
    iso_timestamp,
    read_csv_history,
    setup_logging,
)
from controller.network_controller import (
    NetworkController,
    NetworkError,
    NetworkSnapshot,
    RestorationReport,
    RollbackJournal,
    detect_default_interface,
)
from controller.shell import CommandError, CommandRunner, ToolNotFoundError, is_root
from controller.status import (
    PHASE_FAILED,
    PHASE_RESTORED,
    PHASE_RUNNING,
    PHASE_STARTING,
    PHASE_STOPPING,
    AppState,
    PidFile,
    PidFileError,
    StateStore,
    process_alive,
    render_status,
    summarize_history,
)

DEFAULT_CONFIG: Final[Path] = PROJECT_ROOT / "config" / "config.yaml"

EXIT_OK: Final[int] = 0
EXIT_ERROR: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_RESTORE_FAILED: Final[int] = 3

#: Consecutive ``tc`` failures tolerated before the controller gives up.
MAX_CONSECUTIVE_FAILURES: Final[int] = 5


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _load(args: argparse.Namespace) -> AppConfig:
    """Load the configuration and apply CLI overrides."""
    config = load_config(args.config)
    if getattr(args, "interface", None):
        config = config.with_interface(args.interface)
    if getattr(args, "duration", None) is not None:
        config = _replace_runtime(config, duration_sec=float(args.duration))
    config.ensure_directories()
    return config


def _replace_runtime(config: AppConfig, **fields: object) -> AppConfig:
    import dataclasses

    return dataclasses.replace(config, runtime=dataclasses.replace(config.runtime, **fields))


def _require_root(dry_run: bool) -> None:
    """Abort unless we can actually program the kernel."""
    if dry_run or is_root():
        return
    raise SystemExit(
        "error: root privileges are required to change traffic control rules.\n"
        "       re-run with sudo, e.g.:  sudo python3 main.py start --config config/config.yaml\n"
        "       (use --dry-run to preview the commands without root)"
    )


def _banner(config: AppConfig, model: BandwidthModel, controller: NetworkController, dry_run: bool) -> str:
    ports = ", ".join(str(port) for port in config.rabbitmq.ports)
    return (
        f"RabbitMQ Network Controller {__version__}\n"
        f"  interface   : {controller.interface}"
        + (f"  (+ ingress via {config.network.ifb_device})" if config.network.shape_ingress else "")
        + "\n"
        f"  rabbitmq    : tcp/{ports}\n"
        f"  shaper      : {config.network.shaper} + {controller.classification} classification\n"
        f"  bandwidth   : {model.describe()}\n"
        f"  interval    : {config.bandwidth.update_interval_sec}s\n"
        f"  duration    : "
        + (f"{config.runtime.duration_sec:g}s" if config.runtime.duration_sec > 0 else "unlimited")
        + "\n"
        f"  logs        : {config.logging.csv_file}\n"
        + ("  MODE        : DRY RUN (no kernel changes)\n" if dry_run else "")
    )


def _write_restoration_report(config: AppConfig, report: RestorationReport) -> None:
    """Persist the restoration outcome next to the other logs."""
    try:
        atomic_write_json(config.logging.restoration_report_file, report.to_dict())
    except OSError as exc:  # pragma: no cover - disk failure
        get_logger("main").error("cannot write the restoration report: %s", exc)


def _daemonize(log_path: Path) -> None:
    """Detach from the terminal (double fork), keeping logs on disk."""
    if os.name != "posix":  # pragma: no cover - Linux target
        raise SystemExit("error: --daemon is only supported on Linux")
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir(str(PROJECT_ROOT))
    os.umask(0o022)
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(handle, sys.stdout.fileno())
    os.dup2(handle, sys.stderr.fileno())


def _resolve_interface(config: AppConfig, runner: CommandRunner, state: AppState | None) -> str:
    """Interface to operate on: state file first, then config, then autodetect."""
    if state and state.interface:
        return state.interface
    if config.network.interface != "auto":
        return config.network.interface
    try:
        return detect_default_interface(runner)
    except NetworkError:
        return "eth0"


# --------------------------------------------------------------------------- #
# start
# --------------------------------------------------------------------------- #
def cmd_start(args: argparse.Namespace) -> int:
    config = _load(args)
    logger = setup_logging(config.logging, verbose=args.verbose, quiet=args.quiet)
    for warning in config.warnings:
        logger.warning("config: %s", warning)

    _require_root(args.dry_run)

    if args.daemon:
        _daemonize(config.logging.app_log_file.with_suffix(".daemon.out"))
        logger = setup_logging(config.logging, verbose=args.verbose, quiet=True)

    runner = CommandRunner(dry_run=args.dry_run, logger=get_logger("shell"))
    journal = RollbackJournal.load(config.runtime.journal_file, logger=get_logger("journal"))
    state_store = StateStore(config.runtime.state_file)
    pid_file = PidFile(config.runtime.pid_file)

    if len(journal) and not args.force:
        logger.error(
            "the rollback journal %s still holds %d undo command(s) from a previous run",
            config.runtime.journal_file,
            len(journal),
        )
        print(
            "error: a previous run did not finish its cleanup.\n"
            "       restore the network first:  sudo python3 main.py restore\n"
            "       or start anyway with --force (the old journal will be replayed on exit)",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        pid_file.acquire()
    except PidFileError as exc:
        logger.error("%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    cleanup = CleanupManager(logger=get_logger("cleanup"))
    controller = NetworkController(config, runner, journal, logger=get_logger("network"))
    history = BandwidthHistoryLogger(config.logging)

    # LIFO: teardown runs first, then the history logger is closed, then the lock.
    cleanup.register("release pid file", lambda _reason: pid_file.release())
    cleanup.register("close history logger", lambda _reason: history.close())

    def _teardown(reason: str) -> None:
        state_store.update(phase=PHASE_STOPPING)
        report = controller.teardown(reason=reason)
        _write_restoration_report(config, report)
        history.log(
            bandwidth_mbps=controller.current_mbps or 0.0,
            interface=controller.interface,
            status="shutdown",
            model=config.bandwidth.distribution,
            error="" if report.success else "; ".join(report.errors[:3]),
        )
        state_store.update(
            phase=PHASE_RESTORED if report.success else PHASE_FAILED,
            restoration=report.to_dict(),
            current_bandwidth_mbps=None,
        )
        logger.info("%s", report.render())
        if not report.success:
            logger.error("NETWORK RESTORATION FAILED - run: sudo python3 main.py restore --force")

    cleanup.register("restore network", _teardown, critical=True)
    cleanup.install()

    try:
        warnings = controller.preflight(force=args.force)
        for warning in warnings:
            logger.warning("preflight: %s", warning)

        model = create_model(config.bandwidth, config.runtime.random_seed)
        backup_dir = controller.save_backup()  # snapshots the state captured by preflight()

        state = AppState(
            pid=os.getpid(),
            phase=PHASE_STARTING,
            started_at=iso_timestamp(),
            started_epoch=time.time(),
            config_path=str(config.source_path or args.config),
            interface=controller.interface,
            ifb_device=config.network.ifb_device,
            shaper=config.network.shaper,
            classification=controller.classification,
            shape_ingress=config.network.shape_ingress,
            ports=list(config.rabbitmq.ports),
            model=model.describe(),
            duration_sec=config.runtime.duration_sec,
            update_interval_sec=config.bandwidth.update_interval_sec,
            dry_run=args.dry_run,
            backup_dir=str(backup_dir),
            journal_file=str(config.runtime.journal_file),
        )
        state_store.save(state)

        if not args.quiet:
            print(_banner(config, model, controller, args.dry_run), flush=True)

        initial = model.next_value()
        controller.setup(initial)
        history.log(
            bandwidth_mbps=initial,
            interface=controller.interface,
            status="dry-run" if args.dry_run else "applied",
            update_index=1,
            model=config.bandwidth.distribution,
            direction="both" if controller.ingress_active else "egress",
        )
        state.phase = PHASE_RUNNING
        state.current_bandwidth_mbps = initial
        state.last_update_at = iso_timestamp()
        state.updates_applied = 1
        state_store.save(state)

        return _run_loop(config, controller, model, history, state, state_store, cleanup, args)

    except (ConfigError, NetworkError, ToolNotFoundError, CommandError) as exc:
        logger.error("%s", exc)
        print(f"error: {exc}", file=sys.stderr)
        state_store.update(phase=PHASE_FAILED)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.warning("interrupted during startup")
        return 130
    finally:
        cleanup.run(cleanup.stop_reason or "exit")


def _run_loop(
    config: AppConfig,
    controller: NetworkController,
    model: BandwidthModel,
    history: BandwidthHistoryLogger,
    state: AppState,
    state_store: StateStore,
    cleanup: CleanupManager,
    args: argparse.Namespace,
) -> int:
    """Generate, apply and log bandwidth values until the duration elapses."""
    logger = get_logger("loop")
    interval = config.bandwidth.update_interval_sec
    duration = config.runtime.duration_sec
    started = time.monotonic()
    deadline = started + duration if duration and duration > 0 else None
    next_tick = started
    index = 1
    consecutive_failures = 0

    while not cleanup.should_stop():
        next_tick += interval
        sleep_for = max(0.0, next_tick - time.monotonic())
        if deadline is not None and time.monotonic() + sleep_for >= deadline:
            remaining = max(0.0, deadline - time.monotonic())
            cleanup.wait(remaining)
            logger.info("configured duration of %.0fs elapsed", duration)
            cleanup.request_stop("duration-elapsed")
            break
        if cleanup.wait(sleep_for):
            break

        index += 1
        try:
            value = model.next_value()
        except BandwidthModelError as exc:
            logger.error("bandwidth model stopped: %s", exc)
            cleanup.request_stop("model-exhausted")
            break

        began = time.perf_counter()
        try:
            controller.apply_bandwidth(value)
            elapsed_ms = (time.perf_counter() - began) * 1000.0
            consecutive_failures = 0
            record = history.log(
                bandwidth_mbps=value,
                interface=controller.interface,
                status="dry-run" if args.dry_run else "applied",
                update_index=index,
                model=config.bandwidth.distribution,
                direction="both" if controller.ingress_active else "egress",
                apply_duration_ms=elapsed_ms,
            )
            state.current_bandwidth_mbps = value
            state.updates_applied += 1
            state.last_update_at = record.timestamp
            logger.info("bandwidth #%d -> %.3f Mbit/s (%.1f ms)", index, value, elapsed_ms)
        except (CommandError, NetworkError, ToolNotFoundError) as exc:
            consecutive_failures += 1
            message = str(exc).splitlines()[0]
            logger.error("failed to apply %.3f Mbit/s: %s", value, message)
            history.log(
                bandwidth_mbps=value,
                interface=controller.interface,
                status="failed",
                update_index=index,
                model=config.bandwidth.distribution,
                apply_duration_ms=(time.perf_counter() - began) * 1000.0,
                error=message,
            )
            state.update_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                logger.critical(
                    "%d consecutive failures - stopping and restoring the network",
                    consecutive_failures,
                )
                cleanup.request_stop("too-many-failures")
                state_store.save(state)
                return EXIT_ERROR
        state_store.save(state)

    summary = history.summary()
    if summary.get("count"):
        logger.info(
            "applied %d bandwidth updates (min %.2f / mean %.2f / max %.2f Mbit/s, %d failures)",
            summary["count"],
            summary["min_mbps"],
            summary["mean_mbps"],
            summary["max_mbps"],
            summary.get("failures", 0),
        )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# stop
# --------------------------------------------------------------------------- #
def cmd_stop(args: argparse.Namespace) -> int:
    config = _load(args)
    logger = setup_logging(config.logging, verbose=args.verbose, quiet=args.quiet)
    state_store = StateStore(config.runtime.state_file)
    state = state_store.load()
    pid = state.pid if state else PidFile.read(config.runtime.pid_file)

    if not pid or not process_alive(pid):
        print("no running controller found.")
        journal = RollbackJournal.load(config.runtime.journal_file)
        if len(journal):
            print(
                f"the rollback journal still holds {len(journal)} undo command(s) - restoring now...",
                flush=True,
            )
            return cmd_restore(args)
        return EXIT_OK

    _require_root(False)
    logger.info("sending SIGTERM to controller pid %d", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"error: cannot signal pid {pid}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            break
        time.sleep(0.2)

    if process_alive(pid):
        if not args.force:
            print(
                f"error: pid {pid} is still running after {args.timeout:g}s.\n"
                f"       retry with --force to send SIGKILL and restore the network afterwards.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        logger.warning("pid %d did not exit; sending SIGKILL", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:  # pragma: no cover - race
            pass
        time.sleep(0.5)
        print("controller killed; replaying the rollback journal...", flush=True)
        return cmd_restore(args)

    state = state_store.load()
    restoration = (state.restoration if state else None) or {}
    if restoration.get("success"):
        print(f"controller stopped; network restored at {restoration.get('finished_at', '?')}.")
        return EXIT_OK
    print("controller stopped, but restoration was not confirmed - verifying...", flush=True)
    return cmd_restore(args)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    setup_logging(config.logging, verbose=args.verbose, quiet=True)
    state_store = StateStore(config.runtime.state_file)
    state = state_store.load()
    alive = bool(state and process_alive(state.pid))

    runner = CommandRunner(dry_run=False, logger=get_logger("shell"))
    interface = _resolve_interface(config, runner, state)
    journal = RollbackJournal.load(config.runtime.journal_file)
    controller = NetworkController(config.with_interface(interface), runner, journal)
    rules = None if args.no_rules else controller.describe_rules()

    if args.json:
        payload = {
            "version": __version__,
            "generated_at": iso_timestamp(),
            "running": alive,
            "state": state.to_dict() if state else None,
            "uptime_sec": round(state.uptime_sec, 1) if (state and alive) else 0.0,
            "pending_undo_commands": len(journal),
            "history": summarize_history(read_csv_history(config.logging.csv_file, limit=None)),
            "rules": rules,
        }
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK

    print(
        render_status(
            state,
            alive=alive,
            rules=rules,
            history_path=config.logging.csv_file,
            history_rows=args.history,
            journal_ops=len(journal),
        )
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def cmd_restore(args: argparse.Namespace) -> int:
    config = _load(args)
    logger = setup_logging(config.logging, verbose=args.verbose, quiet=args.quiet)
    _require_root(getattr(args, "dry_run", False))

    state_store = StateStore(config.runtime.state_file)
    state = state_store.load()
    if state and process_alive(state.pid) and not args.force:
        print(
            f"error: controller pid {state.pid} is still running.\n"
            f"       stop it first:  sudo python3 main.py stop\n"
            f"       or force the restore with --force (the running process will be left orphaned)",
            file=sys.stderr,
        )
        return EXIT_ERROR

    runner = CommandRunner(dry_run=getattr(args, "dry_run", False), logger=get_logger("shell"))
    interface = _resolve_interface(config, runner, state)
    journal = RollbackJournal.load(config.runtime.journal_file, logger=get_logger("journal"))
    controller = NetworkController(config.with_interface(interface), runner, journal, get_logger("network"))
    controller.interface = interface

    backup_dir = Path(args.backup) if args.backup else config.runtime.backup_dir / "latest"
    controller.baseline = NetworkSnapshot.load(backup_dir)
    if controller.baseline is None:
        logger.warning("no baseline snapshot found in %s; verification will be limited", backup_dir)
    else:
        logger.info("using baseline snapshot from %s", backup_dir)

    print(f"restoring network state on {interface} ({len(journal)} undo command(s))...", flush=True)
    report = controller.teardown(reason=f"manual-restore:{args.reason}" if args.reason else "manual-restore")

    if args.force:
        sweep_errors = controller.sweep()
        if sweep_errors:
            logger.warning("sweep reported: %s", "; ".join(sweep_errors))
        report.checks = controller.verify_restoration()
        report.verified = True
        report.errors = [error for error in report.errors if "already" not in error.lower()]
        if report.success:
            journal.clear()

    _write_restoration_report(config, report)
    if state is not None:
        state_store.update(
            phase=PHASE_RESTORED if report.success else PHASE_FAILED,
            restoration=report.to_dict(),
            current_bandwidth_mbps=None,
        )

    print(report.render())
    if report.success:
        print("\nnetwork restored - the host behaves exactly as before the controller started.")
        return EXIT_OK
    print(
        "\nrestoration did NOT fully succeed. Last resort commands:\n"
        f"  sudo tc qdisc del dev {interface} root\n"
        f"  sudo tc qdisc del dev {interface} ingress\n"
        f"  sudo iptables -t mangle -D POSTROUTING -o {interface} -j RMQNC\n"
        "  sudo iptables -t mangle -F RMQNC && sudo iptables -t mangle -X RMQNC\n"
        f"  sudo ip link del {config.network.ifb_device}",
        file=sys.stderr,
    )
    return EXIT_RESTORE_FAILED


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Dynamically shape the real network bandwidth available to RabbitMQ traffic.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  sudo python3 main.py start --config config/config.yaml\n"
            "  sudo python3 main.py start --config config/config.yaml --daemon\n"
            "  sudo python3 main.py status\n"
            "  sudo python3 main.py stop\n"
            "  sudo python3 main.py restore --force\n"
            "  python3 main.py start --dry-run          # preview, no root needed\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"rabbitmq-network-controller {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c", "--config", default=str(DEFAULT_CONFIG), help="path to the YAML configuration file"
    )
    common.add_argument("-v", "--verbose", action="store_true", help="debug level logging")
    common.add_argument("-q", "--quiet", action="store_true", help="suppress console logging")

    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", parents=[common], help="install shaping and start updating it")
    start.add_argument("--interface", help="override network.interface")
    start.add_argument("--duration", type=float, help="override runtime.duration_sec (0 = unlimited)")
    start.add_argument("--dry-run", action="store_true", help="print the commands without executing them")
    start.add_argument("--daemon", action="store_true", help="detach and run in the background")
    start.add_argument(
        "--force",
        action="store_true",
        help="take over an existing root qdisc and ignore a non-empty rollback journal",
    )
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop", parents=[common], help="stop the controller and restore the network")
    stop.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for a graceful exit")
    stop.add_argument("--force", action="store_true", help="SIGKILL after the timeout, then restore")
    stop.add_argument("--backup", help=argparse.SUPPRESS)
    stop.add_argument("--reason", default="stop-command", help=argparse.SUPPRESS)
    stop.set_defaults(func=cmd_stop)

    status = subparsers.add_parser("status", parents=[common], help="show bandwidth, rules, uptime and state")
    status.add_argument("--json", action="store_true", help="machine readable output")
    status.add_argument("--no-rules", action="store_true", help="skip the live tc/iptables dump")
    status.add_argument("--history", type=int, default=5, help="number of recent samples to display")
    status.set_defaults(func=cmd_status)

    restore = subparsers.add_parser(
        "restore", parents=[common], help="replay the rollback journal and verify the network state"
    )
    restore.add_argument("--backup", help="baseline snapshot directory (default: runtime_backup/latest)")
    restore.add_argument(
        "--force",
        action="store_true",
        help="also sweep for leftover rules and ignore a running controller",
    )
    restore.add_argument("--dry-run", action="store_true", help="print the commands without executing them")
    restore.add_argument("--reason", default="", help=argparse.SUPPRESS)
    restore.set_defaults(func=cmd_restore)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ToolNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except PidFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

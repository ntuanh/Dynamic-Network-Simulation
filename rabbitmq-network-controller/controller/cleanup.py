"""Guaranteed cleanup on every exit path.

The manager makes sure the registered teardown callbacks run exactly once, no
matter how the process ends:

======================  ===================================================
exit path               mechanism
======================  ===================================================
``Ctrl+C``              ``SIGINT`` handler -> graceful stop -> ``finally``
``kill``/``systemctl``  ``SIGTERM``/``SIGHUP``/``SIGQUIT`` handler
``KeyboardInterrupt``   caught in the run loop, ``finally`` block
uncaught exception      ``sys.excepthook`` and ``threading.excepthook``
normal exit             ``atexit``
second ``Ctrl+C``       immediate forced cleanup, then exit code 130
``SIGKILL`` / crash     not catchable: replay ``runtime_backup/rollback.json``
                        with ``sudo python main.py restore``
======================  ===================================================

The only uncatchable case is ``SIGKILL`` (or a power loss).  That is why every
mutating command is journalled to disk *before* it is executed -- ``main.py
restore`` replays the journal and brings the host back to its original state.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from types import FrameType, TracebackType
from typing import Callable, Final, Sequence

from .logger import get_logger

#: Signals we install handlers for (a subset may be unavailable on some kernels).
HANDLED_SIGNALS: Final[tuple[str, ...]] = ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")

CleanupCallback = Callable[[str], None]


@dataclass(frozen=True)
class CleanupTask:
    """A named teardown callback."""

    name: str
    callback: CleanupCallback
    critical: bool = False


@dataclass
class CleanupManager:
    """Run teardown callbacks exactly once, on every exit path."""

    logger: logging.Logger = field(default_factory=lambda: get_logger("cleanup"))
    tasks: list[CleanupTask] = field(default_factory=list)
    stop_reason: str = ""
    exit_code: int = 0
    #: Granularity of :meth:`wait`; also the worst-case shutdown latency.
    poll_interval_sec: float = 0.05
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _stop: bool = field(default=False, repr=False)
    _done: bool = field(default=False, repr=False)
    _in_progress: bool = field(default=False, repr=False)
    _installed: bool = field(default=False, repr=False)
    _previous_handlers: dict[int, object] = field(default_factory=dict, repr=False)
    _signal_count: int = field(default=0, repr=False)

    # --------------------------------------------------------------- register
    def register(self, name: str, callback: CleanupCallback, *, critical: bool = False) -> None:
        """Register a teardown callback.

        Callbacks run in reverse registration order (LIFO), receive the shutdown
        reason as their single argument, and must never raise -- exceptions are
        caught and logged so that later callbacks still run.
        """
        with self._lock:
            self.tasks.append(CleanupTask(name=name, callback=callback, critical=critical))

    # ---------------------------------------------------------------- install
    def install(self) -> None:
        """Install signal handlers, ``atexit`` and exception hooks."""
        with self._lock:
            if self._installed:
                return
            self._installed = True

        for name in HANDLED_SIGNALS:
            signum = getattr(signal, name, None)
            if signum is None:  # pragma: no cover - platform dependent
                continue
            try:
                self._previous_handlers[int(signum)] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (ValueError, OSError) as exc:  # pragma: no cover - non-main thread
                self.logger.debug("cannot install handler for %s: %s", name, exc)

        atexit.register(self._handle_atexit)
        sys.excepthook = self._handle_exception
        threading.excepthook = self._handle_thread_exception  # type: ignore[assignment]
        self.logger.debug("cleanup handlers installed for %s", ", ".join(HANDLED_SIGNALS))

    # --------------------------------------------------------------- handlers
    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        # Signal handlers must not block: only plain attribute writes happen here
        # (atomic under the GIL), never a lock acquisition on a data structure the
        # interrupted main-thread code could already hold.
        name = signal.Signals(signum).name
        self._signal_count += 1
        if self._signal_count == 1:
            self.logger.warning("received %s - shutting down and restoring the network...", name)
            self.exit_code = 130 if name == "SIGINT" else 143
            self.request_stop(f"signal:{name}")
            return
        if self._in_progress:
            self.logger.warning(
                "received %s again, but the network is still being restored - "
                "please wait (this takes a few seconds at most)",
                name,
            )
            return
        # Second signal outside the teardown: the user is impatient, clean up now.
        self.logger.warning("received %s again - forcing immediate cleanup", name)
        self.run(f"signal:{name}:forced")
        os._exit(130)

    def _handle_atexit(self) -> None:
        self.run("atexit")

    def _handle_exception(
        self,
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            self.logger.warning("KeyboardInterrupt - restoring the network...")
            self.run("keyboard-interrupt")
            self.exit_code = 130
            return
        self.logger.critical(
            "uncaught %s: %s\n%s",
            exc_type.__name__,
            exc_value,
            "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
        )
        self.run(f"exception:{exc_type.__name__}")
        self.exit_code = 1
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    def _handle_thread_exception(self, args: threading.ExceptHookArgs) -> None:  # pragma: no cover
        self.logger.critical(
            "uncaught exception in thread %s: %s",
            getattr(args.thread, "name", "?"),
            args.exc_value,
        )
        self.run(f"thread-exception:{args.exc_type.__name__}")

    # ------------------------------------------------------------------- stop
    def request_stop(self, reason: str) -> None:
        """Ask the main loop to finish; cleanup happens in its ``finally``."""
        if not self.stop_reason:
            self.stop_reason = reason
        self._stop = True

    def should_stop(self) -> bool:
        """True once a shutdown has been requested."""
        return self._stop

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; returns True if a stop was requested.

        Implemented as short polling slices rather than a lock-based primitive so
        that the signal handler never has to touch a lock (see :meth:`_handle_signal`).
        """
        deadline = time.monotonic() + timeout
        while not self._stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(self.poll_interval_sec, remaining))
        return True

    # -------------------------------------------------------------------- run
    def run(self, reason: str = "shutdown") -> bool:
        """Execute every registered callback once (LIFO).  Returns success."""
        with self._lock:
            if self._done:
                return True
            self._done = True
            self._in_progress = True
            tasks = list(reversed(self.tasks))

        self.logger.info("running cleanup (%d task(s), reason=%s)", len(tasks), reason)
        ok = True
        try:
            for task in tasks:
                try:
                    task.callback(reason)
                except BaseException as exc:  # noqa: BLE001 - cleanup must never abort
                    ok = False
                    self.logger.error("cleanup task %r failed: %s", task.name, exc, exc_info=True)
                    if task.critical:
                        self.logger.critical(
                            "CRITICAL cleanup task %r failed - the network may still be shaped. "
                            "Run: sudo python3 main.py restore --force",
                            task.name,
                        )
        finally:
            self._in_progress = False
        return ok

    @property
    def completed(self) -> bool:
        """True once :meth:`run` has executed."""
        return self._done

    def restore_default_handlers(self) -> None:
        """Reinstall the signal handlers that were active before :meth:`install`."""
        for signum, handler in self._previous_handlers.items():
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except (ValueError, OSError, TypeError):  # pragma: no cover - defensive
                pass


def describe_exit_paths() -> Sequence[str]:
    """Documentation helper used by ``main.py status``."""
    return (
        "SIGINT (Ctrl+C)       -> graceful stop, rules removed, state verified",
        "SIGTERM / SIGHUP      -> graceful stop, rules removed, state verified",
        "uncaught exception    -> cleanup then re-raise",
        "normal exit           -> cleanup via atexit",
        "SIGKILL / power loss  -> run 'sudo python main.py restore'",
    )

"""Safe subprocess execution helpers.

Every external command executed by this project goes through :class:`CommandRunner`.
The runner guarantees that:

* commands are always passed as argument *lists* (never a shell string), so no
  shell interpolation / injection is possible;
* every command is logged before execution and its result logged afterwards;
* failures raise a rich :class:`CommandError` carrying stdout/stderr;
* "expected" failures (e.g. deleting a qdisc that does not exist) can be
  tolerated through the ``ignore`` argument;
* ``--dry-run`` mode skips all *mutating* commands while still allowing
  read-only introspection commands to run.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Final, Mapping, Sequence

LOGGER_NAME: Final[str] = "rmqnc.shell"

#: Directories appended to ``PATH`` when looking for tools.  ``sudo`` frequently
#: sanitises ``PATH`` and drops ``/sbin`` where ``tc``/``iptables`` live.
_EXTRA_PATH_DIRS: Final[tuple[str, ...]] = (
    "/sbin",
    "/usr/sbin",
    "/usr/local/sbin",
    "/bin",
    "/usr/bin",
    "/usr/local/bin",
)


class ToolNotFoundError(RuntimeError):
    """Raised when a required external binary cannot be located."""

    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__(
            f"required tool {tool!r} was not found in PATH "
            f"(searched: {os.environ.get('PATH', '')}:{':'.join(_EXTRA_PATH_DIRS)}). "
            f"On Debian/Ubuntu install it with: sudo apt install -y iproute2 iptables"
        )


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a single external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_sec: float
    skipped: bool = False
    tolerated: bool = False

    @property
    def ok(self) -> bool:
        """True when the command succeeded or its failure was tolerated."""
        return self.returncode == 0 or self.tolerated

    @property
    def cmdline(self) -> str:
        """Human readable, copy-pasteable representation of the command."""
        return " ".join(shlex.quote(part) for part in self.argv)

    @property
    def output(self) -> str:
        """stdout if non-empty, otherwise stderr."""
        return self.stdout if self.stdout.strip() else self.stderr


class CommandError(RuntimeError):
    """Raised when an external command fails and the failure is not tolerated."""

    def __init__(self, result: CommandResult, hint: str | None = None) -> None:
        self.result = result
        self.hint = hint
        message = (
            f"command failed with exit code {result.returncode}: {result.cmdline}\n"
            f"  stdout: {result.stdout.strip() or '<empty>'}\n"
            f"  stderr: {result.stderr.strip() or '<empty>'}"
        )
        if hint:
            message += f"\n  hint: {hint}"
        super().__init__(message)


@dataclass
class CommandRunner:
    """Execute external commands with logging, timeouts and dry-run support."""

    dry_run: bool = False
    default_timeout: float = 20.0
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger(LOGGER_NAME))
    _which_cache: dict[str, str | None] = field(default_factory=dict, repr=False)
    history: list[CommandResult] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ tools
    def which(self, tool: str) -> str | None:
        """Locate ``tool``, searching sudo-sanitised paths as well."""
        if tool in self._which_cache:
            return self._which_cache[tool]
        path = shutil.which(tool)
        if path is None:
            search_path = os.pathsep.join(_EXTRA_PATH_DIRS)
            path = shutil.which(tool, path=search_path)
        self._which_cache[tool] = path
        return path

    def require(self, tool: str) -> str:
        """Return the absolute path of ``tool`` or raise :class:`ToolNotFoundError`."""
        path = self.which(tool)
        if path is None:
            raise ToolNotFoundError(tool)
        return path

    def has(self, tool: str) -> bool:
        """True when ``tool`` is available on this host."""
        return self.which(tool) is not None

    # -------------------------------------------------------------- execution
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        read_only: bool = False,
        ignore: Sequence[str] = (),
        timeout: float | None = None,
        input_text: str | None = None,
        hint: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run ``argv`` and return a :class:`CommandResult`.

        Args:
            argv: command and arguments; ``argv[0]`` is resolved through ``PATH``.
            check: raise :class:`CommandError` when the command fails.
            read_only: the command does not mutate system state, therefore it is
                executed even in dry-run mode.
            ignore: substrings; when the command fails and any substring appears
                (case-insensitively) in stderr, the failure is tolerated.
            timeout: seconds before the command is killed.
            input_text: text piped to the command's stdin.
            hint: extra troubleshooting text attached to raised errors.
            env: extra environment variables merged over ``os.environ``.
        """
        argv = tuple(str(part) for part in argv)
        if not argv:
            raise ValueError("argv must not be empty")

        binary = self.which(argv[0])
        if binary is None:
            if self.dry_run or read_only:
                result = CommandResult(
                    argv=argv,
                    returncode=127,
                    stdout="",
                    stderr=f"{argv[0]}: not found",
                    duration_sec=0.0,
                    skipped=True,
                    tolerated=True,
                )
                self.history.append(result)
                self.logger.debug("SKIP (tool missing) %s", result.cmdline)
                return result
            raise ToolNotFoundError(argv[0])

        if self.dry_run and not read_only:
            result = CommandResult(argv=argv, returncode=0, stdout="", stderr="", duration_sec=0.0, skipped=True)
            self.history.append(result)
            self.logger.info("DRY-RUN would execute: %s", result.cmdline)
            return result

        full_env = dict(os.environ)
        full_env.setdefault("LC_ALL", "C")
        full_env["PATH"] = os.pathsep.join([full_env.get("PATH", ""), *_EXTRA_PATH_DIRS])
        if env:
            full_env.update(env)

        self.logger.debug("exec: %s", " ".join(shlex.quote(p) for p in argv))
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, never shell=True
                (binary, *argv[1:]),
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else self.default_timeout,
                input=input_text,
                env=full_env,
                check=False,
            )
            stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - timing dependent
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            stderr = f"{stderr}\ntimeout after {exc.timeout}s".strip()
            returncode = 124
        duration = time.monotonic() - started

        tolerated = returncode != 0 and any(pattern.lower() in stderr.lower() for pattern in ignore)
        result = CommandResult(
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_sec=duration,
            tolerated=tolerated,
        )
        self.history.append(result)

        if returncode == 0:
            self.logger.debug("ok (%.1f ms): %s", duration * 1000, result.cmdline)
        elif tolerated:
            self.logger.debug("tolerated failure: %s -> %s", result.cmdline, stderr.strip())
        else:
            self.logger.warning("failed (%d): %s -> %s", returncode, result.cmdline, stderr.strip())
            if check:
                raise CommandError(result, hint=hint)
        return result

    def capture(self, argv: Sequence[str], *, timeout: float | None = None) -> str:
        """Run a read-only command and return stdout (never raises)."""
        try:
            result = self.run(argv, check=False, read_only=True, timeout=timeout)
        except ToolNotFoundError as exc:  # pragma: no cover - defensive
            return f"<unavailable: {exc.tool} not found>"
        if result.skipped:
            return f"<unavailable: {argv[0]} not found>"
        if result.returncode != 0:
            return f"<error rc={result.returncode}: {result.stderr.strip()}>"
        return result.stdout


def is_root() -> bool:
    """True when the current process has effective UID 0."""
    return hasattr(os, "geteuid") and os.geteuid() == 0  # type: ignore[attr-defined]

"""Safe, auditable execution of external command-line tools.

The runner in this module is deliberately independent of any CFD package.  It
always launches an argument vector with ``shell=False`` and writes one stdout
log, one stderr log, and one JSON metadata record for every attempted run.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import math
import os
import posixpath
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, IO, Mapping, Sequence, TextIO


PathLike = str | os.PathLike[str]
EnvironmentOverlay = Mapping[str, str | os.PathLike[str] | None]
ResourceAbortCheck = Callable[[], str | None]


_MNT_C_EXE_RE = re.compile(r"^/mnt/c(?:/|$).*\.exe$", re.IGNORECASE)
_TERMINATION_GRACE_SECONDS = 0.5
_READER_JOIN_SECONDS = 1.0
_READER_CLOSE_JOIN_SECONDS = 0.25
_RESOURCE_ABORT_CHECK_INTERVAL_SECONDS = 0.5
_CTRL_BREAK_EVENT = getattr(signal, "CTRL_BREAK_EVENT", 1)


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp with an explicit ``Z`` suffix."""

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_status_hex(returncode: int | None) -> str | None:
    """Return one stable 32-bit hexadecimal spelling of an exit status.

    Windows ``NTSTATUS`` values can be exposed as either signed or unsigned
    integers depending on the calling layer.  Masking to 32 bits preserves the
    underlying status bits while keeping ordinary POSIX and small exit codes
    deterministic as well.
    """

    if returncode is None:
        return None
    return f"0x{int(returncode) & 0xFFFFFFFF:08X}"


@dataclass(frozen=True)
class _TerminationOutcome:
    """Bounded graceful/hard-kill evidence returned by process termination."""

    graceful_termination_attempted: bool = False
    graceful_termination_succeeded: bool = False
    hard_kill_attempted: bool = False
    hard_kill_succeeded: bool = False


def _merge_termination_outcomes(
    first: _TerminationOutcome, second: _TerminationOutcome
) -> _TerminationOutcome:
    """Combine evidence when cleanup is invoked more than once."""

    return _TerminationOutcome(
        graceful_termination_attempted=(
            first.graceful_termination_attempted
            or second.graceful_termination_attempted
        ),
        graceful_termination_succeeded=(
            first.graceful_termination_succeeded
            or second.graceful_termination_succeeded
        ),
        hard_kill_attempted=(
            first.hard_kill_attempted or second.hard_kill_attempted
        ),
        hard_kill_succeeded=(
            first.hard_kill_succeeded or second.hard_kill_succeeded
        ),
    )


def _termination_method(
    trigger: str | None, outcome: _TerminationOutcome
) -> str:
    """Encode both the termination trigger and strongest attempted method."""

    if trigger is None:
        return "none"
    if outcome.hard_kill_attempted:
        return f"{trigger}_hard_kill"
    if outcome.graceful_termination_attempted:
        return f"{trigger}_graceful"
    return f"{trigger}_already_exited"


def _canonical_mnt_c_executable(path: str) -> str | None:
    """Recognise WSL C-drive executables after lexical normalization."""

    slash_path = path.replace("\\", "/")
    if not slash_path.startswith("/"):
        return None
    # Collapse duplicate leading slashes as well as embedded ``.`` and ``..``
    # components so policy checks cannot be bypassed lexically.
    canonical = posixpath.normpath("/" + slash_path.lstrip("/"))
    if _MNT_C_EXE_RE.match(canonical):
        return canonical
    return None


@dataclass(frozen=True)
class CommandResult:
    """Complete result and audit paths for one command invocation."""

    executable: str
    args: tuple[str, ...]
    cwd: str
    start_time: str
    end_time: str
    returncode: int | None
    stdout: str
    stderr: str
    stdout_log: Path
    stderr_log: Path
    metadata_log: Path
    dry_run: bool
    timed_out: bool
    interrupted: bool = False
    resource_aborted: bool = False
    resource_abort_reason: str | None = None
    output_complete: bool = True
    previous_logs_archive: Path | None = None
    runner_error: str | None = None
    termination_method: str = "none"
    graceful_termination_attempted: bool = False
    graceful_termination_succeeded: bool = False
    hard_kill_attempted: bool = False
    hard_kill_succeeded: bool = False
    status_hex: str | None = field(init=False)

    def __post_init__(self) -> None:
        """Derive the normalized status from the preserved raw return code."""

        object.__setattr__(
            self, "status_hex", _normalized_status_hex(self.returncode)
        )

    @property
    def command(self) -> tuple[str, ...]:
        """The exact argument vector passed (or that would be passed)."""

        return (self.executable, *self.args)

    @property
    def ok(self) -> bool:
        """Whether the command completed successfully."""

        return (
            self.returncode == 0
            and not self.timed_out
            and not self.interrupted
            and not self.resource_aborted
            and self.output_complete
        )

    @property
    def started_at(self) -> str:
        """Compatibility alias for :attr:`start_time`."""

        return self.start_time

    @property
    def ended_at(self) -> str:
        """Compatibility alias for :attr:`end_time`."""

        return self.end_time

    def as_metadata(self) -> dict[str, object]:
        """Return the JSON-serialisable audit record for this result."""

        return {
            "executable": self.executable,
            "args": list(self.args),
            "command": list(self.command),
            "cwd": self.cwd,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "returncode": self.returncode,
            "status_hex": self.status_hex,
            "stdout_log": str(self.stdout_log),
            "stderr_log": str(self.stderr_log),
            "dry_run": self.dry_run,
            "timed_out": self.timed_out,
            "interrupted": self.interrupted,
            "resource_aborted": self.resource_aborted,
            "resource_abort_reason": self.resource_abort_reason,
            "termination_method": self.termination_method,
            "graceful_termination_attempted": (
                self.graceful_termination_attempted
            ),
            "graceful_termination_succeeded": (
                self.graceful_termination_succeeded
            ),
            "hard_kill_attempted": self.hard_kill_attempted,
            "hard_kill_succeeded": self.hard_kill_succeeded,
            "output_complete": self.output_complete,
            "previous_logs_archive": (
                None
                if self.previous_logs_archive is None
                else str(self.previous_logs_archive)
            ),
            "runner_error": self.runner_error,
        }


class CommandExecutionError(RuntimeError):
    """Raised when a command cannot start or exits unsuccessfully."""

    def __init__(self, result: CommandResult, message: str | None = None) -> None:
        self.result = result
        self.stderr = result.stderr
        self.returncode = result.returncode
        if message is None:
            message = (
                f"Command failed with return code {result.returncode}: "
                f"{result.executable}"
            )
        if result.stderr:
            message = f"{message}\nstderr:\n{result.stderr}"
        super().__init__(message)


class CommandTimeoutError(CommandExecutionError, TimeoutError):
    """Raised when a command exceeds its configured timeout."""

    def __init__(self, result: CommandResult, timeout: float | None) -> None:
        self.timeout = timeout
        super().__init__(
            result,
            f"Command timed out after {timeout} seconds: {result.executable}",
        )


class CommandRunner:
    """Run external programs safely and persist a complete execution audit.

    Parameters
    ----------
    output_dir:
        Directory receiving stdout, stderr, and JSON metadata files.  It
        defaults to ``runs/connection`` relative to the creating process.
    live_output:
        Default for forwarding child stdout/stderr to the current console.
        Individual calls can override it.
    output_encoding:
        Encoding used to expose captured bytes as ``CommandResult`` text and
        for live display.  Log files themselves retain the original bytes.
    allow_mnt_c_executables:
        Explicit policy override for ``/mnt/c/.../*.exe`` paths.  Keep this
        false unless the same permission was read from ``config/tools.json``.
    """

    def __init__(
        self,
        output_dir: PathLike = Path("runs") / "connection",
        *,
        live_output: bool = False,
        output_encoding: str = "utf-8",
        allow_mnt_c_executables: bool = False,
    ) -> None:
        if not isinstance(allow_mnt_c_executables, bool):
            raise TypeError("allow_mnt_c_executables must be a boolean")
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.live_output = bool(live_output)
        self.output_encoding = output_encoding
        self.allow_mnt_c_executables = allow_mnt_c_executables

    def run(
        self,
        executable: PathLike,
        args: Sequence[PathLike] = (),
        cwd: PathLike | None = None,
        env: EnvironmentOverlay | None = None,
        timeout: float | None = None,
        check: bool = True,
        live_output: bool | None = None,
        dry_run: bool = False,
        name: str | None = None,
        stdout_log: PathLike | None = None,
        stderr_log: PathLike | None = None,
        metadata_log: PathLike | None = None,
        abort_check: ResourceAbortCheck | None = None,
        abort_check_interval_seconds: float = (
            _RESOURCE_ABORT_CHECK_INTERVAL_SECONDS
        ),
    ) -> CommandResult:
        """Execute one command and return its captured, logged result.

        ``env`` is overlaid on a copy of the current environment.  A mapping
        value of ``None`` explicitly removes that variable for the child.
        Timeouts always raise :class:`CommandTimeoutError`; non-zero exits
        raise :class:`CommandExecutionError` only when ``check`` is true.
        When supplied, ``abort_check`` is called periodically while the child
        is running.  A non-empty reason terminates the existing safe process
        group and always raises :class:`CommandExecutionError`, even when
        ``check`` is false.
        """

        if isinstance(args, (str, bytes, os.PathLike)):
            raise TypeError("args must be a sequence of individual arguments")
        if abort_check is not None and not callable(abort_check):
            raise TypeError("abort_check must be callable or None")
        if abort_check is not None:
            if isinstance(abort_check_interval_seconds, bool):
                raise ValueError(
                    "abort_check_interval_seconds must be finite and positive"
                )
            try:
                abort_interval = float(abort_check_interval_seconds)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "abort_check_interval_seconds must be finite and positive"
                ) from error
            if not math.isfinite(abort_interval) or abort_interval <= 0.0:
                raise ValueError(
                    "abort_check_interval_seconds must be finite and positive"
                )
        else:
            # Preserve the historical single ``process.wait(timeout=...)``
            # path exactly when no resource monitor is requested.
            abort_interval = _RESOURCE_ABORT_CHECK_INTERVAL_SECONDS

        argument_list = tuple(os.fspath(argument) for argument in args)
        working_directory = Path(cwd or Path.cwd()).expanduser().resolve()
        child_environment = self._overlay_environment(env)
        executable_path = self._validate_executable(executable)
        command = [executable_path, *argument_list]
        start_time = _utc_now()
        stdout_log, stderr_log, metadata_log = self._allocate_logs(
            name=name,
            executable=executable_path,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            metadata_log=metadata_log,
        )
        previous_logs_archive = self._archive_existing_logs(
            (stdout_log, stderr_log, metadata_log)
        )
        use_live_output = self.live_output if live_output is None else live_output

        # Creating all files up front makes dry runs and failed process starts
        # just as auditable as completed invocations.
        stdout_log.write_bytes(b"")
        stderr_log.write_bytes(b"")

        if dry_run:
            result = CommandResult(
                executable=executable_path,
                args=argument_list,
                cwd=str(working_directory),
                start_time=start_time,
                end_time=_utc_now(),
                returncode=0,
                stdout="",
                stderr="",
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                metadata_log=metadata_log,
                dry_run=True,
                timed_out=False,
                previous_logs_archive=previous_logs_archive,
            )
            self._write_metadata(result)
            return result

        try:
            process = subprocess.Popen(
                command,
                cwd=str(working_directory),
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=False,
                bufsize=0,
                **self._process_group_options(),
            )
        except OSError as error:
            error_text = str(error)
            stderr_log.write_bytes(error_text.encode(self.output_encoding, "replace"))
            result = CommandResult(
                executable=executable_path,
                args=argument_list,
                cwd=str(working_directory),
                start_time=start_time,
                end_time=_utc_now(),
                returncode=None,
                stdout="",
                stderr=error_text,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                metadata_log=metadata_log,
                dry_run=False,
                timed_out=False,
                previous_logs_archive=previous_logs_archive,
            )
            self._write_metadata(result)
            raise CommandExecutionError(
                result, f"Could not start command: {executable_path}"
            ) from error

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        reader_errors: list[BaseException] = []
        reader_stop = threading.Event()
        stdout_thread = threading.Thread(
            target=self._drain_stream,
            args=(
                process.stdout,
                stdout_log,
                stdout_chunks,
                sys.stdout if use_live_output else None,
                reader_errors,
                reader_stop,
            ),
            name="command-stdout-reader",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._drain_stream,
            args=(
                process.stderr,
                stderr_log,
                stderr_chunks,
                sys.stderr if use_live_output else None,
                reader_errors,
                reader_stop,
            ),
            name="command-stderr-reader",
            daemon=True,
        )
        timed_out = False
        resource_aborted = False
        resource_abort_reason: str | None = None
        interrupted_error: KeyboardInterrupt | None = None
        unexpected_error: BaseException | None = None
        output_complete = False
        started_threads: list[threading.Thread] = []
        termination_trigger: str | None = None
        termination_outcome = _TerminationOutcome()

        def terminate_for(trigger: str) -> None:
            """Terminate once and retain evidence across defensive retries."""

            nonlocal termination_trigger, termination_outcome
            if termination_trigger is None:
                termination_trigger = trigger
            observed = self._terminate_process_group(process)
            # Existing callers and tests may replace this private helper with
            # a side-effect-only mock.  Production always returns the typed
            # outcome; an untyped replacement must not fabricate evidence.
            if isinstance(observed, _TerminationOutcome):
                termination_outcome = _merge_termination_outcomes(
                    termination_outcome, observed
                )

        try:
            stdout_thread.start()
            started_threads.append(stdout_thread)
            stderr_thread.start()
            started_threads.append(stderr_thread)
            try:
                if abort_check is None:
                    process.wait(timeout=timeout)
                else:
                    timed_out, resource_abort_reason = (
                        self._wait_with_resource_abort(
                            process,
                            timeout=timeout,
                            abort_check=abort_check,
                            interval_seconds=abort_interval,
                        )
                    )
                    resource_aborted = resource_abort_reason is not None
                    if timed_out or resource_aborted:
                        terminate_for(
                            "timeout" if timed_out else "resource_abort"
                        )
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_for("timeout")
            except KeyboardInterrupt as error:
                interrupted_error = error
                terminate_for("interrupt")

            output_complete = self._join_output_threads(
                process,
                (stdout_thread, stderr_thread),
                reader_stop,
            )
        except BaseException as error:
            unexpected_error = error
            terminate_for("runner_error")
            if len(started_threads) == 2:
                try:
                    self._join_output_threads(
                        process,
                        (stdout_thread, stderr_thread),
                        reader_stop,
                    )
                except BaseException:
                    reader_stop.set()
            else:
                reader_stop.set()
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        self._close_pipe_in_background(stream)
                for thread in started_threads:
                    thread.join(_READER_CLOSE_JOIN_SECONDS)

        stdout_bytes = b"".join(stdout_chunks)
        stderr_bytes = b"".join(stderr_chunks)
        result = CommandResult(
            executable=executable_path,
            args=argument_list,
            cwd=str(working_directory),
            start_time=start_time,
            end_time=_utc_now(),
            returncode=process.returncode,
            stdout=stdout_bytes.decode(self.output_encoding, "replace"),
            stderr=stderr_bytes.decode(self.output_encoding, "replace"),
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            metadata_log=metadata_log,
            dry_run=False,
            timed_out=timed_out,
            interrupted=(
                interrupted_error is not None
                or isinstance(unexpected_error, KeyboardInterrupt)
            ),
            resource_aborted=resource_aborted,
            resource_abort_reason=resource_abort_reason,
            termination_method=_termination_method(
                termination_trigger, termination_outcome
            ),
            graceful_termination_attempted=(
                termination_outcome.graceful_termination_attempted
            ),
            graceful_termination_succeeded=(
                termination_outcome.graceful_termination_succeeded
            ),
            hard_kill_attempted=termination_outcome.hard_kill_attempted,
            hard_kill_succeeded=termination_outcome.hard_kill_succeeded,
            output_complete=(
                output_complete
                and not reader_errors
                and unexpected_error is None
            ),
            previous_logs_archive=previous_logs_archive,
            runner_error=(
                None
                if unexpected_error is None
                else "".join(
                    traceback.format_exception(
                        type(unexpected_error),
                        unexpected_error,
                        unexpected_error.__traceback__,
                    )
                )
            ),
        )
        self._write_metadata(result)

        if unexpected_error is not None:
            if isinstance(unexpected_error, (KeyboardInterrupt, SystemExit)):
                unexpected_error.result = result  # type: ignore[attr-defined]
                raise unexpected_error.with_traceback(
                    unexpected_error.__traceback__
                )
            raise CommandExecutionError(
                result,
                "Command runner failed after process start",
            ) from unexpected_error
        if interrupted_error is not None:
            # Preserve the ordinary KeyboardInterrupt contract while still
            # leaving an auditable result for callers that inspect the error.
            interrupted_error.result = result  # type: ignore[attr-defined]
            raise interrupted_error.with_traceback(interrupted_error.__traceback__)
        if timed_out:
            raise CommandTimeoutError(result, timeout)
        if resource_aborted:
            raise CommandExecutionError(
                result,
                f"Command aborted by resource monitor: {resource_abort_reason}",
            )
        if reader_errors or not output_complete:
            capture_error = CommandExecutionError(
                result,
                "Command output capture did not complete cleanly",
            )
            if reader_errors:
                raise capture_error from reader_errors[0]
            raise capture_error
        if check and result.returncode != 0:
            raise CommandExecutionError(result)
        return result

    def _archive_existing_logs(self, paths: Sequence[Path]) -> Path | None:
        """Preserve fixed-name logs before a later invocation reuses them."""

        existing = [path for path in paths if path.is_file()]
        if not existing:
            return None
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        archive_dir = (
            self.output_dir
            / "command_archive"
            / f"{timestamp}_{uuid.uuid4().hex[:8]}"
        )
        archive_dir.mkdir(parents=True, exist_ok=False)
        archived_files: list[dict[str, object]] = []
        for index, source in enumerate(existing, start=1):
            destination = archive_dir / f"{index:02d}_{source.name}"
            shutil.copy2(source, destination)
            content = destination.read_bytes()
            archived_files.append(
                {
                    "original_path": str(source),
                    "archive_path": str(destination),
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        (archive_dir / "archive_manifest.json").write_text(
            json.dumps(
                {
                    "archived_at": _utc_now(),
                    "files": archived_files,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        return archive_dir.resolve()

    @staticmethod
    def _overlay_environment(env: EnvironmentOverlay | None) -> dict[str, str]:
        child_environment = dict(os.environ)
        if env is None:
            return child_environment
        for key, value in env.items():
            key_text = str(key)
            if value is None:
                child_environment.pop(key_text, None)
            else:
                child_environment[key_text] = os.fspath(value)
        return child_environment

    def _validate_executable(self, executable: PathLike) -> str:
        """Require a Toolchain-resolved absolute executable path.

        CommandRunner intentionally performs no PATH search.  Keeping
        discovery in Toolchain prevents bridge callers from bypassing its
        provenance and Windows-executable policies.
        """

        try:
            executable_text = os.fspath(executable)
        except TypeError as error:
            raise ValueError("executable must be a string or path-like value") from error
        if isinstance(executable_text, bytes):
            executable_text = os.fsdecode(executable_text)
        if not executable_text or executable_text.isspace():
            raise ValueError("executable path must not be empty")

        raw_mnt_c = _canonical_mnt_c_executable(executable_text)
        if raw_mnt_c is not None and not self.allow_mnt_c_executables:
            raise ValueError(
                f"/mnt/c Windows executable is disallowed: {executable_text!r}; "
                "allow_mnt_c_executables=True is required"
            )
        if raw_mnt_c is not None:
            # Preserve an explicitly allowed WSL path verbatim.  Passing it
            # through the host ``Path`` implementation would reinterpret it on
            # native Windows and make this policy impossible to test there.
            return raw_mnt_c

        candidate = Path(executable_text).expanduser()
        if not candidate.is_absolute():
            raise ValueError(
                "executable must be an absolute path resolved by Toolchain: "
                f"{executable_text!r}"
            )
        resolved = candidate.resolve(strict=False)

        final_mnt_c = _canonical_mnt_c_executable(str(resolved))
        if final_mnt_c is not None and not self.allow_mnt_c_executables:
            raise ValueError(
                "/mnt/c Windows executable is disallowed after normalization: "
                f"{resolved}; allow_mnt_c_executables=True is required"
            )
        return str(resolved)

    @staticmethod
    def _process_group_options() -> dict[str, object]:
        if os.name == "nt":
            return {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
            }
        return {"start_new_session": True}

    @staticmethod
    def _wait_with_resource_abort(
        process: subprocess.Popen[bytes],
        *,
        timeout: float | None,
        abort_check: ResourceAbortCheck,
        interval_seconds: float,
    ) -> tuple[bool, str | None]:
        """Wait in bounded slices and return ``(timed_out, abort_reason)``.

        Callback exceptions deliberately propagate to the runner's existing
        unexpected-error path, which terminates the process, drains its pipes,
        records a traceback, and raises ``CommandExecutionError``.
        """

        started = time.monotonic()
        deadline = None if timeout is None else started + float(timeout)
        while True:
            if process.poll() is not None:
                return False, None

            if deadline is not None and time.monotonic() >= deadline:
                return True, None

            reason = abort_check()
            if reason is not None:
                if not isinstance(reason, str):
                    raise TypeError(
                        "abort_check must return a string reason or None"
                    )
                if reason:
                    return False, reason

            wait_seconds = interval_seconds
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return True, None
                wait_seconds = min(wait_seconds, remaining)
            try:
                process.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                continue
            return False, None

    @staticmethod
    def _wait_bounded(
        process: subprocess.Popen[bytes], timeout: float
    ) -> bool:
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return process.poll() is not None
        return True

    @classmethod
    def _terminate_process_group(
        cls, process: subprocess.Popen[bytes]
    ) -> _TerminationOutcome:
        """Best-effort bounded termination with explicit escalation evidence."""

        if os.name == "nt":
            graceful_attempted = False
            graceful_succeeded = False
            hard_kill_attempted = False
            hard_kill_succeeded = False
            if process.poll() is None:
                graceful_attempted = True
                try:
                    process.send_signal(_CTRL_BREAK_EVENT)
                except (OSError, ValueError):
                    # Some non-console Windows hosts cannot deliver console
                    # control events.  The hard-kill fallback below remains
                    # bounded, although stdlib has no Job Object tree kill.
                    pass
                graceful_succeeded = cls._wait_bounded(
                    process, _TERMINATION_GRACE_SECONDS
                )
            if process.poll() is None:
                hard_kill_attempted = True
                try:
                    process.kill()
                except OSError:
                    pass
                hard_kill_succeeded = cls._wait_bounded(
                    process, _TERMINATION_GRACE_SECONDS
                )
            return _TerminationOutcome(
                graceful_termination_attempted=graceful_attempted,
                graceful_termination_succeeded=graceful_succeeded,
                hard_kill_attempted=hard_kill_attempted,
                hard_kill_succeeded=hard_kill_succeeded,
            )

        # start_new_session=True makes the child PID its process-group ID.
        graceful_attempted = True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
        graceful_succeeded = cls._wait_bounded(
            process, _TERMINATION_GRACE_SECONDS
        )

        # Kill the group even if its original leader has already exited; a
        # descendant may still own the inherited stdout/stderr pipe handles.
        hard_kill_attempted = True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
        hard_kill_succeeded = cls._wait_bounded(
            process, _TERMINATION_GRACE_SECONDS
        )
        return _TerminationOutcome(
            graceful_termination_attempted=graceful_attempted,
            graceful_termination_succeeded=graceful_succeeded,
            hard_kill_attempted=hard_kill_attempted,
            hard_kill_succeeded=hard_kill_succeeded,
        )

    @staticmethod
    def _close_pipe_in_background(stream: IO[bytes]) -> None:
        def close_stream() -> None:
            try:
                stream.close()
            except OSError:
                pass

        threading.Thread(
            target=close_stream,
            name="command-pipe-closer",
            daemon=True,
        ).start()

    @classmethod
    def _join_output_threads(
        cls,
        process: subprocess.Popen[bytes],
        threads: tuple[threading.Thread, threading.Thread],
        stop_event: threading.Event,
    ) -> bool:
        """Join pipe readers with a fixed upper bound.

        Descendants can inherit pipe handles and keep them open after the
        direct child exits.  A bounded join prevents that condition from
        hanging the orchestration process forever.
        """

        deadline = time.monotonic() + _READER_JOIN_SECONDS
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        alive = [thread for thread in threads if thread.is_alive()]
        if not alive:
            return True

        stop_event.set()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                cls._close_pipe_in_background(stream)

        close_deadline = time.monotonic() + _READER_CLOSE_JOIN_SECONDS
        for thread in alive:
            thread.join(max(0.0, close_deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in threads)

    def _allocate_logs(
        self,
        *,
        name: str | None,
        executable: str,
        stdout_log: PathLike | None,
        stderr_log: PathLike | None,
        metadata_log: PathLike | None,
    ) -> tuple[Path, Path, Path]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        label = name if name is not None else Path(executable).stem
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("._")
        if not safe_label:
            safe_label = "command"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        run_id = f"{timestamp}_{safe_label}_{uuid.uuid4().hex[:8]}"
        paths = (
            self._resolve_log_path(
                stdout_log, self.output_dir / f"{run_id}.stdout.log"
            ),
            self._resolve_log_path(
                stderr_log, self.output_dir / f"{run_id}.stderr.log"
            ),
            self._resolve_log_path(metadata_log, self.output_dir / f"{run_id}.json"),
        )
        if len(set(paths)) != len(paths):
            raise ValueError("stdout, stderr, and metadata log paths must be distinct")
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
        return paths

    def _resolve_log_path(self, value: PathLike | None, default: Path) -> Path:
        path = default if value is None else Path(value).expanduser()
        if not path.is_absolute():
            path = self.output_dir / path
        resolved = path.resolve()
        if resolved != self.output_dir and self.output_dir not in resolved.parents:
            raise ValueError(
                f"command log path must stay under {self.output_dir}: {resolved}"
            )
        return resolved

    def _drain_stream(
        self,
        stream: IO[bytes],
        log_path: Path,
        chunks: list[bytes],
        live_stream: TextIO | None,
        errors: list[BaseException],
        stop_event: threading.Event,
    ) -> None:
        decoder = codecs.getincrementaldecoder(self.output_encoding)(errors="replace")
        try:
            with log_path.open("wb") as log_file:
                while True:
                    chunk = stream.read(64 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    log_file.write(chunk)
                    log_file.flush()
                    if live_stream is not None:
                        text = decoder.decode(chunk)
                        if text:
                            live_stream.write(text)
                            live_stream.flush()
                if live_stream is not None:
                    tail = decoder.decode(b"", final=True)
                    if tail:
                        live_stream.write(tail)
                        live_stream.flush()
        except BaseException as error:  # propagated on the calling thread
            if not stop_event.is_set():
                errors.append(error)
        finally:
            stream.close()

    @staticmethod
    def _write_metadata(result: CommandResult) -> None:
        result.metadata_log.write_text(
            json.dumps(result.as_metadata(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


__all__ = [
    "CommandExecutionError",
    "CommandResult",
    "CommandRunner",
    "CommandTimeoutError",
]

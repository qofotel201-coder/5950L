from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import cfdpipe.process as process_module
from cfdpipe.process import (
    CommandExecutionError,
    CommandRunner,
    CommandTimeoutError,
)


CONNECTION_OUTPUT = Path(__file__).resolve().parents[1] / "runs" / "connection"


class CommandRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        CONNECTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temporary_directory = tempfile.TemporaryDirectory(
            dir=CONNECTION_OUTPUT
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.runner = CommandRunner(self.root / "logs")

    def run_python(self, source: str, *args: str, **kwargs: object):
        return self.runner.run(
            sys.executable,
            ("-c", source, *args),
            cwd=self.root,
            **kwargs,
        )

    def test_success_records_environment_cwd_arguments_and_separate_logs(self) -> None:
        literal_argument = "value with spaces;$(not-a-shell)"
        source = (
            "import os, pathlib, sys; "
            "print(os.environ['CFDPIPE_TEST_VALUE']); "
            "print(pathlib.Path.cwd()); "
            "print(sys.argv[1]); "
            "print('diagnostic stderr', file=sys.stderr)"
        )

        result = self.run_python(
            source,
            literal_argument,
            env={"CFDPIPE_TEST_VALUE": "overlay-value"},
            name="audit success",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout.splitlines(),
            ["overlay-value", str(self.root), literal_argument],
        )
        self.assertEqual(result.stderr, f"diagnostic stderr{os.linesep}")
        self.assertEqual(result.stdout_log.read_bytes(), result.stdout.encode())
        self.assertEqual(result.stderr_log.read_bytes(), result.stderr.encode())

        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertTrue(Path(metadata["executable"]).is_absolute())
        self.assertEqual(Path(metadata["executable"]), Path(sys.executable).resolve())
        self.assertEqual(metadata["args"], list(result.args))
        self.assertEqual(metadata["command"], list(result.command))
        self.assertEqual(metadata["cwd"], str(self.root))
        self.assertEqual(metadata["returncode"], 0)
        self.assertEqual(metadata["stdout_log"], str(result.stdout_log))
        self.assertEqual(metadata["stderr_log"], str(result.stderr_log))
        self.assertRegex(metadata["start_time"], r"Z$")
        self.assertRegex(metadata["end_time"], r"Z$")
        self.assertFalse(metadata["dry_run"])
        self.assertFalse(metadata["timed_out"])
        self.assertFalse(metadata["resource_aborted"])
        self.assertIsNone(metadata["resource_abort_reason"])
        self.assertEqual(metadata["status_hex"], "0x00000000")
        self.assertEqual(metadata["termination_method"], "none")
        self.assertFalse(metadata["graceful_termination_attempted"])
        self.assertFalse(metadata["graceful_termination_succeeded"])
        self.assertFalse(metadata["hard_kill_attempted"])
        self.assertFalse(metadata["hard_kill_succeeded"])

    def test_explicit_log_names_are_written_live_under_output_directory(self) -> None:
        result = self.run_python(
            "import sys; print('solver out', flush=True); "
            "print('solver err', file=sys.stderr, flush=True)",
            stdout_log="solver.stdout.log",
            stderr_log="solver.stderr.log",
            metadata_log="solver.command.json",
        )

        self.assertEqual(result.stdout_log, self.runner.output_dir / "solver.stdout.log")
        self.assertEqual(result.stderr_log, self.runner.output_dir / "solver.stderr.log")
        self.assertEqual(result.metadata_log, self.runner.output_dir / "solver.command.json")
        self.assertEqual(result.stdout_log.read_bytes(), result.stdout.encode("utf-8"))
        self.assertEqual(result.stderr_log.read_bytes(), result.stderr.encode("utf-8"))

    def test_reused_fixed_log_names_archive_the_previous_invocation(self) -> None:
        first = self.run_python(
            "print('first invocation')",
            stdout_log="solver.stdout.log",
            stderr_log="solver.stderr.log",
            metadata_log="solver.command.json",
        )
        first_stdout = first.stdout_log.read_bytes()
        first_metadata = first.metadata_log.read_bytes()

        second = self.run_python(
            "print('second invocation')",
            stdout_log="solver.stdout.log",
            stderr_log="solver.stderr.log",
            metadata_log="solver.command.json",
        )

        self.assertIsNotNone(second.previous_logs_archive)
        archive = second.previous_logs_archive
        assert archive is not None
        self.assertTrue(archive.is_dir())
        archived_contents = {
            path.read_bytes()
            for path in archive.iterdir()
            if path.is_file() and path.name != "archive_manifest.json"
        }
        self.assertIn(first_stdout, archived_contents)
        self.assertIn(first_metadata, archived_contents)
        index = json.loads(
            (archive / "archive_manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(index["files"]), 3)
        current_metadata = json.loads(
            second.metadata_log.read_text(encoding="utf-8")
        )
        self.assertEqual(
            current_metadata["previous_logs_archive"], str(archive)
        )

    def test_explicit_log_path_cannot_escape_output_directory(self) -> None:
        with mock.patch("cfdpipe.process.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "must stay under"):
                self.runner.run(
                    sys.executable,
                    dry_run=True,
                    stdout_log=self.root / "outside.log",
                )

        popen.assert_not_called()

    def test_nonzero_exit_raises_with_original_stderr_and_result(self) -> None:
        source = "import sys; sys.stderr.write('raw failure\\n'); sys.exit(7)"

        with self.assertRaises(CommandExecutionError) as raised:
            self.run_python(source)

        error = raised.exception
        self.assertEqual(error.returncode, 7)
        expected_stderr = f"raw failure{os.linesep}"
        self.assertEqual(error.stderr, expected_stderr)
        self.assertEqual(error.result.stderr, expected_stderr)
        self.assertEqual(
            error.result.stderr_log.read_bytes(), expected_stderr.encode("utf-8")
        )
        self.assertIn(expected_stderr, str(error))
        metadata = json.loads(error.result.metadata_log.read_text(encoding="utf-8"))
        self.assertEqual(metadata["returncode"], 7)
        self.assertEqual(metadata["status_hex"], "0x00000007")
        self.assertEqual(metadata["termination_method"], "none")

    def test_check_false_returns_nonzero_result(self) -> None:
        result = self.run_python(
            "import sys; print('kept', file=sys.stderr); sys.exit(4)",
            check=False,
        )

        self.assertEqual(result.returncode, 4)
        self.assertFalse(result.ok)
        self.assertEqual(result.stderr, f"kept{os.linesep}")

    def test_timeout_kills_process_and_preserves_partial_stderr(self) -> None:
        source = (
            "import sys, time; "
            "sys.stderr.write('before timeout\\n'); sys.stderr.flush(); "
            "time.sleep(5)"
        )

        with self.assertRaises(CommandTimeoutError) as raised:
            self.run_python(source, timeout=0.5)

        result = raised.exception.result
        self.assertTrue(result.timed_out)
        self.assertIn(f"before timeout{os.linesep}", result.stderr)
        self.assertEqual(result.stderr_log.read_bytes(), result.stderr.encode())
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertTrue(metadata["timed_out"])
        self.assertEqual(metadata["returncode"], result.returncode)
        self.assertEqual(
            metadata["status_hex"],
            f"0x{int(result.returncode) & 0xFFFFFFFF:08X}",
        )
        self.assertTrue(metadata["termination_method"].startswith("timeout_"))
        self.assertTrue(metadata["graceful_termination_attempted"])

    def test_timeout_does_not_hang_when_descendant_inherits_pipes(self) -> None:
        child_pid_file = self.root / "child.pid"
        source = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(10)'], "
            "stdout=sys.stdout, stderr=sys.stderr, "
            "cwd=str(pathlib.Path(sys.executable).parent)); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='ascii'); "
            "print('spawned inherited-pipe child', file=sys.stderr, flush=True); "
            "time.sleep(10)"
        )

        started = time.monotonic()
        try:
            with self.assertRaises(CommandTimeoutError) as raised:
                self.run_python(source, str(child_pid_file), timeout=0.5)
        finally:
            # Windows has no stdlib Job Object API.  If CTRL_BREAK could not
            # reach the descendant in this host, avoid leaving the test child
            # alive after confirming the runner itself returned promptly.
            if child_pid_file.exists():
                child_pid = int(child_pid_file.read_text(encoding="ascii"))
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except OSError:
                    pass

        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.0)
        self.assertTrue(raised.exception.result.timed_out)
        self.assertIn("spawned inherited-pipe child", raised.exception.result.stderr)
        self.assertTrue(raised.exception.result.metadata_log.is_file())

    def test_keyboard_interrupt_terminates_group_and_writes_partial_metadata(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"partial stdout")
        process.stderr = io.BytesIO(b"partial stderr")
        process.returncode = -2
        process.wait.side_effect = KeyboardInterrupt()

        with (
            mock.patch("cfdpipe.process.subprocess.Popen", return_value=process) as popen,
            mock.patch.object(self.runner, "_terminate_process_group") as terminate,
            self.assertRaises(KeyboardInterrupt) as raised,
        ):
            self.runner.run(sys.executable)

        terminate.assert_called_once_with(process)
        result = raised.exception.result
        self.assertTrue(result.interrupted)
        self.assertEqual(result.stdout, "partial stdout")
        self.assertEqual(result.stderr, "partial stderr")
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertTrue(metadata["interrupted"])
        self.assertEqual(metadata["stderr_log"], str(result.stderr_log))
        popen_kwargs = popen.call_args.kwargs
        if os.name == "nt":
            self.assertEqual(
                popen_kwargs["creationflags"],
                subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        else:
            self.assertTrue(popen_kwargs["start_new_session"])

    def test_unexpected_wait_error_terminates_and_writes_failure_metadata(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"partial stdout")
        process.stderr = io.BytesIO(b"original child stderr")
        process.returncode = -9
        process.wait.side_effect = RuntimeError("unexpected wait failure")

        with (
            mock.patch("cfdpipe.process.subprocess.Popen", return_value=process),
            mock.patch.object(self.runner, "_terminate_process_group") as terminate,
            self.assertRaises(CommandExecutionError) as raised,
        ):
            self.runner.run(sys.executable)

        terminate.assert_called_once_with(process)
        result = raised.exception.result
        self.assertEqual(result.stderr, "original child stderr")
        self.assertFalse(result.output_complete)
        self.assertIn("unexpected wait failure", result.runner_error or "")
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertIn("unexpected wait failure", metadata["runner_error"])

    def test_resource_abort_forces_failure_with_check_false_and_drains_logs(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"partial resource stdout")
        process.stderr = io.BytesIO(b"original resource stderr")
        process.returncode = -9
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired(
            cmd=[sys.executable], timeout=0.01
        )
        checks: list[int] = []

        def resource_check() -> str | None:
            checks.append(len(checks) + 1)
            if len(checks) == 1:
                return None
            return "available physical memory below reserve"

        with (
            mock.patch("cfdpipe.process.subprocess.Popen", return_value=process) as popen,
            mock.patch.object(
                self.runner,
                "_terminate_process_group",
                return_value=process_module._TerminationOutcome(
                    graceful_termination_attempted=True,
                    graceful_termination_succeeded=False,
                    hard_kill_attempted=True,
                    hard_kill_succeeded=True,
                ),
            ) as terminate,
            self.assertRaises(CommandExecutionError) as raised,
        ):
            self.runner.run(
                sys.executable,
                check=False,
                abort_check=resource_check,
                abort_check_interval_seconds=0.01,
            )

        terminate.assert_called_once_with(process)
        self.assertEqual([1, 2], checks)
        process.wait.assert_called_once_with(timeout=0.01)
        result = raised.exception.result
        self.assertTrue(result.resource_aborted)
        self.assertEqual(
            "available physical memory below reserve",
            result.resource_abort_reason,
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.output_complete)
        self.assertEqual("partial resource stdout", result.stdout)
        self.assertEqual("original resource stderr", result.stderr)
        self.assertEqual(result.stdout.encode(), result.stdout_log.read_bytes())
        self.assertEqual(result.stderr.encode(), result.stderr_log.read_bytes())
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertTrue(metadata["resource_aborted"])
        self.assertEqual(
            "available physical memory below reserve",
            metadata["resource_abort_reason"],
        )
        self.assertFalse(metadata["timed_out"])
        self.assertEqual(metadata["status_hex"], "0xFFFFFFF7")
        self.assertEqual(
            metadata["termination_method"], "resource_abort_hard_kill"
        )
        self.assertTrue(metadata["graceful_termination_attempted"])
        self.assertFalse(metadata["graceful_termination_succeeded"])
        self.assertTrue(metadata["hard_kill_attempted"])
        self.assertTrue(metadata["hard_kill_succeeded"])
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_status_hex_normalizes_unsigned_windows_ntstatus(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"raw ntstatus stderr")
        process.returncode = 3221225786
        process.wait.return_value = 3221225786

        with mock.patch(
            "cfdpipe.process.subprocess.Popen", return_value=process
        ) as popen:
            result = self.runner.run(sys.executable, check=False)

        self.assertEqual(result.returncode, 3221225786)
        self.assertEqual(result.status_hex, "0xC000013A")
        self.assertEqual(result.stderr, "raw ntstatus stderr")
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertEqual(metadata["returncode"], 3221225786)
        self.assertEqual(metadata["status_hex"], "0xC000013A")
        self.assertEqual(metadata["termination_method"], "none")
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_termination_evidence_records_hard_kill_escalation(self) -> None:
        process = mock.Mock()
        process.poll.side_effect = [None, None]

        with (
            mock.patch("cfdpipe.process.os.name", "nt"),
            mock.patch.object(
                CommandRunner,
                "_wait_bounded",
                side_effect=[False, True],
            ) as wait_bounded,
        ):
            outcome = CommandRunner._terminate_process_group(process)

        process.send_signal.assert_called_once_with(
            getattr(signal, "CTRL_BREAK_EVENT", 1)
        )
        process.kill.assert_called_once_with()
        self.assertEqual(wait_bounded.call_count, 2)
        self.assertTrue(outcome.graceful_termination_attempted)
        self.assertFalse(outcome.graceful_termination_succeeded)
        self.assertTrue(outcome.hard_kill_attempted)
        self.assertTrue(outcome.hard_kill_succeeded)

    def test_resource_abort_callback_exception_uses_unexpected_error_path(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"stdout before callback failure")
        process.stderr = io.BytesIO(b"stderr before callback failure")
        process.returncode = -9
        process.poll.return_value = None

        def broken_check() -> str | None:
            raise RuntimeError("resource sampler failed")

        with (
            mock.patch("cfdpipe.process.subprocess.Popen", return_value=process),
            mock.patch.object(self.runner, "_terminate_process_group") as terminate,
            self.assertRaises(CommandExecutionError) as raised,
        ):
            self.runner.run(sys.executable, abort_check=broken_check)

        terminate.assert_called_once_with(process)
        result = raised.exception.result
        self.assertFalse(result.resource_aborted)
        self.assertIsNone(result.resource_abort_reason)
        self.assertEqual("stdout before callback failure", result.stdout)
        self.assertEqual("stderr before callback failure", result.stderr)
        self.assertIn("RuntimeError", result.runner_error or "")
        self.assertIn("resource sampler failed", result.runner_error or "")
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertIn("resource sampler failed", metadata["runner_error"])

    def test_empty_abort_reason_allows_normal_completion(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"")
        process.returncode = 0
        process.poll.return_value = None
        process.wait.return_value = 0
        checks: list[bool] = []

        with mock.patch(
            "cfdpipe.process.subprocess.Popen", return_value=process
        ) as popen:
            result = self.runner.run(
                sys.executable,
                abort_check=lambda: checks.append(True) or "",
            )

        self.assertEqual([True], checks)
        self.assertTrue(result.ok)
        self.assertFalse(result.resource_aborted)
        self.assertIsNone(result.resource_abort_reason)
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_abort_callback_and_interval_are_validated_before_popen(self) -> None:
        with mock.patch("cfdpipe.process.subprocess.Popen") as popen:
            with self.assertRaisesRegex(TypeError, "abort_check"):
                self.runner.run(
                    sys.executable,
                    abort_check="not callable",  # type: ignore[arg-type]
                )
            for invalid in (0, -1, True, float("nan"), float("inf"), "bad"):
                with self.subTest(invalid=invalid):
                    with self.assertRaisesRegex(ValueError, "finite and positive"):
                        self.runner.run(
                            sys.executable,
                            abort_check=lambda: None,
                            abort_check_interval_seconds=invalid,  # type: ignore[arg-type]
                        )

        popen.assert_not_called()

    def test_popen_receives_an_argument_list_and_shell_false(self) -> None:
        process = mock.Mock()
        process.stdout = io.BytesIO(b"")
        process.stderr = io.BytesIO(b"")
        process.returncode = 0
        process.wait.return_value = 0

        with mock.patch(
            "cfdpipe.process.subprocess.Popen", return_value=process
        ) as popen:
            self.runner.run(sys.executable, ("-V",))

        command = popen.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertEqual(command, [str(Path(sys.executable).resolve()), "-V"])
        self.assertIs(popen.call_args.kwargs["shell"], False)

    def test_dry_run_writes_empty_logs_and_metadata_without_starting_process(self) -> None:
        missing_program = self.root / "program-that-does-not-exist"

        with mock.patch("cfdpipe.process.subprocess.Popen") as popen:
            result = self.runner.run(
                missing_program,
                ("--flag", "literal argument"),
                cwd=self.root,
                dry_run=True,
            )

        popen.assert_not_called()
        self.assertTrue(result.dry_run)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout_log.read_bytes(), b"")
        self.assertEqual(result.stderr_log.read_bytes(), b"")
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertTrue(metadata["dry_run"])
        self.assertEqual(metadata["executable"], str(missing_program.resolve()))
        self.assertEqual(metadata["args"], ["--flag", "literal argument"])

    def test_relative_or_bare_executable_is_rejected_without_path_search(self) -> None:
        with mock.patch("cfdpipe.process.subprocess.Popen") as popen:
            for executable in ("python", Path("relative") / "tool"):
                with self.subTest(executable=executable):
                    with self.assertRaisesRegex(ValueError, "absolute path"):
                        self.runner.run(executable, dry_run=True)

        popen.assert_not_called()

    def test_mnt_c_executable_is_rejected_before_and_after_normalization(self) -> None:
        candidates = (
            "/mnt/c/Windows/System32/tool.exe",
            "//mnt/c/Windows/System32/tool.EXE",
            "/mnt/x/../c/Windows/System32/tool.exe",
        )

        with mock.patch("cfdpipe.process.subprocess.Popen") as popen:
            for executable in candidates:
                with self.subTest(executable=executable):
                    with self.assertRaisesRegex(ValueError, "/mnt/c"):
                        self.runner.run(executable, dry_run=True)

        popen.assert_not_called()

    def test_mnt_c_executable_can_be_explicitly_allowed(self) -> None:
        runner = CommandRunner(
            self.root / "allowed-logs",
            allow_mnt_c_executables=True,
        )

        with mock.patch("cfdpipe.process.subprocess.Popen") as popen:
            result = runner.run("/mnt/c/tools/fake.exe", dry_run=True)

        self.assertEqual(result.executable, "/mnt/c/tools/fake.exe")
        self.assertTrue(result.dry_run)
        popen.assert_not_called()

    def test_mnt_c_override_requires_an_actual_boolean(self) -> None:
        with self.assertRaisesRegex(TypeError, "must be a boolean"):
            CommandRunner(
                self.root / "invalid-policy-logs",
                allow_mnt_c_executables="false",  # type: ignore[arg-type]
            )

    def test_live_output_forwards_stdout_and_stderr(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = self.run_python(
                "import sys; print('visible stdout'); print('visible stderr', file=sys.stderr)",
                live_output=True,
            )

        self.assertEqual(stdout.getvalue(), result.stdout)
        self.assertEqual(stderr.getvalue(), result.stderr)

    def test_large_stdout_and_stderr_are_drained_concurrently(self) -> None:
        source = (
            "import sys; "
            "sys.stdout.write('o' * 200000); sys.stdout.flush(); "
            "sys.stderr.write('e' * 200000); sys.stderr.flush()"
        )

        result = self.run_python(source, timeout=5)

        self.assertEqual(len(result.stdout), 200000)
        self.assertEqual(len(result.stderr), 200000)
        self.assertEqual(result.stdout_log.stat().st_size, 200000)
        self.assertEqual(result.stderr_log.stat().st_size, 200000)

    def test_failed_start_is_logged_before_exception(self) -> None:
        missing_program = self.root / "missing-executable"

        with self.assertRaises(CommandExecutionError) as raised:
            self.runner.run(missing_program, cwd=self.root)

        result = raised.exception.result
        self.assertIsNone(result.returncode)
        self.assertTrue(result.stderr)
        self.assertEqual(
            result.stderr_log.read_text(encoding="utf-8"),
            result.stderr,
        )
        metadata = json.loads(result.metadata_log.read_text(encoding="utf-8"))
        self.assertIsNone(metadata["returncode"])
        self.assertFalse(metadata["timed_out"])

    def test_args_rejects_a_single_string(self) -> None:
        with self.assertRaises(TypeError):
            self.runner.run(sys.executable, "-V", dry_run=True)


if __name__ == "__main__":
    unittest.main()

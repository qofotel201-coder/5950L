"""Unit tests for deterministic external-tool resolution."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from cfdpipe.toolchain import (
    TOOL_NAMES,
    ToolResolutionError,
    Toolchain,
)


CONNECTION_OUTPUT = Path(__file__).resolve().parents[1] / "runs" / "connection"


class ToolchainTests(unittest.TestCase):
    def setUp(self) -> None:
        CONNECTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        self._temporary_directory = tempfile.TemporaryDirectory(
            dir=CONNECTION_OUTPUT
        )
        self.root = Path(self._temporary_directory.name)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    def _executable(self, name: str) -> Path:
        path = self.root / name
        path.write_text("test executable\n", encoding="utf-8")
        path.chmod(
            path.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        return path.resolve()

    def _config(
        self,
        *,
        gmsh: str | None = None,
        pvbatch: str | None = None,
        allow_mnt: bool = False,
    ) -> Path:
        path = self.root / "tools.json"
        tools = {name: None for name in TOOL_NAMES}
        tools["gmsh"] = gmsh
        tools["pvbatch"] = pvbatch
        path.write_text(
            json.dumps(
                {
                    "allow_mnt_c_executables": allow_mnt,
                    "tools": tools,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_resolution_priority_cli_env_config_then_which(self) -> None:
        cli = self._executable("cli-gmsh")
        env = self._executable("env-gmsh")
        configured = self._executable("configured-gmsh")
        discovered = self._executable("which-gmsh")
        config = self._config(gmsh=str(configured))

        result = Toolchain(
            config,
            cli_overrides={"gmsh": cli},
            environ={"CFD_GMSH": str(env)},
            which=lambda _name: str(discovered),
        ).resolve("gmsh")
        self.assertEqual((result.path, result.source), (cli, "cli"))

        result = Toolchain(
            config,
            environ={"CFD_GMSH": str(env)},
            which=lambda _name: str(discovered),
        ).resolve("gmsh")
        self.assertEqual((result.path, result.source), (env, "env"))

        result = Toolchain(
            config,
            environ={},
            which=lambda _name: str(discovered),
        ).resolve("gmsh")
        self.assertEqual(
            (result.path, result.source), (configured, "config")
        )

        result = Toolchain(
            self._config(),
            environ={},
            which=lambda _name: str(discovered),
        ).resolve("gmsh")
        self.assertEqual((result.path, result.source), (discovered, "which"))

    def test_resolved_path_is_absolute_and_result_has_required_fields(self) -> None:
        relative_name = "relative-gmsh"
        expected = self._executable(relative_name)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            result = Toolchain(
                self._config(),
                cli_overrides={"gmsh": relative_name},
                environ={},
                which=lambda _name: None,
            ).resolve("gmsh")
        finally:
            os.chdir(previous)

        self.assertEqual(result.name, "gmsh")
        self.assertEqual(result.path, expected)
        self.assertTrue(result.path.is_absolute())
        self.assertEqual(result.source, "cli")

    def test_missing_tool_reports_error(self) -> None:
        resolver = Toolchain(self._config(), environ={}, which=lambda _name: None)
        with self.assertRaisesRegex(ToolResolutionError, "found nothing"):
            resolver.resolve("gmsh")

    def test_optional_tool_returns_none_only_when_no_source_exists(self) -> None:
        resolver = Toolchain(self._config(), environ={}, which=lambda _name: None)
        self.assertIsNone(resolver.resolve_optional("su2_sol"))

    def test_optional_tool_does_not_hide_invalid_explicit_source(self) -> None:
        missing = self.root / "missing-su2-sol"
        resolver = Toolchain(
            self._config(),
            environ={"CFD_SU2_SOL": str(missing)},
            which=lambda _name: None,
        )
        with self.assertRaisesRegex(ToolResolutionError, "Invalid env candidate"):
            resolver.resolve_optional("su2_sol")

    def test_default_which_uses_only_injected_path(self) -> None:
        for injected_path in (str(self.root / "custom-bin"), ""):
            with self.subTest(path=injected_path), mock.patch(
                "cfdpipe.toolchain.shutil.which", return_value=None
            ) as mocked_which:
                resolver = Toolchain(
                    self._config(),
                    environ={"PATH": injected_path},
                )
                with self.assertRaises(ToolResolutionError):
                    resolver.resolve("gmsh")
                mocked_which.assert_called_once_with("gmsh", path=injected_path)

    def test_missing_injected_path_does_not_use_host_path(self) -> None:
        with mock.patch(
            "cfdpipe.toolchain.shutil.which", return_value=None
        ) as mocked_which:
            resolver = Toolchain(self._config(), environ={})
            with self.assertRaises(ToolResolutionError):
                resolver.resolve("gmsh")
            mocked_which.assert_called_once_with("gmsh", path="")

    def test_mpi_which_prefers_mpirun_then_falls_back_to_mpiexec(self) -> None:
        mpirun = self._executable("mpirun")
        calls: list[str] = []

        def find_mpirun(name: str) -> str | None:
            calls.append(name)
            return str(mpirun) if name == "mpirun" else None

        result = Toolchain(
            self._config(), environ={}, which=find_mpirun
        ).resolve("mpiexec")
        self.assertEqual(result.path, mpirun)
        self.assertEqual(calls, ["mpirun"])

        mpiexec = self._executable("mpiexec")
        calls.clear()

        def find_mpiexec(name: str) -> str | None:
            calls.append(name)
            return str(mpiexec) if name == "mpiexec" else None

        result = Toolchain(
            self._config(), environ={}, which=find_mpiexec
        ).resolve("mpiexec")
        self.assertEqual(result.path, mpiexec)
        self.assertEqual(calls, ["mpirun", "mpiexec"])

    def test_invalid_high_priority_candidate_does_not_fall_back(self) -> None:
        configured = self._executable("configured-gmsh")
        discovered = self._executable("which-gmsh")
        missing = self.root / "missing-gmsh"
        resolver = Toolchain(
            self._config(gmsh=str(configured)),
            environ={"CFD_GMSH": str(missing)},
            which=lambda _name: str(discovered),
        )

        with self.assertRaises(ToolResolutionError) as raised:
            resolver.resolve("gmsh")

        message = str(raised.exception)
        self.assertIn("Invalid env candidate", message)
        self.assertIn("Lower-priority sources were not considered", message)

    def test_shutil_which_windows_directory_is_rejected(self) -> None:
        resolver = Toolchain(
            self._config(),
            environ={},
            which=lambda _name: r"C:\Windows\System32\gmsh.exe",
        )
        with self.assertRaisesRegex(
            ToolResolutionError, "silent Windows executable"
        ):
            resolver.resolve("gmsh")

    def test_normalized_windows_directory_is_rejected(self) -> None:
        resolver = Toolchain(
            self._config(),
            environ={},
            which=lambda _name: r"C:\Temp\..\Windows\System32\gmsh.exe",
        )
        with self.assertRaisesRegex(
            ToolResolutionError, "silent Windows executable"
        ):
            resolver.resolve("gmsh")

    def test_pvbatch_resolution_rejects_paraview_gui_and_other_basenames(self) -> None:
        for filename in ("paraview.exe", "pvpython.exe", "pvbatch-helper.exe"):
            with self.subTest(filename=filename):
                forbidden = self._executable(filename)
                resolver = Toolchain(
                    self._config(pvbatch=str(forbidden)),
                    environ={},
                    which=lambda _name: None,
                )
                with self.assertRaisesRegex(
                    ToolResolutionError,
                    "pvbatch must resolve to pvbatch",
                ):
                    resolver.resolve("pvbatch")

    def test_mnt_c_executable_is_rejected_without_config_permission(self) -> None:
        resolver = Toolchain(
            self._config(allow_mnt=False),
            environ={"CFD_GMSH": "/mnt/c/tools/gmsh.exe"},
            which=lambda _name: None,
        )
        with self.assertRaises(ToolResolutionError) as raised:
            resolver.resolve("gmsh")
        self.assertIn("/mnt/c Windows executable is disallowed", str(raised.exception))
        self.assertIn("config/tools.json", str(raised.exception))

    def test_normalized_mnt_c_variants_are_rejected(self) -> None:
        for candidate in (
            "//mnt/c/tools/gmsh.exe",
            "/mnt/../mnt/c/tools/gmsh.exe",
        ):
            with self.subTest(candidate=candidate):
                resolver = Toolchain(
                    self._config(allow_mnt=False),
                    environ={"CFD_GMSH": candidate},
                    which=lambda _name: None,
                )
                with self.assertRaisesRegex(
                    ToolResolutionError, "/mnt/c Windows executable is disallowed"
                ):
                    resolver.resolve("gmsh")

    def test_resolved_symlink_target_mnt_c_is_rejected(self) -> None:
        candidate = self._executable("apparently-native-gmsh")
        resolver = Toolchain(
            self._config(allow_mnt=False),
            environ={"CFD_GMSH": str(candidate)},
            which=lambda _name: None,
        )
        with mock.patch.object(
            Path,
            "resolve",
            return_value=PurePosixPath("/mnt/c/tools/gmsh.exe"),
        ):
            with self.assertRaisesRegex(
                ToolResolutionError, "disallowed after path normalization"
            ):
                resolver.resolve("gmsh")

    def test_config_can_explicitly_allow_mnt_c_executable(self) -> None:
        resolver = Toolchain(
            self._config(allow_mnt=True),
            environ={"CFD_GMSH": "/mnt/c/tools/gmsh.exe"},
            which=lambda _name: None,
        )
        if os.name != "nt":
            with self.assertRaisesRegex(ToolResolutionError, "Windows .exe"):
                resolver.resolve("gmsh")
            return
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch(
            "cfdpipe.toolchain.os.access", return_value=True
        ):
            result = resolver.resolve("gmsh")

        self.assertEqual(result.source, "env")
        self.assertTrue(result.path.is_absolute())

    def test_check_stops_at_first_resolution_failure(self) -> None:
        discovered = self._executable("gmsh")
        calls: list[str] = []

        def fake_which(name: str) -> str | None:
            calls.append(name)
            return str(discovered) if name == "gmsh" else None

        resolver = Toolchain(self._config(), environ={}, which=fake_which)
        with self.assertRaises(ToolResolutionError):
            resolver.check(("gmsh", "su2_cfd", "pvbatch"))
        self.assertEqual(calls, ["gmsh", "SU2_CFD"])


if __name__ == "__main__":
    unittest.main()

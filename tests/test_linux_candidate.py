from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from cfdpipe.bridges.paraview_bridge import ParaViewBridge
from cfdpipe.bridges.su2_bridge import SU2Bridge
from cfdpipe.toolchain import ToolResolutionError, Toolchain


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_HASH = "7b36083bb410fa27c5d0e052929d1a9844a5b09169d66017b72b41aabd49d711"
LINUX_HASH = "4076a948ce22625330d1413d4982e22b5c69fc2f0f7951f5df64c778cf54108c"


@unittest.skipUnless(os.name == "posix", "Linux/POSIX contract")
class LinuxCandidateTests(unittest.TestCase):
    def test_shell_scripts_have_valid_bash_syntax_and_execute_bits(self) -> None:
        scripts = [ROOT / "scripts/cfdpipe.sh", *(ROOT / "scripts/repro").glob("*.sh")]
        for script in scripts:
            completed = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(script.stat().st_mode & stat.S_IXUSR, script)

    def test_launcher_forwards_argument_array_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cfdpipe linux ") as temporary:
            fake_python = Path(temporary) / "python shim"
            capture = Path(temporary) / "argv.txt"
            fake_python.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)
            env = dict(os.environ, CFD_PYTHON=str(fake_python), CAPTURE=str(capture))
            completed = subprocess.run([str(ROOT / "scripts/cfdpipe.sh"), "tools", "value with spaces"], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                capture.read_text(encoding="utf-8").splitlines(),
                ["-m", "cfdpipe", "tools", "value with spaces"],
            )

    def test_native_absolute_tool_and_windows_paths_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); native = root / "SU2_CFD"; native.write_text("x"); native.chmod(0o700)
            config = root / "tools.json"; config.write_text(json.dumps({"allow_mnt_c_executables": False, "tools": {"su2_cfd": str(native)}}))
            self.assertEqual(Toolchain(config_path=config, environ={}).resolve("su2_cfd").path, native.resolve())
            windows_exe = root / "SU2_CFD.exe"; windows_exe.write_text("x"); windows_exe.chmod(0o700)
            for bad in ("/mnt/c/tools/SU2_CFD.exe", str(windows_exe)):
                config.write_text(json.dumps({"allow_mnt_c_executables": False, "tools": {"su2_cfd": bad}}))
                with self.assertRaises(ToolResolutionError): Toolchain(config_path=config, environ={}).resolve("su2_cfd")

    def test_subprocess_calls_use_argument_lists_and_no_shell_true(self) -> None:
        for path in [*ROOT.glob("src/**/*.py"), *ROOT.glob("scripts/**/*.py")]:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", source, path)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"run", "Popen"}:
                    for keyword in node.keywords:
                        if keyword.arg == "shell": self.assertIsInstance(keyword.value, ast.Constant); self.assertFalse(keyword.value.value)

    def test_mpi_and_pvbatch_commands_are_arrays(self) -> None:
        runner = mock.Mock(); runner.run.return_value = mock.Mock(returncode=0)
        SU2Bridge(runner, Path("/opt/su2/bin/SU2_CFD"), Path("/usr/bin/mpiexec")).run(Path("case.cfg"), nproc=2)
        executable = runner.run.call_args.args[0]; args = runner.run.call_args.kwargs["args"]
        self.assertEqual(str(executable), "/usr/bin/mpiexec"); self.assertEqual(args[:3], ["-np", "2", "/opt/su2/bin/SU2_CFD"])
        bridge = ParaViewBridge(runner, Path("/opt/paraview/bin/pvbatch"), ROOT / "scripts/paraview")
        command = bridge.build_command("probe_pvbatch.py", ["--output", "path with spaces.json"])
        self.assertIsInstance(command, list); self.assertEqual(command[-1], "path with spaces.json")

    def test_gmsh_version_once_and_both_hashes(self) -> None:
        text = (ROOT / "requirements-gmsh.txt").read_text(encoding="utf-8")
        self.assertEqual(text.count("gmsh==4.15.2"), 1)
        self.assertIn(WINDOWS_HASH, text); self.assertIn(LINUX_HASH, text)

    def test_linux_platform_is_candidate_only(self) -> None:
        contract = json.loads((ROOT / "config/reproducibility.json").read_text())
        linux = contract["platforms"]["linux_ubuntu_22_04_x86_64"]
        self.assertEqual(linux["status"], "CANDIDATE_PENDING_REMOTE_VERIFICATION")
        self.assertNotIn("VERIFIED", linux["status"])
        self.assertFalse(linux["production_mesh_verified"]); self.assertFalse(linux["mpi_verified"]); self.assertFalse(linux["gpu_verified"])

    def test_environment_probe_keeps_unverified_tool_versions_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / "bin"; bin_dir.mkdir()
            execution_marker = root / "external_tool_was_executed"
            for name in ("gmsh", "SU2_CFD", "SU2_SOL", "mpiexec", "pvbatch"):
                executable = bin_dir / name
                executable.write_text(
                    f"#!/usr/bin/env bash\nprintf ran > {execution_marker!s}\n",
                    encoding="utf-8",
                )
                executable.chmod(0o700)
            output = root / "environment.json"
            env = dict(
                os.environ,
                CFD_PYTHON=sys.executable,
                PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            )
            completed = subprocess.run(
                [
                    str(ROOT / "scripts/repro/check_linux_environment.sh"),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "INCOMPLETE")
            self.assertFalse(report["completion_checks"]["external_tool_versions_match"])
            self.assertFalse(report["policy"]["external_tools_executed_for_version_probe"])
            self.assertFalse(execution_marker.exists())
            for tool in report["tools"].values():
                self.assertEqual(tool["status"], "FOUND_VERSION_UNVERIFIED")
                self.assertEqual(tool["version_status"], "UNVERIFIED")

    def test_l0_l1_runner_uses_acceptance_step_names(self) -> None:
        source = (ROOT / "scripts/repro/run_linux_l0_l1.sh").read_text(
            encoding="utf-8"
        )
        for name in (
            "repository_preflight",
            "cad_free_tests",
            "compileall",
            "git_diff_check",
        ):
            self.assertIn(f"run_step {name} ", source)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import tempfile
import unittest

from cfdpipe.process import CommandRunner


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROCESS_MODULE = (REPOSITORY_ROOT / "src" / "cfdpipe" / "process.py").resolve()
CONNECTION_OUTPUT = REPOSITORY_ROOT / "runs" / "connection"


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is not None:
            return f"{parent}.{node.attr}"
    return None


class ExternalProcessPolicyTests(unittest.TestCase):
    def test_controlling_python_never_imports_paraview(self) -> None:
        violations: list[str] = []
        for path in sorted((REPOSITORY_ROOT / "src").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [node.module or ""]
                else:
                    continue
                if any(
                    name == "paraview" or name.startswith("paraview.")
                    for name in imported
                ):
                    violations.append(f"{path}:{node.lineno}: {imported}")
        self.assertEqual(violations, [])

    def test_only_command_runner_creates_external_processes(self) -> None:
        python_files = sorted(
            path.resolve()
            for root in (REPOSITORY_ROOT / "src", REPOSITORY_ROOT / "scripts")
            if root.exists()
            for path in root.rglob("*.py")
        )
        violations: list[str] = []
        allowed_popen_calls: list[ast.Call] = []

        for path in python_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            aliases: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        aliases[alias.asname or alias.name.split(".")[0]] = alias.name
                        if alias.name == "subprocess" and path != PROCESS_MODULE:
                            violations.append(f"{path}:{node.lineno}: imports subprocess")
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        aliases[alias.asname or alias.name] = f"{module}.{alias.name}"
                        if module == "subprocess" and path != PROCESS_MODULE:
                            violations.append(
                                f"{path}:{node.lineno}: imports from subprocess"
                            )

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                dotted = _dotted_name(node.func)
                if dotted is None:
                    continue
                head, separator, tail = dotted.partition(".")
                expanded = aliases.get(head, head) + (separator + tail if separator else "")
                forbidden = (
                    expanded
                    in {
                        "subprocess.Popen",
                        "subprocess.run",
                        "subprocess.call",
                        "subprocess.check_call",
                        "subprocess.check_output",
                        "os.system",
                        "os.popen",
                        "os.startfile",
                        "asyncio.create_subprocess_exec",
                        "asyncio.create_subprocess_shell",
                        "multiprocessing.Process",
                    }
                    or expanded.startswith("os.spawn")
                    or expanded.startswith("os.exec")
                    or "CreateProcess" in expanded
                )
                if not forbidden:
                    continue
                if path == PROCESS_MODULE and expanded == "subprocess.Popen":
                    allowed_popen_calls.append(node)
                else:
                    violations.append(f"{path}:{node.lineno}: calls {expanded}")

        self.assertEqual(violations, [])
        self.assertEqual(len(allowed_popen_calls), 1)
        popen = allowed_popen_calls[0]
        shell_keywords = [keyword for keyword in popen.keywords if keyword.arg == "shell"]
        self.assertEqual(len(shell_keywords), 1)
        self.assertIsInstance(shell_keywords[0].value, ast.Constant)
        self.assertIs(shell_keywords[0].value.value, False)
        self.assertTrue(popen.args)
        self.assertIsInstance(popen.args[0], ast.Name)
        self.assertEqual(popen.args[0].id, "command")

    def test_module_cli_runs_with_the_current_python_interpreter(self) -> None:
        CONNECTION_OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=CONNECTION_OUTPUT) as temporary:
            runner = CommandRunner(Path(temporary) / "cli-logs")
            existing_pythonpath = os.environ.get("PYTHONPATH")
            source_path = str((REPOSITORY_ROOT / "src").resolve())
            pythonpath = (
                source_path
                if not existing_pythonpath
                else source_path + os.pathsep + existing_pythonpath
            )
            result = runner.run(
                sys.executable,
                args=("-m", "cfdpipe", "--help"),
                cwd=REPOSITORY_ROOT,
                env={"PYTHONPATH": pythonpath},
                timeout=30,
                check=True,
                live_output=False,
            )
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()

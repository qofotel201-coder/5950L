#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
OUTPUT=""
VERIFY_TOOLS=false
if [[ "${1:-}" == "--verify-tools" ]]; then
    VERIFY_TOOLS=true
    shift
fi
if [[ "${1:-}" == "--output" ]]; then
    [[ $# -eq 2 ]] || { echo "usage: $0 [--verify-tools] [--output] <output.json>" >&2; exit 2; }
    OUTPUT="$2"
elif [[ $# -eq 1 ]]; then
    OUTPUT="$1"
else
    echo "usage: $0 [--verify-tools] [--output] <output.json>" >&2
    exit 2
fi
[[ ! -e "${OUTPUT}" ]] || { echo "refusing to overwrite: ${OUTPUT}" >&2; exit 2; }
mkdir -p -- "$(dirname -- "${OUTPUT}")"
PYTHON_EXECUTABLE="${CFD_PYTHON:-$(command -v python3 || command -v python)}"

"${PYTHON_EXECUTABLE}" - "${REPOSITORY_ROOT}" "${OUTPUT}" "${VERIFY_TOOLS}" <<'PY'
import importlib.metadata, importlib.util, json, os, platform, shutil, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

root, output = Path(sys.argv[1]), Path(sys.argv[2])
verify_tools = sys.argv[3] == "true"
contract = json.loads((root / "config/reproducibility.json").read_text(encoding="utf-8"))
linux_contract = contract["platforms"]["linux_ubuntu_22_04_x86_64"]
tool_contract = contract["verified_toolchain"]
def read(path):
    try: return Path(path).read_text(encoding="utf-8").strip()
    except OSError: return None
def run(argv):
    completed = subprocess.run(argv, cwd=root, text=True, capture_output=True, check=False)
    return {"argv": argv, "returncode": completed.returncode, "stdout": completed.stdout.strip(), "stderr": completed.stderr.strip()}
def os_release():
    values = {}
    for line in (read("/etc/os-release") or "").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values
def tool(name, expected_version=None):
    value = shutil.which(name)
    rejected = bool(value and (value.lower().endswith(".exe") or value.replace("\\", "/").lower().startswith("/mnt/c/")))
    status = "REJECTED" if rejected else ("FOUND_VERSION_UNVERIFIED" if value else "MISSING")
    item = {"status": status, "path": value, "expected_version": expected_version, "observed_version": None, "version_status": "UNVERIFIED" if value and not rejected else "NOT_CHECKED", "probe": None}
    if verify_tools and value and not rejected:
        args = [value, "--version"] if name in ("gmsh", "mpiexec", "pvbatch") else [value, "--help"]
        probe = run(args)
        text = probe["stdout"] + "\n" + probe["stderr"]
        if name == "gmsh": observed, match = ("4.15.2" if "4.15.2" in text else None), "4.15.2" in text
        elif name in ("SU2_CFD", "SU2_SOL"): observed, match = ("8.5.0" if "8.5.0" in text else None), "8.5.0" in text
        elif name == "pvbatch": observed, match = ("6.2.x" if "6.2" in text else None), "6.2" in text
        else: observed, match = (text.splitlines()[0] if text.strip() else None), probe["returncode"] == 0
        item.update({"status": "PASS" if probe["returncode"] == 0 and match else "VERSION_MISMATCH", "observed_version": observed, "version_status": "MATCH" if probe["returncode"] == 0 and match else "MISMATCH", "probe": probe})
    return item

release = os_release()
expected = {
  "gmsh": tool_contract["gmsh_cli"]["version"],
  "SU2_CFD": tool_contract["su2_cfd"]["version"],
  "SU2_SOL": tool_contract["su2_sol"]["version"],
  "mpiexec": None,
  "pvbatch": tool_contract["paraview_pvbatch"]["version"],
}
tools = {name: tool(name, expected[name]) for name in expected}
gmsh_spec = importlib.util.find_spec("gmsh")
try:
    gmsh_module_version = importlib.metadata.version("gmsh") if gmsh_spec else None
except importlib.metadata.PackageNotFoundError:
    gmsh_module_version = None
if gmsh_spec and gmsh_module_version is None and verify_tools:
    import gmsh
    gmsh_module_version = gmsh.__version__
expected_os = linux_contract["operating_system"]
os_match = release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "22.04"
arch_match = platform.system() == "Linux" and platform.machine() == linux_contract["architecture"]
python_expected = tool_contract["python"]["version"]
python_match = platform.python_version() == python_expected
gmsh_module_match = gmsh_module_version == tool_contract["gmsh_python_api"]["version"]
required = ("gmsh", "SU2_CFD", "mpiexec", "pvbatch")
tool_versions_match = all(tools[name]["version_status"] == "MATCH" for name in required)
complete = os_match and arch_match and python_match and gmsh_module_match and tool_versions_match
payload = {
  "schema": "cfdpipe.linux_environment.v1",
  "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
  "status": "PASS" if complete else "INCOMPLETE",
  "platform": {"uname": run(["uname", "-a"]), "os_release": read("/etc/os-release"), "machine": platform.machine(), "expected_operating_system": expected_os, "os_match": os_match, "architecture_match": arch_match},
  "resources": {"cpu_max": read("/sys/fs/cgroup/cpu.max"), "memory_max": read("/sys/fs/cgroup/memory.max"), "disk": run(["df", "-Pk", str(root)])},
  "python": {"path": sys.executable, "version": platform.python_version(), "expected_version": python_expected, "version_match": python_match, "gmsh_module": "FOUND" if gmsh_spec else "MISSING", "gmsh_module_version": gmsh_module_version, "gmsh_module_expected_version": tool_contract["gmsh_python_api"]["version"], "gmsh_module_version_match": gmsh_module_match},
  "git": {"head": run(["git", "rev-parse", "HEAD"]), "status": run(["git", "status", "--short"])},
  "tools": tools,
  "policy": {"mnt_c_or_windows_executables_rejected": all(not ((v["path"] or "").lower().endswith(".exe") or (v["path"] or "").replace("\\", "/").lower().startswith("/mnt/c/")) for v in tools.values() if v["status"] != "REJECTED"), "missing_or_unverified_production_tools_status": "INCOMPLETE", "external_tools_executed_for_version_probe": verify_tools},
  "completion_checks": {"os_match": os_match, "architecture_match": arch_match, "python_version_match": python_match, "gmsh_module_version_match": gmsh_module_match, "external_tool_versions_match": tool_versions_match}
}
output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"output": str(output), "status": payload["status"]}, sort_keys=True))
PY

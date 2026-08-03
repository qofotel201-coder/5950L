#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then DRY_RUN=true; shift; fi
if (( $# != 0 )); then echo "usage: $0 [--dry-run]" >&2; exit 2; fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
PYTHON_EXECUTABLE="${CFD_PYTHON:-$(command -v python3 || command -v python)}"

[[ "$(uname -s)" == "Linux" ]] || { echo "ERROR: Linux is required" >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "ERROR: x86_64 is required" >&2; exit 1; }
[[ -f "${REPOSITORY_ROOT}/AGENTS.md" && -d "${REPOSITORY_ROOT}/src/cfdpipe" ]] || { echo "ERROR: invalid repository root: ${REPOSITORY_ROOT}" >&2; exit 1; }
"${PYTHON_EXECUTABLE}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || { echo "ERROR: Python >=3.12 is required" >&2; exit 1; }
"${PYTHON_EXECUTABLE}" -m pip --version >/dev/null || { echo "ERROR: pip is unavailable" >&2; exit 1; }

INSTALL=("${PYTHON_EXECUTABLE}" -m pip install --require-hashes -r "${REPOSITORY_ROOT}/requirements-gmsh.txt")
printf 'repository=%s\npython=%s\nmode=%s\n' "${REPOSITORY_ROOT}" "${PYTHON_EXECUTABLE}" "$([[ "${DRY_RUN}" == true ]] && echo DRY_RUN || echo CHECK_ONLY)"
printf 'suggested_dependency_command='; printf '%q ' "${INSTALL[@]}"; printf '\n'
echo "No packages or system files were modified."

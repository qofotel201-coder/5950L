#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
PYTHON_EXECUTABLE="${CFD_PYTHON:-${VIRTUAL_ENV:+${VIRTUAL_ENV}/bin/python}}"
if [[ -z "${PYTHON_EXECUTABLE}" ]]; then
    PYTHON_EXECUTABLE="$(command -v python3 || command -v python)"
fi
if [[ ! -x "${PYTHON_EXECUTABLE}" ]]; then
    echo "Python executable is not available: ${PYTHON_EXECUTABLE}" >&2
    exit 2
fi

export PYTHONPATH="${REPOSITORY_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
cd -- "${REPOSITORY_ROOT}"
COMMAND=("${PYTHON_EXECUTABLE}" -m cfdpipe "$@")
exec "${COMMAND[@]}"

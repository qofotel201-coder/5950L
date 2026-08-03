#!/usr/bin/env bash
set -euo pipefail

OUTPUT=""
while (( $# )); do
    case "$1" in
        --output) [[ $# -ge 2 ]] || { echo "--output requires a directory" >&2; exit 2; }; OUTPUT="$2"; shift 2 ;;
        *) echo "usage: $0 --output <directory>" >&2; exit 2 ;;
    esac
done
[[ -n "${OUTPUT}" ]] || { echo "usage: $0 --output <directory>" >&2; exit 2; }
[[ ! -e "${OUTPUT}" ]] || { echo "refusing to overwrite evidence directory: ${OUTPUT}" >&2; exit 2; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
PYTHON_EXECUTABLE="${CFD_PYTHON:-$(command -v python3 || command -v python)}"
mkdir -p -- "${OUTPUT}"
OUTPUT="$(cd -- "${OUTPUT}" && pwd -P)"
RESULTS="${OUTPUT}/steps.tsv"
: > "${RESULTS}"

run_step() {
    local name="$1"; shift
    local started ended rc
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    set +e
    "$@" >"${OUTPUT}/${name}.stdout.log" 2>"${OUTPUT}/${name}.stderr.log"
    rc=$?
    set -e
    ended="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf '%s\t%s\t%s\t%s\n' "${name}" "${started}" "${ended}" "${rc}" >> "${RESULTS}"
    if (( rc != 0 )); then finalize FAIL "${name}"; exit "${rc}"; fi
}

finalize() {
    local status="$1" failed="${2:-}"
    "${PYTHON_EXECUTABLE}" - "${RESULTS}" "${OUTPUT}" "${status}" "${failed}" <<'PY'
import json, sys
from pathlib import Path
steps=[]
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    name, start, end, rc = line.split("\t")
    steps.append({"name":name,"started_at_utc":start,"ended_at_utc":end,"returncode":int(rc),"stdout":f"{name}.stdout.log","stderr":f"{name}.stderr.log"})
payload={"schema":"cfdpipe.linux_l0_l1_summary.v1","status":sys.argv[3],"failed_step":sys.argv[4] or None,"steps":steps}
out=Path(sys.argv[2]); (out/"summary.json").write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")
lines=["# Linux L0/L1 evidence", "", f"Status: **{payload['status']}**", "", "| Step | Return code | Started UTC | Ended UTC |", "|---|---:|---|---|"]
lines += [f"| {s['name']} | {s['returncode']} | {s['started_at_utc']} | {s['ended_at_utc']} |" for s in steps]
(out/"summary.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
PY
}

cd -- "${REPOSITORY_ROOT}"
export PYTHONPATH="${REPOSITORY_ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
run_step repository_preflight "${PYTHON_EXECUTABLE}" -B scripts/repro/verify_repository.py
run_step cad_free_tests "${PYTHON_EXECUTABLE}" -B scripts/repro/run_cad_free_tests.py
run_step compileall "${PYTHON_EXECUTABLE}" -B -m compileall -q src tests scripts
run_step git_diff_check git diff --check
finalize PASS
echo "Linux L0/L1 evidence: ${OUTPUT}"

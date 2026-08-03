# AutoDL Pilot repair execution plan

Status: `READY_NOT_EXECUTED`. L6 is PASS, but this plan does not authorize a production mesh, SU2, ParaView, CUDA, or any quality-threshold change.

## Current blocker

- The frozen Prism6 Scaled Jacobian gate is `0.01`; the retained direction-search endpoint has minimum `0.003730823` and 12 below-threshold prisms, all on stable source triangle prefix `9aaa2aed`, layers 49–60, wall fingerprint `8c1b…52864`.
- Core tetrahedra remain above their `0.001` gamma threshold (`0.001112365` minimum); nonpositive and nonfinite counts are zero.
- Static, fine and three-root direction searches are exhausted. Evidence identifies an outer-tail schedule mismatch at a two-wall junction, so the next bounded method is the existing root-schedule collar v5 with absolute-state replay and full-mesh A-B-A-B verification.
- Previous `_008` was stopped by the unchanged runtime memory guard; `_009` was interrupted and is not consumable. A new directory and at least 8.8 GiB available physical memory are required.

## Minimum transfer

Transfer only the five JSON files listed in `autodl_pilot_repair_execution_plan.json`, preserving their relative paths and SHA-256 values. Do not transfer the full `runs` tree. `config/coarse_mesh.toml` is tracked and has SHA-256 `4e4cbd25…e1f2`.

## Unique execution entry

Use the exact `unique_execution_entry` string in the JSON plan after all five files have been independently hash-verified on AutoDL. It invokes `pipeline coarse-repair-audit` in audit-only mode, uses a new `autodl_pilot_repair_<UTC>` output, retains the 840-second and memory guards, and does not write a mesh or call SU2/pvbatch.

The only permitted rendering operation is replacing the single `<UTC>` token with one basic-format UTC timestamp. The endpoint permits at most 4096 quality evaluations; its collar-v5 bound is 3730. Direction candidate sources and collar ring widths are exactly the enumerations in `execution_contract` and may not be extended or skipped.

# Medium mesh execution authorization

Status: **AUTHORIZED_NOT_BUILT**.

The accepted coarse mesh proves the frozen partition is spatially ordered as requested: farfield is coarsest, transition and near-body core are progressively refined, internal passage is finest, and the 60-layer prism stack supplies the finest wall-normal resolution. The medium mesh may therefore be generated with the same BREP, markers, region extents, transitions, boundary layer, and Pilot repair parameters.

The medium target is 10,000,000 cells with an allowed range of 8,000,000–12,000,000. All four volume sizes use the same `0.794` factor; the coarse-core cubic projection is 9,640,375 cells. The bound family-plan SHA-256 is `e1980dcbfce385f73d310423d5641ee8a23bee328aad2bcc2bac492cf668fd84`.

The exact command is the `unique_execution_entry` in `reports/medium_mesh_execution_authorization.json`. Only `<UNIQUE_RUN_ID>` may be expanded, to a new directory below `runs/mesh/medium`. This authorization does not authorize RANS or the fine mesh. The generated medium mesh must independently pass every production mesh quality and SU2 readback gate.

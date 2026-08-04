# AutoDL production medium mesh result

Status: **PASS**. Run `production_medium_20260805T030000Z` generated 9,860,365 three-dimensional cells, within the fixed 8–12 million contract and close to the 10 million target.

- The mesh contains 9,580,045 Tet4 and 280,320 Prism6 cells. All 4,672 wall columns retain the frozen 60 layers.
- Authoritative fresh readback minimum Scaled Jacobian is `0.010098230484760115`. Below-threshold, negative-volume, nonpositive-Jacobian, nonfinite-quality, marker-omission and shared-interface-error counts are all zero; Prism/Tet conformity passed.
- `mesh.msh` and `mesh.su2` were written and independently hashed. The bounded SU2 text audit passed, followed by an actual SU2 8.5.0 `INNER_ITER=0` preprocessing run with return code zero and empty stderr.
- ParaView 6.2 `pvbatch` rendered an isometric surface view and a center-plane mesh view through its bundled OSMesa llvmpipe backend. No GUI, Euler iteration, formal RANS, CUDA or fine mesh was run.
- The 317 MiB evidence archive was downloaded and re-hashed locally as `6fb78e70c9d1ff862d1798359944cc051dacd5232418d10480cce84b0c3237c8`; its machine report and all acceptance checks were parsed again as PASS.

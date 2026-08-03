# AutoDL Pilot repair audit result

- Status: **FAIL / production coarse mesh NOT APPROVED**
- Run: `autodl_pilot_repair_20260803T163705Z`
- Git SHA: `9191582aa3dcbc489bd28cd770daa0f8c7509952`
- Return code: `1`
- Threshold: `0.01`
- Initial minimum / below-threshold: `0.003730823` / `12`
- Accepted candidate: none
- Best observed but rejected: ring `32`, minimum `0.00773904276716142`, below-threshold `20`
- Evaluations: `1537` / `4096`
- Peak cgroup memory: `7062949888` bytes
- Blocker: All seven immutable collar candidates created low-quality prism lineage outside the frozen residual set; the best unaccepted minimum Scaled Jacobian remained below 0.01.
- SU2 / pvbatch / CUDA / mesh write: false / false / false / false

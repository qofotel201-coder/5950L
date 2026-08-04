# AutoDL production coarse mesh result

生产粗网格在 AutoDL 上通过全部硬门：4,965,647 个三维单元、935,410 个节点、60 层冻结边界层。280,320 个 Prism6 的最小 Scaled Jacobian 为 `0.010098248712453189`，阈下单元为0；负体积、非正 Jacobian、非有限质量、core gamma 阈下、marker 遗漏及共享接口错误均为0，棱柱与四面体核心共形。

`mesh.msh` 和 `mesh.su2` 均成功写出并通过 fresh readback/流式文本审计。SU2 8.5.0 以 `INNER_ITER=0` 实际读取并预处理933,844点、4,965,647体单元及4个marker，返回码0；没有执行 Euler 步、RANS、中网格或细网格。

证据包位于 `remote_evidence/production_coarse_production_coarse_occ_20260805T010000Z_s0918389/production_coarse_occ_20260805T010000Z_s0918389.tar.gz`，本机复算 SHA-256 为 `3866355f420db7acaaff96d5f949e83492bb06c2f678f3b712bd2dbf0c5288b0`，解包后的机器报告与主 manifest 均再次解析为 PASS。

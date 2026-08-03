# Production mesh family plan

本计划只授权生产粗网格。粗网格目标为500万单元，合法范围475万至525万；中网格和细网格分别规划为1000万和2000万，本阶段不得生成。

Pilot 已审计通过的壁面 `h=0.4 m` 三角化、60层边界层、首层高度、增长率、ring-32 层高和局部方向修复全部保持不变。单元量校准只作用于棱柱顶面之后的四面体核心，不允许通过减少层数或重建壁面三角化改变冻结证据链。

四个物理区域为 farfield、transition、near_body_core 和 internal_passage。farfield 最疏，近体核心和内流道逐级加密；每个区域使用固定空间范围与正的过渡宽度，尺寸在边界外连续恢复。粗、中、细的物理范围保持不变，尺寸统一乘以 `1.000/0.794/0.630`。实际单元数允许在合同范围内浮动，网格尺度比和 GCI 必须使用实际单元数计算。

AutoDL 首轮 OCC/HXT 校准 `production_coarse_occ_20260804T073000Z_s1000` 在原区域尺寸下得到166,405个核心四面体和280,320个冻结棱柱。按500万总量目标对四个区域统一应用立方根计数因子0.328；校准后粗级尺寸依次为farfield 0.05904 m、transition 0.03936 m、near-body 0.02624 m、internal-passage 0.01804 m。该调整不改变区域范围、过渡宽度、壁面三角化或60层边界层。

任何一级出现 Scaled Jacobian、体积、Jacobian、非有限质量、marker、共享接口或共形性失败，均禁止进入 RANS。只有粗网格加密区域经人工/机器证据确认合理且全部质量门通过后，才可单独授权生成中网格和细网格。

机器真源及唯一执行入口见 `reports/production_mesh_family_plan.json`。当前状态为 `CALIBRATION`；AutoDL 实测冻结最终体尺寸后才可改为 `FROZEN`。

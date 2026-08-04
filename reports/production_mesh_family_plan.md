# Production mesh family plan

本计划已冻结通过验收的生产粗网格分区，并单独授权生成中网格。粗网格目标为500万单元，合法范围475万至525万；中网格目标1000万、合法范围800万至1200万；细网格规划为2000万但仍未授权。本阶段审查本身不生成中网格。

Pilot 已审计通过的壁面 `h=0.4 m` 三角化、60层边界层、首层高度、增长率、ring-32 层高和局部方向修复全部保持不变。单元量校准只作用于棱柱顶面之后的四面体核心，不允许通过减少层数或重建壁面三角化改变冻结证据链。

四个物理区域为 farfield、transition、near_body_core 和 internal_passage。farfield 最疏，近体核心和内流道逐级加密；每个区域使用固定空间范围与正的过渡宽度，尺寸在边界外连续恢复。粗、中、细的物理范围保持不变，尺寸统一乘以 `1.000/0.794/0.630`。实际单元数允许在合同范围内浮动，网格尺度比和 GCI 必须使用实际单元数计算。

AutoDL 首轮 OCC/HXT 校准后继续按实际优化后单元数应用统一体尺寸系数 `0.9183890364757337`。冻结粗级尺寸依次为 farfield `0.05422168871352732 m`、transition `0.03614779247568488 m`、near-body `0.024098528317123252 m`、internal-passage `0.016567738218022234 m`。最终 run `production_coarse_occ_20260805T010000Z_s0918389` 得到4,965,647个三维单元并通过全部质量门；该调整未改变区域范围、过渡宽度、壁面三角化或60层边界层。

粗网格本体的 AutoDL 实测确认四面体特征尺度中位数依次为 farfield `0.0368541 m`、transition `0.0247716 m`、near-body `0.0164372 m`、internal-passage `0.0117414 m`；60层棱柱壁面法向厚度中位数为 `2.76986e-7 m`。因此“远场疏、近体核心密、内流道密、壁面法向密”同时得到尺寸场合同与实际网格证据支持。

任何一级出现 Scaled Jacobian、体积、Jacobian、非有限质量、marker、共享接口或共形性失败，均禁止进入 RANS。中网格获准生成，但生成后必须独立通过全部质量门；细网格仍须另行授权。

机器计划见 `reports/production_mesh_family_plan.json`，最终计划哈希绑定的唯一中网格入口见 `reports/medium_mesh_execution_authorization.json`。当前状态为 `COARSE_REGIONS_FROZEN_MEDIUM_AUTHORIZED`；不得自动生成细网格或启动 RANS。

# Production mesh family plan

本计划已完成粗—中系统加密一致性审计并授权生成约2000万单元细网格。粗网格为4,965,647单元；新的系统加密中网格为9,582,917单元。细网格尚未生成，RANS继续禁止。

Pilot 已审计通过的60层边界层、首层高度、增长率、ring-32层高和局部方向修复全部保持不变。中网格以确定性的整柱质心细分增加壁面切向分辨率：壁面三角形/柱列由4,672增加至7,412，面积等效切向尺度比为`0.7939327402`；所有新柱仍为60层。

四个物理区域为 farfield、transition、near_body_core 和 internal_passage。farfield 最疏，近体核心和内流道逐级加密；每个区域使用固定空间范围与正的过渡宽度，尺寸在边界外连续恢复。粗、中、细的物理范围保持不变，尺寸统一乘以 `1.000/0.794/0.630`。实际单元数允许在合同范围内浮动，网格尺度比和 GCI 必须使用实际单元数计算。

AutoDL 首轮 OCC/HXT 校准后继续按实际优化后单元数应用统一体尺寸系数 `0.9183890364757337`。冻结粗级尺寸依次为 farfield `0.05422168871352732 m`、transition `0.03614779247568488 m`、near-body `0.024098528317123252 m`、internal-passage `0.016567738218022234 m`。最终 run `production_coarse_occ_20260805T010000Z_s0918389` 得到4,965,647个三维单元并通过全部质量门；该调整未改变区域范围、过渡宽度、壁面三角化或60层边界层。

粗网格本体的 AutoDL 实测确认四面体特征尺度中位数依次为 farfield `0.0368541 m`、transition `0.0247716 m`、near-body `0.0164372 m`、internal-passage `0.0117414 m`；60层棱柱壁面法向厚度中位数为 `2.76986e-7 m`。因此“远场疏、近体核心密、内流道密、壁面法向密”同时得到尺寸场合同与实际网格证据支持。

粗—中四区实测P50尺寸比为farfield `0.808263`、transition `0.807300`、near-body `0.808320`、internal-passage `0.798706`，均通过冻结目标`0.794±0.02`。中网格最小Scaled Jacobian为`0.0100982487`，全部失败计数为0，marker与470面共享接口关系保持一致。

任何一级出现 Scaled Jacobian、体积、Jacobian、非有限质量、marker、共享接口或共形性失败，均禁止进入 RANS。约2000万细网格仅获生成授权，生成后仍必须独立通过全部质量门。

机器计划见 `reports/production_mesh_family_plan.json`，审计见`reports/coarse_medium_systematic_refinement_audit.json`。当前状态为`COARSE_MEDIUM_SYSTEMATIC_REFINEMENT_PASS_FINE_AUTHORIZED`；本轮未生成细网格且不得启动RANS。

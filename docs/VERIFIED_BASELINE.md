# 已验证基线

本页记录截至 2026-08-03 的实机基线。路径指向原开发电脑上的本地证据；`runs/` 不进入 Git，复刻电脑必须重新生成或按 [私有输入交接](PRIVATE_INPUTS.md) 取得匹配工件。

## 软件基线

| 组件 | 版本 | 本地证据 |
|---|---:|---|
| Python | 3.13.14 | `runs/connection/connection_report.json` 的 `python_to_gmsh` |
| cfdpipe | 0.1.0 | `src/cfdpipe/__init__.py` 与 `python -m cfdpipe --version` |
| Gmsh Python API | 4.15.2 | 同一连接报告及 Gmsh manifest |
| Gmsh CLI | 4.15.2 | 参数列表 `gmsh.exe --version`、返回码 0 |
| SU2_CFD | 8.5.0 “Harrier” | `runs/connection/su2/run_manifest.json` |
| ParaView / pvbatch | 6.2 | `runs/connection/paraview/paraview_manifest.json` |

参考 Python 包绑定在 `requirements-gmsh.txt`；外部工具版本合同位于 `config/reproducibility.json`。版本相同不等于功能已验证，仍需按层级重新执行连接测试。

## 通用小型连接：PASS

权威报告：

- 路径：`runs/connection/connection_report.json`
- SHA-256：`397c236a48e97932138f8a024d95e7f6906d3771aa0dd0596fb98cbf43fb88d0`
- 六段状态：`python_to_gmsh`、`gmsh_to_su2_mesh`、`python_to_su2`、`su2_to_paraview_file`、`python_to_pvbatch`、`pvbatch_to_results` 全部 PASS；`overall=PASS`。

实测内容：

- Gmsh：994 点、1,866 个一阶三角形、120 条 `farfield` 边，二维流体域为 `fluid`；
- SU2_CFD：串行读取同一网格并完成迭代 0～4，共 5 步，返回码 0；
- 输出：`history.csv` 与 `connection_solution.vtu`；VTU 为 994 点、1,866 单元；
- pvbatch：识别 `Unstructured Grid`，读取 Density、Momentum、Energy、Pressure、Temperature、Mach、Pressure_Coefficient、Velocity，并输出合法 JSON/CSV。

这是程序连接证据，不是物理算例。

## 真实项目小型连接：PASS

权威报告：

- 路径：`runs/real_connection/real_connection_report.json`
- SHA-256：`2c68ce829c131b2b154c1c294997e6209698ff96f9a1d2ceb43bf7c43fabc9a8`
- 六段 `step_to_gmsh`、`gmsh_to_su2_mesh`、`mesh_to_su2`、`su2_to_visualization_file`、`visualization_file_to_pvbatch`、`pvbatch_to_json` 全部 PASS；`overall=PASS`。

实测内容：

- 只读原始 STEP 作为 provenance；只读共享拓扑 BREP 是实际 pipeline geometry；
- 2 个流体体、9,049 节点、39,319 个一阶 Tet4；
- 四个 solver marker：`front_conical_surface=3120`、`rear_outlet_1=310`、`rear_outlet_2=1498`、`vehicle_internal_and_external_walls=4972`；
- 非正 Jacobian、SICN 和体积计数均为 0；Gmsh 的 8 个 ill-shaped tetra warning 被原样保留；
- SU2_CFD 8.5.0 串行完成 5 步并生成 VTU；pvbatch 读取 9,049 点、39,319 单元并输出 JSON/CSV；
- `connection_only=true`、`production_eligible=false`，五步结果不作物理解读。

## 真实小型边界层网格：PASS，仅诊断

权威 manifest：

- 路径：`runs/boundary_layer_quality/yplus_corrected_20260802_015/boundary_layer_smoke_manifest.json`
- SHA-256：`f679675a699455a9bf95939bf7d244234788c31642349105977f2773a16c9184`
- 网格 SHA-256：`mesh.su2=201b93477a3607af1944e1878e5a8d9c208fe610a43cf5213438837851c994f1`

实测内容：

- SU2 文本 49,345 点、117,551 个三维单元；
- 81,300 个 Prism6、36,251 个 Tet4；
- 5,420 个柱列，每列恰 15 层，48/48 wall 组覆盖率 1.0；
- fresh readback Prism 最小 `minSJ=0.0119748983`；核心 Tet4 最小 `gamma≈0.00348267`；
- 负体积、非正 Jacobian、非有限值、阈下 Prism 和阈下柱列均为 0；
- 四个 solver marker 完整；
- `smoke_only=true`、`production_mesh_eligible=false`，本阶段未调用 SU2 或 ParaView。

## 小型串行 SST RANS 与 y+：PASS，仅诊断

权威报告：

- 路径：`runs/rans_smoke/yplus_final_diagnostic200_001/rans_smoke_report.json`
- SHA-256：`96db28490f1b62801ac446ffe5007877ea1aeb9547e2bfcd94efa982c6a3b12b`
- ParaView 诊断 JSON SHA-256：`ec19750d76346ddd1ca289ade1d322078c6bbdfc92a865035200bef278859231`

实测内容：

- SU2_CFD 8.5.0，设计点 `M50_H21_A8_B0`，`nproc=1`，SST RANS，一阶诊断 200 步；
- pvbatch 成功读取本轮解；2,708 个唯一 wall 点全部有限且为正；
- 最大 `y+=0.92289495`，p99 `0.72532912`，超过 1.0 的点数和面积均为 0；
- 两个后部出口的采样均超声速且无回流；全局相对质量不平衡约 `3.54e-5`；
- `diagnostic_only=true`、`production_eligible=false`、`convergence_claimed=false`。

20 步启动与 200 步诊断是有界软件/质量门，不能作为正式收敛证明或生产载荷结果。

## 设计点后部出口语义：PASS，范围受限

权威报告：

- 路径：`runs/rear_outlet_freeze/design_case_20260802_003/rear_outlet_freeze_report.json`
- SHA-256：`ca74db17db0645f069deee019c4b7bda8bebcc1d585d27b85ec7a24735e74167`

Euler、一阶 SST RANS 和二阶 restart SST RANS 的哈希绑定证据共同支持 `M50_H21_A8_B0` 的 `rear_outlet_1/2` 使用 `MARKER_SUPERSONIC_OUTLET`。合同状态是 `FROZEN_FOR_DESIGN_CASE_ONLY`，仍明确 `production_eligible=false`。其它四个工况没有被外推。

## 当前未通过的粗网格质量门

正式粗网格和正式 CFD 尚未完成。当前可审计的设计点投影为 294,059 个三维单元，其中 280,320 个 Prism6、13,739 个核心 Tet4；该投影只用于小于 30 万单元的修复审计，不是项目配置要求的 5,000,000 单元粗网格。

现有 schedule-continuation 证据把残差收缩到 12 个局部 Prism，最小 `minSJ=0.003730823`，仍低于硬门 `0.01`。root-schedule collar v5 软件测试通过，但：

- 真实 `_008` 因运行期物理内存保护被合法中止，不是质量结论；
- 真实 `_009` 被会话中断且缺少终态 command/manifest，标记为 `INVALID_INTERRUPTED/NOT_CONSUMABLE`；
- 没有 `mesh.msh/mesh.su2` 生产写盘授权，没有调用 SU2 或 pvbatch。

因此不得声称正式粗网格、正式 RANS、粗/中/细网格无关性、MPI 或 GPU 已完成。

## 已知平台限定

- 已验证 SU2 包为 Windows `win64-omp`；MPI 只有命令构造测试，没有实际兼容性结论；
- ParaView 6.2 Windows 构建的 `pvbatch --help` 能力探测可能超时，工作流不会猜测 offscreen 参数；实际启动和脚本执行均有返回码 0 证据；
- Gmsh、SU2、ParaView 的程序路径属于本机信息，不进入 Git；
- WindowsApps 的 `python3.exe` 别名在原机器无效，Windows 使用仓库 `.venv\Scripts\python.exe` 或 `scripts/cfdpipe.ps1`；
- 没有满足仓库 GPU 证据规则，因此不得声称使用 GPU。

## 版本或输入发生变化时

以下任一变化都会使下游 PASS 证据失效：

- Python/Gmsh/SU2/ParaView 版本或可执行文件变化；
- 原始 STEP、派生 BREP、项目/工况/marker 配置变化；
- marker 几何指纹、网格、restart 或可视化文件哈希变化；
- 跨 run 混用 manifest 或输出文件。

变化后从 `verify_repository.py`、`tools check` 和通用 `connect-test` 开始，逐级重新取证；不要手工修改 manifest 或配置哈希延续旧 PASS。

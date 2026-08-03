# PLAN

## 本轮任务：AutoDL Pilot coupled direction/schedule audit-only 修复

状态：已完成并通过。以上一轮完整评估但未接受的 ring width 32 状态为显式起点，在同一局部闭包内联合调整外层累计高度与柱方向；未降低 `0.01` Scaled Jacobian 阈值，未修改 CAD、marker、首层高度、工况或边界条件，未写生产网格，未调用 SU2/pvbatch/RANS/CUDA。

### 最终状态

- `REMOTE_AUTODL_L6 = PASS`
- `PILOT_MESH_REPAIR = PASS`
- `PILOT_REPAIR_PARAMETERS = FROZEN`
- `PRODUCTION_COARSE_MESH = READY_TO_BUILD`
- `RANS = DIAGNOSTIC_L6_PASS`
- `PRODUCTION_CFD = HOLD`

### 最终结果与证据

- AutoDL 固定提交 `ba78d712e530167788d524f78102350252812b45` 的正式 run `autodl_coupled_repair_20260803T183059Z` 返回码为0；controller、worker isolation 与 authoritative discovery 三层状态均为 PASS，`profile_complete=true`。
- 接受候选为 ring width 32 层高计划加9个有界方向细化；总质量评估1990次，其中原方向搜索1530次、collar 7次、coupled方向细化453次，未超过4096次上限。
- 最小 Prism6 Scaled Jacobian 从未接受最佳值 `0.00773904276716142` 提升到 `0.010098248712453189`；阈下 Prism6 从20降为0，非正元素、非有限质量、core gamma阈下均为0。冻结阈值保持 `0.01`。
- 本轮为 audit-only：`mesh_written=false`、`production_mesh_eligible=false`，SU2、pvbatch、CUDA均未调用；结果只冻结下一次生产粗网格构建参数，不是生产网格或生产CFD结果。
- 远程证据位于 `/root/autodl-tmp/5950L/runs/pilot_repair/autodl_coupled_repair_20260803T183059Z` 与对应 `runs/mesh/coarse`/worker evidence；证据包为 `/root/autodl-tmp/transfer/autodl_coupled_repair_20260803T183059Z.tar.gz`。
- 本机回传目录为 `remote_evidence/pilot_repair_autodl_coupled_repair_20260803T183059Z/`；本机复算证据包 SHA-256 为 `965595818382d44e4027751e9b8c57c804010ef7eb67ba995971fb4b594cfdb1`，解包后再次解析 manifest 为 PASS、最小质量 `0.010098248712453189`、阈下计数0。

### 目标与验收标准

- 新策略必须保留上一轮七个 collar 候选及其 SHA-256 证据，以 ring width 32 的 `0.00773904276716142` 最佳未接受状态作为显式、可重放的 coupled 搜索起点；禁止目录搜索或隐式选择旧工件。
- 对 ring width 32 产生的全部当前阈下棱柱重新建立稳定 root/triangle/wall 闭包，并在该闭包内执行受限方向坐标下降；候选来源、角步长、评估上限和词典序质量目标必须写入机器合同，任何候选产生非正、非有限、core 退化或越界坐标立即回退。
- 质量目标严格为：非有限数、非正数、core 阈下数、阈下 Prism 数、最大 Scaled Jacobian 缺口、缺口范数、最小 Scaled Jacobian、方向改变量；不得以迁移低质量 lineage 作为改善，最终必须对全局 Prism/core 重新评估。
- 仅当最小 Prism Scaled Jacobian `>=0.01`、阈下 Prism `=0`、非正/非有限/core 阈下均为0、首层与至少15层原则不变、marker/interface/CAD哈希不变且 A-B-A-B 重放一致时，coupled audit 返回0并冻结参数。
- 全部本地专项与 CAD-free 回归、repository preflight、compileall、`git diff --check` 必须 PASS；AutoDL 使用固定新提交、干净跟踪工作区、全新 run 目录和 tmux 执行，保存所有候选、返回码、资源、失败单元及输入输出哈希。
- PASS 后才更新 `PILOT_MESH_REPAIR=PASS`、`PILOT_REPAIR_PARAMETERS=FROZEN`、`PRODUCTION_COARSE_MESH=READY_TO_BUILD`；本轮仍保持 `PRODUCTION_CFD=HOLD`，且不实际生成生产粗/中/细网格。

## 本轮任务：AutoDL Pilot audit-only 局部棱柱质量修复审计

状态：已完成审计，结论为 FAIL / NO-GO。严格消费 `reports/autodl_pilot_repair_execution_plan.json` 的 `unique_execution_entry`，仅展开 `<UTC>`；七个冻结 collar 候选全部完成评估，但均因产生冻结残差集合之外的新低质量棱柱 lineage 而不可接受。未写网格、未调用 SU2/pvbatch/RANS/CUDA，生产粗网格未获授权。

### 最终状态

- `REMOTE_AUTODL_L6 = PASS`
- `PILOT_MESH_REPAIR = FAIL_CANDIDATE_SET_EXHAUSTED`
- `PILOT_REPAIR_PARAMETERS = NOT_FROZEN`
- `PRODUCTION_COARSE_MESH = HOLD`
- `RANS = DIAGNOSTIC_L6_PASS`
- `PRODUCTION_CFD = HOLD`

### 实施与验证结果

- 本地、GitHub 与 AutoDL 最终审计提交固定为同一 SHA；五项最小历史 JSON 均为普通非链接文件，大小与计划 SHA-256 全部 PASS。唯一命令除输出目录 UTC 外未修改，最终 run 为 `autodl_pilot_repair_20260803T163705Z`。
- 控制器、worker、Gmsh、历史 provenance、4096 次评估上限与 audit-only 零写出门全部通过；实机暴露的路径绑定、Linux worker 资源、portable mesh lineage 和候选记录缺陷均以最小代码修复并经过专项与完整 CAD-free 回归。
- collar 七个 ring width `1,2,4,8,16,32,64` 均实际评估。累计质量评估 `1537/4096`；所有候选的非正与非有限计数均为 0，但七个候选的 `lineage_contained` 均失败，故接受候选为 none。
- 初始冻结证据最小 Scaled Jacobian 为 `0.003730823`、阈下 12；本轮最佳观测但未接受的 ring width 32 为 `0.00773904276716142`、阈下 20，仍低于冻结阈值 `0.01`。不得将该观测作为修复结果或生产参数。
- 最终命令返回码为 1，审计报告明确 `production_coarse_mesh_approved=false`；证据包回传至 `remote_evidence/pilot_repair_autodl_pilot_repair_20260803T163705Z/`，本机复算 SHA-256 为 `8ff2ea73dc5eb8fe91c2bf286822b7597a5227b227f48f198028a89e5f87e201`。
- 因 audit-only 未通过，本轮没有生成 `production_mesh_family_plan.*`，也没有给出或执行生产粗网格入口；下一步仍必须是新的、单独批准的 Pilot 候选策略设计阶段，不能进入生产网格。

### 下一阶段唯一入口

```bash
cd /root/autodl-tmp/5950L && CFD_PYTHON=/root/autodl-tmp/envs/cfdpipe/bin/python bash scripts/cfdpipe.sh pipeline coarse-repair-audit --help
```

该命令只读取下一轮候选策略入口帮助，不执行网格或审计；任何扩大候选枚举、改变策略或新 unique entry 都必须先形成新的机器计划并单独批准，阈值仍不得降低。

### 目标与验收标准

- 本机、GitHub 与 AutoDL 固定到同一提交且远端跟踪工作区干净；五个计划内历史 JSON 在两端均为普通非链接文件，大小、SHA-256 与 provenance 引用逐项一致，只传这些最小文件而不传整个 `runs`。
- JSON 是机器真源；补齐但不改变执行参数的审计元数据，并修正 Markdown 中与 JSON 不一致的 wall 指纹缩写。CLI `--help` 必须证明所有参数存在，controller 的内建 preflight 必须在 Gmsh 前验证 AUDIT_ONLY、零写网格/求解/后处理、资源和哈希门。
- 运行目录必须是全新的 `runs/mesh/coarse/autodl_pilot_repair_<UTC>`；命令除模板中的 `<UTC>` 唯一展开外逐字来自 `unique_execution_entry`，最大质量评估4096次，实际预算上界3730次，候选来源和ring width只能来自计划枚举。
- 仅当返回码0、最终阈下Prism为0、最小Scaled Jacobian不低于0.01、core/非正/非有限/marker/interface/源文件全部严格通过且SU2/pvbatch/CUDA零调用时标记Pilot审计PASS；否则保存精确阻塞证据并保持生产粗网格未获授权。
- 生成远端JSON/Markdown汇总、完整命令/时间/资源/候选/失败单元证据，打包回传并本机验SHA；PASS后只规划约250万/500万/1000万同族生产网格，不在本轮生成。

## 本轮任务：AutoDL L6 十五层边界层小网格与诊断 RANS 验收

状态：已完成；AutoDL L6 十五层真实模型小网格、一阶 SST 诊断、显式 restart 二阶 SST 诊断及 pvbatch 严格门均为 PASS。结果仅为诊断，不具备生产资格、不声明收敛；未启动 Pilot 或生产网格，未启用 CUDA，未修改冻结物理、marker、工况、参考量、输入哈希或质量阈值。

### 最终状态

- `PRIVATE_INPUTS = TRANSFERRED_VERIFIED`
- `REAL_PROJECT_L5 = PASS`
- `REMOTE_AUTODL_L6 = PASS`
- `PILOT_MESH_REPAIR = READY`
- `RANS = DIAGNOSTIC_L6_PASS`
- `PRODUCTION_CFD = HOLD`

### 实施与验证结果

- AutoDL 重新验证只读 STEP/BREP 冻结 SHA-256，以共享拓扑 BREP 重建新 L6 网格；两个流体体、两个共享 patch、四个求解 marker 与 `turning_section_outlet` 测量面均通过本轮 manifest/诊断交叉检查。
- 最终网格为 49,345 点、117,564 个三维单元（81,300 Prism6、36,264 Tet4），48 个真实壁面全部使用统一冻结物理日程且每柱 15 层。fresh readback 的最小 Scaled Jacobian 为 `0.0119748982911`，非正体积、非正 Jacobian、非有限质量、marker/interface 错误均为 0，SU2 文本读取 PASS。
- 一阶 SST 以单进程、CUDA关闭、CFL 0.05 完成 500 步并返回0；restart 完整且 pvbatch PASS。二阶仅消费该一阶显式 `run_manifest.json`/restart SHA，完成200步并返回0；没有 fatal、NaN 或非物理解门失败。
- 二阶 pvbatch 诊断的最大 y+=`0.689589619637`、P95=`0.482633395493`、P99=`0.541528907418`、y+>1面积比例为0；全局相对质量不平衡为`6.89127807950e-05`。两个后出口回流面积比例均为0，最小面心法向Mach分别为`2.45642322095`与`4.51570152682`；这些数值只用于 L6 软件/诊断质量门，不作生产物理解读或边界冻结。
- 两次普通兼容缺陷均按最小范围闭环：恢复完整 smoke 的统一15层schedule消费路径；让RANS在保留历史实例哈希门的同时接受由同一冻结配置/STEP/BREP逐哈希绑定的新重建manifest。AutoDL SU2的表面字段名则延迟到实际pvbatch数组严格验证，不把未使用输出名误当配置语法。
- 最终 L6 汇总18/18检查PASS；远程证据包回传至 `remote_evidence/autodl_l6_final_20260803T143342Z/`，本机复算SHA-256为`38aa27297ea23cbaa7278ef8f3c031dc83033bfa29a9c780bff35a199bd6239b`并重新解析overall PASS。
- Pilot 修复计划已定位冻结阈值`0.01`下的12个外层低质Prism及root-schedule collar v5策略，列出五个最小历史JSON和精确审计命令；本轮未传输这些Pilot历史证据，也未启动Pilot。

### 目标与验收标准

- 固定本地、GitHub 与 AutoDL Git 提交；远端跟踪工作区保持干净。重新验证 STEP/BREP 的冻结 SHA-256、只读权限和 Git 忽略状态，以共享拓扑 BREP 为计算几何，STEP 仅用于来源追溯。
- 先保存当前 `gmsh inspect`、`boundary-layer-smoke`、`rans-smoke` 与 `rear-outlet-freeze-check` 的实际 CLI 帮助，再通过配置和 provenance 确认重建依赖；优先重建，仅列出确实不可重建的最小历史 JSON，不上传整个 `runs`。
- 在全新且唯一的 `runs/boundary_layer_quality/autodl_l6_<UTC>` 中生成真实模型 15 层小规模混合网格。边界层只作用于真实壁面，不作用于 farfield、后部出口、内部测量面或共享接口。
- 网格 manifest 必须严格 PASS：marker 与共享接口无错误，所有要求柱列不少于 15 层，非正体积、非正 Jacobian、非有限质量均为 0，SU2 读取成功；失败时禁止启动 RANS，且不得降低既有质量阈值。
- 仅在显式 PASS 网格 manifest 上运行 `M50_H21_A8_B0` 的低 CFL、一阶 SST 诊断；其严格 PASS 且 restart 完整后，使用显式一阶 manifest/restart 运行二阶诊断。所有输入、命令、返回码、资源、日志和工件均以 SHA-256 绑定，禁止搜索旧目录。
- 仅由 pvbatch 读取本轮二阶结果，严格输出 y+、质量守恒、两个后部出口、回流和内部测量面诊断；字段缺失即 FAIL，不用替代字段。结果保持 `diagnostic_only=true`、`production_eligible=false`、`convergence_claimed=false`，不作生产或物理解读。
- L6 只有在真实输入、几何、网格、一阶/二阶 SST、pvbatch、最大 y+<1、质量平衡、双出口诊断及本地/远程证据全部严格通过后才标记 PASS；证据使用唯一目录打包、回传并在本机复验 SHA-256 与验收报告。
- PASS 后生成下一阶段最小 Pilot 修复执行计划，但不启动生产规模 Pilot；状态更新为 `REMOTE_AUTODL_L6=PASS`、`PILOT_MESH_REPAIR=READY`、`RANS=DIAGNOSTIC_L6_PASS`、`PRODUCTION_CFD=HOLD`，Linux 仍非完整生产 VERIFIED。

## 本轮任务：AutoDL L4 私有输入门与 L5 真实项目小型连接复现

状态：已完成；AutoDL L4严格私有输入门和L5真实项目小型连接均为PASS。不运行边界层、Pilot修复、RANS或生产网格，不启用CUDA，不作物理解读。Linux平台仍不是完整生产VERIFIED。

### 最终状态

- `PRIVATE_INPUTS = TRANSFERRED_VERIFIED`
- `REAL_PROJECT_L5 = PASS`
- `HISTORICAL_EVIDENCE = PLANNED_NOT_TRANSFERRED`
- `PILOT_MESH_REPAIR = HOLD`
- `RANS = HOLD`
- `PRODUCTION_CFD = HOLD`

### 实施与验证结果

- 本机与AutoDL的只读STEP（2,553,767字节）和共享拓扑BREP（3,028,066字节）分别严格匹配冻结SHA-256 `d7d1cb…1a6b` 与 `13698e…e121`；均为普通文件、非链接、Git忽略且未跟踪，远端模式为0444。
- L4以固定Git提交运行 `verify_repository.py --require-private-inputs`，35/35检查PASS、返回码0、stderr为空；命令、cwd、SHA、UTC、stdout/stderr与哈希进入唯一证据目录。
- 初次L5安全暴露可移植性缺陷：运行时marker验证强制依赖四个生成期历史JSON。最小修复保留完整marker结构/完整性摘要、project/STEP/BREP直接路径与哈希以及运行时几何指纹检查，仅将缺失生成期provenance明确延期；存在但错误的历史文件仍拒绝。新增回归后全部本地门PASS。
- 冻结后部出口合同在SU2前要求其明确列名、哈希绑定的8个小型JSON。仅传输这8个L5必需JSON并逐项验哈希、设只读；未传mesh、restart、VTU、完整runs目录，也未执行历史Euler/RANS。冻结评估PASS且保持 `MARKER_SUPERSONIC_OUTLET`、不设置静压/背压。
- 最终L5生成无边界层topology-smoke网格：9,047点、39,299个四面体、4个完整marker、2个共享面，非正Jacobian/质量/体积计数均为0；串行SU2五步返回0并输出当前run VTU，pvbatch读取后生成当前run JSON/CSV。
- `step_to_gmsh`、`gmsh_to_su2_mesh`、`mesh_to_su2`、`su2_to_visualization`、`visualization_to_pvbatch`、`pvbatch_to_results` 六段及overall全部PASS。五步Euler仅用于软件连接，不作载荷、压力、流量或气动性能解释。
- 下一阶段历史证据计划只列最小JSON及重建建议，未传输其中剩余L6/Pilot证据；边界层、Pilot修复、RANS、生产网格和CUDA均未启动。

### 下一阶段唯一入口

```bash
cd /root/autodl-tmp/5950L && /root/autodl-tmp/envs/cfdpipe/bin/python -m cfdpipe gmsh inspect --help
```

该命令只确认L6几何检查入口，不执行工具、网格或求解；实际L6/Pilot工作仍需独立批准。

### 目标与验收标准

- 仅从受控位置选取普通只读文件 `geometry/raw/model1.step` 与 `geometry/derived/model1_shared_topology/model1_shared_topology.brep`；拒绝symlink/junction/reparse point，大小与SHA-256必须分别严格匹配冻结合同 `d7d1cb…1a6b` 和 `13698e…e121`，不得修改合同接受新哈希。
- 生成不含私有内容的本地交接清单，证明两个文件未被Git跟踪且被忽略；通过rsync/scp直接传入AutoDL固定相对路径，不经过GitHub，远端设为只读并重新验证类型、权限、大小和SHA-256。
- 在AutoDL固定Git提交和干净跟踪工作区中，以全新证据目录运行 `verify_repository.py --require-private-inputs`；记录命令、cwd、Git SHA、UTC起止、stdout/stderr和返回码，严格门必须返回0且PASS。
- 先以当前CLI `--help`确认真实项目命令，再使用冻结的STEP、project/cases/markers/topology-smoke配置、`M50_H21_A8_B0`、smoke、单进程、五步、300秒执行唯一目录L5；共享拓扑BREP必须由markers哈希绑定选择。
- L5必须完成小型无边界层网格、五步串行Euler、当前run的VTU及pvbatch JSON/CSV；marker、共享接口、负体积、工件新鲜度与关键返回码全部通过，六段及overall均为PASS。
- 打包并回传严格门、报告、manifest、命令日志、网格/marker摘要、版本及逐文件SHA-256；本机验证压缩包哈希并重新解析所有状态。
- 只规划L6/Pilot所需最小历史证据，不上传整个runs或任何历史工件；PASS后状态仅更新为 `PRIVATE_INPUTS=TRANSFERRED_VERIFIED`、`REAL_PROJECT_L5=PASS`、`HISTORICAL_EVIDENCE=PLANNED_NOT_TRANSFERRED`，继续保持Pilot/RANS/生产CFD为HOLD及Linux非生产VERIFIED。

## 本轮任务：AutoDL 远程 CFD 工具链部署与 L2/L3 通用连接验收

状态：已完成；AutoDL 工具链、L2 与 CAD-free L3 均为 PASS，私有输入未传输，未运行真实项目网格、RANS或生产CFD。Linux 平台仍为 `CANDIDATE_PENDING_REMOTE_VERIFICATION`，真实项目复现尚未开始。

### 最终状态

- `LOCAL_LINUX_L0_L1 = PASS`
- `REMOTE_AUTODL_L0_L1 = PASS`
- `REMOTE_CFD_TOOLCHAIN = PASS`
- `LINUX_L2 = PASS`
- `LINUX_L3 = PASS`
- `PRIVATE_INPUTS = NOT_TRANSFERRED`
- `REAL_PROJECT_REPRODUCTION = PENDING`
- `PILOT_MESH_REPAIR = HOLD`
- `RANS = HOLD`
- `PRODUCTION_CFD = HOLD`

### 实施与验证结果

- AutoDL 用户空间部署 Gmsh 4.15.2 nox SDK（Python API 与 CLI 同版）、SU2 8.5.0 Linux MPI 构建、MPICH/HYDRA 4.0 启动器及 ParaView/pvbatch 6.2.0-RC1；`config/tools.json` 仅存在于远程且保持Git忽略，全部为原生Linux绝对路径，Windows `.exe` 数量为0。
- 官方普通 Gmsh Linux 包因远端缺少 `libGLU.so.1` 被保存为失败诊断，改用同版本官方 nox SDK后 `gmsh.initialize()`、OCC矩形、二维网格及 `gmsh.finalize()` 均成功；未启动GUI。
- L2环境报告为PASS：Python 3.13.14、Gmsh Python/CLI 4.15.2、SU2_CFD 8.5.0、MPI启动器和pvbatch 6.2.x均由实际命令验证；SU2_SOL已部署，本轮因SU2_CFD直接写出VTU而明确为 `FOUND_NOT_REQUIRED`。
- L3六段 `python_to_gmsh`、`gmsh_to_su2_mesh`、`python_to_su2`、`su2_to_paraview_file`、`python_to_pvbatch`、`pvbatch_to_results` 及overall全部PASS。网格为994点、1866个三角形，含120条 `farfield` 边；串行SU2完成5步并输出history和有限VTU；pvbatch读取VTU并输出可解析JSON、CSV。
- 同一网格另以MPI 2进程运行SU2，返回码0、完成5步、history及有限VTU均通过，点数和单元数与输入一致。输入、输出、命令、stdout/stderr、版本与关键工件SHA-256均进入远程证据。
- 实机发现并最小修复两项跨平台问题：SU2二维预处理会输出有符号 `-nan` 的不可用正交统计，以及官方ParaView启动器由 `pvbatch` 转入同目录 `pvbatch-real`；均新增回归，不放宽求解/网格质量门。
- 本阶段没有上传或读取客户STEP/BREP，没有运行真实项目网格、RANS或生产CFD，没有启用CUDA或GUI，没有修改marker、工况、CAD哈希、边界条件或网格质量阈值。

### 下一阶段唯一入口

私有输入经单独授权传输并按合同路径落盘后，第一条质量门命令为：

```bash
cd /root/autodl-tmp/5950L && /root/autodl-tmp/envs/cfdpipe/bin/python scripts/repro/verify_repository.py --require-private-inputs
```

### 目标与验收标准

- 在 AutoDL 数据盘的受控目录部署 Gmsh Python API/CLI 4.15.2、SU2_CFD/SU2_SOL 8.5.0、兼容 MPI 和 ParaView/pvbatch 6.2.x；记录官方来源、绝对路径、版本、SHA-256、UTC命令和返回码，不启用CUDA或GUI。
- `config/tools.json` 只在 AutoDL 工作树生成并保持Git忽略，全部路径为原生Linux绝对路径，Windows `.exe` 数量为0，`pvbatch`不得由GUI或`pvpython`替代。
- L2必须通过实际版本与最小功能验证：Gmsh initialize/OCC/二维MSH及SU2写出；SU2串行和2进程MPI各运行约5步Euler并产生history与ParaView文件；pvbatch打开本轮解并生成合法JSON/CSV。仅命令存在或版本未验证不得判PASS。
- L3必须在无客户CAD条件下完成 Python→Gmsh→smoke.su2→SU2_CFD→VTU/PVTU→pvbatch→JSON/CSV 六段连接，六段与overall全部PASS，关键返回码为0，marker、迭代数、点/单元数、日志和输入输出哈希完整。
- 任一普通安装、依赖、路径、MPI、脚本或测试问题都保存证据并执行最小修复、回归、审查、重新部署和全新目录复验；不得删除测试、放宽门限、忽略返回码、手改结果或复用旧PASS。
- 本轮不得上传或使用STEP/BREP，不修改marker、工况、CAD哈希、边界条件或网格质量阈值，不运行真实网格、RANS、后处理或生产CFD；CUDA保持关闭且不声明GPU验证。
- PASS后回传并校验证据，更新状态为 `REMOTE_CFD_TOOLCHAIN=PASS`、`LINUX_L2=PASS`、`LINUX_L3=PASS`、`PRIVATE_INPUTS=NOT_TRANSFERRED`、`REAL_PROJECT_REPRODUCTION=PENDING`、`PILOT_MESH_REPAIR=HOLD`、`RANS=HOLD`、`PRODUCTION_CFD=HOLD`，完成本地质量门、提交和正常推送。

## 本轮任务：发布 Linux 候选并完成 AutoDL L0/L1 实机验证

状态：本地与 AutoDL L0/L1 均已 PASS；证据已打包回传并通过 SHA-256 校验。Linux 仍为 `CANDIDATE_PENDING_REMOTE_VERIFICATION`，L2/L3 与外部 CFD 工具链尚未验证。

### 最终状态

- `LOCAL_LINUX_L0_L1 = PASS`
- `REMOTE_AUTODL_L0_L1 = PASS`
- `LINUX_L2_L3 = PENDING`
- `REMOTE_CFD_TOOLCHAIN = NOT_INSTALLED`
- `PRIVATE_INPUTS = NOT_TRANSFERRED`
- `PILOT_MESH_REPAIR = HOLD`
- `RANS = HOLD`
- `PRODUCTION_CFD = HOLD`

### 目标与验收标准

- 保留当前 `linux-autodl-candidate` 分支全部既有修改，完成 Linux/POSIX 候选实现审查与最小修复；Gmsh 固定为 4.15.2 并保留 Windows/Linux x86-64 wheel 双 SHA-256 与 `--require-hashes` 策略。
- 本地依次通过新增 shell 的 `bash -n`、仓库预检、CAD-free 测试、Linux 专项测试、`compileall`、`git diff --check`，并在全新目录运行 L0/L1 runner；四步返回码和 `overall` 必须全部为 PASS/0。
- 提交前扫描私钥、令牌、真实 AutoDL 主机/端口、个人工具绝对路径、CAD/网格/restart/VTU/runs 工件和 `config/tools.json`；只提交审核过的代码、测试、合同、文档和必要小型报告。
- 以提交信息 `build: add Linux AutoDL candidate reproduction` 正常提交并推送，不强推；GitHub 分支 SHA 必须与本机一致。
- AutoDL 只在 GitHub clone/fetch 期间临时启用 `/etc/network_turbo`，随后清除代理；远端固定 checkout 到已推送 SHA且工作区干净，使用 `/root/autodl-tmp/envs/cfdpipe/bin/python` 在唯一证据目录完成环境报告和 L0/L1。
- 远程失败必须回传证据、定位、最小修复、完整本地回归、重新提交推送和复验，直到远程 `overall=PASS`；不删除测试、不降低质量门、不忽略返回码或复用旧 PASS。
- PASS 后打包远程证据并校验 SHA-256，下载到本机 `remote_evidence/`；最终 PLAN 明确 `LOCAL_LINUX_L0_L1=PASS`、`REMOTE_AUTODL_L0_L1=PASS`、`LINUX_L2_L3=PENDING`、`REMOTE_CFD_TOOLCHAIN=NOT_INSTALLED`、`PRIVATE_INPUTS=NOT_TRANSFERRED`、`PILOT_MESH_REPAIR=HOLD`、`RANS=HOLD`、`PRODUCTION_CFD=HOLD`。
- Linux 平台合同仍保持 `CANDIDATE_PENDING_REMOTE_VERIFICATION`；本轮不安装或运行 Gmsh、SU2、MPI、pvbatch、CUDA，不运行网格、Euler、RANS或后处理，不修改 marker、工况、CAD 哈希、边界条件或网格质量阈值。

### 本地实施与验证结果

- 审查并修复远程验收接口：`check_linux_environment.sh` 兼容 `--output <path>` 与原位置参数；L0/L1 summary 使用规定的 `repository_preflight`、`cad_free_tests`、`compileall`、`git_diff_check` 四个步骤名。
- 新增回归后，shell `bash -n` PASS，仓库预检35/35 PASS，CAD-free 209/209 PASS，Linux专项9/9 PASS，`compileall` PASS，`git diff --check` PASS。
- 全新本地证据目录 `runs/reproducibility/linux_l0_l1_local_20260803T095505Z_20758/` 的四步返回码均为0，`summary.json` 为PASS；除unittest正常进度所在的CAD-free stderr外，其余stderr均为空。
- 提交候选不包含未跟踪的旧 readiness 报告；这些旧报告含历史机器标识和个人路径，只在本地保留，不进入公开 Git 历史。候选内容未包含私钥、密码、令牌、CAD、网格、restart、VTU、`runs/` 或 `config/tools.json`。
- 初次 AutoDL 运行 `linux_l0_l1_20260803T102113Z` 按失败闭合保留并回传：预检返回0，CAD-free因测试夹具写死 `/usr/bin/env python3` 返回1；AutoDL只提供合同指定的绝对Python路径。最小修复将夹具改为Bash参数捕获，不改变产品代码或质量门；完整本地门再次PASS后提交并推送。
- 修复提交 `0dbce7921f274d510d176812e21c41e82e2f7eef` 在 AutoDL 干净detach工作区复验。环境报告为预期 `INCOMPLETE`，没有误报PASS；L0/L1的 `repository_preflight`、`cad_free_tests`、`compileall`、`git_diff_check` 四步返回码全部为0，overall为PASS。
- PASS run为 `/root/autodl-tmp/transfer/linux_l0_l1_20260803T102312Z`；环境报告为同目录前缀的 `_environment.json`。证据包 `linux_l0_l1_20260803T102312Z.tar.gz` 已下载至 `remote_evidence/linux_l0_l1_20260803T102312Z/`，远端与本机SHA-256均为 `885b845c3b5f05ff95eab915a5627b17cae4e3b098ff97e310662f6c5681f989`，本机解包后再次解析summary为PASS。
- 本阶段没有安装或启动Gmsh、SU2、MPI、pvbatch或CUDA，没有传输STEP/BREP，没有运行网格、Euler、RANS或后处理；marker、工况、CAD哈希、边界条件和网格质量阈值均未修改。

### 下一阶段唯一入口

```bash
cd /root/autodl-tmp/5950L && CFD_PYTHON=/root/autodl-tmp/envs/cfdpipe/bin/python bash scripts/repro/bootstrap_linux.sh --dry-run
```

该命令只做L2/L3部署前检查并打印受哈希约束的依赖建议，不安装或启动外部CFD工具。任何实际工具部署仍需作为下一独立质量门执行。

## 本轮任务：修复 Linux 环境探测误报

状态：已完成；仅修复 review 已证实的 `check_linux_environment.sh` 可能把不兼容 OS/Python/工具误报为PASS的问题，Linux候选状态和其它工程合同未改变。

### 目标与验收标准

- 从 `config/reproducibility.json` 读取 Ubuntu 22.04、x86-64、Python 3.13.14、Gmsh 4.15.2、SU2 8.5.0、ParaView 6.2.0合同，不在脚本中建立第二份版本真源。
- 未取得外部工具版本证据时，即使同名可执行文件和Gmsh模块存在也必须保持 `INCOMPLETE`；不为取版本而启动 Gmsh、SU2、MPI 或 pvbatch。
- Linux专项测试覆盖当前探测结果和“仅发现同名工具不足以PASS”的失败闭合合同；随后按用户指定顺序重跑 shell语法、CAD-free、Linux专项、compileall、diff检查与全新目录L0/L1 runner。
- 不访问远程写操作、不安装工具、不修改任何质量阈值、物理配置、marker、工况或CAD哈希。

### 实施与验证结果

- `check_linux_environment.sh` 现在从 `config/reproducibility.json` 读取 Ubuntu 22.04 x86-64、Python 3.13.14、Gmsh 4.15.2、SU2 8.5.0和ParaView 6.2.0目标，不重复硬编码版本。
- OS必须同时为Linux、x86-64且 `/etc/os-release` 为Ubuntu 22.04；Python必须精确匹配3.13.14；Gmsh模块只通过不导入模块的metadata读取版本并要求4.15.2。
- 本轮禁止启动外部CFD工具，因此仅在PATH发现的 Gmsh/SU2/MPI/pvbatch 被标记为 `FOUND_VERSION_UNVERIFIED`，`observed_version=null`、`version_status=UNVERIFIED`；这些证据绝不满足PASS。JSON新增逐项 `completion_checks` 和 `external_tools_executed_for_version_probe=false`。
- 新增失败闭合测试使用五个同名可执行fixture；报告保持 `INCOMPLETE`、所有工具版本均为UNVERIFIED，且执行标记不存在，证明探测未启动这些fixture。Linux专项8/8 PASS。
- 按指定顺序复验：全部新增shell `bash -n` PASS；CAD-free 208/208 PASS；Linux专项8/8 PASS；`python3 -B -m compileall -q src tests scripts` PASS；`git diff --check` PASS；全新 `runs/reproducibility/linux_l0_l1_local_20260803_002/` 的四步均返回0且summary为PASS。
- runner证据中仓库预检、compileall和diff-check stderr均为0字节；CAD-free stderr只含unittest进度及 `Ran 208 tests ... OK`。本地实际环境仍正确为 `INCOMPLETE`：架构匹配，但OS、Python、Gmsh模块及外部工具版本门未匹配。
- 本轮未访问远程、未安装或启动任何CFD工具、未修改质量阈值/marker/工况/CAD哈希、未commit或push。下一质量门仍是审查后的AutoDL远程L0/L1。

## 本轮任务：Linux/AutoDL L0～L3 候选复现能力

状态：本地实现与验证已完成；Linux 仍为 `CANDIDATE_PENDING_REMOTE_VERIFICATION`，下一质量门是审查批准后的 AutoDL 远程 L0/L1。本轮未访问网络、未修改远程 AutoDL、未启动任何 CFD 外部工具。

### 本轮目标与验收标准

- 在 `linux-autodl-candidate` 分支保留既有 PLAN 和 readiness 报告，固定 Gmsh 4.15.2 并同时绑定 Windows x86-64 与 Linux manylinux x86-64 wheel SHA-256，保持 `--require-hashes`。
- 新增安全的 POSIX `cfdpipe` launcher、只检查/建议而不安装的 Linux bootstrap、输出 JSON 且缺生产工具时为 `INCOMPLETE` 的环境检查，以及不覆盖证据目录、失败即停的 L0/L1 runner。
- 更新平台合同与部署文档：Windows 参考工作站继续 VERIFIED；Ubuntu 22.04 x86-64 仅为 `CANDIDATE_PENDING_REMOTE_VERIFICATION`，L2/L3 等待 AutoDL 实机验证，生产网格、MPI、GPU 和正式 CFD 均不声明已验证。
- 增加 Linux/POSIX 专项测试，覆盖 shell 语法/权限、argv 转发、原生绝对路径、Windows `.exe` 与 `/mnt/c` 拒绝、subprocess 列表、MPI/pvbatch argv、无 `shell=True`、双 wheel 哈希和候选状态；保留且不放宽全部 Windows 测试。
- 本地只运行 bash 语法、仓库预检、CAD-free 测试、compileall、Linux 专项测试、`git diff --check` 与一次全新目录的 L0/L1 runner；不导入/运行 Gmsh，不运行真实 CAD、SU2、MPI、pvbatch、网格或 RANS。
- 检查全部日志、输出和 Git diff 后更新本节，记录通过项、缺失工具、AutoDL 下一步精确命令、候选状态、风险与下一质量门；本轮不 commit、不 push。

### 实施与验证结果

- 已创建并切换到 `linux-autodl-candidate`；保留了任务开始前已修改的 `PLAN.md` 和未跟踪的 `reports/autodl_readiness_check.{json,md}`，未覆盖提交、未 commit、未 push。
- `requirements-gmsh.txt` 仍只声明一次 `gmsh==4.15.2`，同时绑定 Windows x86-64 wheel `7b3608…d711` 与 Linux manylinux x86-64 wheel `4076a9…108c`；安装文档继续要求 pip `--require-hashes`。
- 新增 `scripts/cfdpipe.sh`、`bootstrap_linux.sh`、`check_linux_environment.sh` 和 `run_linux_l0_l1.sh`，均为 bash、`set -euo pipefail`、权限0755。launcher 使用 argv 数组并支持含空格参数；bootstrap 只检查并打印建议命令；环境检查不导入 Gmsh、缺生产工具时输出 `INCOMPLETE`；L0/L1 runner 拒绝覆盖证据目录并逐步记录 stdout/stderr/返回码/UTC 时间。
- 平台合同保留 Windows `VERIFIED_ON_REFERENCE_WORKSTATION`，新增 Ubuntu 22.04 x86-64 `CANDIDATE_PENDING_REMOTE_VERIFICATION`；只声明 L0/L1 可运行，L2/L3 待远端验证，生产网格、MPI、GPU、正式 CFD 均为未验证。
- Linux 专项测试7/7 PASS；与既有 toolchain 合跑24/24 PASS。完整 CAD-free 门207/207 PASS；仓库预检35/35 PASS；`compileall -q src tests scripts`、全部新 shell 的 `bash -n`、实际0755权限和 `git diff --check` 均 PASS。未导入/运行 Gmsh，未启动 SU2、MPI、pvbatch、网格或 RANS。
- 本地 runner 证据位于 `runs/reproducibility/linux_l0_l1_local_20260803_001/`（Git忽略）：四步 `verify_repository`、`cad_free_tests`、`compileall`、`git_diff_check` 均返回0，`summary.json`/`summary.md` 状态为PASS；CAD-free stderr仅含 unittest 正常进度与最终 `Ran 207 tests ... OK`，其余三份stderr为0字节。
- 本地环境探测 JSON 状态为预期 `INCOMPLETE`：Python `/usr/bin/python3` 3.14.4，pip、Gmsh模块/CLI、SU2_CFD、SU2_SOL、mpiexec、pvbatch均MISSING；Windows `.exe` 和 `/mnt/c` 可执行文件拒绝策略为true。`bootstrap_linux.sh --dry-run` 因pip缺失返回非零并明确报告，未安装软件。

### AutoDL 下一质量门命令（审查、提交并推送批准 SHA 后执行）

```bash
cd /root/autodl-tmp/5950L
git fetch origin
git switch --detach <APPROVED_COMMIT_SHA>
CFD_PYTHON=/root/autodl-tmp/envs/cfdpipe/bin/python scripts/repro/bootstrap_linux.sh --dry-run
CFD_PYTHON=/root/autodl-tmp/envs/cfdpipe/bin/python scripts/repro/check_linux_environment.sh /root/autodl-tmp/linux-evidence/environment.json
CFD_PYTHON=/root/autodl-tmp/envs/cfdpipe/bin/python scripts/repro/run_linux_l0_l1.sh --output /root/autodl-tmp/linux-evidence/l0_l1_<UTC_TAG>
```

GitHub clone/fetch 如需 AutoDL 网络加速，只在该操作期间启用并在完成后关闭代理。上述命令当前未执行；`<APPROVED_COMMIT_SHA>` 与 `<UTC_TAG>` 必须在审查后替换为不可变提交和全新证据标识。

### 剩余风险

- 当前 WSL Python 不是远端指定的3.13.14且缺pip；本地只证明代码级候选 L0/L1，不能替代 AutoDL 实机证据。
- AutoDL 上的 Gmsh、SU2/MPI、pvbatch 尚未部署或验证；L2/L3、CUDA/GPU、生产网格和正式 CFD 均未通过，Linux不得标记 VERIFIED/PASS。
- 本轮未传输 STEP/BREP 或 `runs/`；L4以后仍需独立受控渠道和原有SHA-256/只读/lineage门。
- 原有 Pilot/粗网格质量门仍为INCOMPLETE，本轮没有改变 marker、工况、CAD哈希、网格阈值或任何物理合同。

## 本轮任务：AutoDL 连接复核和 Linux 迁移计划

状态：进行中；本轮仅执行本地与远程只读检查并形成计划，不部署、不传输私有数据、不启动任何 CFD 外部工具。

### 本轮目标与验收标准

- 使用用户指定的 `ssh autodl-5950` 只读命令复核远端主机、Ubuntu、cgroup CPU/内存、数据盘、GPU、指定 Python 以及 git/tmux/rsync 可见性；不修改远程文件、不安装软件、不运行 `network_turbo`。
- 报告本地 Git 根、当前 commit、`git status --short`，并基于现有文档、代码和测试核对 Windows/Linux 差异及 `scripts/cfdpipe.sh`、Linux bootstrap、Linux 工具路径测试、Linux 命令构造测试、跨平台 Gmsh wheel 锁定现状。
- 只制定 Linux/AutoDL L0-L3 兼容改造、固定 commit 发布/克隆、工具部署、连接测试、私有输入/证据交接、真实项目复现和 Pilot 网格质量修复的分阶段计划，不执行这些步骤。
- 生成并校验 `reports/autodl_readiness_check.json` 与 `reports/autodl_readiness_check.md`，明确通过项、缺失项、证据、风险和唯一下一条建议命令；不上传 CAD、不启动 Gmsh/SU2/pvbatch、不运行网格或 RANS、不修改质量阈值、不提交 Git。

### 实施与验证结果

- 远程只读脚本经 `ssh -F ~/.ssh/config autodl-5950` 返回0：Ubuntu 22.04.1、CPU `2500000/100000=25`核、内存上限120 GiB、`/root/autodl-tmp` 550G、RTX PRO 6000 Blackwell 97887 MiB/driver 590.44.01、指定Python 3.13.14以及git/tmux/rsync均实测存在。原样SSH在连接前被WSL系统SSH include所有权门拒绝；未修改系统文件。
- 本地Git根为 `<LOCAL_REPOSITORY_ROOT>`，分支`main`，HEAD=`5e3af9ab71d7bdc2a9f5cd3a42f7d54f694cc764`；WSL dubious ownership仅以逐命令`-c safe.directory=<LOCAL_REPOSITORY_ROOT>`绕过，未改全局配置。本轮初始短状态为` M PLAN.md`。
- `scripts/cfdpipe.sh`与Linux bootstrap缺失；工具解析和argv构造有通用POSIX分支，但无Linux专项合同/CI。Gmsh锁只有一个哈希，已安装wheel元数据为`win_amd64`，故跨Windows/Linux哈希锁缺失。
- JSON解析PASS；SU2 MPI、pvbatch、几何审查pvbatch三条不启动外部程序的命令构造测试3/3 PASS。`tests.test_toolchain`在当前WSL `/mnt/c`工作树17项中9项因临时fixture `chmod` 返回EPERM而ERROR，证明Linux L1当前不能标PASS；一次错误测试选择器已纠正，不计作产品缺陷。
- 已生成 `reports/autodl_readiness_check.json` 与 `.md`，整体保持`INCOMPLETE`并列出九阶段迁移计划、风险和下一条仅建议但未执行的分支命令。本轮未安装、上传、启动CFD工具、运行网格/RANS、改阈值或提交Git。

## 本轮任务：合并首次公开发布 PR

状态：已完成；用户明确确认后，公开仓库 `qofotel201-coder/5950L` 的 PR #1 已由 draft 转为 ready，并以普通 merge commit 合并到默认分支 `main`。远端与本地 `main` 均已同步。

### 本轮目标与验收标准

- 合并前再次确认本地分支干净、远端 head SHA 一致、PR base 为 `main`、仓库仍为 PUBLIC，且所有必需检查成功。
- 只合并 PR #1，不 force-push、不覆盖远端历史、不上传被 `.gitignore` 隔离的 CAD、BREP、运行结果、本机工具路径或虚拟环境。
- 将草稿 PR 明确转为 ready 后采用普通 merge commit 合并；合并后从 GitHub 读取 PR 状态、merge commit SHA 与远端 `main` SHA，要求完全一致。
- 同步本地 `main`，把实际合并结果写回本节并运行短预检；本轮不调用 Gmsh、SU2、ParaView、MPI/GPU，不生成网格或运行 CFD。

### 实施与验证结果

- 最终PR head固定为 `744d2c12cfa111da30ce3ed7650b42cfdfa9e2ca`；合并前状态为 `MERGEABLE/CLEAN`，审查线程0个，GitHub Actions run `30782892052` 的Windows/Python3.12与3.13任务均success。
- GitHub App因integration权限对ready操作返回403后，按已验证发布回退使用管理员账户的GitHub CLI；CLI在合并时使用`--match-head-commit`绑定上述head SHA，未使用`--admin`、force或删除分支。
- PR #1于 `2026-08-03T03:54:36Z` 报告`MERGED`；merge commit为 `bf12693e0943a7d578243b1686995bad43e5823b`，GitHub远端`main`、本地`origin/main`与同步后的本地`main`三者一致。
- 合并没有新增工程代码、外部软件调用或CFD工件；私有CAD/BREP、运行结果、本机工具路径和虚拟环境继续由既有忽略与仓库预检合同隔离。

## 本轮任务：首次公开 GitHub 发布

状态：已完成；用户指定的 `qofotel201-coder/5950L` 已确认 PUBLIC，GitHub-ready 内容已在保留远端初始历史的前提下提交并推送到 `agent/github-ready-repro`，草稿 PR #1 已创建并指向 `main`，首次 GitHub Actions 离线门成功。

### 本轮目标与验收标准

- 再次核对全部暂存内容，确保客户 CAD、派生 BREP、`runs/` 工件、`.venv`、`config/tools.json`、个人绝对路径和凭据均不进入公开提交。
- 仅为当前仓库设置 GitHub 隐私邮箱提交身份；创建一个可追溯的首次提交，不修改系统或全局 Git 身份。
- 使用现有公开 GitHub 仓库 `qofotel201-coder/5950L`，设置 `origin`，从其 `main` 基线发布 GitHub-ready 内容并建立跟踪关系。
- 推送后从 GitHub 读取仓库可见性、默认分支和远端提交 SHA，确认它们与本地一致；检查 GitHub Actions 首次运行状态但不伪造成功。
- 不上传私有 CAD/运行工件，不创建正式发布标签，不运行新的 Gmsh、SU2、ParaView、网格或 CFD。

### 实施与验证结果

- GitHub CLI 2.97.0 已使用账户 `qofotel201-coder` 的现有认证；仅在本仓库设置提交身份 `qofotel201-coder <qofotel201-coder@users.noreply.github.com>`，没有修改全局身份。
- 远端 `main` 原提交 `99d7747de2bb64c72bf4457064b4c9a566ae574c` 仅含占位 README；本轮将它作为父提交，未 force-push、未改写历史。
- 首个项目提交为 `140c34005177dc07e33da914da8c744ba84cdf41`（`Add reproducible CFD pipeline`），远端分支 SHA 与本地逐字一致。
- 发布前严格预检35/35 PASS；CAD-free门200/200 PASS、1项因Windows symlink权限按设计skip；163个公开候选中私有工件、大于等于1 MB文件、个人路径及凭据命中均为0。
- GitHub App创建PR因集成权限返回403后，按发布工作流改用已认证GitHub CLI成功创建草稿PR `https://github.com/qofotel201-coder/5950L/pull/1`；PR head/base与提交SHA均经远端读取确认。
- 首次GitHub Actions `Offline Python gates` run `30765481363` 对该提交执行完毕并返回success。公开仓库的正式默认分支仍为`main`；PR保持draft，待所有者审阅后再合并，未越权自动合并。

## 本轮任务：GitHub-ready 离线复刻包

状态：已完成；GitHub-ready 本地 `main` 仓库、版本合同、私有工件隔离、离线预检、CAD-free CI、复现文档和干净克隆复验均已建立。本轮未启动 Gmsh、SU2、pvbatch、MPI/GPU，未安装软件、访问网络、提交或设置远程，也未改变当前粗网格质量门结论。

### 本轮目标

把当前 Python 总控、Gmsh/SU2/pvbatch 桥接、真实 STEP/BREP 稳定 marker、连接测试、边界层小网格与小规模串行 RANS 的已验证成果整理为可在另一台 Windows 电脑克隆后按分级质量门复验的 GitHub-ready 仓库。项目物理与边界真源继续只来自 `config/`；客户 CAD、派生几何、大网格、求解结果、本机工具绝对路径和虚拟环境默认不进入版本库，改由哈希绑定的本地输入准备与离线预检说明交接。

### 验收标准

- 提供清晰的根目录 README、Windows 安装/工具路径、分级复验、已验证基线、CAD/运行工件交接和安全边界文档；不得宣称尚未通过的粗网格或正式 CFD 已完成。
- 提供标准 Python `src/` 包元数据和 `cfdpipe` 命令入口；依赖版本、工具覆盖变量与 `config/tools.json` 示例明确，现有有效本机配置不被覆盖。
- 提供无网络、无外部程序启动的仓库预检命令，验证必要源文件、配置可解析、代码可编译、禁止项和可选私有输入 SHA-256；失败返回非零且保留具体原因。
- 提供只运行标准库/纯 Python 门的 GitHub Actions 工作流；CI 不下载客户 CAD、不启动 Gmsh/SU2/ParaView、不运行真实 STEP、网格或 CFD。
- `.gitignore`/`.gitattributes` 明确排除 `.venv`、缓存、`runs/`、客户 CAD、派生几何和大体积 CFD 工件，同时保留目录说明与示例；不得删除或改写当前本地有效工件。
- 从仓库根目录执行离线预检、相关单元测试、完整 `unittest discover` 和 `compileall` 均通过；检查日志、输出、Git 状态与新增文件，最后把实际命令、结果、限制和下一步写回本节。

### 实施结果

- 在原非 Git 工作目录原位执行 `git init -b main`，没有创建提交、配置身份或远程。新增 `pyproject.toml` 的标准 `src/` 包元数据和 `cfdpipe=cfdpipe.cli:main` 入口；Python 最低语法版本为3.12，实机基线仍为3.13.14，Gmsh 可选依赖固定4.15.2，SU2/ParaView继续是外部CLI而不是pip包。
- 新增根 `README.md`、`CONTRIBUTING.md`、`SECURITY.md`、`docs/REPRODUCIBILITY.md`、`docs/VERIFIED_BASELINE.md` 和 `docs/PRIVATE_INPUTS.md`，明确通用/真实项目小链PASS、小型SST RANS与y+仅诊断、正式粗网格和正式CFD未完成；默认推荐私有GitHub仓库，未擅自选择开源许可证。
- 新增 `config/reproducibility.json` 和可直接复制的 `config/tools.example.json`；本机有效 `config/tools.json` 保持原样且被Git忽略。版本合同记录Python3.13.14、Gmsh4.15.2、SU2 8.5.0和ParaView6.2，以及五个环境变量覆盖和私有输入策略。
- 新增标准库 `scripts/repro/verify_repository.py`：不导入/启动任何外部工程软件，不联网；严格验证GitHub核心文件、TOML/CSV/JSON语义、设计工况与marker绑定、版本合同、唯一安全外部进程入口、普通Python/ParaView/Gmsh GUI禁令、私有STEP/BREP路径/只读/SHA-256和输出路径。缺私有输入默认为WARN/返回0，`--require-private-inputs`严格FAIL；报告只允许新建于`runs/`且拒绝覆盖、symlink/junction或越根路径。
- 新增 `scripts/repro/run_cad_free_tests.py` 作为CI与文档共用的单一15模块清单；GitHub Actions在Windows Python3.12/3.13只执行预检、compileall、CLI help和该CAD-free标准库门，不下载客户CAD、不安装或启动Gmsh/SU2/ParaView。完整`unittest discover`仍需先恢复哈希匹配的私有输入。
- `.gitignore`、`.gitattributes`、`.editorconfig`、Issue/PR模板和目录README已建立。约620 MiB `runs/`、约152 MiB `.venv`、本机工具路径、原始/派生CAD、根目录CAD副本及CFD大工件全部被排除；Git候选共163个文件，0个文件达到1 MiB。历史PLAN中的两条个人绝对路径已脱敏，候选内容未发现凭据或用户名路径。
- 将 `tests/test_coarse_mesh.py` 中一个不必要的真实BREP测试依赖替换为测试目录内临时只读 `.brep`，保持生产代码和合同不变；29/29专项测试PASS。干净克隆仍因其它真实项目配置测试按设计需要私有STEP/BREP，故CI使用显式CAD-free集合而不伪造完整回归资格。

### 验证结果

- 严格当前仓库预检：`.\.venv\Scripts\python.exe -B scripts/repro/verify_repository.py --require-private-inputs --output runs/reproducibility/github_ready_20260803_002/repository_preflight.json` 返回0，35/35 PASS；报告SHA-256=`f9f2b7a0284236b6763e218faae8717bd61ded5cccf592cf7630d4f00c142866`。
- CAD-free统一门：`.\.venv\Scripts\python.exe -B scripts/repro/run_cad_free_tests.py` 为200/200 PASS，1项因当前Windows账户无创建目录symlink权限按设计skip；没有外部CFD程序调用。
- 当前完整私有输入回归：`PYTHONPATH=src .\.venv\Scripts\python.exe -B -m unittest discover -s tests -v` 为803/803 PASS、1 skip、返回0，耗时27.155秒；stdout/stderr日志位于 `runs/reproducibility/github_ready_20260803_003/`，SHA-256分别为`7c1f8ac39d2a23ac445d861b29a255a29096df7649cf5e2dcc37384e2badf5dd`和`edfcffa652c319ba4e1b3d1dd4dddf0341ccba646e1f3b1877d8261780dba9b2`。
- `compileall -q src tests scripts` 返回0；Markdown本地链接检查PASS；私有CAD/BREP仍只读且SHA-256继续匹配`markers.toml`。
- 以Git非忽略清单构造 `runs/reproducibility/clean_clone_20260803_002`：163个文件、0个私有输入/工具配置/历史run；预检返回0且为预期`WARN`（32 PASS、2项仅提示缺STEP/BREP、0 FAIL），compileall和`python -m cfdpipe --help`均返回0，CAD-free门200/200 PASS。
- 一次更早的干净快照 `_001` 曾按错误的“完整回归无需私有输入”假设运行并得到12 FAIL/42 ERROR；该证据被保留且未掩盖。由此收紧合同、CI和文档，明确完整发现依赖私有输入，随后全新 `_002` 按正确范围严格PASS。
- `git add .` 后共163个候选文件进入本地暂存区；`git diff --cached --check` 返回0，私有CAD、`config/tools.json`、`runs/`工件及CFD网格/解扩展名进入暂存区的数量为0。未创建commit或remote。

### 交付限制与下一步

- 本地仓库已具备提交到GitHub所需结构，但本轮没有执行`git commit`或`git push`，也没有设置远程；实际发布前需由用户选择仓库名称、私有/公开策略、许可证和Git身份。鉴于`markers.toml`含几何特征，默认只能先建私有仓库。
- 代码克隆本身可复刻L0/L1；L2以后仍需目标机管理员提供已批准版本的Gmsh/SU2/ParaView，L4以后还需通过受控渠道按原相对路径交接只读STEP/BREP及所需历史证据。仓库不会自动安装软件或上传客户数据。
- GitHub-hosted Actions尚未在远端真实触发；本机已执行与工作流相同的命令和干净克隆模拟。首次私有push后应确认Python3.12/3.13两个job均PASS，再打版本标签。
- 本轮只包装已完成成果；正式5M粗网格、两阶段正式RANS、中/细网格与网格无关性仍受下一质量门约束，没有被README、版本合同或CI提升为已完成。

## 当前阶段：设计点粗网格与正式 RANS 就绪门

状态：进行中；minimum-growth交互组件方向审计已双跑确定性 PASS，真实固定方向schedule-homotopy也已完成14点正/逆序取证并合法返回 `INCOMPLETE`：仅λ=0 PASS，最小正分数λ=2^-12已在2个稳定wall、5个源三角上出现37个低质Prism，而core仍PASS。count-first v2真实审计 `_004` 已把旧目标扩散出的134个阈下Prism收缩为13个；fine v3真实审计 `_005` 又以6个已接受细步把它降为12个并略升最小minSJ至0.003731。三根同步v4真实审计 `_007` 已完成24个step/pass、192个合法原子组合且0次接受，质量与v3完全相同；残差仍全部是原始 `9aaa…` 三角第49–60层，Core/非正/非有限仍为0，故方向自由度已证据化耗尽。局部root-schedule collar v5软件门及资源中止证据加固已完成，全库788/788测试通过；真实 `_008` 因运行期内存保护合法FAIL，`_009` 在完成Gmsh三维初始网格后被会话中断且没有终态manifest，均不是collar质量结论。下一有效尝试必须使用全新 `_010` 目录，在不少于8.8 GiB启动余量和临时非任务资源守卫下完整运行AUDIT_ONLY审计；首层高度、60层、方向、阈值和AUDIT_ONLY权限保持不变。尚未授权写盘、SU2、ParaView或正式 CFD

### 本轮目标

真实 `minimum-growth-owner-free-component-pattern-audit` 已在两个独立、受3 GiB Job限制的Gmsh进程中严格 PASS，并得到完全一致的稳定观察、交互拓扑、A-B-A-B坐标/质量、最终组件复核、空残差清单和全局质量哈希。交叉审计同时确认该PASS只覆盖growth=1的60层薄栈，总厚度约为 `60×2.764577127e-7=1.658746276e-5 m`，不能证明当前真实局部边界层日程（总厚最高 `0.0778871424 m`）可行。后续固定方向重放已在两个全新目录得到相同 discovery/quality SHA，证明真实日程确定性不通过，而不是CLI、内存或Gmsh基础设施故障。当前最小门改为固定已批准方向下的 schedule-homotopy 取证：从minimum-growth到权威physical schedule按显式分数、每次从绝对原点重建坐标并查询全部294,059个Prism/Core单元，只定位已测可行域，不假设单调性、不将路径诊断冒充为最终修复。只有后续独立网格质量门严格PASS，才允许写出 `mesh.msh/mesh.su2` 并在第二个全新Gmsh会话fresh readback。

新增粗网格专用 `AUDIT_ONLY` 修复发现入口：它必须消费当前 `0.40 m` 粗网格合同、权威 projection 和当前局部60层日程，在隔离 worker、Windows Job Object 与物理内存保护下只运行到内存中的修复签名发现和验证阶段；只发布 JSON/日志证据，不写 MSH/SU2，不授权 calibration PASS 或 production mesh，不调用 SU2/ParaView。验收要求包括完整 observed repair counts、稳定 wall fingerprint、source-triangle SHA-256、root-coordinate SHA-256、Gmsh 生命周期、源 BREP 前后哈希与不变性，所有严格质量阈值保持不变，且标准库测试无需启动外部工具即可覆盖 CLI/worker/失败闭合。

在不跳过仓库既定质量门的前提下，把已验证的真实 STEP/派生只读共享拓扑 BREP、稳定 marker 指纹、设计点出口冻结证据和实际 y+ 首层高度扩展为 `M50_H21_A8_B0` 的可审计粗网格。先以小型校准网格确定 Gmsh 尺寸—单元数—内存关系，只有资源预检和粗网格全部质量指标 PASS 后，才允许建立低 CFL 一阶启动、显式 restart 二阶收敛的正式 RANS 运行入口；本阶段不把短诊断结果解释为生产物理结论，也不跳到中/细网格、多工况、MPI 或 GPU。

本轮安全修正只把第二校准点从 `0.25 m` 调整为 `0.32 m`，使首点 `294,059` 个投影单元的保守立方外推约为 `574,334`，严格低于 `750,000` 校准上限；验收要求是配置、归一化合同和相关测试一致，旧投影证据与旧局部日程绑定明确失效，纯 Python 组合回归及 `compileall` 通过，且本轮不启动 Gmsh、SU2 或 ParaView。

本轮同时收紧 `AUDIT_ONLY` 局部日程的最终消费门：在任何 Gmsh 生命周期开始前复验规范化 strategy config 哈希、粗网格合同与日程 binding 血缘；消费点复用权威 binding 校验、拒绝大小写指纹碰撞，并要求 audit/smoke/calibration/projection 模式矩阵完全互斥。路径防护仅在不拒绝 Windows 正常绝对路径的前提下拒绝实际 symlink/junction；验收为相应标准库对抗测试和编译检查通过，不启动 Gmsh、SU2 或 ParaView，不改变 `_004` 的 `INCOMPLETE` 物理结论及任何物理、质量或资源阈值。

### 本轮 continuation v2 跨模块兼容子任务

目标：在不触及 continuation 权威实现、runner/contract 及物理阈值的前提下，清除隔离 worker 与其专项测试中对 continuation discovery v1 的写死假设，使 ENDPOINT/DISCOVERY v2 升级由权威模块常量唯一驱动。

验收标准：worker 保持 Gmsh 重模块延迟导入，continuation wrapper schema 来自已延迟导入的权威 `DISCOVERY_SCHEMA`；全库排除专属 continuation/smoke 测试后无旧 v1 字面量；worker/controller/CLI 相关标准库测试与编译检查通过，不启动 Gmsh、SU2 或 ParaView。

### 验收标准

- `growth=1 + 既有方向修复` 必须是显式命名的组合端点，不得被表述为schedule-only证明；其首层高度和60层保持不变，候选总厚只能为 `60×首层高度`，端点对象、权威schedule binding、实际运行时应用证据和worker argv必须交叉哈希验证。后续任何schedule sweep必须从同一绝对one-layer坐标重建候选，不得累积移动，不得假设质量随growth单调；持久证据只使用稳定surface/root/triangle SHA-256。任一非有限值、logger错误、源变化、写网格或下游调用都必须FAIL。
- owner-free方向审计必须按任意共享根连通而不按wall fingerprint拆分失败源三角；候选源、内点权重、分量数、根数和质量查询次数均由自哈希端点冻结。每个失败候选必须恢复当前最佳的绝对坐标，最终执行A-B-A重放；PASS必须同时满足全局Prism `minSJ>=0.01`、Core Tet4 `gamma>=1e-3`、全部三维单元四项质量有限且严格为正、零owner猜测、源不变、零写盘和零下游调用。
- 固定方向重放合同必须与audit-only搜索schema及未来write/readback schema分离，禁止通过翻转旧audit-only标志解除安全限制；必须同时接受两份显式路径/SHA-256指定且经权威validator完整复验的独立pattern审计manifest，并要求stable observation、endpoint、interaction topology、A/B坐标与质量、final quality、component recheck、空残差清单和changed roots逐项一致。
- 重放前须重建真实local schedule下的稳定坏三角、基础/交互组件和16个候选根；根身份至少绑定坐标SHA、相邻坏三角SHA、wall fingerprints和基础/交互组件SHA并在全部base roots中唯一。仅10个changed root可按float-hex绝对方向移动；另外6个必须保持fresh A态，零匹配、多匹配、上下文漂移或非授权坐标变化均FAIL。
- 真实日程重放审计必须重新验证全部294,059个三维单元，Prism6恰为280,320、Core Tet4恰为13,739、Prism `minSJ>=0.01`、Core Tet4 `gamma>=1e-3`，四项质量全部有限且严格为正，四个marker及48面/60层覆盖保持不变；本门 `mesh_written=false`、无外部命令、SU2/ParaView均false。仅该门PASS后才建立单独的写盘/fresh-readback合同。
- schedule-homotopy 必须同时绑定两份minimum-growth方向审计、共识approval、真实local schedule binding和上一固定重放失败证据；分数固定为 `[0, 2^-12, 2^-11, ..., 2^-1, 1]` 的14个递增dyadic点，每个分数都从同一绝对root原点和批准方向构造，至少做正/逆序重放并保持首层高度、60层、48面和所有质量阈值不变。不得因为一个已测分数FAIL而假定未测分数FAIL，也不得将最大已测PASS称为连续最优或生产日程；该门始终AUDIT_ONLY、不写网格、不搜索新方向、不调用SU2/ParaView。
- 首个direction-continuation门只允许处理权威homotopy证据给出的λ=2^-12失败前沿，必须绑定其manifest/SHA、discovery/SHA、两份minimum-growth方向审计、approval和local schedule。每次先从绝对λ=0坐标与批准方向构造λ=2^-12，重新得到完全相同的37个低质Prism、5个稳定源三角、2个wall且core/非正计数均为0；随后仅对这些源三角及受元素耦合合并得到的小组件运行既有有界pattern搜索。真实范围证据固定为3个共享根组件、12个候选root；新合同允许组件不超过3、候选root不超过16、质量评估不超过4096，且按当前算法、12根、5三角及最多15唯一边计算的最坏上界为3627。不得再自动扩cap、不得以wall数替代组件数、不得改变质量阈值。λ=0与λ=2^-12最终状态都须全294,059单元PASS并做A-B-A-B绝对坐标/质量重放；任一坏集漂移、未经授权的cap耗尽、core恶化、非有限值或路径状态依赖均FAIL。该门仍为AUDIT_ONLY，不写网格、不改变schedule、不调用SU2/ParaView；通过后才可把方向作为下一个dyadic前沿的seed，不能直接授权λ=1或生产。
- 首前沿的方向质量目标必须由自哈希、版本化端点逐字段声明并由运行器按该列表构造，不能用布尔开关隐式改变排序。旧minimum-growth pattern端点及其已批准哈希保持原语义；continuation专用端点必须在非有限、非正和core门之后优先最小化Prism阈下数量，再比较Prism最大/L2/L1缺陷、最小质量和方向变化惩罚，防止以扩散失败单元换取单个最坏值改善。端点schema、mode或目标顺序不符时必须在搜索前FAIL。
- count-first v2若只剩唯一3-root/1-source-triangle组件，可建立新的版本化细尺度端点，把tangent步长从既有 `2^-1…2^-5 rad` 延伸到 `2^-12 rad`；运行器必须把“最多1个可细化组件、该组件恰为3根/1三角”作为首次细化前的硬门，其他组件若不再PASS则立即FAIL。按现有静态搜索、12个dyadic步长、2次坐标pass、每边最多16个pair和最终A-B-A-B/全局复核，实际范围保守上界为 `738 + 12×2×(4×3+16×3) + 9 = 2187 < 4096`；不得据此扩大root、pair、margin或质量阈值。
- fine v3若仍仅剩同一3-root/1-source-triangle组件，可建立新的版本化三根同步端点，但必须完整重放静态阶段和single/pair fine阶段，并从fine最终最佳坐标/方向/质量状态无重置进入triple阶段。每个12个dyadic步长、2个pass中，每根最多4个已通过既有cone与changed-margin门的tangent候选，三根组合必须原子评估，最多 `4^3=64` 个组合/step/pass；不得部分提交、不得移动其他组件。保守总预算固定为 `738 + 1440 + 12×2×64 + 9 = 3723 < 4096`，余量373；所有handoff、评估边界、A-B-A-B、全局质量和残差血缘须严格哈希验证，0个静态失败时不运行fine/triple，多于1个失败或组件形状漂移时在细化前FAIL。
- triple v4若在同一唯一3-root/1-source-triangle组件上合法耗尽且残差恰为连续outer tail，可建立continuation v5的AUDIT_ONLY root-schedule collar；它必须从triple-final绝对状态无重置进入，动态令 `K=min_bad_layer-1` 且 `K>=15`，冻结首层、1..K层、60层总层数、方向场、连接关系及所有非候选坐标。仅在目标wall的稳定source-triangle edge graph上，以目标3根为distance=0按ring widths `[1,2,4,8,16,32,64]` 生成空间权重 `w=max(0,1-d/W)`；对每根原target累计日程B、minimum-growth累计日程 `A_i=i*h1`，只允许在 `i>K` 使用 `C_i=B_K+(A_i-A_K)+(1-w)*((B_i-B_K)-(A_i-A_K))`，且逐层证明 `A<=C<=B`、严格递增、首层float-hex不变。七个候选必须分别从同一S0重建并对全部294,059单元各查询一次，预算固定为 `738+1440+1536+7+9=3730<4096`、余量366；PASS间优先更小affected closure/坐标改变量。graph/距离/affected root-triangle-element closure、inner/non-target坐标和connectivity均须稳定哈希绑定；任何新坏lineage扩散、core/非正/非有限恶化或7档耗尽均合法停止，不得减层、放宽阈值或直接进入混合拓扑。
- 任一真实worker因物理内存保护中止时，controller必须发布独立、严格FAIL的resource-abort manifest，记录原始命令元数据、十六进制返回码、实际终止方式、graceful/hard-kill证据、`gmsh.finalize`为已证明或`UNKNOWN_NOT_PROVEN`、源BREP中止后路径/只读/大小/SHA-256及输出目录完整清单；不得把`output_complete`解释为Gmsh生命周期完成。Windows worker应对`SIGBREAK`执行有界受控取消以尽力进入既有`finally`，仍保留资源保护器的短宽限和强杀回退；任何中止证据缺项都维持FAIL且不得消费为下游证据。
- 运行前重新校验 `project.toml`、`cases.csv`、`markers.toml`、`topology_smoke.toml`、`boundary_layer_trial.toml`、`boundary_layer_smoke.toml`、`rear_outlet_freeze.toml`、原始 STEP、派生 BREP和权威小网格/y+证据的状态、路径与 SHA-256；任一陈旧或不一致均失败闭合；
- 粗网格目标必须来自 `project.mesh.grid_levels.coarse_target_cells=5000000`，不得在 Python 中另写项目常数；新增外部粗网格合同必须明确允许偏差、资源上限、输出根和质量阈值，并哈希绑定所有上游真源；
- 在正式粗网格前先运行至少一个低于30万单元的只读几何校准，依据实测单元数、峰值内存、运行时间和尺寸缩放给出可复核预测；资源门必须为预计峰值内存和磁盘保留安全余量，资源不足时不得启动会导致系统失稳的5M网格；
- 复用经过验证的两体共享拓扑和稳定 surface/volume fingerprint；禁止以 runtime entity tag 作为跨运行选择依据，原始 STEP/BREP前后哈希和只读属性必须不变；
- 粗网格必须覆盖四个且仅四个求解 marker，`turning_section_outlet` 仍只能是内部后处理测量面；所有外边界恰覆盖一次、两个体的共享接口恰覆盖两次且方向相反；
- 壁面首层高度必须来自现有实际 y+ 证据；项目目标20层是基线且任何位置不得少于15层，若为覆盖外缘厚度而增加层数，必须由外部合同和净空证据导出，增长率仍只来自项目配置；每个稳定 wall 成员须报告实际层数、覆盖率、停止/合并情况和最差柱质量；
- 正式粗网格须报告节点数、三维单元总数和类型、各 marker 面数、负体积/负 Jacobian/非有限计数、核心四面体和棱柱的严格质量统计、等效非正交性指标、边界层覆盖率、输出大小与 SHA-256；任一负体积、marker遗漏、共享接口错误或非有限值禁止启动SU2；
- 每次校准或正式尝试均使用新的 `runs/` 子目录，保留 PASS/FAIL manifest 和完整 Gmsh 日志，禁止覆盖既有有效工件；失败后只能根据新证据在外部配置中调整尺寸/局部场或稳定几何选择并在新目录重试，不得放宽零负体积、marker完整性或 y+ 上限；
- 只有粗网格质量门和资源门都 PASS，才可进入设计点串行正式 RANS：阶段一低 CFL 稳健一阶获得 restart，阶段二从显式哈希绑定 restart 继续二阶；后部出口只使用设计点冻结的 `MARKER_SUPERSONIC_OUTLET`，禁止压力/背压猜测；
- 正式 RANS 就绪/收敛判据必须外部配置化并同时覆盖残差、六分量力/力矩稳定性、内部测量面流量和总压恢复稳定性、全局质量守恒、出口回流、实际 y+、NaN/Inf 和持续振荡；稳态持续周期振荡时必须判定需要 URANS，不得取最后一步冒充收敛；
- 所有 SU2_CFD 与 pvbatch 调用继续经统一 `CommandRunner`、参数列表及显式 `shell=False`，保存绝对程序路径、argv、cwd、开始/结束时间、返回码及完整 stdout/stderr；普通 Python不得导入 `paraview.simple`，不得启动GUI；
- 先补齐配置、实现和标准库测试，再运行短校准；粗网格、SU2和后处理严格按门依次执行，任一前序阶段失败不得调用下游。每个阶段结束后检查日志、输出、完整回归与工作区状态并更新本节。

### 实施记录

- 2026-08-03：v4第一次真实启动目标 `_006` 在任何runner/Gmsh构造前按物理内存门失败闭合；预检实测可用物理内存7,782,854,656字节，低于8,053,063,680字节要求，`command_runner_constructed=false`、输出目录不存在且无Python/Gmsh残留。失败报告保留于 `_worker_evidence/schedule_frontier_direction_040_20260802_006/repair_audit_preflight.json`，SHA-256=`5f748db7add9225ec54e42ca51248931a5b0dfccbd5ae8da8f02d546d56e87c4`。随后仅对Codex/ChatGPT、Edge/WebView、微信和HP用户进程调用可恢复`EmptyWorkingSet`，未终止任何进程或窗口；在全新 `_007` 重试。
- 2026-08-03：真实三根同步v4审计 `_007` 合法 `INCOMPLETE/rc=2`，manifest SHA-256=`7abcf8808e85ef7383d7da2630f2f514417d276a90e83cdae31ff13ecb5a5ee2`、discovery SHA-256=`853002ecb2f0cf7e6672790b17a4303ad1475358115a4ca862a0ed2f2b09eb88`、endpoint SHA-256=`5d2bcf6c6bd4027632d54be940f349d15b1c907a21c7e1d53fb4c183e8bc5bad`，耗时171.565秒。静态645+fine684后从评估索引1329无重置进入24个triple step/pass；首8个步长因根`756a…`的cone/margin门无合法候选，后4个步长共评估192个原子组合，84个单根候选被margin门拒绝，0次接受，最终总评估1530。最终仍为12个阈下Prism、minSJ=0.003730823，全部是wall`8c1b…`、三角`9aaa…`、层49–60；Core gamma最小0.001112365、非正/非有限均0，源BREP不变、写盘尝试0、SU2/ParaView均未调用。
- 2026-08-03：`_007` 的root schedule证据定位了方向优化无法解决的离散原因：坏三角两根`756a…/9e318…`同时邻接wall`26d33…`与`8c1b…`，在λ=2^-12按逐层最小累计厚度为`1.7850814213513045e-5 m`；第三根`e356…`只邻接`8c1b…`，累计厚度为`2.6199575199841104e-5 m`，层60高出46.77%，差异从层49开始越过质量门并与12层残差严格对应。下一门不再扩大方向搜索，而以稳定root/triangle/wall血缘对该CAD面交界建立有界空间渐缩collar，先在内存中逐层平滑累计高度并重新查询全部294,059单元；任何新坏集扩散、首层/60层变化、质量/marker/core恶化均FAIL。
- 2026-08-03：完成首前沿三根同步tangent v4软件门并统一运行器、续接合同和低层组件验证器。运行器严格从fine最终状态无重置进入12个dyadic步长×2 pass的原子三根笛卡尔搜索，每根最多4个既有cone/margin合法候选、每pass最多64个组合，不允许部分提交；`fine_exit_checkpoint`以9个严格字段绑定完整quality/objective、三条selected directions及重算的direction state/field哈希。续接合同把静态738、fine1440、triple1536和tail9冻结为总上界3723<4096、余量373，并逐条验证24个step/pass记录、连续评估索引、handoff、最终状态和A-B-A-B。四模块组合93/93 PASS，全库770/770 PASS，`compileall -q src tests scripts` PASS；产品代码无`shell=True`、Gmsh GUI调用或普通Python导入`paraview.simple`。本门未启动Gmsh、SU2或ParaView。
- 2026-08-03：v4文件SHA-256分别为：`coarse_direction_feasibility.py=b82868b1478877bb56fc385e61537dbb60b5f6c964076d16f96b7be584e2d640`、其测试`dd1b033da789c537de85e7d67c11285dd85f3bd9122fa84adf4138625e6ab820`；`coarse_schedule_continuation.py=abb678ae3e471d36d522a8410eaf5543d19a81d91d2495efbc7246f6026a9577`、其测试`91d3b7912f269c7bffdf936d7a86652245c6102fd1ce6c508ad08ac1081a0f42`；`coarse_component_direction.py=72c4c8059a29f4754a242f78fc2967c5b71d6d3567b2b1e31a0e0fa691ce699e`、其测试`eaf21a899b8e746b19fc46cd4d62128a7cbbe5c5ee3ec3d07eec2a62f3292af6`；`boundary_layer_smoke.py=e241c88d0630a9bbac1306f67e7e03076c9be31ba10ae8e1a26cc06e0b601c28`、其测试`1356b9ccd8e6d21edb6ffaaeb83e42762deb0acd607988f22bb5bbb97d9609a7`。内存复核时可用物理内存低于真实审计的8,053,063,680字节门；只对ChatGPT/Codex白名单进程执行可恢复`EmptyWorkingSet`，回收约1.23 GB但仍未满足启动门，因此未强行启动Gmsh。
- 2026-08-03：完成root-schedule collar v5软件门。端点冻结唯一3-root/1-source-triangle连续outer-tail组件、动态`K=min_bad_layer-1>=15`、ring widths `[1,2,4,8,16,32,64]`及逐层`C_i`公式；运行器从triple-final绝对状态无重置进入，每档从同一S0重建，对全部294,059单元各查询一次，任何坏血缘扩散、core/非正/非有限恶化、坐标或连接漂移均FAIL。续接验证器独立重导edge graph/BFS距离、60层A/B/C、受影响root/triangle/Prism/core闭包和A-B-A-B最终状态，预算严格为3730<4096。四模块定向回归99/99 PASS，全库776/776 PASS，`compileall -q src tests scripts`返回0；产品源码未发现`shell=True`、Gmsh GUI调用或普通Python导入`paraview.simple`。本软件门未启动Gmsh、SU2或ParaView。
- 2026-08-03：v5当前文件SHA-256为：`coarse_direction_feasibility.py=6c72bcda166209edd76712a0cdb1568d542a31ae25f946479d22582483623a4e`、其测试`7150ab7e8a9a474b572ddfd7003d8ff900856acd5c900818810791657932b32b`；`coarse_component_direction.py=72c4c8059a29f4754a242f78fc2967c5b71d6d3567b2b1e31a0e0fa691ce699e`、其测试`eaf21a899b8e746b19fc46cd4d62128a7cbbe5c5ee3ec3d07eec2a62f3292af6`；`coarse_schedule_continuation.py=5a70d1c2a967f5cb82edb6b90a9cd3e229718fd4dea55267afb6e7491e7db1a7`、其测试`eff0291a26d202bc2403f03866a7481691658d20188439d0ebdef942d6d27b46`；`boundary_layer_smoke.py=496ad81ba667ecd17cdef614655f4c0bed6ec1f87dcd3131d44607635cf837dd`、其测试`55beb662fefbaba6771c0eb7abb0d8222c959425032731176c989619fec295f2`。当前目录仍非Git仓库，`git status --short`按预期无法生成diff，继续以完整回归、静态策略扫描和精确哈希替代。
- 2026-08-03：首次v5真实目标 `_008` 在8,381,952,000字节可用物理内存下通过启动预检并进入Gmsh网格构造，但运行期可用内存降至4,804,853,760字节，低于4,831,838,208字节保护线约25.7 MiB，监控器按设计终止worker并保留FAIL证据；这不是collar质量结论。`worker.command.json`记录`resource_aborted=true`、未超时、原始stderr为空、输出目录不存在且零MSH/SU2/VTK；派生只读BREP重算SHA-256仍为`13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121`，无Python/Gmsh残留。preflight/worker/command证据SHA-256分别为`9ede283b8fd5b0088b8fe1fa9f15f862ebafa32e2fb38c3248e66a7127d3d4bc`、`49cf4fc59327cc8e87e7bc32ea638bde574e7bd0c0b29d5364f35dde0f894d25`、`4bc08b7070ed3890970c7b31e12f3b5b9a3e0801d3a89860c2913dac49b4c1a6`。下一次只能在全新 `_009` 且更大运行期物理内存余量下重试，不得放宽3 GiB Job、4 GiB系统保留或512 MiB监控余量。
- 2026-08-03：修复 `_008` 暴露的资源中止证据缺口而不改变资源/质量门。`CommandRunner`现以32位二补码记录`status_hex`及graceful/hard-kill尝试与成功证据，Windows仍先`CTRL_BREAK_EVENT`、0.5秒后有界强杀；worker在Job limit安装且初始证据落盘后注册可恢复的`SIGBREAK`受控取消，使Gmsh已有`finally`有机会执行。controller在资源中止时独立发布原子`resource_abort_manifest.json`，严格重导终止方式、记录`gmsh.finalize=UNKNOWN_NOT_PROVEN`、后验只读BREP身份和递归输出清单，并固定`FAIL/downstream_consumable=false`；旧或伪造元数据失败闭合。四模块交叉114/114、全库788/788 PASS，`compileall -q src tests scripts`返回0，产品源码安全扫描无命中。
- 2026-08-03：资源中止加固文件SHA-256为：`process.py=301a138bd9490bc299d73fb9e9520f0432e52b24454d671cf8d07332ddfe06d9`、其测试`e2566b2fa6f90a4b44144c0fa8d2ce206f4bfbb5bf5449398b28e7d89b007ed9`；`coarse_repair_audit_worker.py=48164d3aa5e4df1cb462fae38e991763852577d1a87f4e8470b04f4683e331f8`、其测试`d18419dabb1fa6c0c123b8d8491ff3ab540841ff67e36f707daed99a4cf4b6ca`；`coarse_repair_audit.py=8914abf120db19b4e32195c791f927d93ddcdb11f20c54b3528d03bbe558084f`、其测试`1b2b063d5a8972950ada6ec1fbb7081d517f564cf05f2a1e5c8be725d768ba6f`；`cli.py=1220f14c65271355a3015b542a574710c549cf441e44f63d0425e0eeab9ed90e`、其CLI测试`49bf6538774c3f2888be82976329aca66a21cac0827287cdcd23a882fde656c5`。当前目录仍非Git仓库，继续以全回归、精确哈希和静态策略扫描替代diff。
- 2026-08-03：全新 `_009` 在临时白名单资源守卫下以9,767,510,016字节（9.097 GiB）可用物理内存启动，Gmsh完成三维初始网格（6,531节点、35,123元素）后，统一执行会话因用户状态询问而被中断；当前无controller/worker/Gmsh或守卫进程、正式output目录不存在、stderr为0，派生BREP仍只读且SHA-256不变。由于外层中断发生在CommandRunner写终态metadata之前，evidence目录只有preflight、worker初始骨架和截断stdout，没有`worker.command.json`、resource-abort manifest或正式audit manifest；故 `_009` 明确为`INVALID_INTERRUPTED/NOT_CONSUMABLE`，不得推断collar PASS/FAIL，也不得覆盖或续跑。四份残留证据SHA-256依次为`157630c7dae2a63a2dfd5ef386748d2f667533303cf9e30348eb1e8e55164796`、`5445d5ac84a2537cbecafb139055ff48ccaf5f4044a28614c36d0485e4b2ceac`、`d81d43535674ed8a2f18b9966f6db2b7c57568ca3f1f957697b4a03d836dd68b`和空stderr的`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。
- 2026-08-03：在全新目录 `runs/mesh/coarse/schedule_frontier_direction_040_20260802_005` 完成真实fine v3审计。CLI仅因最初误传3个当前parser不接收、而由权威输入内部复验的冗余SHA参数在argparse阶段返回1；该次没有创建目录或启动Gmsh。按实际帮助移除冗余项后，隔离worker合法 `INCOMPLETE/rc=2`：manifest SHA-256=`2957712288afdf7274f82f487921f4a91f468a3cd0b6924cedc2817ecbd2aa99`、discovery SHA-256=`b902331d06395700637aff6995f4961650db8f304700a64bf357365a6e2d46d2`、v3 endpoint SHA-256=`0e82de3c9b1cf7573eb63233f9f4c4c9707cc604b0068a7443a25d1d0b987acf`。三组件静态退出证据与 `_004` 完全连续，唯一失败组件在684次fine评估中接受6次移动；总1338次评估后阈下Prism由13降至12、最小minSJ由0.003676277升至0.003730823，12个残差只覆盖原始三角`9aaa2aed…`第49–60层。Core gamma最小0.001112365、core失败/非正/非有限均为0；A-B-A-B、源不变、3 GiB Job、空stderr、零write/SU2/ParaView均成立。下一门只补三根同步tangent组合，不放宽物理或资源门。
- 2026-08-03：完成首前沿唯一失败组件的 fine continuation v3 软件门。新端点严格冻结 `2^-1…2^-12` tangent 步长、count-first 目标、最多1个可细化组件且必须恰为3根/1源三角/3边；运行器先让全部3个静态组件从 fresh A 态退出，只有且恰有1个满足上述形状的失败组件才进入细化，其他组件完全冻结。前五步 checkpoint 与后七步 tail 的坐标、方向、质量、目标和评估边界连续性均由 discovery validator 交叉验证；预算自哈希证明固定为 `738 + 1440 + 9 = 2187 < 4096`，没有扩大根集、pair、质量阈值或执行权限。controller/worker/CLI继续从权威 schema 常量解析，不写网格、不调用SU2/ParaView。
- 2026-08-03：v3 四模块组合回归83/83 PASS；随后从仓库根目录执行完整 `unittest discover -s tests -v` 为760/760 PASS、返回码0，`compileall -q src tests scripts` PASS。静态扫描未发现产品代码中的 `shell=True`、Gmsh GUI调用或普通Python导入`paraview.simple`；命中项仅为测试中的禁止性断言。目录无`.git`元数据，继续以逐文件哈希、完整测试和运行证据替代Git diff。
- 2026-08-03：运行前内存安全门曾低于要求；按用户明确授权，仅对精确白名单 `ChatGPT`、`codex`、`codex-code-mode-host` 调用Windows `EmptyWorkingSet`，未终止进程、未调优先级、未触碰其他应用。最近一次可用物理内存由7,783,682,048字节恢复到9,072,541,696字节，超过8,053,063,680字节安全门；真实审计启动前仍须重新测量，不足时只重复同样的可恢复整理。
- 2026-08-02：在全新目录 `runs/mesh/coarse/schedule_frontier_direction_040_20260802_004` 完成真实count-first v2审计，manifest SHA-256=`150f6bdb229f8072cce8173625de2a4fbaee97e26848ecac2f607001dd40f1c4`、discovery SHA-256=`b02157aad074bd5566df06dabce6eeb55db8e481a8bc49395a0b01d13cfbcca7`、子搜索endpoint SHA-256=`5c928003f43bcef8070939facea5d07b1f52ec01ecd1e69f89a6f2ed0bebf235`；worker合法 `INCOMPLETE/rc=2`。894次评估、A-B-A-B、3 GiB Job、源不变、Gmsh生命周期和零write/SU2/ParaView均通过。前两个interaction组件继续PASS；唯一失败的 `5cc043…` 为3根/1源三角，阈下数由入口21降为13，且13个全部属于初始 `9aaa2aed…`、wall `8c1b…`、层48–60，没有新增lineage，最小minSJ=0.003676277；Core gamma最小0.001112365且零core失败。现有最小tangent步长0.03125 rad且240次pattern评估无accepted move，故下一门仅增加该组件的细dyadic分辨率，不改变物理门。
- 2026-08-02：将首前沿continuation授权与搜索语义升级为显式v2合同。顶层endpoint/discovery/mode、自哈希count-first子端点、旧generic pattern来源SHA、真实范围 `C/R/T=3/12/5`、Prism/Core阈值、3/16/4096/16 caps及循环推导预算证明均被交叉绑定；预算 `B(C,R,T)=15C+88R+504T+6` 对实测范围为3627、cap范围为3979、余量117，proof本身另有schema/status/SHA。运行器现按endpoint声明顺序构造目标，不再以布尔值隐式选择目标；旧minimum-growth端点golden SHA保持不变。相关6模块组合144/144、完整748/748标准库测试及`compileall -q src tests scripts`均PASS，尚未启动本v2真实Gmsh审计。
- 2026-08-02：完成 continuation ENDPOINT/DISCOVERY v2 升级的最小跨模块兼容修正。隔离 worker 不再写死 discovery v1，而是在 continuation 请求已成立且重模块安全门通过后，从延迟导入的 `coarse_schedule_continuation.DISCOVERY_SCHEMA` 获取权威 schema，并对缺失/非字符串值失败闭合；worker 测试夹具同样复用该权威常量。controller、CLI 和 smoke builder 原已导入权威常量，无需修改；后续复核确认 sibling 已同步把 `test_boundary_layer_smoke.py` 的最后一处断言切换为现有 `CONTINUATION_DISCOVERY_SCHEMA` 别名。本轮未触及 continuation 实现、runner/contract、阈值或对应专属 continuation 测试。
- 2026-08-02：在3组件/12根合同下于全新目录 `runs/mesh/coarse/schedule_frontier_direction_040_20260802_003` 完成首次真实有界搜索，worker合法 `INCOMPLETE/rc=2`；manifest SHA-256=`879d19a02b0005a3350f551b29fa844c85864fe63e6448172e869d281759c2b7`、discovery SHA-256=`86376b91010192a791aa6c39b51a11bf648dbf7fba1088dfbf1a34b3f83ccb3c`、endpoint SHA-256=`fb4b328f6e56760f8f7be1541b99d77031a20953cf3009576801b15bfe9ce5e6`。894次质量评估、A-B-A-B坐标/质量重放、3 GiB Job、initialize/clear/finalize、源不变和零write/SU2/ParaView均通过；两个交互组件最终PASS，第三个3-root组件仍FAIL。旧目标把Prism最大缺陷由0.007315706降至0.003354382、最小minSJ由0.002684294升至0.006645618，但阈下Prism从37增至134且残差扩散到3个原本非候选源三角；core低质、非正、非有限仍全为0。因此阻塞是目标排序与“消除阈下集合”目的不一致，不是资源、Gmsh、拓扑cap或随机性；下一最小修复只新增continuation专用count-first版本化pattern端点，旧端点和全部阈值/cap/候选保持不变。
- 2026-08-02：增强fail-closed异常计数并通过46/46边界层回归后，在全新 `schedule_frontier_direction_040_20260802_002` 只读重放到同一搜索前范围门。manifest SHA-256=`e7515592fde22aebb3ae778077c6cdc3124cda684cccf5950951a5ec931b88c6` 明确记录 `component_count=3 > old cap=2`、`candidate_root_count=12 <= cap=16`；source_unchanged、initialize/clear/finalize、零write/SU2/ParaView和空external_commands均成立。按现有搜索代码，静态段最坏 `14C+48R+24T=738`，tangent/pair段最坏 `40R+160E<=2880`，A-B-A-B/最终复核保留9次，总计最多3627<4096。因此新endpoint只把component cap由2精确改为3；root16、eval4096、pair16、λ、schedule、Prism0.01/Core0.001及所有AUDIT_ONLY权限不变，若实际拓扑再漂移或超过3仍FAIL。
- 2026-08-02：全库739/739测试与`compileall`通过后，以新目录 `schedule_frontier_direction_040_20260802_001` 启动首次真实λ=2^-12方向continuation审计。资源预检、3 GiB Job、权威homotopy/方向/日程哈希、Gmsh生命周期、源不变和零写盘/零下游均正常，但在任何方向候选评估前由 `_frontier_component_precheck` 返回基础设施FAIL：5个稳定坏三角按“共享任意root才连通”的真实拓扑得到的组件或候选根超出当前2组件/16根合同，原错误未持久化两项实际计数，因此不能据此猜测或直接扩大cap。最小修复仅让同一fail-closed异常携带实际component/root count并增加单测；下一次必须用全新目录取证，仍不得搜索超出原cap、写网格或调用SU2/ParaView。
- 2026-08-02：完成首个失败前沿方向continuation的软件门接线，但尚未启动真实Gmsh审计。新增请求 `physical-schedule-first-frontier-direction-audit`；controller必须显式接收两份独立方向审计及其SHA、权威homotopy manifest及其SHA，重新校验当前合同/projection/local-schedule/approval/homotopy endpoint，并只接受 `INCOMPLETE`、最大已测PASS=`0`、首个FAIL=`2^-12` 的证据。controller构造自哈希continuation endpoint后仅把endpoint SHA传给隔离worker；worker在3 GiB Job和既有运行中物理内存保护不变的条件下独立重新加载所有来源、重建endpoint并逐项比较，任何缺失、路径/SHA漂移或endpoint不一致均在audit前FAIL。运行器只允许37个低质Prism、5个源三角、2个wall构成的最多2组件/16根、4096次质量评估小搜索，并由单一权威validator严格核对A-B-A-B、组件caps、pattern endpoint、changed roots、全局质量及自哈希；仍禁止写网格、SU2、ParaView和生产授权。只读复核真实 `_002` manifest SHA=`85ae060051eb802295262e9c1d6451f1b517d4c55a2b1059c443677b1bb62412`、discovery SHA=`146763548965358f581a258c45ac00e039cea8628c59eb4cfcaeb60bc75ce7b0` 及上述首前沿完全吻合。专项合同/runner/controller/worker/CLI组合测试132/132 PASS，`py_compile` PASS；测试使用mock/临时工件，未调用真实Gmsh、SU2或ParaView。
- 2026-08-02：修复上述旧schema守卫后在全新 `schedule_homotopy_040_20260802_002` 完成真实14点正/逆序同伦审计，manifest SHA-256=`85ae060051eb802295262e9c1d6451f1b517d4c55a2b1059c443677b1bb62412`、discovery SHA-256=`146763548965358f581a258c45ac00e039cea8628c59eb4cfcaeb60bc75ce7b0`。28次全网格质量评估及逐点coordinate/root-schedule/quality/aggregate哈希完全复现，worker合法`INCOMPLETE/rc=2`；294,059个单元、280,320 Prism6、13,739 Core Tet4、源文件不变、零写盘和零下游调用均受验证。λ=0最小Prism minSJ=`0.0110672354`、Core gamma=`0.00109865957`并PASS；λ=2^-12首次FAIL为37个低质Prism/5个源三角/2个wall、零core低质和零非正，故首因是物理厚度触发的局部方向/三角化不连续，不是core或资源基础设施。λ=1与既有固定重放一致为10,662个低质Prism、202个低gamma Core Tet4、12,195个非正，未被伪造为PASS。
- 2026-08-02：首次真实 fixed-direction schedule-homotopy 审计写入新目录 `runs/mesh/coarse/schedule_homotopy_040_20260802_001`。资源预检、3 GiB Job、源哈希、Gmsh initialize/clear/finalize、28次内存质量评估以及零写盘/零SU2/零ParaView均正常，worker峰值远低于上限且未超时；但完成扫描后被 builder 的旧安全schema守卫误拒绝：该守卫看到 fixed-direction approval 就固定期望旧 physical-replay schema，未优先识别新的 `HOMOTOPY_DISCOVERY_SCHEMA`，因此返回基础设施 `FAIL/rc=1`，没有发布或伪造科学质量结论。下一最小修复仅扩充该安全守卫的显式homotopy schema分支并增加旧重放/新同伦互斥回归；修复后必须在全新目录重跑。
- 2026-08-02：按规定重新完整读取 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv` 和 `config/markers.toml`；确认设计点、SI单位、5.2 m参考长度、0.2123716634 m²参考面积、(2.6,0,0) m力矩原点、RANS/SST、y+<1、粗/中/细目标和稳定 marker 均只来自 config 真源。
- 2026-08-02：复核权威小网格 `_015`：49,406个Gmsh节点、117,551个三维单元（81,300 Prism6 + 36,251 Tet4）、5,420列×15层，四个marker完整，fresh readback、SU2文本、零非正/非有限、核心gamma>=0.00348267和棱柱minSJ>=0.0119749均PASS；其后的200/500步一阶与200步二阶小型RANS已证明实际最大y+<1、零出口回流和设计点出口语义，但均明确无生产资格。
- 2026-08-02：本机只读资源快照为24逻辑处理器、15.64 GiB物理内存、约1.9 GiB即时可用内存、C盘约155 GiB可用；仓库 `runs/` 约0.52 GiB。当前资源不足以未经校准直接安全承诺5M网格，故先建立可量化资源预检，不将工具可启动等同于生产可用。
- 2026-08-02：三路独立只读审计一致确认现有 smoke 实现不可直接放宽：它把15层、30万单元、离散化相关修复计数和高内存逐单元/逐面审计绑定为诊断语义；正式粗网格必须使用独立 schema、工作进程和低内存审计。审计同时确认当前没有 `templates/su2`、正式粗网格CLI或联合RANS收敛/振荡判定，故这些属于粗网格PASS后的独立质量门，不能以现有 `rans-smoke` 冒充。
- 2026-08-02：新增独立 `config/coarse_mesh.toml`、边界层外缘厚度规划器、粗网格合同/隔离worker、常数内存SU2文本审计、资源投影与校准预检模块。合同把首层高度绑定到实际最大 `y+=0.9228949547` 的证据，采用仅作规划的平板湍流 `delta99` 估计并导出60层、总厚度 `0.0778871424 m`；该估计明确要求后续用真实解验证，未被冒充为生产证据。
- 2026-08-02：校准点调整为 `0.30/0.25 m`：首点用于争取满足小于30万单元的标定要求，第二点复用已验证的 `0.25 m` 离散尺度；每点仍受75万单元、840秒、串行、无SU2/ParaView和新目录硬门限制。粗网格builder现在无条件交叉核对Gmsh生成、fresh readback及序列化SU2的单元数/类型、marker数、文件大小和SHA-256；Gmsh deferred诊断仅严格PASS才可发布PASS，失败工件和原始异常均保留。
- 2026-08-02：新增 `pipeline coarse-mesh-calibrate`。控制进程只以绝对当前Python、参数列表和统一 `CommandRunner` 启动隔离worker；后续资源复核发现原首点“8 GiB即时可用”常量没有实测依据，不能证明4 GiB系统保留量，因此已转为外部合同中的“系统保留量 + worker硬限额 + 监控余量”公式。第二点仍必须显式引用首点PASS manifest并通过实测内存/时间/磁盘/单元上限门；页文件不能替代物理内存，任一预检FAIL不构造runner、不导入Gmsh并保存严格JSON证据。
- 2026-08-02：补充正式RANS只读差距审计。现有 `rans-smoke`、诊断配置与短步后处理明确不可升级标签后冒充正式入口；即使粗网格PASS，仍须独立的case-scoped两阶段配置/模板、正式粗网格manifest门、检查点编排、Pyramid5后处理兼容、restart链及 `rans_convergence.py` 联合收敛接线。该工作保持在粗网格质量门之后，没有提前运行SU2或pvbatch。
- 2026-08-02：新增不生成网格的 `boundary_layer_clearance.py` 及 `pipeline boundary-layer-clearance` 入口；它在fresh Gmsh Python API会话中以稳定指纹重匹48个wall面，对每面5个定向样本使用 `model.isInside` 作倍增/二分法向净空取证；严格记录initialize/finalize、源BREP前后哈希及完整异常，不调用 `mesh.generate`、`gmsh.write`、SU2、ParaView或GUI。
- 2026-08-02：真实净空报告证明全壁面统一 `0.0778871424 m` 厚度不可行后，新增纯Python局部日程规划：48个稳定wall成员均保留实际y+导出的首层高度和60层，只把每面的总厚限制为 `min(全局设计厚度, 0.40×该面最小单侧净空)` 并求解不超过项目1.2增长率的唯一局部日程。现有计划覆盖48/48面、240/240样本，20面受净空限制，局部总厚范围 `0.005191275–0.0778871424 m`，但在builder接线和新网格质量门通过前不授权挤出或生产。
- 2026-08-02：修正粗网格临时构造壳的危险参数错误：临时一层高度现在只能来自已验证base-smoke的 `construction_first_layer_height_m`，不再把完整60层总厚 `0.077887 m` 误传给单层构造。新增projection-only路径，只生成内存中的临时一层Prism6与两个core，在60层细分、写盘和任何下游调用前返回，并按 `core非棱柱数 + 壁面源列数×60` 严格检查小于30万。
- 2026-08-02：新增Windows Job Object worker硬限额、projection资源预检与 `pipeline coarse-mesh-project`。外部合同设置2 GiB投影worker上限、512 MiB监控余量和4 GiB系统保留量，故真实启动要求至少6.5 GiB即时物理内存；页文件不能补救。worker在导入Gmsh/粗网格模块前安装硬限额并先写证据，失败保留原始Win32/嵌套job异常；普通Python仍经绝对当前解释器、参数列表和 `shell=False` 启动。
- 2026-08-02：把运行中物理内存保护接入统一 `CommandRunner`。projection/calibration worker启动后每0.5秒读取Windows物理内存；低于“4 GiB系统保留+512 MiB监控余量”即安全终止进程组、排空stdout/stderr并记录 `resource_aborted/resource_abort_reason`，采样本身失败同样fail-closed。独立128 MiB子进程实机证明本桌面会话可成功创建、配置并分配带 `PROCESS_MEMORY/KILL_ON_JOB_CLOSE` 的Windows Job Object，排除了嵌套Job不可用这一潜在阻断。
- 2026-08-02：将净空证据依赖收敛为独立稳定子合同，只包含只读pipeline BREP路径/SHA、marker配置路径/SHA与匹配容差、48个wall稳定指纹、所需总厚、采样与搜索语义；单元目标、校准尺寸、质量阈值、worker内存策略和下游局部厚度比例均明确排除。原 `_002` 报告保持不变，经纯Python逐项重验证生成 `clearance_revalidated_20260802_001/boundary_layer_clearance.json`，保留原路径/SHA/旧合同哈希，且明确重验证期间未调用Gmsh/SU2/ParaView。
- 2026-08-02：完成局部60层日程到实际粗网格subdivision的hash-bound接线。每个root按其incident稳定surface fingerprints取逐层累计高度的元素级最小值，共享根因此保持连续且不会放大任一面的净空计划；chain构造、top relocation、方向平滑和fresh验证均消费root-specific schedule。calibration CLI和隔离worker现均要求显式 `--local-schedule/--local-schedule-sha256`，在Job前检查路径/JSON/SHA，Job后再验证完整lineage与48面覆盖，缺失、篡改或陈旧证据均在builder前停止。
- 2026-08-02：从重验证净空证据生成当前合同的 `local_schedule_20260802_002/boundary_layer_local_schedule.json` 并实际完成消费绑定：48/48面、60层、20面净空受限，总厚范围 `0.0051912750000000394–0.07788714239282687 m`；plan SHA-256为 `30f5edc4ffbea1dccb22763c0f5ba4353ebd8f0cb74cad57ec09d6919b13d3a1`，binding SHA-256为 `c8142b01918fb991e5b4c7837f035b5871529b5f418d895d39fb875b2da41fb9`。该PASS仅授权后续受限校准消费，不授权生产网格或求解。
- 2026-08-02：资源恢复后真实执行首个 `0.30 m` projection；控制器、Windows Job、配置/合同哈希、只读源、Gmsh initialize/finalize/clear 均正常，进程峰值private commit仅约279 MiB，但HXT在核心PLC恢复时明确报告线段 `[3660,282]` 与facet `[279,3644,12]` 于 `(5.1218,0.212288,0.115495)` 相交并FAIL。该离散失败完整保留且未进入细分、写盘、校准、SU2或ParaView。依据已验证 `0.25 m` HXT网格的36,251个core Tet4与5,420个wall columns作保守尺寸外推，`0.28 m` 的60层预计约28.5万单元；因此仅把外部首校准尺度收紧为 `0.28 m`，保持HXT、60层和所有质量阈值不变，并要求新projection实际证明严格小于30万。
- 2026-08-02：`0.28 m` 新目录重试仍在HXT PLC恢复阶段FAIL，但交叉转移到后部镜像区域 `(5.12459,-0.194433,-0.137789)`；5036个边界顶点、886个待恢复facet，线段 `[3944,293]` 与facet `[295,3928,15]` 相交。两次失败分别映射到后部镜像top-shell稳定wall fingerprints，证明仅调整全局尺寸不稳健。下一最小修复改为恢复首点全局 `0.30 m`，并在粗网格合同中仅对后部六个对称稳定wall fingerprints施加已由全局 `0.25 m` 成功HXT证据支持的局部 `0.25 m` size cap；该局部场必须同时被projection、calibration和未来production消费、纳入合同哈希，且实际projection仍须严格小于30万。
- 2026-08-02：全局 `0.30 m` 加上述六面局部cap后，HXT已成功恢复PLC并完成内存中一层网格，证明局部稳定指纹修复对症；随后严格计数门按设计拒绝 `332,375` 个预计三维单元（23,975个core Tet4 + 5,140列×60层），超出30万32,375个。六个局部cap面实测仅290列，其余面4,850列；用本次实际分解外推全局 `0.32 m`、保持六面 `0.25 m` cap得到约292,917个预计单元。故下一新合同只把候选上限和首校准点扩为 `0.32 m`，不改变局部PLC修复、HXT、层数或质量阈值，仍由真实projection作最终严格判定。
- 2026-08-02：`0.32 m` 加同一局部cap的HXT再次成功并完整finalize/source校验，但实测投影仍为 `321,144=20,904+5,004×60`，说明固定局部细化使wall-column数量对全局尺寸响应显著弱于纯 `h^-2`。以 `0.30/0.32 m` 两次真实成功HXT的core/column分量分别拟合“固定局部floor + 尺寸幂项”，得到 `h=0.38 m` 仅约1,448单元余量而不安全，`h=0.40 m` 预计约292,768并保留约7,232余量；下一首点据此改为 `0.40 m`，仍保留六面 `0.25 m` cap并以实际严格计数决定，不把拟合值当PASS。
- 2026-08-02：首次 `0.40 m` 在同一后部区域暴露新的PLC交叉 `(5.1665,-0.128384,-0.194894)`，严格FAIL后按预先审计退路只把同六面局部cap由 `0.25 m` 收紧到 `0.225 m`；新目录真实projection最终PASS：13,739个core Tet4、4,672个wall columns、60层投影总数 `294,059`，严格余量5,941。manifest SHA-256为 `329b271b95477afab3a540139b418909a000f08cb69f4d14c98acedfe16e26bc`，独立严格加载器复验PASS；Gmsh finalize/source unchanged/Windows Job均PASS，峰值private commit约277.4 MiB，48.51秒，无网格写盘、无完整层细分、无SU2/ParaView。该证据仅授权同合同首个校准点启动，不授权生产网格或求解。
- 2026-08-02：上述配置变更使旧local schedule按设计变为陈旧；在不导入Gmsh的纯Python阶段，从稳定净空报告重新生成 `local_schedule_20260802_003`。新plan SHA-256为 `ef6853eed651e83a03999deedea7c63c6297e7508354be8f7ec275d046b9dc9e`，绑定SHA-256为 `7d4d893124b37d2533ce0601772543c0d59ec964c703a2ab335158e71c4ac007`；它精确绑定当前合同 `e70ab170...05a45`、稳定净空子合同与报告SHA，48/48面、60层、20面受净空限制、无runtime tags，严格PASS。该重生成没有网格、SU2或ParaView调用。
- 2026-08-02：校准控制器、隔离worker和最终manifest门完成严格证据接线：配置/合同SHA、首点projection路径/SHA/计数及local schedule路径/SHA/binding均在轻量预检、Job内重导入后和worker返回后复验；首点实际单元数必须与projection精确相等，返回码0但证据不完整仍FAIL。组合测试同时发现原第二点 `0.25 m` 从首点 `0.40 m` 作保守三次缩放时预计 `1,204,466` 单元，必然超过75万校准上限；因此在实跑前把第二点改为 `0.32 m`，保守预计 `574,334`，不放宽任何单元/内存/质量门。
- 2026-08-02：第二点变更产生新的raw config SHA-256 `4e4cbd25b65483f3236a7df861c59a3170d99a550ed8043367c2ee97660ee1f2` 和normalized contract SHA-256 `41cdfa75d995520b751fd653b6dd74e3cd139fcda12238023f1cfe3942f1d3b8`，旧projection/local schedule按设计失效。新合同下真实重跑 `projection_040_rearcap0225_cal032_20260802_001` 再次严格PASS，仍为 `294,059=13,739+4,672×60`，manifest SHA-256 `9fdad48e0c60178b39bf56cfd717490d137d47b1a80a7465f713056ebc343b6d`；随后纯Python重建 `local_schedule_20260802_004`，plan SHA-256 `23a66ddfb78ae9dc28daa7f51d61cdafbe8e447948a72b15faf64f5c682941d1`、binding SHA-256 `efa52e21ab39b75d6f0fbc127812f0eae1dd4ce82178eb4e2b7ae7ba999637c6`，48面/60层严格PASS。二者仅授权首点校准，不授权生产网格或求解。
- 2026-08-02：严格投影血缘现已接入校准controller、隔离worker、builder和最终manifest门：worker argv显式携带原始配置SHA、规范化合同SHA、投影manifest路径/SHA；首点生成数必须与投影数精确相等，第二点只继承首点投影lineage；即使worker返回0，旧式极简PASS manifest也会失败闭合。发现 `0.40/0.25 m` 的保守立方外推会把实测首点 `294,059` 放大到约 `1,204,466`，必超75万门后，将第二点安全修正为 `0.32 m`，对应保守预测约 `574,334`、余量约 `175,666`。新原始配置SHA-256为 `4e4cbd25b65483f3236a7df861c59a3170d99a550ed8043367c2ee97660ee1f2`，规范化合同SHA-256为 `41cdfa75d995520b751fd653b6dd74e3cd139fcda12238023f1cfe3942f1d3b8`；旧projection和 `local_schedule_20260802_003` 均按设计失效，必须在新合同下重建，本轮未启动Gmsh/SU2/ParaView。
- 2026-08-02：首次完整首点校准 `calibration_040_cal032_20260802_001` 在3 GiB Windows Job和运行期物理内存保护下安全执行；Gmsh initialize/clear/finalize、源BREP只读、projection/local-schedule血缘与进程证据均正常，worker峰值private commit约291.3 MiB，未触发timeout或资源中止。严格修复合同在写网格前正确FAIL：粗尺度实际一层坏Prism6为14个、旧smoke派生合同仅允许11个；同时观察到非正Jacobian14、非正体积8。失败未写MSH/SU2，未启动SU2/ParaView，证明离散修复数量必须按粗网格证据单独标定，不能直接复制小网格常量。
- 2026-08-02：新增粗网格专用 `AUDIT_ONLY` 修复发现核心、隔离worker和 `pipeline coarse-repair-audit`。该路径只在内存中完成完整60层细分、坏棱柱/源三角/根坐标稳定签名与唯一owner发现，明确禁止网格写出、calibration PASS、production资格及下游调用；partial initialize和异常路径仍保留原始traceback并finalize。CLI返回后还会严格复验3 GiB Job安装顺序、六组内存快照、worker argv、projection/local-schedule绑定、独立worker证据与embedded manifest一致性；伪造或缺失隔离证据即使返回码为0也FAIL。专项20/20、完整621/621标准库测试和 `compileall -q src tests` 均PASS；尚未在资源硬门以下启动真实审计。
- 2026-08-02：真实审计前的独立对抗复核发现并修复三条伪PASS风险：严格失败三角没有唯一直接sharp owner时不再允许被一环邻居推断吞掉，而是强制 `INCOMPLETE`；Gmsh logger的Fatal/Error/Warning/NaN/Inf及logger缺失/捕获失败均fail-closed；一旦尝试initialize，无论状态检查false、缺失或抛错都无条件尝试clear/finalize。审计层还通过proxy禁止并计数任何 `gmsh.write`，明确区分内存mesh mutation与最终smoothing。根CLI复用同一公共严格discovery schema，并逐项验收canonical UTC、Gmsh模块身份、logger诊断、零write、精确observed-count/repair-site签名、Win32 Job flags、生产内存语义、同一worker PID、重复JSON键、config/projection/schedule/source BREP重哈希以及外置/内嵌worker证据一致性。专项36/36、完整637/637标准库测试和compileall均PASS。
- 2026-08-02：随后两次新目录首点repair-audit尝试 `_001/_002` 均在runner构造和Gmsh导入前被物理内存硬门正确拦截；第一次正式preflight记录即时可用物理内存 `7,969,189,888 B`，低于要求 `8,053,063,680 B` 约80 MiB，页文件未被接受为补救。两次均仅保留 `_worker_evidence/.../repair_audit_preflight.json`，没有输出run目录、MSH/SU2、Gmsh、SU2或ParaView调用；不得放宽4 GiB系统保留、3 GiB Job或512 MiB监控余量，资源恢复后只能用下一新目录重试。
- 2026-08-02：资源满足硬门后在新目录执行首个真实只读审计 `_003`。Gmsh实际运行约49秒，Windows Job、运行期物理内存保护、logger、源BREP只读不变、initialize/clear/finalize和零 `gmsh.write` 均正常，worker峰值private commit约291.2 MiB，未产生MSH/SU2且未调用SU2/ParaView；但在完整60层细分入口因 `_validated_local_surface_schedules` 只接受 `coarse_calibration` 而错误拒绝安全的 `coarse_repair_audit_only`，严格返回FAIL。失败manifest SHA-256为 `5c6ec4c0700a9a37c9847c6b3d411d5cfb488a659eb6f58106f4204f99ea7ea5`；它是接线缺陷证据，不是修复owner歧义或物理质量PASS。
- 2026-08-02：最小修复仅允许在合同模式精确为 `coarse_repair_audit_only` 且 `repair_audit_only=true`、`calibration_PASS_authorized=false`、`production_mesh_eligible=false`、`mesh_written=false`、`su2_called=false`、`paraview_called=false` 六项安全scope全部逐值匹配时消费哈希绑定的局部60层日程；普通smoke、缺项或任一scope翻转仍fail-closed。新增逐字段对抗测试后，完整标准库回归638/638 PASS、0 failure（17.250秒），`compileall -q src tests` PASS；下一步必须用两个全新目录获得两次独立0.40 m审计PASS并比较稳定签名，不能把已失败的 `_003` 计入重复性证据。
- 2026-08-02：首个完成全部60层发现的真实 `_004` 严格返回 `INCOMPLETE`，manifest SHA-256为 `17217b76a4d60ccea425e4750ec37373b54b654ed3ad015d6dbdb02597ccc5a7`。它确认单层14个坏Prism6/8个负体积可经既有orientation/Chebyshev路径恢复为全正，但按当前局部日程直线外推后出现10,801个低于 `minSJ=0.01` 的棱柱，其中10,546个为非正；1,477个严格失败源三角形波及31/48个wall面，失败从第20层出现并增至第60层1,475个，最小minSJ约 `-9587.29`。1,454个owner歧义中1,451个没有任何候选sharp root，故主因是层链几何/日程失效，而不是可由外部批准表补齐的owner歧义；禁止把这些零候选记录硬映射或把旧small-smoke期望计数复制进粗网格profile。
- 2026-08-02：`_004` 的隔离和禁止下游门本身正常：运行约59秒，Gmsh 4.15.2完成initialize/clear/finalize，logger未见致命诊断，源BREP不变，`gmsh.write`计数为0，输出树无MSH/SU2/VTK类文件，SU2/ParaView均未调用。下一最小动作改为独立schedule-feasibility只读扫描；在该扫描证明至少一个保持首层高度、60层和严格质量阈值的可行日程之前，暂停双跑repair profile及0.32扩展，更不得进入calibration、粗网格写盘或求解。
- 2026-08-02：完成 `AUDIT_ONLY` 局部日程消费门的对抗加固。编排层现在在Gmsh初始化前以当前完整粗网格合同重新读取并重建source-plan binding，拒绝即使自重哈希但内容不同的binding，并复核strategy config规范哈希、合同/指纹/层参数血缘及audit/smoke/calibration/projection完整互斥矩阵；实际schedule消费点再次读取源计划、核对每面日程与物理几何级数并拒绝casefold键碰撞。Windows路径检查在保留普通绝对路径和盘符根的同时逐组件拒绝symlink/junction。该修复未改变首层高度、60层、增长率、质量/资源阈值或 `_004` 的 `INCOMPLETE` 物理结论，且未运行Gmsh、SU2或ParaView。
- 2026-08-02：从 `_004` 严格证据提取首层不变量：固定拓扑、根坐标、方向、首层高度时共有8个首层Prism6低于 `minSJ=0.01`，实测范围约 `2.5949e-4–4.0157e-3`，因此growth-only不能改变这些失败坐标。新增自哈希 `coarse_schedule_feasibility` 端点及严格应用验证：48面全部只在内存中改为growth=1，保留首层高度和60层；CLI/worker只在 `coarse_repair_audit_only` 下接受显式 `minimum-growth-existing-direction-repair`，并交叉验证端点请求、argv、配置、binding和实际strategy应用，禁止MSH/SU2及任何下游。专项64/64、完整658/658标准库测试PASS，`compileall -q src tests`返回0；尚未据此宣称方向或粗网格质量PASS。
- 2026-08-02：真实执行 `minimum-growth-existing-direction-repair`，输出 `runs/mesh/coarse/schedule_endpoint_040_min_growth_20260802_001/coarse_repair_audit_manifest.json`，SHA-256为 `9d95e0cd05773e5a35aa68e71756c6b5aa0807720b8056d66d785a42d05c827f`。Gmsh 4.15.2完成initialize/finalize、只读源未变、写入尝试为0；growth=1把完整60层非正数降为0，但仍有457个阈下棱柱，最小minSJ约 `9.0138342e-5`。8个稳定失败源三角中旧路径有3项owner歧义、3个组、8个候选根且实际改变根数为0，因此严格 `INCOMPLETE`，未生成网格、未调用SU2/ParaView。
- 2026-08-02：为下一最小质量门新增hash-bound `minimum-growth-owner-free-component-direction-audit`。它把8个失败三角按共享任意根组成4个分量（不按wall fingerprint拆分），候选根上限64、分量上限16、质量查询上限4096；有限候选包含分量/incident Chebyshev、坏三角法向、incident/邻域均值与4个严格内点权重。搜索只用绝对根坐标和60层累计高度重放，候选失败立即恢复当前最佳，最终查询全部Prism/Core并执行A-B-A。证据验证器现要求受信endpoint、48面精确血缘、组件/根/三角覆盖、全局质量计数与SHA、零sharp-owner和严格audit-only范围；首轮专项100/100标准库测试及编译检查PASS，尚未运行该真实方向端点。
- 2026-08-02：`minimum-growth-owner-free-component-pattern-audit` 已在 `direction_pattern_040_min_growth_20260802_001/_002` 两个独立Gmsh worker中严格PASS；两份manifest SHA-256分别为 `7ae92aba9f6a7ec0a325412a9d2eb1f8e978329bcf4ffa1c7be500b82c06b2bc` 和 `cc66b618b083309ac5fd447e836f9baea69858d328bee9381e04d0824eacb314`，共识approval SHA-256为 `2319a23dd6c7dd068335b320caf5432999b9fdda4860cbd755593059c4b4fb35`。16个候选根中10个方向变更，两跑的拓扑、坐标重放、质量SHA和空残差清单完全一致；该PASS只对growth=1有效。
- 2026-08-02：完成真实local schedule固定方向重放及严格validator。实现中修复了三个只影响证据接线的缺陷：只在已批准稳定子图中重放而不用新增物理坏单元扩大授权；对fresh/consensus原方向只接受最多 `2^-52` 的可重算binary64舍入证据；worker和CLI均把带元数据的schedule binding记录投影为权威数值列表再验证。相关CLI/worker联合36/36测试PASS。
- 2026-08-02：两个全新目录 `direction_physical_replay_040_20260802_007/_008` 的Gmsh worker均完成并合法返回 `INCOMPLETE`；`_008` 还通过了修正后的CLI最终权威验证。两跑discovery SHA-256同为 `83f968f5ecf1fadd19f5dca030dc654b53fa51d6bae87f2c1539edc569bf2ec0`，quality SHA-256同为 `eafe8bed84091b289211e8586dddabfe942c01ecca1de34396339db508fa518f`；294,059个三维单元中Prism阈下10,662个、Core Tet4阈下202个、非正12,195个、非有0个，Prism最小minSJ `-9587.29225887122`、Core最小gamma `1.046094003084404e-08`。两跑均initialize/logger/clear/finalize完整、源BREP不变、stderr为空、零write/SU2/ParaView，因此主阻塞是真实日程层链质量，不是基础设施或随机性。

### 验证结果

- continuation v2 跨模块专项：`tests.test_coarse_repair_audit_worker` 18/18 PASS，`tests.test_coarse_repair_audit` + `tests.test_coarse_repair_audit_cli` 60/60 PASS，`tests.test_boundary_layer_smoke` 50/50 PASS；最后 schema 分流断言单项1/1 PASS。`python -B -m compileall -q src/cfdpipe/coarse_repair_audit_worker.py tests/test_coarse_repair_audit_worker.py` 返回0。仅排除权威 continuation 实现及其专属测试后，全库对旧 endpoint/discovery/mode v1 精确扫描为0命中；全过程未启动 Gmsh、SU2 或 ParaView。
- 真实首点命令 `$env:PYTHONPATH=(Resolve-Path 'src').Path; .\.venv\Scripts\python.exe -m cfdpipe pipeline coarse-mesh-calibrate --config config/coarse_mesh.toml --characteristic-length 0.30 --output runs/mesh/coarse/calibration_030_20260802_001 --timeout 840 --quiet` 在Gmsh启动前按设计FAIL；报告记录物理内存总量 `16,790,917,120 B`、即时可用 `4,308,594,688 B`、虚拟可用 `18,033,156,096 B`、磁盘可用 `165,765,324,800 B`。总内存/磁盘/timeout均PASS，唯一硬失败为即时可用物理内存低于 `8,589,934,592 B`；页文件救援被明确拒绝。输出目录未创建、无Python worker/Gmsh/SU2/pvbatch进程启动，证据位于 `runs/mesh/coarse/_worker_evidence/calibration_030_20260802_001/calibration_preflight.json`，SHA-256 `e20764e2474bbd6abfa8516b643da6867c5bf4fc89d4c25f2bcb694b47f69043`。
- 真实只读净空审计 `runs/mesh/coarse/clearance_20260802_001/boundary_layer_clearance.json` 完成48面×5样本=240个法向探针，`execution_status=PASS`、Gmsh finalize成功、源BREP未变；但统一60层总厚 `0.0778871423928269 m` 的工程要求为FAIL。最小保守净空仅 `0.0129781875 m`，共6个稳定wall面低于统一厚度；报告严格JSON、3,494,382 B，SHA-256 `0a454ce35b52de0952d969e7ed949bb67b57c25e1ac58802606823bfb48f8164`。该FAIL在Gmsh网格、SU2和pvbatch之前停止，未改变首层高度、marker或边界语义。
- 新增/修改的粗网格、资源、净空与收敛相关专项测试全部通过；完整 `$env:PYTHONPATH=(Resolve-Path 'src').Path; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为498/498 PASS，0 failures，耗时12.219秒；`compileall -q src tests scripts` 返回0。当前目录仍无 `.git` 元数据，不能执行Git diff，继续以完整回归、源码人工复核、命令日志和哈希作为替代审计。
- 修正合同及新增projection资源门后的纯mock专项组合为63/63 PASS（1.723秒），覆盖计数公式、严格30万上限、Gmsh异常finalize、Job硬限额先于重模块导入、物理内存/页文件门、绝对Python参数列表、禁止隐式shell以及局部计划。完整回归须在并行实现合入后重新执行，不能沿用旧498项结果冒充当前工作树验证。
- 真实命令 `pipeline coarse-mesh-project --characteristic-length 0.30 --output runs/mesh/coarse/projection_030_20260802_001` 在Gmsh启动前按设计返回2；报告要求6.5 GiB即时物理内存，实测仅4.917 GiB，reason为 `PHYSICAL_AVAILABLE_INSUFFICIENT/PAGEFILE_NOT_ACCEPTED`，`command_runner_constructed=false`、无输出目录/worker日志，报告SHA-256为 `c379e5c0eead962f45064db144e860cd7e9ab666b47cde11beefb65b2aad6edf`。
- 合同修正后尝试重跑只读净空审计 `_003` 时，SolidWorks在运行中重新扩张，系统可用物理内存由约4.2 GiB降至0.523 GiB；仅经命令行核实并终止本次两个cfdpipe审计Python进程，未终止SolidWorks。该尝试没有网格或输出报告，明确保存 `boundary_layer_clearance_aborted.json` 为FAIL；源BREP终止后SHA-256仍为 `13698e9a…` 且只读，不把未确认finalize伪造为PASS。
- 稳定净空重验证报告SHA-256为 `2392e1b495501ed5209fb9f5ad78eebcc069ec985bf01a17b4889df59346216d`，其上游原 `_002` 报告SHA-256为 `c4877bb6aac05337d75c763594115ddbdabc58d62eda2d4a263fcbb0dc0eda74`，稳定净空子合同SHA-256为 `58bd4f22ba2148d5079120d83f226666f6ffa36168a1e62f13b32a8a337f76ff`；48面/240样本、生命周期、只读源、marker和所有数值一致性均PASS。
- 更新后首点实际控制器预检 `calibration_030_20260802_004` 已先成功绑定上述local schedule，再在任何worker/Gmsh导入前返回FAIL：实测可用物理内存 `2,258,386,944 B`，要求 `8,053,063,680 B`（4 GiB OS保留+3 GiB calibration Job硬限额+512 MiB监控余量），`command_runner_constructed=false`、输出目录不存在；报告SHA-256为 `bb2b5f05737895c6d6eed880263df5cbda618369daf07eb2c5502db1d33af383`。这证明当前唯一失败已位于资源启动门，而非日程lineage或Gmsh构造逻辑。
- 所有并行实现合入后的完整命令 `$env:PYTHONPATH=(Resolve-Path 'src').Path; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为593/593 PASS、0 failures，耗时16.934秒；`.\.venv\Scripts\python.exe -m compileall -q src tests scripts` 返回0。旧498项结果不再作为当前工作树结论。
- 第二校准点安全修正、projection证据链和严格manifest门合入后的完整回归为601/601 PASS、0 failures，耗时16.393秒；相关组合回归96/96 PASS，CLI专项12/12 PASS，`compileall -q src tests scripts` 返回0。新合同下projection命令返回0、未写网格、未运行SU2/ParaView，独立严格加载器复验计数、配置/合同SHA和worker证据全部PASS。
- 第二点改为 `0.32 m` 并完成严格投影lineage接线后的纯标准库组合回归为96/96 PASS、0 failures，耗时3.832秒；`.\.venv\Scripts\python.exe -B -m compileall -q src tests scripts` 返回0。该结果不包含外部工具运行，也不把旧合同下的projection或local schedule视为可消费证据。
- 本轮消费门加固专项为33/33 PASS；完整 `$env:PYTHONPATH=(Resolve-Path 'src').Path; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为651/651 PASS、0 failures，耗时16.690秒；`.\.venv\Scripts\python.exe -B -m compileall -q src tests` 返回0。测试覆盖权威binding重建、自重哈希伪造、strategy hash/lineage、模式冲突、源计划运行前篡改、runtime/downstream scope、非物理累计日程、大小写指纹碰撞以及正常Windows绝对路径/symlink/junction；未启动任何外部CFD程序。

### 剩余风险与下一质量门

- continuation v2 跨模块修正仅通过纯标准库/mock 门，未重放真实 Gmsh 工件；它只消除 worker wrapper 的旧 schema 拒绝，不改变现有 `INCOMPLETE` 物理结论、端点哈希、搜索语义或生产授权。当前目录仍无 Git 元数据，无法执行 Git diff，已以精确字面量扫描、修改片段复核、专项测试与编译检查替代。
- 当前主要风险是既有边界层构造/修复合同与117,551单元小网格拓扑强绑定，以及16 GiB主机对5M网格的峰值内存余量未知；必须通过新校准与资源证据解决，不能直接把小网格参数线性放大。
- 局部60层日程、builder接线、CLI/worker三重证据绑定、新合同projection和完整回归均已完成；当前唯一首点完整校准启动阻断是瞬时物理内存门。projection已在满足6.5 GiB门时成功；calibration必须在启动瞬间至少有 `8,053,063,680 B` 可用物理内存，并由运行中4.5 GiB保护门及3 GiB Job private-commit硬限额继续监控。最近实测可用内存在约7.1–8.1 GB波动，不能用页文件补救，也不得降低系统保留、内存、y+、非正体积/Jacobian、marker或共享接口要求。首点实跑仍需验证完整60层细分和离散相关repair计数；失败必须保留结构化证据并在同合同下处置，禁止把失败工件送入SU2。
- 本质量门PASS后，下一质量门才是同一设计点粗网格上的两阶段正式 RANS 收敛验证；中网格、细网格和网格无关性必须随后逐级完成，不能因粗网格可运行而声明最终生产结论。

## 当前阶段：后部出口设计工况冻结证据门

状态：已完成；设计工况 `M50_H21_A8_B0` 的两个后部出口边界语义已由哈希绑定的 Euler、一阶 SST RANS 与二阶 restart SST RANS 证据冻结为 `MARKER_SUPERSONIC_OUTLET`，但小网格与短诊断仍不具生产资格，其余四个工况继续为 TBD

### 本轮目标

把 `rear_outlet_1` 与 `rear_outlet_2` 从“临时超声速诊断边界”提升为可审计、按工况限定的正式边界语义。先以当前设计点 `M50_H21_A8_B0` 的 Euler 与 SST RANS 证据为基线，补充多检查点稳定性和从受验证 restart 启动的二阶小规模串行诊断；只有所有出口特征方向、回流、质量守恒、拓扑、哈希与日志门同时通过，才生成独立冻结合同。冻结只适用于已验证设计工况，不外推到其余四个工况，也不把小网格解声明为生产解。

### 验收标准

- 运行前重新校验 `project.toml`、`cases.csv`、`markers.toml`、STEP、派生 BREP、权威边界层网格 manifest/mesh 和既有 Euler/RANS 证据的路径、状态与 SHA-256；任何源文件或已通过证据不一致立即停止；
- 两个出口必须继续由稳定 group/member fingerprint 唯一选择：`rear_outlet_1` 恰含 3 个完整外边界成员，`rear_outlet_2` 恰含 2 个完整外边界成员；成员互斥、逐面恰有一个体 owner，内部 `turning_section_outlet` 不得进入 SU2 marker；
- 外向法向每次从边界面唯一体 owner 重构，不接受 CAD 参数法向或 runtime tag 作为冻结依据；所有 solver marker 必须精确、无遗漏、无重复地覆盖外边界，owner error、退化面、非流形面均为 0；
- 只允许设计点、小于 30 万三维单元、`nproc=1`、GPU 关闭、无 GUI。所有 SU2_CFD/pvbatch 命令继续经统一 `CommandRunner` 以参数列表和 `shell=False` 启动，单次 timeout 不超过 840 秒并保留完整 stdout/stderr/command JSON；
- 至少保留两个一阶 SST RANS 检查点，并从已验证的一阶 restart 启动二阶 SST RANS 检查点；restart 输入路径、SHA-256、来源 run manifest 和配置语法证据必须显式记录，禁止通过目录搜索选取旧文件；
- 每个合格检查点的两个出口均须满足：所有采样法向 Mach 严格大于外部配置门限且至少保留 0.25 的超声速裕量，亚声速/非正法向 Mach 面积为 0，回流面积、反向质量流量为 0，所有数值有限；全局相对质量不平衡不超过外部配置门限；
- 多检查点必须验证出口最小/面积加权法向 Mach 和净质量流量稳定性；阈值只能来自新的 `config/rear_outlet_freeze.toml`，不得在 Python 中重复硬编码。任一检查点或稳定性门失败必须保存 FAIL manifest，并允许在新的独立小型目录中调整有证据的数值策略后重试，不得放宽物理门限；
- 二阶配置必须由当前 SU2 8.5.0 安装证据支持，只允许 `RESTART_SOL=YES`、显式本地 `SOLUTION_FILENAME` 和 `MUSCL_FLOW/MUSCL_TURB=YES` 的受控差异；继续禁止 `MARKER_OUTLET`、出口静压、背压、CUDA 和 MPI；
- 只有完整证据门 PASS 后才生成哈希绑定的 `config/rear_outlet_freeze.toml` 最终合同与 `runs/rear_outlet_freeze/<run>/rear_outlet_freeze_report.json`，合同必须明确 `FROZEN_FOR_DESIGN_CASE_ONLY`、`MARKER_SUPERSONIC_OUTLET`、`boundary_mode_frozen=true`、`allow_static_pressure=false`、`allow_back_pressure=false`；其余四个工况仍为 TBD；
- 扩展真实流水线边界门时必须保留原 connection-only pilot 分支，并确保只有合同列出的 case_id 与当前所有输入/证据哈希一致时才可使用冻结模式；冻结边界语义不等于当前小网格或解具有生产资格；
- 标准库测试覆盖配置严格键、哈希/路径、单工况作用域、restart/二阶配置、串行命令、失败即停、单快照拒绝、拓扑/marker隔离、Mach/回流/质量守恒/稳定性阈值、非有限值、禁止压力/背压/GUI/MPI/GPU/`shell=True`；专项与完整测试通过后检查实际日志、产物、源哈希及 Git 状态，并更新本节。

### 实施记录

- 2026-08-02：按顺序重读 `AGENTS.md`、完整 `PLAN.md`、`config/project.toml`、`config/cases.csv` 与 `config/markers.toml`；三路只读审计分别复核现有 Euler/RANS 出口证据、几何/marker/法向合同和仓库实现缺口。
- 2026-08-02：审计确认两个后部出口均为完整、互斥的真实外边界组，内部测量面只由 pvbatch 体解切片生成且不在四个 SU2 marker 中。Euler 200 步和合格 15 层网格的 RANS 20/200 步均显示两个出口所有采样超声速、零回流；但现有文件明确 `NOT_FROZEN`、一阶、单终态且 `convergence_claimed=false`，因此没有伪造冻结结论，转入本独立证据门。
- 2026-08-02：新增受当前 SU2 8.5.0 安装文件逐项证明的二阶 restart 渲染入口。它只允许在经验证的一阶 RANS/SST 配置上增加 `RESTART_SOL=YES`、`READ_BINARY_RESTART=YES`、本地 `SOLUTION_FILENAME` 以及 `MUSCL_FLOW/MUSCL_TURB=YES`；restart 必须由显式 manifest 选择并验证二进制 magic、字段表、点数、精确文件长度、全部有限值及 SHA-256，禁止目录搜索、压力/背压、MPI、CUDA 和 `shell=True`。CLI `pipeline rans-smoke` 新增成对的 `--spatial-order 2 --restart-manifest ...`，缺任一参数均在外部程序前失败。
- 2026-08-02：在同一权威 49,345 点、117,551 单元的 15 层小网格上完成新的 `outlet_freeze_first_order500_001`。SU2_CFD 串行返回0并完成500步，pvbatch返回0；最大 y+ 为 `0.7565769`，两个出口最小顶点法向 Mach 分别为 `2.4565289` / `4.5159036`，反向质量流量和回流面积均为0，全局相对质量不平衡为 `1.49337e-5`。报告 SHA-256 为 `40262f71efc93fcf40ab381a869763cefc173ad4500a9951d22f42fef645d847`，输出 restart SHA-256 为 `e3ac833a2e4d41573beafb18339d2a7106b0d643ccf66be5feb35a7efe8b7d59`。
- 2026-08-02：`outlet_freeze_second_order200_002` 从上述显式 restart 启动二阶小规模诊断。SU2 日志原文确认 `Read flow solution from: rans_input_restart.`，输入 restart 求解前后哈希一致，SU2_CFD 返回0并完成200步；pvbatch返回0，最大 y+ 为 `0.6895896`。两个出口最小顶点法向 Mach 为 `2.4564043` / `4.5156974`，零回流、零反向质量流量，全局相对质量不平衡 `6.89459e-5`；报告 SHA-256 为 `d609add8ac32716f6f2f18617e7555e46d55ff352c9a41bda5a57e3db11add42`。该运行仍明确 `diagnostic_only=true`、`production_eligible=false`、`convergence_claimed=false`。
- 2026-08-02：新增纯标准库证据评估器 `rear_outlet_freeze.py` 与哈希绑定合同 `config/rear_outlet_freeze.toml`。首次真实评估目录 `_001` 暴露旧 Euler 诊断字段与新评估 schema 的兼容缺陷并按 FAIL 保留为空目录；未修改任何原证据。评估器随后只对实际、唯一且可审计的旧字段语义作 fail-closed 归一化，新增缺字段/非有限/错误计数回归测试，在全新 `_002` 以及正式 CLI `_003` 均得到相同 PASS 报告。
- 2026-08-02：新增 `pipeline rear-outlet-freeze-check`，固定把新报告写在 `runs/rear_outlet_freeze/<run>`，拒绝路径逃逸、符号链接及覆盖已有报告；该命令不解析工具链也不启动外部软件。真实流水线 `_rear_outlet_gate` 现优先评估固定的 case-specific 合同：只有 PASS、当前输入哈希一致、请求 case 精确匹配、明确冻结且仍非生产资格时才授权超声速出口；已存在但失败或工况不匹配时立即停止且不回退 pilot，无冻结文件时仍保留旧 connection-only pilot 分支。`project.toml` 的全局 TBD 未修改。

### 验证结果

- 正式复现命令 `$env:PYTHONPATH=(Resolve-Path 'src').Path; .\.venv\Scripts\python.exe -m cfdpipe pipeline rear-outlet-freeze-check --config config/rear_outlet_freeze.toml --output runs/rear_outlet_freeze/design_case_20260802_003` 返回0。合同状态为 `FROZEN_FOR_DESIGN_CASE_ONLY`，评估报告为 `status=PASS`、`boundary_mode_frozen=true`、`production_eligible=false`、`errors=[]`；Euler 1份、一阶 RANS 2份、二阶 restart RANS 1份共4/4证据记录通过，restart lineage、RANS网格/拓扑一致性、跨检查点漂移和 NaN/Inf 门全部通过。
- Euler 补充证据中控制性的 `rear_outlet_1` 顶点法向 Mach 裕量为 `0.957081>0.25`，全局相对质量不平衡为 `0.00185104<0.002`；全部检查点的两个出口亚声速/非正法向 Mach 面积、回流面积与反向质量流量均精确为0。三个 RANS 检查点的最大相对漂移为 `1.78170e-4`（`rear_outlet_2` 面积加权法向 Mach），低于外部合同 `0.005` 门限；净质量流量最大相对漂移仅 `1.88016e-5`。
- `config/rear_outlet_freeze.toml` SHA-256 为 `546db52649730f26ae6d64618d8ffcbd92c29b5f1412f91d8531f47791ce3989`；最终 CLI 报告 `runs/rear_outlet_freeze/design_case_20260802_003/rear_outlet_freeze_report.json` SHA-256 为 `ca74db17db0645f069deee019c4b7bda8bebcc1d585d27b85ec7a24735e74167`。直接以真实 STEP/project/cases/markers/topology 配置加载流水线边界门也返回 `effective_mode=supersonic_outlet`、`su2_option=MARKER_SUPERSONIC_OUTLET`、`case_specific=true`、`pressure_or_backpressure_guessed=false`。
- 最终专项组合测试110/110 PASS；完整 `$env:PYTHONPATH=(Resolve-Path 'src').Path; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为414/414 PASS，0 failures；`compileall -q src tests scripts` 返回0。新增一阶/二阶 SU2 与 pvbatch stderr 均为空，manifest 的 fatal matches 为空；源 STEP、派生 BREP、project/cases/markers/topology 配置哈希前后分别保持 `d7d1cbec…`、`13698e9a…`、`96ec212c…`、`c27cb7b8…`、`a249c79b…`、`af7729ef…`。当前目录没有 `.git` 元数据，`git status --short` 仍返回 `fatal: not a git repository`，故继续以完整回归、命令记录、日志和哈希作替代审计。

### 剩余风险与下一质量门

- 本阶段冻结的是设计点两个后部出口的边界语义，不是小网格解、载荷、内部截面结果或任何网格的生产资格；二阶200步仍是 bounded restart 诊断，未声明物理收敛。其余四个工况没有被外推，`project.toml` 继续保留全局 `TBD_AFTER_SUPERSONIC_PILOT`，这些工况在获得各自证据前会被流水线拒绝。
- 下一质量门才可准备设计点粗网格，并重新执行体/边界层质量、y+、质量守恒、回流及两阶段 RANS 收敛门；不得直接跳到800万至1200万中网格、细网格、多工况、MPI、GPU或生产载荷解读。

## 当前阶段：小规模串行 SST RANS 与 y+ 校核门

状态：已完成；哈希绑定的小型串行 SU2 8.5.0 RANS/SST 启动门与实际壁面 y+ 严格门均已 PASS，但结果仍仅为诊断，不声明物理收敛或生产资格

### 本轮目标

以 `runs/boundary_layer_quality/yplus_corrected_20260802_015` 的 PASS manifest 与 `mesh.su2` 为唯一网格输入，建立一个可重复、哈希绑定、失败即停的小规模压缩性 RANS/SST 质量门。先执行 20 步启动门；只有 SU2_CFD 返回0、完成指定步数、history/restart/ParaView 解文件齐全且日志无 fatal/NaN/Inf 时，才允许由普通 Python 通过 subprocess 启动 pvbatch 读取本轮解文件。随后提取实际壁面 y+、载荷趋势、两个后部出口和全局质量守恒证据；若 y+ 超过项目上限1.0，只允许在新的小型网格/运行目录中修正外部边界层参数并重新验证，不得放宽门限或进入生产网格。

### 验收标准

- 运行时必须重新校验 `project.toml`、`cases.csv`、`markers.toml`、`topology_smoke.toml`、`boundary_layer_trial.toml`、`boundary_layer_smoke.toml`、STEP、派生 BREP、网格 manifest 与 `mesh.su2` 的实际路径、状态和 SHA-256；源文件和既有 PASS 工件前后不变；
- 工况只能选择项目设计点 `M50_H21_A8_B0`，大气、Mach、项目攻角/侧滑、参考面积/长度/力矩原点、RANS/SST、无滑移绝热壁和 y+ 门限只能来自 config 真源；项目角度必须继续经过已验证的项目坐标到 SU2 坐标映射；
- 配置语法必须由当前安装 SU2 8.5.0 自带配置证据逐项确认，记录参考文件绝对路径与 SHA-256。不得凭记忆发出当前版本未证明支持的键；不得修改安装目录模板；
- 两个后部出口只允许沿用 `config/rear_outlet_pilot.toml` 和既有 200 步 Euler/pvbatch 证据支持的临时 `MARKER_SUPERSONIC_OUTLET` 诊断策略；禁止 `MARKER_OUTLET`、静压、背压或任何生产边界冻结声明；内部测量面仍不是 solver marker；
- SU2 只允许 `nproc=1`、GPU关闭、稳健一阶格式、低 CFL 和外部配置限定的有界迭代；第一阶段固定20步。任一前置、返回码、迭代数、非有限值、致命日志、输出新鲜度或哈希检查失败立即停止，不得调用 pvbatch；
- 所有外部命令必须经统一 `CommandRunner` 以参数列表和显式 `shell=False` 执行，保存绝对可执行路径、参数、cwd、开始/结束时间、返回码及完整 stdout/stderr；每次重试使用新的独立目录并保留失败证据；单次求解 timeout 不得超过840秒，预计或实际运行不得超过15分钟；
- 成功 SU2 阶段必须生成 `case.cfg`、本地只读副本 `mesh.su2`、`history.csv`、restart、当前运行新生成的 ParaView 可读文件和独立 run manifest；必须确认实际读取49,345点、117,551个三维体单元和四个正确 marker，且完成精确请求步数；
- pvbatch 脚本只能读取 SU2 manifest 中的 `visualization_file`，普通 Python 禁止导入 `paraview.simple`。JSON 必须报告实际数组、点/单元数、bounds、壁面 y+ 的有限样本数/min/max/mean/分位统计和超过1.0的数量/面积比例；无 y+ 数组或无法可靠映射 wall 时必须 FAIL，不得估算或伪造；
- 诊断同时记录可获得的六分量力/力矩趋势、两个后部出口的法向 Mach/回流、全局质量不平衡及内部测量面可用性；本轮不要求物理收敛，不对短步结果作物理解读，也不得用短步结果冻结出口边界；
- y+ 门只有在所有实际 wall 样本有限、最大值不超过 `physics.maximum_yplus=1.0` 且无遗漏 wall marker 时才 PASS。若20步流场尚不足以形成可信 y+，允许在同一配置合同下执行一次不超过500步且总时限小于15分钟的串行诊断；仍不声明收敛或生产资格；
- 输出只允许进入 `runs/rans_smoke/<run>`；禁止 MPI、CUDA、GUI、SU2_SOL（除非 SU2_CFD 成功但仅缺可视化且当前版本回退已有严格实现）、新真实 STEP 修改、生产规模网格、多工况和二阶生产求解；
- 标准库测试必须覆盖配置/哈希门、RANS配置键、禁止压力/背压、串行命令、失败即停、timeout、非零返回码、非有限日志、history/VTU新鲜度、y+ JSON解析与阈值、普通 Python 禁止导入 ParaView；专项测试和完整 unittest 通过后才运行真实短门，最终检查日志、manifest、输出哈希和仓库状态并更新本节。

### 实施记录

- 2026-08-02：按顺序重读 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`、`config/markers.toml`。确认前置 `quality_gate_20260802_001` 为 PASS：49,345点、117,551个三维单元（81,300 Prism6 + 36,251 Tet4）、5,420个完整15层柱列、四个 marker、核心与棱柱硬质量门和独立 SU2 文本解析均通过；该目录尚未调用 SU2/ParaView。
- 2026-08-02：确认既有 `physical_gate_retry1` 已用同一项目坐标/大气和临时超声速出口完成200步小型 Euler 诊断，两个后部出口均为超声速且无回流，全局质量不平衡约0.1851%；该证据只授权本轮小型连接诊断继续使用无压力/背压的临时出口，不构成生产边界冻结。
- 2026-08-02：首轮20步及后续200步串行 SST RANS 均成功完成且输出/后处理链完整，但实际 wall 最大 y+ 分别约2.358和2.030，严格判 FAIL；200步证据据此把最终物理首层高度从 `1.382288564e-6 m` 缩小为 `3.455721409e-7 m`。直接用该亚微米高度构造 Gmsh 临时单层壳时，5,420个源 Prism6 全部出现非有限质量，因此禁止将该失败计数写成新常量。后续修复只允许把已验证旧高度作为临时拓扑/方向构造高度，再将所有最终节点重定位到修正后的15层物理日程；最终层高、质量、元素上限和 y+<1 门均不得放宽。
- 2026-08-02：独立失败目录 `yplus_corrected_20260802_004` 证明 construction/final 高度解耦已消除临时壳的全体非有限退化，但新物理日程只剩2个末层非正 Prism6，与旧日程的34/6/4/10修复合同不一致，故未写盘并停止。增强后的只读诊断在 `yplus_corrected_20260802_005` 一次性得到2个坏棱柱、2个稳定源三角形（SHA-256 `f795bf7c…`、`8f1147fb…`）、2个动态尖根组及4个候选邻根；据此把外部修复合同重标定为2/2/2/4。该重标定不接受任何非正单元，只改变新日程下应被动态修复的精确数量。
- 2026-08-02：`yplus_corrected_20260802_006/007` 在零非正修复后均停于原 `minSJ>=0.01` 硬门；007完整报告仅60/81,300个棱柱、4/5,420个柱低于门限，最小`2.62294e-4`而p0.1为`0.01270`，证明是两个稳定wall fingerprint上的局部方向退化，不能归因于全部薄层的高展弦比并放宽门限。随后008/009按fail-closed保留了多尖根归属歧义；010以“严格非正源三角形的唯一owner优先、其余唯一尖根、同wall完整共享边一环”消除歧义，实测初始阈值候选为90个棱柱/6个稳定源三角形/3个owner组。动态尖根全部保护，只有8个非尖根允许进入可行锥平滑；最终`minSJ>=0.01`与零非正门保持不变。
- 2026-08-02：正式候选 `yplus_corrected_20260802_011` 返回0并通过 generation/fresh-readback 双硬门。最终为49,345点、117,551个三维单元（81,300 Prism6 + 36,251 Tet4）、5,420个完整15层柱列，所有48个wall稳定组覆盖率均为1.0；全局最小 `minSJ=0.011974869156002889`，非正体积、非正Jacobian和非有限计数均为0。`mesh.su2` SHA-256为`64bbb289c2deb66c7dcdac69055ef93f70c47cc804600d00764b5e45272ac3c2`，manifest SHA-256为`49017b6c32b7f7b16d3a0be9e366c1698524254b867dcb04e6dbf705bc329ae9`；源BREP前后哈希一致，且本阶段未调用SU2或ParaView。RANS配置现已显式绑定该网格和当前边界层配置哈希。
- 2026-08-02：`yplus_corrected_startup20_001` 与 `yplus_corrected_diagnostic200_001` 均由SU2 8.5.0串行返回0并准确完成20/200步，pvbatch成功打开49,345点、117,551单元的本轮解并输出合法诊断JSON；pipeline仅因严格y+门主动返回非零。最大y+从20步的`1.1842804`降至200步的`1.0307642`，200步只剩4个壁点、约`5.09e-6`壁面积比例超限，p99为`0.80969`；两个后部出口全采样超声速且无回流，内部测量面PASS，全局相对质量不平衡约`3.55e-5`。因此不放宽`y+<1`，而以该200步JSON的SHA-256证据把外部首层安全系数从0.125降至配置原有下限0.1，并在新目录重建/复验15层小网格。
- 2026-08-02：首个0.1安全系数候选 `yplus_corrected_20260802_012` 在写盘前按fail-closed停止：稳定源三角`3f0e2798…`仍含两个动态尖根，但更薄日程不再产生可用的严格非正owner，旧规则拒绝猜测。配置现仅允许引用011 PASS manifest（SHA-256 `49017b6c…`）中该稳定三角、稳定wall fingerprint与preferred owner的记录，并追溯同一owner在另一稳定源三角上的严格非正证据，再以owner根稳定坐标SHA-256 `6391ff49…`重新匹配；代码和配置均不保存本轮runtime node/entity tag，证据文件哈希、PASS状态、复合选择键、严格lineage及根坐标映射任一不一致立即FAIL。专项测试覆盖唯一映射、其他尖根保护和未使用证据拒绝。
- 2026-08-02：`yplus_corrected_20260802_013` 首次正确消费上述稳定审批后，按旧数量合同在写盘前停止；新日程实测为0个完整层非正棱柱、90个初始阈值候选、6个稳定源三角、3个owner组和8个可移动非尖根。只把“完整层非正数允许为0”和该实测数量更新为新日程合同，零非正、`minSJ>=0.01`、完整15层、marker和单元上限硬门不变。
- 2026-08-02：加固历史审批验证，要求证据 manifest 同时满足schema v2、总体PASS、诊断质量PASS、源几何未变且明确无需继续质量修复；随后权威候选 `yplus_corrected_20260802_015` 返回0。其 manifest SHA-256为`f679675a699455a9bf95939bf7d244234788c31642349105977f2773a16c9184`，`mesh.su2` SHA-256为`201b93477a3607af1944e1878e5a8d9c208fe610a43cf5213438837851c994f1`；49,345个SU2点、117,551个体单元（81,300 Prism6 + 36,251 Tet4）、5,420列×15层，48个wall组覆盖率均1.0。fresh回读棱柱最小`minSJ=0.0119748983`、核心四面体最小gamma约`0.00348267`，负体积/非正Jacobian/非有限/阈下棱柱与柱列均为0，四个marker完整，源BREP未变；本阶段未调用SU2或ParaView。
- 2026-08-02：`yplus_final_startup20_001` 的SU2 8.5.0串行命令返回0并精确完成20步，history、restart、体/面VTU均新生成，pvbatch也成功打开解；严格后处理因实际最大y+ `1.0595832`（8个壁点、约`2.09e-5`壁面积超限）主动返回非零，失败证据完整保留。未修改门限、边界定义或数值格式。
- 2026-08-02：按既定有界诊断规则执行 `yplus_final_diagnostic200_001`。SU2串行返回0并精确完成200步，pvbatch返回0；2,708个唯一wall点全部有限且为正，最大y+ `0.92289495<1.0`、p99 `0.72532912`、超过1.0的点数和面积均为0，故startup与y+门均PASS。两个后部出口所有采样点仍超声速且无回流，全局相对质量不平衡约`3.54e-5`，内部测量面诊断PASS；所有源哈希前后一致，stderr为空且日志无fatal/error/NaN/Inf。该200步结果明确`diagnostic_only=true`、`production_eligible=false`、`convergence_claimed=false`。

### 验证结果

- 专项边界层/试验测试43项通过，RANS专项测试13项通过；权威网格 `_015`、20步启动链和200步实际y+链均已从仓库根目录重复执行并检查manifest、日志、输出与SHA-256。
- 完整 `python -m unittest discover -s tests -v` 最终为386/386 PASS。首次全量运行暴露一个仅属于测试夹具的旧首层高度硬编码，并因固定输出目录留下预检失败证据；测试现从当前 smoke 配置派生日程并使用隔离临时目录，单测及全套复跑均PASS，产品CLI的fail-closed检查未被削弱。
- 工作区没有 `.git` 元数据，无法执行 Git diff；已改为逐一复核本轮修改文件、当前SHA-256、命令记录及所有冻结源文件运行前后哈希，未发现范围外修改，原始STEP和派生BREP哈希保持不变。
- 小规模串行 RANS 与 y+ 校核门：PASS。最终证据为 `runs/rans_smoke/yplus_final_diagnostic200_001/rans_smoke_report.json`（SHA-256 `96db28490f1b62801ac446ffe5007877ea1aeb9547e2bfcd94efa982c6a3b12b`）和 `paraview/rans_diagnostics.json`（SHA-256 `ec19750d76346ddd1ca289ade1d322078c6bbdfc92a865035200bef278859231`）。

### 剩余风险与下一质量门

- 最终物理首层高度为 `2.7645771273320875e-7 m`，已由同一哈希绑定修正网格的200步实际解验证最大y+小于1；但该小网格和一阶短诊断未达到物理收敛，不能把载荷、总压恢复或局部流动作为生产结论。
- 后部出口生产边界仍未冻结；本轮仅证明临时超声速出口在小型诊断中可运行且无采样回流。下一质量门应先形成独立的出口边界冻结证据，再准备粗网格；不得直接进入800万～1200万中网格、GPU、多工况或正式生产计算。

---

## 当前阶段：小规模 RANS 前核心四面体与棱柱最差列质量改善门

状态：已完成；稳定指纹双尺度场与 Gmsh HXT 核心算法已把核心 `gamma < 1e-3` 数量从 11 降为 0，并把完整 15 层 Prism6 的最小 `minSJ` 从约 `3.16e-4` 提升到 `1.1165e-2`；正式 build、写盘、fresh 回读和独立 SU2 文本检查全部 PASS，未运行 SU2_CFD、ParaView、MPI、GPU、GUI 或生产规模网格

### 本轮目标

以已通过完整拓扑、15 层棱柱连续性、marker、写盘回读和 SU2 文本检查的 `runs/boundary_layer_smoke/quality_gate_20260802_009/mesh.msh` 为诊断基线，在全新的 Gmsh Python API 会话中按单元类型和三维实体定位最差单元；冻结 wall 根面、4,964 个柱列、15 层层高、四个 solver marker、边界语义和两个核心共享接口。先验证当前 Gmsh 版本的 scoped core optimizer；若其不能在不触碰保护对象的前提下修复，则以稳定 wall fingerprint 驱动 top-shell 局部切向细化，并对动态检测到的低 `minSJ` 折转列执行可行锥方向平滑。任何候选都必须写入新的独立目录并 fresh 回读复验，不覆盖 `quality_gate_20260802_009`。本轮不启动求解器，只为后续受限一阶 RANS 建立网格质量前置证据。

### 验收标准

- 输入必须校验 `quality_gate_20260802_009` manifest、MSH、SU2、完整 smoke 配置和 pipeline BREP 的路径、状态及 SHA-256；原始 STEP、派生 BREP、009 网格和既有运行目录前后保持不变；
- 诊断必须在 fresh Gmsh 会话中按实际 element type/entity 统计 `volume`、`minDetJac`、`minSJ`、`minSICN` 或当前版本等效质量；不得把高展弦比 Prism6 直接套用 Tet4 的形状阈值，也不得把 runtime entity/element/node tag 写成长期选择条件；
- 必须给出 11 个 ill-shaped Tet4 的稳定空间位置、所属核心、质量值及局部邻域证据，并区分核心四面体问题与棱柱层各向异性；零匹配、多匹配、非有限值或基线证据不一致立即 FAIL；
- scoped optimizer 只允许作用于动态解析的核心 Tet4；wall/prism 节点坐标与连接、4,964 个柱列、每列 15 层、累计层高、marker 面元、共享接口和单元总量上限必须逐项与冻结基线一致。若当前 Gmsh API 无法保证局部作用域，则该候选不得发布，并转入从只读 BREP 全量重建的稳定指纹局部细化方案；
- 优先测试当前安装实际支持的 `gmsh.model.mesh.optimize` 局部方法；仅当独立探针证明局部优化不足时，才允许把可配置的稳定几何位置/尺度场接入从 BREP 重建的 smoke 入口。不得降低层数、增大首层高度、改变边界定义或放宽非正质量门；
- Tet4 质量目标为 Gmsh `ill-shaped tets` 从 11 降至 0，wall-adjacent core 的 `gamma < 1e-3` 数量必须为0；两个核心的 `volume/minDetJac/minSJ/minSICN/gamma` 全有限且严格为正。诊断已证明 11 个低 gamma 四面体全部由三个稳定 top-shell 区域的界面三角化驱动，局部细化选择必须使用对应 wall fingerprint，不得保存本次 top/entity tag；
- Prism6 必须按柱列独立审计 `minSJ`。当前最差折转列的 15 层 `minSJ` 约为 `3.16e-4～6.93e-3`，不能被“高展弦比导致 SICN 小”解释掩盖；通过门限及选择规则必须写入外部配置。独立锥诊断已经证明该列同时含两个达到各自最大可行 margin 的 sharp root，共同方向投影不能安全改善；因此允许按项目既有尖边策略，把动态选中的完整列替换成显式、共形且可审计的 Tet4/Pyramid5 termination collar。collar 必须保留原 Prism6 的全部外边界面、wall marker 和邻接接口，逐单元严格正质量；其 wall 面积/比例必须单列报告。禁止静默删单元、留下空洞、降低15层或把任意普通列豁免；
- 新三维单元总数仍不得超过 300,000。只有 generation 与 fresh MSH 回读的单元类型/数量、拓扑所有权、marker、15 层契约、四项质量、日志和独立 SU2 文本解析全部一致时才能 PASS；本轮不得运行 SU2_CFD、SU2_SOL 或 pvbatch；
- 所有候选和最终工件只能写入新的 `runs/boundary_layer_quality/<run>`；失败保留完整 traceback/Gmsh 日志并立即停止，不得继续下游或伪造 PASS；
- 标准库测试必须覆盖输入哈希门、按类型质量统计、tag-free 核心选择、局部优化命令、冻结棱柱/marker 比对、警告归零、fresh 回读、异常 finalize 和禁止下游调用；先运行专项测试，再运行完整 `unittest`，最后检查日志、输出哈希和 Git 状态并更新本节。

### 实施记录

- 2026-08-02：以 `quality_gate_20260802_009` 为冻结诊断基线，fresh Gmsh 审计证明 11 个 `gamma < 1e-3` 的 Tet4 全部位于 wall-adjacent core，并由三个稳定 top-shell wall fingerprint 附近的高边长比界面三角驱动；另按 4,964 个完整柱列逐列审计，唯一最差列的 15 层 `minSJ` 为约 `3.16e-4～6.93e-3`。所有长期选择继续使用稳定 fingerprint；entity/node/element tag 只保留为当次审计字段。
- 2026-08-02：有界验证当前 Gmsh 4.15.2 的 scoped optimizer。日志明确给出 `Optimization of specified model entities is not interfaced yet`；默认与 Netgen 优化会触碰保护的 Prism，Relocate3D 产生大量非正体积/Jacobian，独立核心提取也不能保持精确边界不变量。因此正式配置将 scoped optimizer 冻结为禁用，并保留 `runs/boundary_layer_quality/probes/core_opt_20260802_0001/manifest.json` 作为拒绝证据。
- 2026-08-02：把原 topology-smoke 14 面 Threshold 场保持不变，另以三个稳定 wall fingerprint 建立独立 `minimum_size=0.05 m` Threshold 场，并由 Gmsh `Min` 组合。仅使用默认三维 Delaunay 时，低 gamma Tet 已由11降至2，完整15层 Prism 最小 `minSJ` 已升至约0.011165；合法 2→3/3→2/4→4 cavity flip、受保护节点移动和表面边翻转均因体积/共形/保护边界不变量而被 fail-closed 拒绝，没有把非法候选接入正式代码。
- 2026-08-02：最终只把当前安装已实际证明可用的 HXT 核心算法（`Mesh.Algorithm3D=10`）加入外部质量合同；一层和完整15层独立探针均把两个核心的 `gamma < 1e-3` 清零且无 Gmsh warning。`boundary_layer_smoke.v2` 现在对 core Tet4 分区域统计五项质量，对 Prism6 按完整柱列统计 `minSJ`，硬编码零容忍计数门而非运行时 tag；build 与 fresh readback 的类型/计数/硬质量签名不一致立即 FAIL。此前星形 Pyramid5/Tet4 termination collar 已独立证明拓扑可行，但 HXT 主方案质量更高且无需 collar，故正式网格没有采用该备用方案。

### 验证结果

- 正式命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe pipeline boundary-layer-smoke --step geometry/raw/model1.step --project config/project.toml --cases config/cases.csv --markers config/markers.toml --topology-smoke-config config/topology_smoke.toml --schedule-config config/boundary_layer_trial.toml --smoke-config config/boundary_layer_smoke.toml --case M50_H21_A8_B0 --output runs/boundary_layer_quality/quality_gate_20260802_001` 返回0。最终 manifest 为 `status=PASS`、`diagnostic_quality_status=PASS`、`smoke_only=true`、`production_mesh_eligible=false`、`requires_quality_improvement_before_production=false`、`su2_called=false`、`paraview_called=false`、`external_commands=[]`。
- generation 与 fresh readback 均为117,551个三维单元：81,300个 `Prism 6` 与36,251个 `Tetrahedron 4`；5,420个柱列各恰好15层，48/48 wall 面的 prism/total coverage 最小值均为1.0。四个 solver marker 面元数为3,120 / 310 / 1,498 / 5,420；共享接口、外边界所有权和非流形门全部 PASS。
- build 的 wall-adjacent/other core 最小 gamma 分别为 `0.00326205209210105` / `0.274478104493929`，两核心 `gamma < 1e-3` 数量均为0。Prism 列 build/fresh 最小 `minSJ` 分别为 `0.0111653846277215` / `0.0111653845860078`，低门限单元和低门限列均为0。fresh 全局最小 `volume=3.990874523372141e-13`、`minDetJac=8.02795733072924e-15`、`minSJ=0.011165384586007776`、`minSICN=2.6748365472936283e-8`，非正/非有限计数均为0；累计层高最大误差约 `3.98e-16 m`。
- 独立 SU2 文本解析为 `NDIME=3`、`NELEM=117551`、`NPOIN=49345`、`NMARK=4`，体单元和 marker 计数与 generation 一致；这只是文本验证，没有调用 SU2_CFD。`mesh.msh` / `mesh.su2` / manifest / Gmsh log 的 SHA-256 分别为 `fd29c5f42753a17dfc95f130eff9ddcc727bd59052022e5cce9b99dc3b218d4a`、`a42f6d4c4f9f34c8e3c2938f1a02eefd04c7cbbcb270f2897a6b78f0854e8491`、`d1a595c4a3b35ccdc51f3136ceaf11c90dfcd38cef23b4c856bfb0c13e644643`、`4960195c7eecf4852d887d6adcf303edd2dadeff656fdf42231c6e5833fb577a`。
- build/readback 两个 Gmsh 会话均 finalize、cleanup error为0，日志没有 Warning/Error/Fatal、ill-shaped、NaN/Infinity 或 negative Jacobian/volume；输出目录只有 MSH、SU2文本、JSON manifest和Gmsh日志，没有 cfg、restart、VTK/VTU/PVTU/VTM或CSV。只读派生 BREP前后SHA-256均为 `13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121`，原始STEP仍为 `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b`。
- 专项测试最终为30/30 PASS；完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为366/366 PASS、0 failures，耗时9.723秒。当前目录仍不是Git仓库，无法给出Git diff；以完整回归、正式CLI返回码、manifest/log、文件哈希、fresh readback和只读源快照作为替代审计证据。

### 剩余风险与下一质量门

- 本阶段只解决小型真实几何网格在启动 RANS 前的核心 Tet4 与最差 Prism6 数值质量门，不代表生产网格、目标 y+ 已由流场验证、后出口生产边界已冻结或解已收敛。HXT 探针与正式运行的核心 Tet 总数存在小幅差异，但两次都独立满足相同的 tag-free 拓扑、硬质量和30万单元门；正式合同故意不冻结运行时 Tet tag/精确数量，只冻结必须可重复满足的物理边界与质量条件。
- 下一质量门是基于本次 PASS 小网格执行受限、串行、低 CFL 的一阶小规模 RANS，并复核 y+、六分量载荷、两个内部/出口诊断面、质量守恒、回流和非有限值；只有该门通过后才讨论边界层参数修正或粗网格。仍不得直接跳到800万～1200万中网格、生产计算、GPU或多工况。

---

## 当前阶段：完整小规模边界层混合网格质量门

状态：已完成；真实派生几何上的完整 15 层棱柱壳、两个共形四面体核心、写盘回读和独立 SU2 文本验证均 PASS；Gmsh 的 11 个低形状质量四面体被保留为显式 WARN，因此仍不是生产网格，且未运行 SU2 或 ParaView

### 2026-08-02 续行目标与验收

- 以现有全壁面探针定位到的 13 个非正基面柱列为唯一修复范围，先验证可追溯的局部过渡方案；不得通过降低 15 层、放宽非正质量门、整面豁免或改变真实边界语义取得通过。
- 正式 smoke 入口必须保留完整 Gmsh 日志、输入/配置哈希、48 面方向审计、精确 wall/prism/collar 覆盖面积、Physical Groups 和 fresh MSH 回读证据；任一必需所有权、marker、质量或 300,000 单元上限失败即停止。
- 先运行专项与完整标准库测试，再只允许一次新的小型真实 Gmsh smoke 运行；本轮仍禁止 SU2、ParaView、MPI、GPU、GUI 和生产规模网格。

### 本轮目标

在已通过的单面局部棱柱试验基础上，解决全部 48 个 wall 面的边界层连接、尖锐边缘终止和核心体重建问题。实现一个独立、哈希绑定、fail-closed 的 Gmsh Python API 小型混合网格入口：以稳定 fingerprint 重新匹配所有真实边界；优先利用当前真实几何已经证明的闭合 wall shell 整体挤出，仅对明确定位的负棱柱锐边区使用局部厚度缩放或内部 termination collar；移除原核心体后由棱柱顶面、必要的过渡面和原非 wall 表面重建两个共享同一接口的核心体。本轮只证明完整混合网格拓扑与质量，不启动任何求解器或后处理。

### 验收标准

- 新配置必须 SHA-256 绑定当前 `project.toml`、`cases.csv`、`markers.toml`、`topology_smoke.toml` 和 pipeline BREP；运行时 tag 只能作为当次审计证据，所有 wall/非 wall/volume 身份必须由稳定 fingerprint 和邻接关系重新解析；
- 48/48 wall 面必须重新执行多点双侧 `gmsh.model.isInside` 定向；零个或多个有效方向、非 wall 选择、共享接口角色歧义或源哈希变化立即 FAIL；
- 先生成全边界共形二维网格并构造 surface/edge adjacency catalog。wall-wall 平滑/尖锐连接以及 wall 与非 wall 的共边关系必须实测分类；当前探针已证明 48 个 wall 面组成闭合壳且没有 wall-rear-outlet/shared-interface 共边，但正式运行仍须重新验证，禁止在代码中硬编码该结论或真实 surface tag；
- 优先对闭合 wall shell 整体生成不少于15层的一阶 `Prism 6`。只有明确映射到稳定 fingerprint 的负单元锐边区才允许局部缩小厚度或建立 wall 内部 termination collar；collar 仍属于 wall marker并由核心混合单元覆盖。所有未覆盖棱柱的 wall 面积必须能追溯到配置批准的 collar，禁止隐藏任意遗漏；
- 首层高度、层数和增长率继续只来自项目/工况及外部配置；设计点首层规划值约 `1.382288564e-6 m`、15层、增长率1.2。允许通过 Gmsh view 对局部总厚度向下缩放，但不得自动减少层数、增大首层高度或放宽质量条件；
- 原两个 OCC 流体核心不得与向内挤出的棱柱体同时保留。新 volume 1 core 必须由闭合棱柱 top shell 作为内壳，并以 `rear_outlet_1` 和两个 shared surfaces 等原 volume 1 非 wall 面组成外壳；volume 2 core 必须由 `front_conical_surface`、`rear_outlet_2` 和完全相同的 shared surface 实体闭合。若启用局部 collar，其 lateral/collar 面必须加入对应核心壳。共享接口不得成为 solver marker；
- 最终三维网格允许且只允许一阶 `Prism 6`、`Tetrahedron 4` 和在 collar 四边形接口处确有必要的 `Pyramid 5`。每个外边界三角形恰有一个体单元所有者并属于唯一正确 marker；每个棱柱 top/lateral、pyramid 过渡面和两个核心共享接口面必须具有正确且共形的两侧所有权；孤立面、重复面、开放壳、非流形面、重叠体、负/零 Jacobian、负/零体积和非有限值数量必须为0；
- 每个实际棱柱柱列必须恰好15层；报告全部48面、逐面基面三角形数、棱柱覆盖面积、collar面积及覆盖率。smoke 目标覆盖率由外部配置冻结且不得运行时降低；任何整面无棱柱且不属于明确尖锐退化例外时 FAIL；
- 总三维单元数不得超过300,000；超过上限时只能调整外部配置中的切向/core 尺寸，禁止降低层数或改变项目物理常数；
- 只在完整审计通过后写出 `mesh.msh` 和 `mesh.su2`，随后 fresh Gmsh/独立文本解析回读实际文件并重复单元、marker、接口所有权和质量检查。manifest 必须明确 `smoke_only=true`、`production_mesh_eligible=false`、`su2_called=false`、`paraview_called=false`；
- 输出只允许进入新的 `runs/boundary_layer_smoke/<run>`，不得覆盖已有工件。失败后立即停止并保留完整 traceback、Gmsh 日志和原始 stderr（若不存在外部命令则明确记录）；原始 STEP/BREP 前后必须保持只读且哈希不变；
- 标准库测试必须覆盖配置/哈希门、48面身份和方向、edge 分类、collar归属、层计数、混合单元所有权、共享接口共形、marker完整性、单元上限、非正质量、写盘回读、异常 finalize及禁止任何下游程序；专项和完整测试通过后才允许执行一次真实小型混合网格；
- 本质量门通过也只允许进入下一阶段“小规模一阶 RANS 与 y+ 校核”。本轮不得运行 SU2_CFD、SU2_SOL、pvbatch、MPI、GPU、GUI、粗中细网格或生产计算。

### 实施记录

- 2026-08-02：独立一层证明先动态识别 13 个任一质量指标非正的 Prism6，其中 6 个为负体积；仅依据实际 signed volume 反转这 6 个单元，再对 15 个违反节点局部可行锥的根节点求 Chebyshev 方向，得到 `4964 Prism6 + 39491 Tet4` 的全正一层网格。随后独立 15 层证明从一层 PASS 状态出发，按配置的精确累计层高细分 Prism6、lateral Quad4 和 vertical Line2；动态把 34 个上层坏柱映射为 6 个源三角形、4 个锐根组和 10 个相邻根，并把相邻方向投影到完整 incident-face 可行锥，最终得到全部质量严格为正的 15 层离散网格。选择过程不使用固定 entity、node、element tag。
- 2026-08-02：把上述证明集成到 `boundary_layer_smoke.py` 的正式 real-project strategy。修复模式只让 Gmsh 先挤出一层；正式代码从官方 primary faces 与实际 Quad4 边推导 base/top 双射，动态解析当前 Gmsh element type，先执行 300,000 单元上限门，再建立 14 个中间层节点并同步替换 Prism/Quad/Line。fresh readback 使用持久化链契约重新构造全部 15 层，重复检查层高、连接哈希、元素数量和四项质量；`config/boundary_layer_smoke.toml` 只保存算法阈值/期望计数，不保存真实运行时 tag。
- 2026-08-02：首次正式集成运行 `quality_gate_20260802_006` 已证明修复后 113,951 个单元全部为正，但最终 role-face 审计暴露 `_face_key` 的节点数前缀被误当节点号，故按 fail-closed 保留为 FAIL。新增纯 `_derive_internal_role_faces` 后，只用 key 的首字段判断三/四边形并把 `key[1:]` 交给所有权审计；Pyramid transition 同时限定为双 owner。回归测试直接覆盖两个 Prism 共享层间三角形与 lateral quad，避免旧逻辑再次把三角面误认侧面。
- 2026-08-02：`quality_gate_20260802_007` 越过全部网格审计后，只因旧日志正则把 `Warning: 11 ill-shaped tets are still in the mesh` 与真正倒置/非有限错误同等处理而 FAIL；该失败证据未改判或覆盖。日志门现只允许这一条精确 build-phase 模式进入 PENDING，任何其他 Warning、Error、Fatal、segfault、inverted、negative Jacobian/volume、NaN/Inf 仍立即 FAIL。PENDING 只能在 generation 与 fresh readback 双审计、四项严格正质量、SU2 文本解析、源哈希、输出哈希、两个 finalize/cleanup 和禁止下游调用全部通过后，转为 `WARN_AFTER_COMPLETE_SMOKE_GATE`。`quality_gate_20260802_008` 的初步 PASS 促成对判定时序的只读复核；最终权威运行另存为 `quality_gate_20260802_009`，没有覆盖任何先前证据。

### 验证结果

- 最终命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe pipeline boundary-layer-smoke --step geometry/raw/model1.step --project config/project.toml --cases config/cases.csv --markers config/markers.toml --topology-smoke-config config/topology_smoke.toml --schedule-config config/boundary_layer_trial.toml --smoke-config config/boundary_layer_smoke.toml --case M50_H21_A8_B0 --output runs/boundary_layer_smoke/quality_gate_20260802_009` 返回 0。manifest 为 `status=PASS`、`smoke_only=true`、`diagnostic_quality_status=WARN`、`production_mesh_eligible=false`、`requires_quality_improvement_before_production=true`、`su2_called=false`、`paraview_called=false`、`external_commands=[]`。
- generation/fresh readback 均为 113,951 个三维单元：74,460 个 `Prism 6` 与 39,491 个 `Tetrahedron 4`；4,964 个柱列各恰好 15 层，48/48 wall 面的 prism/total coverage 最小值均为 1.0。所有权审计得到 wall base 4,964、prism/core top 4,964、prism/prism lateral 111,690、两个核心 shared interface 1,170 个面，transition 为 0；四个 solver marker 分别为 3,120 / 310 / 1,498 / 4,964 个面元。
- generation 的最小 `volume=3.9908745262740195e-13`、`minDetJac=8.027957227115327e-15`、`minSJ=3.156888970586223e-4`、`minSICN=2.6748366278677254e-8`；fresh readback 的对应值为 `3.990874523372141e-13`、`8.02795733072919e-15`、`3.1568890113171407e-4`、`2.6748365472936283e-8`。两次非正 volume、非正 Jacobian 和非有限计数全部为 0；最大累计层高误差 `3.984442983884229e-16 m`，fresh connectivity SHA-256 为 `28be9253fef34fcd466e13257d6963cc6606ba6264f82139e193a5c26afd82dc`。
- 独立 SU2 文本解析为 `NDIME=3`、`NELEM=113951`、`NPOIN=46260`、`NMARK=4`，体单元类型和四个 marker 计数与 generation 完全一致。最终 `mesh.msh` / `mesh.su2` / manifest / Gmsh log 的 SHA-256 分别为 `75c1fd55780273c4053c9458d7a2fe89f3555afb8e806771a461c3c40f0721a8`、`6cfb13e75ebd0e4665570f23eddd758e8b6d71e148a14c932c4695f69b66cc73`、`fb4b893f2fe0dafaac5ef27e2498db638f5cad5030be0df5c7e521824d48b012`、`b01cf87046c4fd001a264321b09a3d5d97e2d43f56b83e5001c97fcff5e3c520`；manifest 中的网格哈希与实际文件一致，输出目录没有 cfg/VTK/VTU/PVTU/VTM/CSV。
- Gmsh 4.15.2 的 build/readback 会话均执行 finalize 且 cleanup error 为 0；只读 pipeline BREP 前后 SHA-256 均为 `13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121`。最终专项测试 56/56 PASS；完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 362/362 PASS、0 failures，耗时 11.019 秒。

### 剩余风险与下一质量门

- 本阶段解决的是完整小规模真实几何混合网格的拓扑、15 层连续性、marker、写盘回读和“严格正性”问题，不是生产质量优化。Gmsh 在自动 tetra 优化阶段仍报告 volume 49 有 11 个 ill-shaped tetra；该原文、数量、行号及最终双审计证据均保留，不能声称无 warning。全局 `minSICN≈2.67e-8`、`minSJ≈3.16e-4` 也明确说明后续仍需建立数值质量阈值并改善最差单元。
- 下一步应先做“小规模 RANS 前网格质量改善门”：按稳定几何位置定位 11 个低质量核心四面体和全局最差单元，调整局部 core sizing/优化策略并冻结可接受的最小质量阈值；通过后才运行受限的一阶 RANS 与 y+ 校核。本轮没有运行 SU2_CFD、SU2_SOL、pvbatch、MPI、GPU、GUI、粗中细网格或生产计算。
- 当前目录不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`，因此无法提供 Git diff；以 362 项完整回归、正式 CLI 返回码、manifest/log、输入输出哈希、fresh readback 和禁止工件扫描作为替代审计证据。

---

## 当前阶段：真实壁面局部边界层网格质量门

状态：已完成；真实派生几何上的局部棱柱层生成与写盘回读质量门 PASS，未运行 SU2 或 ParaView，且未提升为生产网格

### 本轮目标

在保持原始 STEP 和已验证共享拓扑 BREP 只读、且不改变既有 topology-smoke 连接流水线的前提下，建立一个独立、可重复、fail-closed 的 Gmsh Python API 局部边界层试验。试验面必须由 `markers.toml` 中 wall marker 的稳定几何指纹选择，层数、增长率和目标 y+ 必须来自项目配置，首层高度必须由所选工况和当前标准大气状态推导并记录方法与中间量；本轮只验证 Gmsh 能否在真实壁面代表性面片上生成并审计不少于 15 层的三维棱柱层。

### 验收标准

- 新增独立边界层试验配置，哈希绑定当前 `project.toml`、`cases.csv`、`markers.toml` 和 pipeline BREP；配置只保存稳定 surface fingerprint，不保存或依赖 Gmsh runtime tag，且只能选择 `kind = "wall"` 的成员；
- 从设计工况、美国标准大气、项目 `target_yplus`、参考长度和增长率计算首层高度及累计层高，manifest 必须记录 Mach、海拔、密度、黏度、速度、Re、摩阻估算、摩擦速度、首层高度、层数和总厚度；项目常数不得重复硬编码到 Python；
- Gmsh 必须使用普通 Python API、fresh initialize/try/finally/finalize、无 GUI；只在会话内导入只读 BREP并创建局部离散边界层实体，不修改任何几何源文件；
- 生成至少 15 层一阶棱柱单元，选中面片覆盖率必须为 100%，同时报告其占完整 wall 面积的比例；三维单元总数不得超过 300,000，不得存在非正 Jacobian、非正体积或非有限值；
- 输出仅写入独立的 `runs/boundary_layer_trial`，包括 MSH、JSON manifest 和完整 Gmsh 日志及 SHA-256；失败证据也必须保留且不得伪造 PASS；
- 标准库测试覆盖配置/哈希门、wall 指纹唯一匹配、层高计算、挤出方向判定、棱柱层计数、单元上限、非正质量、异常 finalize 和禁止下游调用；运行专项及完整测试后，再执行一次预计远少于 15 分钟的真实几何局部试验；
- 本质量门通过只代表局部边界层生成方法可行，不代表全壁面覆盖、y+ 已由流场验证、RANS 已通过或后部出口边界已冻结。本轮不得运行 SU2、pvbatch、粗中细网格或生产计算。

### 实施记录

- 2026-08-01：新增 `config/boundary_layer_trial.toml`，以 SHA-256 绑定当前项目、工况、marker 配置和只读 pipeline BREP；试验只选择 `vehicle_internal_and_external_walls` 中稳定 fingerprint `f635f5d86c87aeb266b584dcf5f5447ee16ec8b9eb393045a0abb826584b2f39`，不保存或依赖 runtime tag。新增独立 `boundary_layer_trial.py` 和 `pipeline boundary-layer-trial` 命令；该路径不解析外部工具链、不创建完整流体域，也没有 SU2、ParaView、MPI、GPU、GUI 或 shell 调用。
- 2026-08-01：设计点 `M50_H21_A8_B0` 的首层高度从项目配置和标准大气计算：`rho=0.0748735018 kg/m³`、`mu=1.42710356e-5 Pa·s`、`U=1478.748457 m/s`、`Re=4.03431964e7`、估算 `Cf=0.0021302704`、`u_tau=48.261040 m/s`。未加安全系数的目标 y+ 首层高度为 `2.764577127e-6 m`；试验按外部配置的 0.5 安全系数使用 `1.382288564e-6 m`，15 层、增长率 1.2、总厚度 `9.957330580e-5 m`。这些只是网格规划估算，manifest 明确 `yplus_verified=false`。
- 2026-08-01：fresh Gmsh 4.15.2 会话从只读 BREP 重新计算稳定指纹并唯一解析所选面为当次 tag 10；挤出方向不使用实体朝向符号，而是在五个 trimmed-surface 样点分别检查法向两侧。负法向 5/5 点进入唯一相邻流体体，正法向失败，因此使用负累计高度。原流体核心体只在内存中移除，避免与局部棱柱层重叠；源 STEP/BREP 没有修改。
- 2026-08-01：首次 `qualified_patch_1` 已得到正确的 23,550 个棱柱，但独立文件回读发现 manifest 报告的是写盘前 15,741 个内存节点，而实际 MSH 为 13,504 个节点。该目录保留为审计证据且未覆盖。随后增加“写出后 clear/open 原文件并重新完整审计”的强制质量门和回归测试；最终在全新 `runs/boundary_layer_trial/qualified_patch_2` 重跑，manifest 以实际文件回读值为权威，并记录 Gmsh 在 `Mesh.SaveAll=0` 下省略的 2,237 个内存节点差值，棱柱拓扑和质量必须在写盘前后完全一致才可 PASS。

### 验证结果

- 最终真实命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe pipeline boundary-layer-trial --step geometry/raw/model1.step --project config/project.toml --cases config/cases.csv --markers config/markers.toml --trial-config config/boundary_layer_trial.toml --case M50_H21_A8_B0 --output runs/boundary_layer_trial/qualified_patch_2` 返回 0；Gmsh initialize/finalize 均已执行，日志扫描无 warning/error/fatal/ill-shaped/inverted/negative/NaN/Infinity。
- 写盘后 fresh Gmsh 回读得到 13,504 节点、1,570 个基面三角形、23,550 个 `Prism 6`，严格等于 `1570 × 15`；实际层数 15、选中面覆盖率 100%、三维单元数低于 300,000。`minDetJac=2.98406329e-10`、`minSJ=0.991120718`、`minSIGE=0.874960624`、最小体积 `2.98586723e-10 m³`，全部有限且所有非正计数均为 0。所选面面积 `1.632546823 m²`，占完整 wall 面积 `18.1177708%`；manifest 明确不宣称全壁覆盖。
- 最终 MSH、manifest、日志 SHA-256 分别为 `ae69860907cea8a937beb68c2dcd7bcde953e77c7f3cb35051cce55e8d185c5a`、`597323af9b2b14f223a324ada868a46862d5259a7bc5ccca14983513a6cef458`、`8e081fbf14d9eff7bb9afabbacd1acb57672c3c04f869edeca91a590c6aa5b6a`。原始 STEP 与派生 BREP 仍只读且 SHA-256 保持 `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b` / `13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121`。
- 专项测试为 9/9 PASS；最终完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 331/331 PASS、0 failures，耗时 13.602 秒。当前目录不是 Git 仓库，无法执行有效 Git diff；继续使用完整回归、真实 manifest/log、文件哈希和只读源快照作为审计证据。

### 剩余风险与下一质量门

- 本阶段已解决“Gmsh 能否在真实项目壁面上按项目参数生成、写出并严格审计 15 层正体积棱柱”的问题。输出故意是没有核心体的局部方法试验，`complete_fluid_domain=false`、`core_filled=false`、`solver_mesh_eligible=false`、`production_mesh_eligible=false`，不能交给 SU2，也不能用于任何物理解读。
- 下一质量门是把逐面双侧 `isInside` 定向扩展到全部 48 个 wall 面，处理相邻面层间连接/终止/合并，重建不重叠的四面体核心并验证两个流体体共享接口；随后才可生成完整的小规模边界层网格并进行小型 RANS/y+ 校核。本轮按一次只推进一个质量门的规则在此停止。

## 当前阶段：项目坐标、标准大气与后部出口物理证据质量门

状态：已完成；修正后的坐标/大气/SU2/pvbatch 小型诊断门 PASS，后出口获得 topology-smoke 范围内的临时超声速支持证据，但生产边界仍未冻结

### 本轮目标

解决真实连接 pilot 暴露出的最后一个物理前置问题：把项目坐标系中的攻角/侧滑角无歧义地转换为当前 SU2 版本使用的来流方向，为所选工况从标准大气模型计算 SI 自由流状态，并通过普通 Python → SU2_CFD → VTU → pvbatch 对两个已确认后部出口输出逐面法向 Mach、回流与质量流量证据。该证据只来自现有小型 topology-smoke 网格上的受限 Euler 诊断，不修改原始 STEP，不猜测静压或背压，不作生产物理解读。

### 验收标准

- 从本机 SU2 8.5.0 安装/运行证据确定 `AOA`、`SIDESLIP_ANGLE` 与速度分量的实际约定；项目角度与求解器角度、目标/实际单位来流向量及误差必须写入 manifest，不能继续把项目攻角直接当作 SU2 `AOA`；
- 新增可独立测试的标准大气计算，只由 `project.toml` 选择的标准大气/Sutherland 模型和 `cases.csv` 海拔驱动，输出静温、静压、密度、声速与黏度，全部为 SI；不得把项目工况或参考量重复硬编码到 Python；
- 只为工况 `M50_H21_A8_B0` 生成独立的小型诊断配置，使用现有 topology-smoke 网格、串行 SU2、无 GPU、无 MPI、无出口静压/背压；运行上限必须小于 15 分钟且不提升为生产求解；
- ParaView 诊断脚本只能由 `pvbatch` 执行。它必须从当前 run manifest 精确读取解文件，并用 SU2 网格边界三角形与相邻四面体定向外法向，分别报告 `rear_outlet_1`/`rear_outlet_2` 的面数、面积、法向 Mach 范围、亚声速面积比例、回流面积比例、正/负/净质量流量，以及所有外边界的全局质量不平衡；坐标映射不唯一、数组缺失、非有限值或输出 JSON 无效均立即 FAIL；
- 现有 connection-only pipeline 行为与 309 项测试不得回退；新增标准库测试必须覆盖大气层边界、21 km 数值、角度映射、SU2 配置禁止压力、边界面外法向/通量、manifest 输入、失败即停及 `shell=False`；
- 即使诊断显示出口超声速，本轮也不得自动修改 `project.toml` 的 `TBD_AFTER_SUPERSONIC_PILOT`。只有合格边界层小网格、充分迭代/稳定性和重复验证后，才可由项目方冻结生产边界；本轮只输出可审计证据和下一质量门结论。

### 实施记录

- 2026-08-01：完成现状复核并确认先前真实小算例的软件链六段均 PASS，但 SU2 日志中项目 `alpha=8°, beta=0°` 被直接写成 `AOA=8°, SIDESLIP=0°` 后，速度偏转落在 `+Z` 而不是项目定义的 `+Y`；先前结果因此只保留连接证据，不能作为后出口物理依据。本机 SU2 8.5.0 的运行日志及随安装 `polarSweepLib.py` 共同证明 `Ux=U cos(AOA) cos(AoS)`、`Uy=U sin(AoS)`、`Uz=U sin(AOA) cos(AoS)`；新增通过单位向量反解的通用转换，设计点现严格写为 `AOA=0°`、`SIDESLIP_ANGLE=8°`，非零 alpha/beta 组合也不再交换物理轴。
- 2026-08-01：新增纯标准库 `atmosphere.py`，实现 0–84.852 km 地势高度范围内的 1976 美国标准大气分层和 Sutherland 黏度；模型选择及海拔只从 `project.toml`/`cases.csv` 读取。设计点 21 km 得到 `T=217.65 K`、`p=4677.876050 Pa`、`rho=0.074873502 kg/m³`、`a=295.749691 m/s`、`mu=1.42710356e-5 Pa·s`。独立诊断配置同时写入 config 真源中的参考面积、长度和力矩原点；`FREESTREAM_PRESSURE` 明确为远场大气状态，仍禁止 `MARKER_OUTLET`、出口静压和背压。
- 2026-08-01：新增 `outlet_diagnostics.py` 纯 Python 计算层、只允许 `pvbatch` 解释的同名 ParaView 脚本，以及 `pipeline outlet-check` 受限命令。脚本独立解析当前 type-10 四面体/type-5 三角形 SU2 网格，把每个边界三角形与唯一相邻四面体配对并据此定向外法向；9,049 个 SU2/VTU 点必须按坐标一一校验。质量流量直接积分 Momentum，正/反向通量和回流面积在线性零交叉处解析裁剪；法向 Mach、两个出口和全外边界质量闭合均写入 JSON。普通 Python 继续不导入 ParaView，SU2_CFD 与 pvbatch 都只由统一 runner 用绝对路径和参数列表启动。
- 2026-08-01：首次 200 步诊断的 SU2 已返回 0，但控制层因 SU2 CSV 表头在字段引号外带空格而 fail-closed，按规则没有启动 pvbatch；该完整 FAIL 证据保留在 `runs/outlet_validation/physical_gate`。修正并新增回归测试后，使用全新目录 `physical_gate_retry1` 重跑，没有覆盖失败证据。最终命令为 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe pipeline outlet-check --step geometry/raw/model1.step --project config/project.toml --cases config/cases.csv --markers config/markers.toml --topology-smoke-config config/topology_smoke.toml --case M50_H21_A8_B0 --mesh runs/real_connection/mesh/mesh.su2 --mesh-manifest runs/real_connection/mesh/mesh_manifest.json --max-iterations 200 --timeout 300 --output runs/outlet_validation/physical_gate_retry1 --quiet`。

### 验证结果

- `outlet_validation_manifest.json` 为 PASS、`diagnostic_gate=PASS`、`production_boundary_frozen=false`。SU2_CFD 8.5.0 串行完成恰好 200 步，返回码 0、无 fatal、stderr 为空并生成有限 VTU；日志实测来流 `(1464.37, 205.804, 0) m/s`，目标/实际单位向量最大分量误差 `8.052e-7`，21 km 静压相对误差 `8.443e-7`。SU2 明确报告达到 200 步上限而未宣称收敛，本轮同样不作收敛结论。
- ParaView 6.2 由 pvbatch 参数列表成功打开本轮 VTU，返回码 0、stderr 为空。9,049 个点坐标映射无缺失/歧义，最大欧氏误差 `2.661e-7 m`；9,900 个外边界三角形全部且仅各有一个相邻四面体，边界覆盖完整。`rear_outlet_1` 最小顶点法向 Mach `1.957081`、回流面积/质量流量均 0；`rear_outlet_2` 最小顶点法向 Mach `4.427986`、回流面积/质量流量均 0。全局相对质量不平衡 `0.00185104`（约 0.1851%），低于本轮 1% 诊断门。
- 最终 manifest、case.cfg、history.csv、VTU、出口诊断 JSON SHA-256 分别为 `37586e2869a9377b006e2e33801f240a95ebf2c1eb9f16b70d4799221aa5a4be`、`ddca5708e2f8da70fc26a0778427ad2a5a0e5c771a4364171b32bf903ba28e92`、`8492958c7291cf6facaba3466c6fe3b3d7b2a82a01d6a390646d30ecc58d1f26`、`8ba79eb92d81284b53a28aeba262d2520486a0188e32a93b09f016b9e7e121be`、`9e3f3ff19222b0255e1002478a5db0e9ad3cbc078eebd6901a55198c9d0d14f7`。
- 专项测试先后为 45/45、25/25 PASS；最终完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 322/322 PASS、0 failures，耗时 9.260 秒。原始 STEP 与派生 BREP 仍只读，SHA-256 仍为 `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b` / `13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121`。当前目录不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`，因此仍以完整回归、运行清单、日志、哈希和只读快照替代 Git diff。

### 剩余风险与下一质量门

- 当前问题的“小网格坐标/大气/出口证据链”已经解决，两个后出口在本轮 topology-smoke Euler 解上均有明确的局部超声速且无回流证据，可以支持下一阶段继续采用临时 `MARKER_SUPERSONIC_OUTLET` 做合格小网格验证；这不等于生产边界已经冻结。
- 当前网格没有边界层、仍含 8 个低质量但正体积四面体，且 200 步只达到迭代上限；因此 `project.toml` 继续保持 `TBD_AFTER_SUPERSONIC_PILOT`。下一质量门只能是合格的小规模边界层网格、重复且稳定的解、同一套出口法向 Mach/回流/质量守恒复核；通过后再由项目方冻结边界并进入小规模 RANS。本轮在此停止。

---

## 当前阶段：后部出口超声速 connection-only pilot 与真实小型端到端连接

状态：已完成；真实项目的受限 STEP 派生几何→Gmsh→SU2_CFD→VTU→pvbatch 连接链六段全部 PASS，仍明确禁止提升为生产边界或物理结论

### 本轮目标

在不猜测静压或背压、不修改原始 STEP/派生 BREP、不生成生产网格的前提下，把 `config/project.toml` 的 `TBD_AFTER_SUPERSONIC_PILOT` 落实为一个严格受限、可审计且不得提升为生产物理结论的超声速出口连接 pilot。使用当前已经 PASS 的真实三维 topology-smoke 网格，为 `M50_H21_A8_B0` 生成当前本机 SU2 8.5.0 有语法证据支持的五步串行 Euler 配置，两个已确认 rear marker 临时使用 `MARKER_SUPERSONIC_OUTLET`，随后只在 SU2 成功时由普通 Python 通过 `pvbatch` 打开本轮解文件并生成 JSON/CSV，最终写出真实连接报告。

### 验收标准

- `TBD_AFTER_SUPERSONIC_PILOT` 只能在 `mesh_level=smoke`、`nproc=1`、`max_iterations<=5`、无 GPU、无边界层且网格不超过 300,000 单元时进入；manifest 必须标记 `connection_only=true`、`boundary_mode_frozen=false`、`production_eligible=false`，其它上下文继续 fail-closed；
- 当前安装证据必须明确包含 `MARKER_SUPERSONIC_OUTLET`；生成配置使用 `SOLVER=EULER`、工况配置中的 Mach/AOA/侧滑角、真实 mesh marker 名称、相对 `MESH_FILENAME=mesh.su2`、五步迭代、CSV history 和 ParaView 输出，不写压力/背压，不把项目物理常数硬编码进 Python；
- `front_conical_surface` 使用当前版本已验证的 `MARKER_FAR`，`rear_outlet_1` 与 `rear_outlet_2` 仅在本 pilot 中使用 `MARKER_SUPERSONIC_OUTLET`，壁面使用当前版本有本地语法证据的 Euler 滑移壁边界；内部 `turning_section_outlet` 不得成为求解 marker；
- SU2_CFD 必须由统一 `CommandRunner` 以绝对可执行路径和参数列表串行启动，禁止 shell/MPI/GPU；保存完整命令、cwd、UTC 起止时间、返回码、stdout/stderr，非零返回码、timeout、致命日志、非有限 history/VTU 或少于指定步数立即 FAIL；
- 只有 SU2 manifest 为 PASS 且产生当前运行目录内的新 ParaView 可读文件时才允许调用 `pvbatch`；pvbatch 必须从 SU2 manifest 的 `visualization_file` 精确取得输入并输出合法 inventory、postprocess JSON 和 CSV，SU2 失败后不得调用；
- `real_connection_report.json` 的六段链接必须全部来自本轮 manifest；连接成功可以 overall=PASS，但报告必须明确五步结果不作物理解读、不能冻结生产边界。后续正式冻结仍需合格小规模网格上的法向 Mach、回流和质量守恒证据；
- 先增加标准库 mock/静态测试覆盖 pilot gate、配置渲染、禁止压力、真实阶段顺序和失败即停，再运行专项测试、完整 `unittest` 与一次用户指定的真实小型 pipeline 命令；检查日志、输出、输入哈希和 Git 状态，随后更新本节实施记录、结果与剩余风险并停止。

### 实施记录

- 2026-08-01：新增 `config/rear_outlet_pilot.toml`，把项目方本轮授权固化为严格哈希绑定的 connection-only 合同。它只允许当前 `project.toml`/`cases.csv`/`markers.toml`/`topology_smoke.toml`/原始 STEP、工况 `M50_H21_A8_B0`、`mesh_level=smoke`、`SOLVER=EULER`、`nproc=1`、五步、无 GPU；项目配置本身继续保留 `TBD_AFTER_SUPERSONIC_PILOT`。合同明确 `MARKER_SUPERSONIC_OUTLET` 仅为临时 pilot、`boundary_mode_frozen=false`、不允许静压或背压、不得物理解读或用于生产。
- 2026-08-01：在 `su2_bridge.py` 增加独立三维项目 pilot 配置渲染、配置/网格准备和严格串行运行器，没有改变既有二维 smoke 路径。配置项先从本机 SU2 8.5.0 安装证据解析，随后真实 SU2 运行作为更强的兼容性证据；配置使用 cases 中 Mach/AOA/侧滑角及 markers 中真实物理组名称，只写相对 `mesh.su2`，输出五步 CSV history、restart 和 `connection_solution.vtu`，没有 pressure/back-pressure 输入或键。
- 2026-08-01：在 `pipeline.py` 把真实阶段从显式 stub 接成 input→Gmsh→mesh validation→hash-bound pilot approval/config→SU2→VTU validation→pvbatch。任一异常由 `PipelineStage` 立即短路；SU2 固定 `nproc=1`，ParaView 只读取当前 SU2 manifest 的 `visualization_file`。终审又修正两个证据一致性问题：三个指定 JSON/CSV 必须逐文件核对绝对路径、大小和 SHA-256 后才把 pvbatch 链标为 PASS；已有 FAIL manifest 也必须补齐独立 stage log。初次加入测试 helper 时产生过一次 Python `IndentationError`，已在任何真实外部程序启动前修复并由专项/完整回归覆盖；首次尝试给 PowerShell 分配 TTY 时宿主 `CreateProcess` 被拒绝，pipeline 未启动，随后使用普通非交互参数列表正常执行。
- 2026-08-01：最终从仓库根目录执行 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe pipeline run --step geometry/raw/model1.step --project config/project.toml --cases config/cases.csv --markers config/markers.toml --case M50_H21_A8_B0 --mesh-level smoke --nproc 1 --max-iterations 5 --output runs/real_connection --timeout 300`。本轮实际网格入口是由原始 STEP 受控修复、独立验证并哈希绑定的只读 SI BREP；原始 STEP 只作为只读 provenance，原因是既有证据已证明 STEP round-trip 不能保持两体共享拓扑。最终运行时间为 `2026-08-01T13:40:25.312888Z` 至 `13:41:03.593344Z`，所有七阶段均实际执行。

### 验证结果

- `real_connection_report.json` 六段 `step_to_gmsh`、`gmsh_to_su2_mesh`、`mesh_to_su2`、`su2_to_visualization_file`、`visualization_file_to_pvbatch`、`pvbatch_to_json` 全部为 PASS，`overall=PASS`；报告 SHA-256 为 `2c68ce829c131b2b154c1c294997e6209698ff96f9a1d2ceb43bf7c43fabc9a8`，同时记录 `connection_only=true`、`production_eligible=false`、`rear_outlet_boundary_frozen=false` 和 `BLOCKED_PENDING_PHYSICAL_BOUNDARY_VALIDATION`。
- Gmsh 4.15.2 重新得到 9,049 节点、39,319 个一阶四面体和 4 个非空 solver marker：`front_conical_surface=3120`、`rear_outlet_1=310`、`rear_outlet_2=1498`、`vehicle_internal_and_external_walls=4972`；`mesh.msh`/`mesh.su2` SHA-256 分别为 `7fb6d5e0485eedd268a4904d0f8e7b93ce912ba065c060a5f83ca019d67865cc`、`0717051899a6a159a8879a5fce61655c3e465365e0b58e3983e4bdb241edd320`。非正 Jacobian/SICN/体积数量均为 0，原始 logger 的 8 个 ill-shaped tetra 警告继续保留。
- SU2_CFD 8.5.0 以绝对路径、参数列表 `[SU2_CFD.exe, case.cfg]` 在当前 `runs/real_connection/su2` 串行运行，成功读取 9,049 点/39,319 四面体/4 markers，返回码 0，`history.csv` 恰好 5 行且全部有限，日志无 fatal，`solver.stderr.log` 为空。参考配置为本机 `FADO/examples/example4_SU2/data.zip::configFlow.cfg`，SHA-256 `3dcc9385847fc7be5f8af169c54c638f4e92b23ed41a614342e1d1ebc83d177e`；`case.cfg`、history、VTU SHA-256 分别为 `6f567a3fb021ea1ae6a03cfcd4721020add221085ac8bf7e7cda23c2c2411817`、`1e79c11a5bd8672337ba550b4912c9a9c770db4db128ea0721015a333e35056d`、`f12a78dc7a659f1fb1096d66a0ca96b8f32ed4458bbaf2787688c598cffcdf7c`。
- ParaView 6.2 的最小启动 probe、`inspect_solution.py` 和 `smoke_postprocess.py` 均由普通 Python 经 `pvbatch` 参数列表启动并返回 0；实际 `pvbatch.stderr.log` 为空。VTU 被读为 `Unstructured Grid`，9,049 点、39,319 单元，检测到 Density、Momentum、Energy、Pressure、Temperature、Mach、Pressure_Coefficient、Velocity；inventory/postprocess/integral SHA-256 分别为 `ece633edc82fe7cf70ef95f64b9155755a09e1a8fd75e4e9567b32a3797336f5`、`4366ae68b83e5c974dab3d46b033baa715d6b0bb5e7f1268e4a3ac792ea7e166`、`c84ba155bae34dce96e39bd3787bced3599b4dc18a01d747805f2e866cc0f379`。可选的 `pvbatch --help` 能力探测在 Windows 上超时并被安全终止，未启用未经证实的 offscreen 参数；该非关键诊断完整保留，后续三个实际关键命令均返回 0。
- 专项测试最终为 60/60 PASS；完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 309/309 PASS、0 failures，耗时 10.203 秒。静态策略测试继续证明外部程序只有统一 runner 可以创建、`shell=False`、普通 Python 不导入 `paraview.simple`。原始 STEP/BREP SHA-256 仍为 `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b` / `13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121` 且保持只读。当前目录不是 Git 仓库，`git status --short` 仍返回 `fatal: not a git repository`，故不能生成 Git diff；以完整回归、双次真实运行、manifest/log、SHA-256 与只读 snapshot 替代该检查。

### 剩余风险与下一质量门

- 本轮已经解决真实项目的软件/数据连接问题，但没有解决后部出口的生产物理边界冻结。五步 Euler 不能证明两个 rear outlet 上的局部法向 Mach 全部超声速，也未验证分出口回流、质量流量/全局质量守恒或背压敏感性；因此 `project.toml` 必须继续保持 `TBD_AFTER_SUPERSONIC_PILOT`，不得把本次 PASS 当作边界物理验证。
- 当前网格没有边界层且含 8 个低质量但正体积 tetra，只能用于连接；不能用于 RANS、y+、载荷、测量面指标、收敛或任何物理解读。正式下一质量门应先生成合格的小规模边界层网格并实现 marker-resolved 法向 Mach/回流/质量守恒证据，再由项目方决定并冻结 rear outlet 模式；本轮在此停止。

---

## 当前阶段：稳定 marker 配置与真实 BREP topology-smoke 体网格

状态：已完成；真实派生 BREP 的三维 topology-smoke 网格与 SU2 文本质量门均 PASS，并按后部出口未冻结规则停在 SU2 配置前

### 本轮目标

把已经独立验证为 PASS 的共享拓扑 BREP、边界重匹配和接口持久性证据固化为正式 `config/markers.toml`。marker 身份必须来自名称、角色、稳定几何/拓扑指纹及证据哈希，不能把某次 Gmsh 会话的数字 tag 当作长期身份。随后让真实项目流水线把 `geometry/raw/model1.step` 仅作为只读来源与审计输入，明确选择经过本轮网格可用性质量门的只读派生 BREP 作为唯一可网格化几何；在全新的 Gmsh Python API 会话中重新计算指纹、唯一匹配实体并建立命名 Physical Groups，生成不超过 300,000 单元的三维 topology-smoke 网格。本阶段只检查几何、marker、网格拓扑与质量，不运行 SU2 或 pvbatch，不对后部出口边界条件作任何猜测。

真实网格探针已经证明初版 `model1_shared_topology.brep` 的若干近切 BSpline 面会产生自交二维三角片并导致 Delaunay/HXT 的 PLC 恢复失败；全局加密、局部加密、降低重叠阈值和通用 OCC healing 均不能作为合格修复。项目方另提供了只读原生 `Model1-计算区域.SLDPRT`，本机也存在已注册的 SolidWorks 自动化 API。因此允许新增一个受控、无界面、静默的“原生 CAD 重导出→Gmsh 几何等价性→共享拓扑 fragment→网格可用性”子质量门；任何输出只能进入 `geometry/derived` 与 `runs/real_connection`，不得覆盖原始 SLDPRT/STEP，且没有完整几何等价、marker 重匹配和网格证据时不得替换 pipeline geometry。

### 验收标准

- 严格按顺序重读 `AGENTS.md`、完整 `PLAN.md`、`config/project.toml`、`config/cases.csv` 并检查 `config/markers.toml`；正式配置只能从当前 PASS 的 repair/rematch/interface-persistence 证据生成，所有被引用文件必须校验状态、路径及 SHA-256；
- `config/markers.toml` 必须新建时拒绝覆盖，保存 `farfield`、`wall`、`rear_outlet_1`、`rear_outlet_2`、`fluid` 和非求解 `turning_section_outlet` 的名称、项目角色、维度、solver-boundary 属性、稳定成员指纹和几何特征；不得保存或依赖旧 runtime entity tag，缺失、重复、交叉或歧义匹配必须立即 FAIL；
- 原始 STEP 与最终 BREP 运行前后均保持只读、路径、大小、mtime 和 SHA-256 不变；网格入口必须明确拒绝非 pipeline-eligible 的派生 STEP，并验证最终 BREP SHA-256 为当前修复证据声明的值；
- 若使用 SLDPRT 修复路线，必须通过不可见、静默、无 GUI 点击的程序接口只读打开 `Model1-计算区域.SLDPRT`，把新 STEP/BREP 写到新的 `geometry/derived` 子目录；记录 SolidWorks 可执行程序绝对路径、版本、参数/操作、输入输出 SHA-256、开始结束时间、返回状态和完整错误，finally 中关闭文档与应用。新导出几何必须与 `geometry/raw/model1.step` 的两个流体体在体积、质心、bounds、外露面/接口面几何覆盖上双向等价，否则立即 FAIL；
- 新派生几何必须重新执行两体 BooleanFragments、独立 BREP 重导入、共享接口持久性、55 个外露面 marker 双射及内部测量面重匹配；不得沿用旧 runtime tag，不得因为可网格化而放宽业务边界语义；
- 在 owned Gmsh Python API 生命周期中导入 BREP、synchronize、重新目录化并以稳定指纹唯一匹配 marker；必须得到 2 个 volume、57 个唯一面、55 个单邻接外露面和 2 个双邻接共享面，两个共享面不得成为 solver marker，55 个外露面必须被四个 solver marker 无遗漏且无重叠覆盖；
- 创建命名 Physical Groups：`farfield`、`wall`、`rear_outlet_1`、`rear_outlet_2`、`fluid`；`turning_section_outlet` 只保留为内部测量语义，`solver_boundary=false`，不得成为 SU2 边界 marker；
- 只生成一阶、无边界层的三维 topology-smoke 网格，输出限于 `runs/real_connection`；总单元数必须不超过 300,000，节点/体单元及各单元类型计数大于零，负或零 Jacobian 数量为零，记录最小等效质量、每个边界组面单元数、边界层实际层数 0 与覆盖率 0，并明确这是 topology-smoke 而非 RANS/生产网格；
- `mesh.msh` 与 `mesh.su2` 必须由 Gmsh Python API 写出，SU2 文本必须是三维、关键计数可解析且非零、无 NaN/Inf，并且恰好包含所需 solver marker；每个输出保存 SHA-256、Gmsh 版本/模块路径、选项、开始结束时间、完整日志、lifecycle/finalize 状态和独立 manifest；
- 任一 marker、拓扑、质量、单元上限、写出或文本验证失败都立即停止；本阶段不得生成 SU2 配置、不得启动 SU2_CFD/SU2_SOL、不得启动 pvbatch，不得使用 shell、GUI、MPI 或 GPU；
- 先运行专项标准库测试，再运行完整 `unittest`；检查日志、输出、输入快照及 Git 状态，随后把真实命令、结果、哈希、修改文件和剩余风险写回本节。只有本质量门 PASS 后，下一阶段才可进入后部出口边界模式冻结/小规模 SU2 配置检查。

### 实施记录

- 2026-08-01：复核初次真实失败证据后，没有修改只读 STEP/BREP，也没有降低拓扑检查条件。`topology_smoke_mesh.py` 现会把 Gmsh logger 的 overlapping-facet/PLC/HXT 错误映射回稳定 surface fingerprint，并在失败 manifest 中保存 `mesh_failure_diagnostics` 与可审计的 `cad_repair_request.json`；runtime tag 只保留为当次会话审计，异常路径仍分别收集最后实体/节点错误并执行 finalize。
- 2026-08-01：全局尺寸、算法切换和通用 healing 均失败后，用只读探针证明当前 BREP 可通过稳定指纹驱动的局部 Distance/Threshold 场安全网格化。新增 `config/topology_smoke.toml`，只保存 14 个当前 `markers.v2` 外露壁面指纹、`minimum_size_m=0.012`、`distance_max_m=0.08` 和 `sampling=100`，不保存任何 Gmsh runtime tag；其 provenance 固定当前 markers SHA-256 与 pipeline BREP SHA-256，policy 明确 `topology_smoke_only=true`、`production_mesh_eligible=false`。
- 2026-08-01：`pipeline.py` 在导入 Gmsh 前把 `topology_smoke.toml` 作为 config 根下普通非 reparse 输入检查，严格验证 schema/policy、marker/BREP 哈希、指纹归属、唯一性和数值范围；`cli.py` 新增可显式覆盖的 `--topology-smoke-config`，默认仍使项目方给定命令无需增加参数。Gmsh 每次 fresh import 后从稳定指纹重新唯一匹配 runtime surface，再从本次 surface adjacency 得到 49 条边界曲线并创建 Distance/Threshold background field；代码中没有模型 surface tag 常量。
- 2026-08-01：第一次正式局部网格已完成 38,205 个四面体但严格所有权检查发现两个镜像薄壁面各有 8 个 orphan triangle，因此未发布网格、未调用下游。只读诊断把它们唯一定位为稳定指纹 `86cbe21b...` 与 `1c5e6d72...`；把两者加入同一外部配置后，独立探针得到 39,319 个四面体，55 个外露面全部一侧归属、两个共享面全部双侧归属。
- 2026-08-01：最终从仓库根目录执行项目方指定的 `pipeline run` 命令。Gmsh 4.15.2 在 owned Python API 会话中重新导入只读 BREP、建立四个命名边界 Physical Group 和 `fluid`，生成 `runs/real_connection/mesh/mesh.msh` 与 `mesh.su2`。随后流水线选择且记录工况 `M50_H21_A8_B0`，在 `_rear_outlet_gate` 得到 `REAR_OUTLET_BOUNDARY_NOT_FROZEN` 后立即停止；没有生成 case.cfg、没有启动 SU2_CFD/SU2_SOL、pvbatch、MPI、GPU 或 GUI。

### 验证结果

- `topology_smoke_manifest.json` 与独立 `mesh_manifest.json` 均为 PASS：9,049 节点、39,319 个一阶四面体、11,074 个三角面，两个 fluid volume 分别 11,967/27,352 个四面体；四个 marker 面元数为 `front_conical_surface=3120`、`vehicle_internal_and_external_walls=4972`、`rear_outlet_1=310`、`rear_outlet_2=1498`。9,900 个外边界三角形精确穷尽全部单归属 tetra face；两个共享面共 1,174 个三角形精确穷尽两体交叉 face。
- 网格质量记录为最小 `minDetJac=1.375746023780503e-08`、最小 `minSICN=0.0054531395479363925`、最小体积 `2.2929100396341718e-09 m³`；三项非正数量均为 0。Gmsh logger 原样保留 volume 1 的 8 个 ill-shaped tetra 警告，因此该网格只通过 topology-smoke，不得提升为边界层/RANS/生产网格。
- `mesh.su2` 独立文本验证为 `NDIME=3`、`NELEM=39319`、`NPOIN=9049`、`NMARK=4`，四个 marker 名称和非零计数与配置完全一致，节点索引从 0 连续，无 NaN/Inf。`mesh.msh`/`mesh.su2` SHA-256 分别为 `7fb6d5e0485eedd268a4904d0f8e7b93ce912ba065c060a5f83ca019d67865cc`、`0717051899a6a159a8879a5fce61655c3e465365e0b58e3983e4bdb241edd320`。
- 原始 STEP 与 pipeline BREP 前后 SHA-256 仍分别为 `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b`、`13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121`，均保持只读；Gmsh logger 正常 stop、session 正常 finalize。`real_connection_report.json` 中 `step_to_gmsh=PASS`、`gmsh_to_su2_mesh=PASS`，后续链接因显式配置门为 FAIL/NOT_RUN，未伪造 overall PASS。
- 专项测试最终为 57/57 PASS；完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 299/299 PASS、0 failures，耗时 9.155 秒。新增测试覆盖 tag 重排下的指纹局部场、未知指纹 generate 前停止并 finalize、stale config hash、未知配置指纹及 pipeline 参数透传。
- 当前目录仍不是 Git 仓库，无法生成 Git diff；以 299 项完整回归、真实 CLI 清单/日志、输入输出 SHA-256、只读 snapshot 和当前 SU2/ParaView 目录仅含 NOT_RUN manifest 的检查替代。

### 剩余风险与下一质量门

- `config/project.toml` 中后部出口模式仍为 `TBD_AFTER_SUPERSONIC_PILOT`；真实 STEP→Gmsh→三维 `mesh.su2` 的软件/数据连接问题已经解决，但完整真实 STEP→SU2→pvbatch 流水线仍在这个业务边界条件质量门停止。下一质量门必须先由项目方冻结 rear outlet 模式，或另行批准一个不猜静压/背压且可审计的超声速出口验证方案；在此之前不得启动 SU2。
- 当前 topology-smoke 网格含 8 个低质量但正体积 tetra，足以检查拓扑、marker 和文件连接，不适合 RANS 试算、边界层或生产结果。后续在边界模式冻结后仍须先进行局部边界层试验和小规模 RANS 质量门，不能把本轮 39,319 单元网格当作正式网格。

---

## 当前阶段：真实 STEP 派生共享拓扑修复与确认映射重匹配

状态：已完成；共享拓扑 BREP、确认映射重匹配及独立接口持久性复验证据全部 PASS，未生成正式 marker、体网格或求解输入

### 本轮目标

先把仓库根目录只读 `Model1-计算区域.STEP` 以字节相同、只读且拒绝覆盖的方式建立为配置声明的规范原始输入 `geometry/raw/model1.step`，两者保留同一 SHA-256。随后以该规范只读 STEP 和已通过的接口完全重合证据为输入，在全新的 Gmsh Python API 会话中对两个三维流体体执行一次受控 OpenCASCADE BooleanFragments，使原来 `1↔54`、`3↔58` 两对重复界面成为两个真正由两体共享的拓扑面。修复结果只写入 `geometry/derived`；独立重导入实测证明 OpenCASCADE BREP 保持共享拓扑，而同一形状经 STEP AP214 导出后重新导入会恢复为 59 个单邻接面、丢失两体共享身份。因此本阶段把重导入 PASS 的 BREP 固定为后续 Gmsh pipeline 几何，STEP 仅作为明确标记为非 pipeline 可用的交换/诊断副产物，并保留其失败证据，绝不以 STEP 成功写出冒充拓扑持久。随后重新生成 surface/volume catalog，以体 lineage、几何覆盖和聚合指纹把项目方已确认的四个完整 solver-boundary group 及唯一非求解测量面重匹配到修复后实体。本阶段不创建 Physical Group、不建网、不写 `config/markers.toml`，不运行 SU2、ParaView、MPI、GPU 或 GUI。

### 验收标准

- 依次重读 `AGENTS.md`、完整 `PLAN.md`、`config/project.toml`、`config/cases.csv` 并检查 `config/markers.toml`；确认上游 STEP 为仓库内普通只读文件。仅当 `geometry/raw/model1.step` 不存在时才以新文件方式复制，复制后立即验证字节/SHA-256一致并设为只读；已有目标绝不覆盖。上游源运行前后路径、大小、只读属性、mtime 与 SHA-256 完全不变；
- 所有实现先由标准库 mock 测试验证，再允许一次真实低成本几何运行；Gmsh 生命周期必须为 `initialize()` / `try` / `finally` / `finalize()`，导入前设置 `Geometry.OCCTargetUnit=M`，不启动 FLTK，不通过 shell/CLI 修改几何；
- 输入两个 volume 必须通过 STEP solid/shell 与体积/质心/bounds 证据唯一解析，不把旧 Gmsh tag 作为长期身份；仅调用一次 `occ.fragment(object, tool, tag=-1, removeObject=True, removeTool=True)`，不得静默追加 `removeAllDuplicates`、healing、sewing、放大 tolerance 或 union；
- fragment 输出必须恰好为两个不同三维体，`outDimTagsMap` 对两个输入均一对一，两个体分别及总体体积、质心、bounds 在容差内守恒；不得产生第三个 sliver 体或把两个域并成一个体；
- 修复后的两个体必须形成恰好 2 个“共享接口 lineage”；每个 lineage 可由一个或多个 fragment patch 组成，但每个 patch 都必须邻接两个体且在两体有向 boundary 中符号相反。若没有进一步分片，预期为 57 个唯一面和 2 个共享面；若 OCC 合法分片，则唯一面数必须精确等于 55 个完整外露 descendant 加共享 patch 数，并给出面积/第一矩/bounds/无重叠覆盖证明；
- 两个共享接口 lineage 必须分别完整覆盖旧接口对 `1↔54`、`3↔58` 的面积、质心、bounds、双向投影和边界曲线几何，不得仍存在单邻接的重合副本；其他 55 个外露原面必须按体 lineage 和几何/拓扑指纹一对一或经严格 descendant-union 守恒零歧义重匹配，所有新外露面必须恰好有一个语义祖先；
- 先写进程专用 staging CAD，分别输出 `.brep` 和 `.step`，并在独立新 Gmsh 会话中重导入。BREP 必须重复通过“两体、2 个共享接口 lineage、全部共享 patch 双邻接、55 个外露原面完整 lineage、体积/尺度守恒”，才允许成为唯一 `pipeline_geometry`。STEP 重导入结果必须完整记录；当前实测为 2 体/59 单邻接面/0 共享面，故必须标记 `NONCONFORMAL_AFTER_STEP_ROUNDTRIP`、`pipeline_eligible=false`，不能触发整个 BREP 修复失败，也绝不能被后续 pipeline 选择。最终文件已存在时拒绝覆盖，失败不得留下可误选为 PASS 的正式派生 CAD；
- 生成 `runs/real_connection/geometry_repair/` 下独立的 pre/post catalog、fragment lineage、接口验证、marker rematch、Gmsh 日志和 repair manifest，记录 Gmsh 版本/模块、完整 API 参数、UTC 时间、源/输出 SHA-256、异常 traceback、logger 与 finalize 状态；
- marker rematch 只消费 `marker_confirmation_confirmed.json` 的稳定决定，四个 solver role 必须各映射完整外露组且互不重叠；`turning_section_outlet` 必须重新找到同一 `x=3.77 m`、法向 `+X`、`solver_boundary=false` 的唯一闭环。任一零匹配、多匹配、拆分覆盖不全或测量面丢失立即 FAIL；
- 不创建 Physical Group，不调用 `mesh.generate`，不写 `.msh/.su2/.vtk/cfg`，不运行 SU2/pvbatch；完成专项与完整回归后检查日志、产物、源哈希和 Git 状态并更新本阶段。只有本质量门 PASS 后，下一阶段才可生成正式 `config/markers.toml` 和 topology smoke mesh。

### 实施记录

- 2026-08-01：按强制顺序复核仓库和配置真源后，新增 `geometry_input.py`，把仓库根目录只读 `Model1-计算区域.STEP` 以拒绝覆盖、字节相同、mtime 相同和只读的方式建立为 `geometry/raw/model1.step`。`canonical_input.json` 为 PASS；源和规范副本均为 2,553,767 字节且 SHA-256 同为 `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b`。
- 2026-08-01：新增 `geometry_repair.py` 和固定 `gmsh repair` CLI。新的 Gmsh 4.15.2 Python API 会话先用 solid/shell、体积、质心、bounds、几何类型及边界曲线 incidence 重建 59 个稳定 surface fingerprint，真实复核 `matching_uses_old_runtime_tags=false`；随后仅调用一次 `occ.fragment(..., tag=-1, removeObject=True, removeTool=True)`，没有 healing、deduplication、网格、Physical Group、CAD GUI 或外部进程。
- 2026-08-01：前两次正式尝试分别暴露并保留了 STEP AP214 重导入丢失共享拓扑、以及 STEP surface 15 几何漂移的问题；失败证据已可恢复地归档到 `geometry_repair_attempt1_step_roundtrip_fail_20260801T091423Z` 和 `geometry_repair_attempt2_step_external_drift_fail_20260801T091855Z`。没有删除或覆盖这些失败证据，也没有把失败 STEP 标记为可用。
- 2026-08-01：第三次正式命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe gmsh repair --step geometry/raw/model1.step` 返回 0。fragment 与独立 BREP 重导入均为 2 个 volume、57 个唯一面、55 个外露面、2 个共享面；每个共享面均在两个 volume 的有向 boundary 中取 `[-1,+1]`。volume 1 体积差为 0，volume 2 绝对/相对差为 `1.43772394097e-5 m³ / 1.60618149811e-7`，通过事先记录的 `2e-5 m³ / 2e-7` 双重容差；没有第三个 sliver volume。
- 2026-08-01：发布只读 `geometry/derived/model1_shared_topology/model1_shared_topology.brep` 作为唯一 pipeline geometry；SHA-256 为 `13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121`。同轮 `.step` SHA-256 为 `899aad6a3b475f14c6d2dfa8ac2140ede903941f9e2a629d58ab11bf74c70bc0`，独立重导入实测 2 体/59 个单邻接面/0 共享面，已明确标记 `NONCONFORMAL_AFTER_STEP_ROUNDTRIP`、`pipeline_eligible=false`。
- 2026-08-01：在最终 BREP 上重新执行测量面发现与 tag-free marker rematch。四个 solver role 完整成员数为 `2/48/3/2`，互不重叠且覆盖全部 55 个外露面；两个内部共享面不进入 solver marker。`turning_section_outlet` 唯一重匹配到 `x=3.77 m`、闭环、法向 `+X`、`solver_boundary=false` 的测量面；两个报告使用的 derived CAD hash 均与 pipeline BREP 一致。
- 2026-08-01：终审发现原 `shared_interface.json` 只有拓扑/质量属性证据，尚不足以满足 post-fragment 几何覆盖条款，因此新增 `interface_persistence.py` 和固定 `gmsh validate-repair` CLI，而没有降低验收条件。真实命令重新在一个独立 owned Gmsh 会话中按 stable pair fingerprint 导入只读源 STEP 和最终 BREP，构造 2×2 候选矩阵；每个源 pair 的两个 copy 均执行双向 17×17 面投影、绝对参数法向匹配和每条曲线 201 点双向 sampled Hausdorff，最后要求两个 stable pair 与两个共享面严格双射并穷尽。
- 2026-08-01：独立验证返回 PASS，`interface_persistence.json` SHA-256 为 `ab604729626f0356ac4289a015a918d7db4f7444191fbae643c4252cbe38de7d`。两 lineage 最大双向面距分别为 `4.25086404278e-15 m`、`4.20421391673e-15 m`，最大边界 sampled Hausdorff 为 `4.43742265155e-15 m`、`4.53019971827e-15 m`，最小 `|normal dot|` 均为 `0.9999999999999998`；每 lineage 实际只有一个 patch，故无内部 patch 重叠，两个 patch 又被全局双射完整穷尽。报告结构化记录 owned initialize/logger/finalize 均成功，源 STEP 与 BREP 前后 snapshot 完全不变。

### 验证结果

- `repair_manifest.json` 为 PASS，SHA-256 为 `2ff98181b7b9cac714f31cf8b1f847a3696414a3b3daf42e3675214a9a3a2cbf`；Gmsh 为 4.15.2，Python 模块为仓库 `.venv\Lib\site-packages\gmsh.py`，正式运行时间 `2026-08-01T09:47:21.308695Z` 至 `09:47:57.825928Z`。OCC 的四条 `BOPAlgo_AlertUnableToOrientTheShape` warning 已原样保留，只有在两体邻接、共享面反向、外露 lineage、体积/bounds、BREP clean reimport 和最终接口投影全部 PASS 后才接受，没有静默忽略。
- `shared_interface.json`、`measurement_surface_candidates.json`、`marker_rematch.json` 分别为 PASS；SHA-256 分别为 `90a383a94a232f6896e3b4135f05528edda0fcacd2d11bc7e1ea5d1451e4a3f2`、`0cd6f749605b9fd8f61589a9648cb9a7a66292c5063c690d35412a579c96b3fb`、`6cb2e86b9c162d3eb3bec7f555790b4233c9ca246cddb3a07183507aab03b9d0`。最终 `interface_persistence.json` 为独立 PASS，且 `matching_uses_old_runtime_tags=false`、source/BREP unchanged、`finalize_succeeded=true`。
- 新增/修改的专项测试首先为 31/31 PASS；最终命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 271/271 PASS、0 failures，耗时 8.094 秒。异常 owned-session 测试继续证明 action 失败仍 finalize；新测试覆盖严格双射、零/多/复用匹配 fail-closed、边界曲线互为最近的双射、曲线数变化、当前 project hash、pipeline CAD hash、新写输出和禁止 mesh/CAD write/GUI/process 调用。
- 最终禁止工件扫描确认本阶段正式 repair、validation 和 derived 目录中 `.msh/.su2/.vtk/.vtu/.pvtu/.vtm/.cfg` 数量为 0；`config/markers.toml` 仍不存在，没有运行 SU2、pvbatch、MPI、GPU 或 GUI。根目录源 STEP 和规范 STEP 当前 SHA-256 仍相同，二者及派生 CAD 均保持只读。
- 当前目录仍不是 Git 仓库；`git status --short` 返回 `fatal: not a git repository`，因此无法提供 Git diff。已用 271 项回归、真实 CLI 返回码、独立重导入/投影证据、输入输出 SHA-256、只读 snapshot 和禁止工件扫描替代该检查。

### 剩余风险与下一质量门

- 本质量门已证明当前实际模型没有合法 split：每个 interface lineage 恰好一个共享 patch。当前实现对未来 CAD 若产生多 patch 会 fail-closed，不会静默接受；若以后确需支持 split，必须另行实现 patch-union 外边界消内部共边和无重叠覆盖证明，不能把本轮单 patch 结论泛化。
- STEP AP214 副产物不保持共享拓扑，后续 Gmsh pipeline 必须明确选择上述 BREP，禁止回退到派生 STEP。原 repair manifest 的每次内部会话没有逐会话结构化 lifecycle 字段；当前 pipeline BREP 已由独立 companion report 重新验证并结构化证明 owned initialize/logger/finalize 成功，后续若重跑 repair 应把同一结构直接合并进新 repair manifest。
- 下一阶段才允许从已通过的 `marker_rematch.json` 生成正式 `config/markers.toml`，并以 pipeline BREP 生成受 300,000 单元上限约束的 topology smoke 体网格；本阶段没有提前建网。
- 后部出口物理模式仍为 `TBD_AFTER_SUPERSONIC_PILOT`。即使下一阶段 marker 和 smoke mesh 都通过，真实流水线仍必须在 SU2 配置质量门停止，直到项目方冻结边界模式；不得为使算例运行而猜测静压或背压。

---

## 当前阶段：项目方边界映射确认固化

状态：已完成；用户确认已固化并通过当前证据的独立重验证，未跨越“几何修复先于正式 marker”质量门

### 本轮目标

把项目方在 2026-08-01 明确回复的“确认推荐映射”写入独立确认文档：`front_conical_surface` 选择完整上游组，`vehicle_internal_and_external_walls` 选择完整 void-shell 组，`rear_outlet_1` 选择当前审计组 `{2,4,5}`，`rear_outlet_2` 选择 `{56,59}`，`turning_section_outlet` 选择 `x=3.77 m`、法向 `+X` 的非求解测量面。用当前 STEP/project/catalog/boundary/measurement 证据重新验证所有稳定指纹及一一映射，并生成验证报告。本阶段不写正式 `config/markers.toml`，因为完整接口尚未在 `geometry/derived` 中 imprint/fragment，修复后必须重新目录化和唯一匹配。

### 验收标准

- 依次读取 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`，确认正式 marker 尚不存在；
- 完成文档必须从现有待确认模板派生，不允许直接拼造新实体或组；选择只能来自模板候选，两个 rear role 必须完整、一一、无重复映射；
- 记录确认人、用户原始决定摘要、UTC 时间、模板/边界证据/测量面证据/STEP/project SHA-256；输出拒绝覆盖且不得位于 `config`；
- 对完成文档调用独立验证器，要求所有 solver boundary 以完整组重新唯一匹配，测量面保持 `solver_boundary=false`，旧 tag 仅用于 audit；验证失败不得产生 PASS 报告；
- 原 STEP、SLDPRT、IGES 保持原有大小、属性和 SHA-256；不调用网格、SU2、ParaView、MPI、GPU 或 GUI；
- 新增标准库测试覆盖合法确认、错误组、重复 rear 映射、错误测量面、拒绝覆盖和验证失败无 PASS；运行专项测试及完整回归；
- 更新本阶段实施记录、验证结果和下一质量门。当前目录不是 Git 仓库时继续记录该限制。

### 实施记录

- 2026-08-01：按仓库强制顺序重新读取 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`，并确认 `config/markers.toml` 仍不存在。复核现有待确认模板、独立边界组证据和唯一测量面候选，没有从旧 Gmsh tag 直接构造生产 marker。
- 2026-08-01：在 `marker_confirmation.py` 新增 `solidify_marker_confirmation(...)`。该入口只接受未修改的 pending 模板；当前 audit tag 集合必须精确命中模板中的一个完整组，部分组、未知面、重复组、遗漏角色、错误 measurement、非 UTC 时间、`config` 输出和覆盖全部 fail-closed。写入的长期身份是 stable group fingerprint；tag 只保留在 `human_confirmation.audit`，并明确 `gmsh_tags_are_matching_criteria=false`。
- 2026-08-01：`validate_marker_confirmation(...)` 现会交叉核验确认人、canonical UTC、原始决定摘要、模板哈希、STEP/project/boundary 证据、measurement candidate 哈希、稳定组映射、角色决定及 audit tag 完整组的一致性。新增 `write_marker_confirmation_validation(...)`，先完成全部验证再原子新写 PASS 报告；失败不会遗留可被误认成 PASS 的文件。
- 2026-08-01：把项目方原始回复“确认推荐映射！”固化为 `project_owner` 决定：`front_conical_surface={55,57}`、`vehicle_internal_and_external_walls={6..53}`、`rear_outlet_1={2,4,5}`、`rear_outlet_2={56,59}`、`turning_section_outlet=measurement_loop_1e3f5f1668ebcdbb`。完成文档记录时间 `2026-08-01T08:17:50.908329Z`。命令行首次直接携带中文智能引号时因终端编码产生 Python `SyntaxError`，返回码 1 且未生成文件；随后只把同一原文改用 Unicode escape 传入，公开 API 正常返回 `CONFIRMED_BY_HUMAN/PASS`，文件内复核为正确中文原文。
- 2026-08-01：生成新文件 `marker_confirmation_confirmed.json` 和 `marker_confirmation_validation.json`。未修改待确认模板，未写 `config/markers.toml`，未创建规范 STEP 副本或 `geometry/derived`，也未调用 Gmsh、SU2、ParaView、MPI、GPU 或 GUI。

### 验证结果

- `marker_confirmation_confirmed.json` 状态为 `CONFIRMED_BY_HUMAN`，内嵌 solidification validation 为 PASS，SHA-256 为 `8239C0DBC3B710306B136EDBF1F75C890B58A088EF0C25911EFB41F0F2C5D7BB`。四个 solver role 分别唯一匹配成员数 2、48、3、2 的完整组；测量面保持 `solver_boundary=false`。
- `marker_confirmation_validation.json` 状态为 PASS、`marker_configuration_ready=true`、`markers_toml_written=false`，SHA-256 为 `19A48932A7111AB7924625124FDBEEA9F79B21CC5CF43288247AC68988C17E40`。模板、边界证据和测量候选 SHA-256 均在确认链中保留；当前 marker review SHA-256 为 `C34184CF41080E253B1C900DB5D64AB55DDF7CF87FFEFC341B4F95F50BA85714`。
- 专项命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest tests.test_marker_confirmation -v` 为 18/18 PASS；`compileall` 通过；完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 240/240 PASS、0 failures。
- 原 STEP、SLDPRT、IGES 的终检 SHA-256 仍分别为 `D7D1CBEC76CCC6B45AAD5689D61A28C41E1BA41B9CDA6C90D46EB6FF891C1A6B`、`CB39FF7128B7A94E1E0DFD842B95BA43DB7F0E5AF573F9C6238536D56BF7853F`、`9D66574763C785BE80CECEB7D45667942C1C344D23B78BB8C87F6ECC7FB0E06D`；STEP/SLDPRT 仍为只读，大小和属性未变。本阶段只新增两个 JSON 审计工件；目录中的 `diagnostic_surfaces.msh` 是上一只读二维审查阶段的既有工件，不是本轮生成的体网格或 SU2 输入。
- 当前目录仍不是 Git 仓库；`git status --short` 返回 `fatal: not a git repository`，因此无法提供 Git diff。已以专项/完整回归、实际 JSON 解析、来源哈希和禁止工件检查替代该质量门。

### 剩余风险与下一质量门

- 业务语义和两个 rear outlet 的编号已经由项目方确认，不再是歧义；但当前确认所附 tag 仍只是修复前审计定位，不能直接成为生产 marker。两对完整重合接口尚未共享拓扑，仓库规则要求先在 `geometry/derived` 对只读原 STEP 的副本执行受控 imprint/fragment，再重新生成 surface catalog 并以稳定几何/壳/拓扑指纹唯一重匹配本次确认。
- 若派生修复后任一完整组出现零匹配或多匹配，必须停止并回到几何审查；只有全部唯一匹配后才能生成正式 `config/markers.toml` 和规范只读 `geometry/raw/model1.step` 输入。本轮没有提前创建它们。
- 后部出口的物理边界模式仍为 `TBD_AFTER_SUPERSONIC_PILOT`。即使后续 marker 和 smoke 体网格通过，真实流水线仍须在 SU2 配置质量门停止，不得为运行而猜测静压或背压。

---

## 当前阶段：原生 CAD 取证与边界语义解阻

状态：已完成；几何证据与待确认语义已分离，项目方映射随后已在上一阶段正式固化

### 本轮目标

在保持 `Model1-计算区域.SLDPRT`、`Model1-计算区域.STEP` 和同源 IGES 原样且不写入的前提下，穷尽本机可用的无界面原生 CAD 元数据、STEP/OCC 拓扑、两体接触关系、封闭壳和轴向截面证据。建立稳定的几何指纹与显式确认输入层，使后续 `markers.toml` 不依赖易变的 Gmsh tag；若原始文件仍缺少项目语义，则输出最小、可视化、可审计的待确认清单和派生测量面规范，而不伪造自动识别成功。

### 验收标准

- 依次读取配置真源；原始三种 CAD 文件运行前后保持原有属性、大小和 SHA-256 不变（STEP/SLDPRT 原本只读，IGES 原本为 Archive 且可写），不启动 GUI、不安装软件、不访问网络；
- 检查本机已安装或已注册的 SolidWorks/Document Manager/eDrawings/FreeCAD 及可用无界面转换能力；只允许读取和探测，不自动调用 GUI 程序；
- SLDPRT/IGES 取证必须区分“同源/几何一致证据”和“成功读取原生 feature/face 名称”，不得把前者冒充后者；
- 对两个 OCC volume 建立并验证位置、体积、壳、跨体近重合面、装配外露面和轴向平面关系；只把多重几何/拓扑证据唯一支持的内容标为 `CONFIRMED_GEOMETRY`；
- marker 配置不得保存裸 Gmsh tag 作为唯一依据，必须包含角色名称、所属 solid/shell、面积/质心/bounds/类型等稳定指纹及容差；重新导入时必须重新匹配并要求唯一命中；
- `rear_outlet_1/2` 的编号、后部边界物理模式和 `turning_section_outlet` 的位置/法向若无配置真源，必须保持待确认。允许生成审查模板或派生几何规范，但不得写入正式 `config/markers.toml`；
- 新增或修改代码采用标准库测试，覆盖唯一匹配、零匹配、多匹配、tag 重排、非法/缺失确认和测量面规范 fail-closed；运行专项测试和完整回归；
- 真实诊断仅允许低成本几何查询或二维审查工件，不生成三维生产网格，不运行 SU2、MPI、GPU 或新的 ParaView 求解后处理；
- 检查日志、manifest、源/输出哈希和 Git 状态（若仍非 Git 仓库则记录限制），完成后更新本阶段实施记录、验证结果与剩余风险并停止。

### 实施记录

- 2026-08-01：新增纯标准库 `iges_metadata.py`，严格解析同源 SolidWorks 2025 IGES 的 80 列记录、全局段、目录段、属性和组证据。真实文件共有 877 个目录实体、59 个 trimmed surface，单位为 mm；entity label、type 406 property、type 402 group 和 type 308 subfigure 均为 0，因此 IGES 证明几何同源，但没有补回 CFD 面语义。
- 2026-08-01：完成本机原生 CAD 只读取证。SolidWorks 2025 SP5 已安装，但已有用户会话 PID 3180；本地官方 API 帮助确认 `CreateObject` 可能附着现有会话，无法安全保证隔离，故未打开/关闭用户文档。离线 Document Manager 因缺有效 license key 返回 `0x80040112`；Windows 属性和原始二进制字符串也没有 feature/body/face/selection-set 语义。SLDPRT 前后哈希一致，未留下额外 SolidWorks 进程。
- 2026-08-01：新增 `interface_coincidence.py`，对两对跨体接触面执行可复现的双向 17×17 内点投影、相反参数法向核验和每条边界曲线 201 点双向 Hausdorff 匹配。它要求只读 STEP，采用 Gmsh initialize/try/finally/finalize，不建网、不写 CAD，并在异常路径保留原 traceback、在结束后复核源快照。
- 2026-08-01：新增 `boundary_semantic_candidates.py`。分类不再把 Gmsh 参数法向误当流体外法向，而是读取 `config/project.toml` 的 +X 定义，按 STEP solid/root-shell、完整接口边曲线邻接、相对 X 范围和连通性把 59 个面完整分区；所有 Gmsh surface/curve tag 仅保留在 audit，组身份由成员几何、壳归属和拓扑指纹确定。
- 2026-08-01：新增独立人工确认层 `marker_confirmation.py`。所有求解边界必须选择完整 surface-group fingerprint；farfield/wall 只有一个完整候选，两个 rear role 必须显式一一映射两个完整下游组，禁止按单面质心 Z 或旧 marker_review 的散面候选编号。测量面只接受一个 `solver_boundary=false` 候选。模板为非生产 JSON，拒绝写入 `config`、拒绝覆盖，验证时对当前证据重新唯一匹配，零/多/部分/重复/过期匹配全部失败。
- 2026-08-01：修正真实流水线输入转换：`solver_boundary=false` 的二维测量面不再进入 Gmsh Physical Group 或 SU2 必需 marker；三维 fluid group 保留。新增回归测试证明后处理测量面不会误成求解边界。
- 2026-08-01：只读调试测量面发现器确认旧结果漏掉的不是闭环拼接，而是负 Z 支路的 OCC B-Spline 曲线 79/81/83 在 `x≈2.88 m` 处存在约 `1.390e-6 m` 峰谷轴向微波动；旧 `1e-8 m` 平面阈值逐条排除了这些边，正 Z 支路因残差约 `1e-10 m` 被保留，造成错误的一环结果。
- 2026-08-01：将近似坐标平面默认容差最小放宽为 `2e-6 m`，继续保持端点容差 `1e-8 m` 和站位聚类容差 `1e-7 m` 独立；报告显式记录全部发现参数。新增“约 1.4 微米 B-Spline 波动仍保留两支路”与“超过 2 微米阈值仍拒绝”回归测试，未根据项目坐标硬编码候选角色。

### 验证结果

- `interface_completeness.json` 为 PASS：`1↔54` 与 `3↔58` 的最大双向面距分别约 `4.25e-15 m` 和 `4.16e-15 m`，最大边界近似 Hausdorff 距离约 `1.83e-15 m`，边界曲线一一匹配且参数法向相反。因此两对面确认为完整几何接口；它们尚未 imprint/fragment，仍不是共享拓扑。
- `boundary_semantic_candidates.json` 为 PASS 且 59/59 面恰好分配一次：front/upstream 完整候选组为当前 audit tags `{55,57}`，wall 完整候选为 `{6..53}`，两个未编号下游完整候选为 `{2,4,5}` 与 `{56,59}`，内部接口为 `{1,54}` 与 `{3,58}`。其中 tag 仅用于当前审计，不是重匹配条件。
- 轴向几何复核证明 surface 6/7 是同一个 `x=5.2 m` 薄环后缘的两半，内/外半径约 `0.25/0.26 m`、合计面积 `0.016022153724 m²≈π(0.26²−0.25²)`，属于完整 void-shell wall 组；surface 4 是 `x=6.3053623 m`、半径 `0.26 m` 的完整端盖，是下游组 `{2,4,5}` 的成员。旧的“6/7 是两个 rear outlet”解释已被证据否定。
- `marker_confirmation_template.json` 已生成，状态为 `PENDING_HUMAN_CONFIRMATION`，包含 4 个完整 solver-boundary group 和 1 个非求解测量面候选，明确 `markers_toml_written=false`；`config/markers.toml` 仍不存在。
- 真实 STEP 只读复核在 `2e-6 m` 阈值下只比旧阈值新增缺失的一个闭环，其他七个已发现截面不变。`x≈2.88 m` 的两环质心分别位于 `z≈-0.0642477 m` 与 `z≈+0.0642477 m`，面积均约 `0.00687125 m²`；17/33/65/129/257 点五档采样均稳定识别两个环，最大残差收敛至约 `7.745e-7 m`。下一合格单环唯一位于 `x=3.77 m`，因此分类为 `CONFIRMED_GEOMETRY/CANDIDATE_ROLE`，仍未断言业务角色。
- 原 STEP、SLDPRT、IGES 的终检 SHA-256 分别为 `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b`、`cb39ff7128b7a94e1e0dfd842b95ba43db7f0e5af573f9c6238536d56bf7853f`、`9d66574763c785be80ceceb7d45667942c1c344d23b78bb8c87f6ecc7fb0e06d`，大小和属性均未改变；没有建网、写 CAD、运行 SU2/ParaView 或启动 GUI。完整标准库回归 233/233 PASS。当前目录不是 Git 仓库，`git status --short` 仍返回 `fatal: not a git repository`。

### 历史风险及后续状态

- 原 CAD/IGES 确实没有可读取的 CFD 角色名称；本阶段留下的人工确认项已由项目方在上一阶段明确确认并固化为 stable group fingerprint，不再是当前歧义。
- 正式输入路径 `geometry/raw/model1.step` 和 `config/markers.toml` 仍缺失。必须先完成下述 `geometry/derived` 接口共享拓扑修复和重新目录化，再将已确认语义唯一重匹配到修复后几何，最后才可建立规范只读 STEP 输入和正式 marker；不能把根目录文件或旧 tag 静默代入生产流水线。
- 两对完整几何接口尚未共享拓扑；任何 smoke 体网格前必须在 `geometry/derived` 中执行受控 imprint/fragment、重新目录化并重新匹配稳定指纹。修复会改变运行时 tag，不能沿用本轮 audit tag。
- 后部出口物理模式仍是 `TBD_AFTER_SUPERSONIC_PILOT`。即使 marker 已确认，真实流水线也必须在网格验证后停在 SU2 配置质量门，不得猜测静压或背压。
- 若项目方坚持从原生 SLDPRT 读取 feature/face 名称，必须先关闭现有 SolidWorks 用户会话再进行受控隐藏 COM 读取，或提供有效 Document Manager license key；当前没有安全理由干扰 PID 3180。

---

## 当前阶段：真实 STEP 无界面拓扑取证与 marker 候选收敛

状态：已完成无界面取证与真实诊断运行；marker 仍为 `AMBIGUOUS/MISSING`，质量门继续禁止正式 marker、体网格和 SU2

### 本轮目标

在上一阶段 2 体/59 面只读目录证据之上，进一步穷尽无需猜测的本地证据：解析 STEP BREP 实体引用链和 solid/shell/face 归属，补充 Gmsh 几何类型与近重合面证据，并生成只用于人机审查的低密度、带 `diagnostic_surface_<tag>` 标识的表面网格及 pvbatch 多视角图片。最终输出机器可读 marker 审查报告；只有所有项目角色都能被名称、拓扑和几何多重证据唯一确认时才允许提出正式配置，否则保留候选并继续阻止网格/SU2。

### 验收标准

- 依次读取配置真源后才开始；物理角色名称只从 `config/project.toml` 取得，不在代码中复制项目常数或擅自解释后出口条件；
- 标准库 STEP 解析器只读源文件，支持多行 `#id=TYPE(...)` 记录、引用图、STEP `\X2\...\X0\` 名称解码，并对 `MANIFOLD_SOLID_BREP`、`BREP_WITH_VOIDS`、shell 和 `ADVANCED_FACE` 输出可复核归属；只有 face 数等指纹唯一匹配时才把源 solid 与 Gmsh volume 关联；
- Gmsh 诊断入口继续采用 initialize/try/finally/finalize、OCCTargetUnit=M、OCC import+synchronize；记录 `getType`、面积/体积、质心、bounds、法向和邻接，并计算跨体近重合面候选及误差。原 STEP 前后只读属性、大小和 SHA-256 必须一致；
- 只允许生成二维诊断表面网格，不生成三维体单元；每面临时组名固定为 `diagnostic_surface_<tag>`，不得使用 farfield/wall/outlet 等 CFD 名称。诊断三角形硬上限 100,000，输出只位于 `runs/real_connection/geometry_review`，不得写 `.su2`；
- 普通 Python 不导入 ParaView。若诊断 VTK 通过有限性/计数检查，则只由现有已配置 `pvbatch` 通过 `CommandRunner` 参数列表运行专用脚本，生成外体、内体和多坐标视角 PNG/JSON；不得启动 ParaView GUI；
- `marker_review.json/.md` 必须区分 `CONFIRMED`、`CANDIDATE`、`AMBIGUOUS`、`MISSING`，逐项给出 surface tag、实体类型、几何特征、相邻体、来源 solid、证据和反证。任何角色非唯一时不得写 `config/markers.toml`；
- 单元测试覆盖 STEP 引用解析/名称解码、唯一与歧义 volume 映射、诊断物理组不使用 CFD 名称、二维/10 万上限、异常 finalize、pvbatch 参数列表和报告 fail-closed；先短测试、再完整回归，最后才运行真实诊断；
- 检查日志、图片、JSON、源/输出哈希及是否存在体单元/SU2/solver 命令。完成后更新 PLAN 并停止，不做 geometry fragment/removeAllDuplicates，不写 `geometry/derived`，不运行 SU2、MPI、GPU 或正式 CFD。

### 实施记录

- 2026-08-01：用户要求继续穷尽解决方案。按顺序重读 `AGENTS.md`、完整 `PLAN.md`、`config/project.toml`、`config/cases.csv`，确认 `config/markers.toml` 仍缺失；上一阶段目录及原 STEP 哈希保持有效。
- 2026-08-01：只读初查 STEP 引用链发现：`MANIFOLD_SOLID_BREP` 引用含 6 个面的 closed shell；`BREP_WITH_VOIDS` 的外壳/空腔链预计对应另一组 53 面。这提供了比数字 tag 更强的 solid 归属线索，但尚未构成边界角色证明。
- 2026-08-01：新增标准库 `step_topology.py`，完整解析 19,073 条 STEP 记录、引用链、X2 名称、solid/root shell/face 归属和循环/悬空引用；仅按双向唯一 face-count 指纹把 STEP solid 映射到 Gmsh volume。实测唯一对应为 volume 1 的 5 面 outer shell 与 48 面 void shell，以及 volume 2 的 6 面 boundary shell；报告明确不把 STEP shell 语义解释成 CFD role。
- 2026-08-01：新增 `geometry_review.py` 及独立 `gmsh review` 编排。Gmsh 4.15.2 仍采用 Python API 生命周期、米制 OCC 只读导入；只生成每面名为 `diagnostic_surface_<tag>` 的一阶二维三角形和 VTK，不生成三维单元或 SU2。兼容当前 Gmsh 4.15.2 的 7 标量 `occ.getDistance` 返回，并把 logger warning 原样写入 manifest。
- 2026-08-01：新增专用 `render_geometry_review.py` 与普通 Python pvbatch bridge。控制层只通过统一 `CommandRunner` 的显式参数列表调用已配置 `pvbatch.exe`，脚本在 ParaView 内按实体组输出全局/分组四视角 PNG；输入、分组、图片和 manifest 均做路径、状态、数量和 SHA-256 交叉校验。
- 2026-08-01：新增保守 `marker_review.py`。它只写审查 JSON/Markdown，不写 `markers.toml`；零 OCC 距离只有同时满足面积、质心、包围盒和距离阈值时才列为近重合候选，避免把共边误报成接口；轴向候选按几何 X 厚度识别，因此不会错误丢弃 Gmsh 标为 B-Spline 的近似平面端面。
- 2026-08-01：新增/完善 `geometry_review_pipeline.py`、CLI 及相应标准库测试；任何源路径、只读属性、哈希、二维/计数上限、工具、render 或 marker 依赖不一致均 fail-closed，且 `marker_configuration_ready=false` 时 `next_stage_allowed=false`。

### 验证结果

- 真实命令：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe gmsh review --step 'Model1-计算区域.STEP' --project config/project.toml --output runs/real_connection/geometry_review --mesh-size 0.2 --max-triangles 100000 --timeout 300 --quiet`，返回码 0。`geometry_evidence_manifest.json` 为证据采集 `PASS`、分类 `REVIEW_REQUIRED`、`marker_configuration_ready=false`、`next_stage_allowed=false`。
- Gmsh Python 4.15.2（模块 `.venv\Lib\site-packages\gmsh.py`）生成 6,457 节点、12,910 个一阶二维三角形、0 个三维单元，低于 100,000 上限且坐标有限。诊断质量为 `WARN`：原始日志保留 `surface 6` 和 `surface 7` 各 6 个 invalid elements；该诊断网格明确 `production_mesh_eligible=false`。
- 原 STEP 始终是只读的 2,553,767 字节文件；运行前、运行后和独立终检 SHA-256 均为 `D7D1CBEC76CCC6B45AAD5689D61A28C41E1BA41B9CDA6C90D46EB6FF891C1A6B`，`source_unchanged=true`。未创建 `geometry/derived`，没有修改或复制源 STEP。
- 强近重合接触候选仅剩 `1↔54` 与 `3↔58`：面积相对差约 `1.255e-6`、质心距离约 `1.344e-5 m`、OCC 最小距离 0；交叉的共边 pair 因质心相距约 `1.233 m` 已被排除。即便如此，报告仍把两对标记为 `CANDIDATE`，不声称完整面重合或自动接口。
- 几何轴向候选为 tag 6、7（`x=5.2 m`，Gmsh 类型 B-Spline）和 tag 4（`x=6.3053623 m`，Plane）。两个后出口 role 都得到相同候选集并保持 `AMBIGUOUS`，没有擅自编号；59 个面均无持久名称，farfield/wall 也保持 `AMBIGUOUS`。没有任何面邻接两个体，因此 `turning_section_outlet` 为 `MISSING`。
- ParaView 6.2 的 `pvbatch.exe` 以显式参数列表返回 0，成功读取 6,457 点/12,910 单元的 VTK，生成 100 张 1200×900 PNG；命令 cwd、UTC 起止时间、返回码、stdout/stderr 和全部 100 张图片 SHA-256 已记录，独立复核为 0 个不匹配。审计发现的 28 张旧版生成图已从顶层安全移到 `renders/stale_png_archive/pre_exact_inventory_20260801`（可恢复、未删除）；bridge 随后增加并在真实重跑中通过“顶层 PNG 必须与本轮 manifest 完全相等”的质量门。
- 最终审计确认 `runs/real_connection/geometry_review` 中不存在 `.su2`、`case.cfg`、history 或 solver 工件，`config/markers.toml` 仍不存在；没有调用 SU2、SU2_SOL、MPI、GPU 或 GUI。`compileall` 通过；完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 191/191 PASS、0 failures。
- 当前目录仍不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`，无法生成 Git diff；已逐文件复核本轮 `PLAN.md`、CLI、6 个取证/审查模块、ParaView 脚本和 6 个对应测试文件，并以 191 项回归、真实 manifest/命令日志、源/输出/图片哈希及禁用工件扫描替代 diff 质量门。

### 剩余风险与后续前置条件

- 已穷尽当前无名称 STEP 在不修改几何条件下能够提供的本地拓扑、几何、低密度网格和多视角证据；证据不能唯一决定 CFD role，不能安全自动生成 `config/markers.toml`。
- 必须由客户/几何负责人确认：tag 6/7 与 `rear_outlet_1/2` 的对应关系；farfield 与 vehicle wall 的面集合；`turning_section_outlet` 的准确位置/方向。当前 STEP 没有可证明的独立测量面，若该面确实需要存在，应由几何真源提供或在后续明确授权的 `geometry/derived` 阶段构造，不能猜测。
- 两体存在两对近重合但未共享拓扑的面。在正式分类前需确认它们是否为预期接触，并另设经授权的派生几何 imprint/fragment 质量门；修复会改变 entity tag，必须先修复、重新目录化，再冻结 marker。本轮没有执行 Boolean 操作。
- `surface 6/7` 的诊断三角形警告也必须在任何三维 smoke mesh 前处理；当前二维 VTK 只供审查，不能作为生产网格质量 PASS。
- 后部出口条件仍为 `TBD_AFTER_SUPERSONIC_PILOT`。即使 marker 后续确认，真实流水线仍应停在 SU2 配置质量门，直到边界模式被客户冻结。

---

## 当前阶段：真实 STEP 只读几何目录与 marker 解阻证据

状态：已完成只读目录质量门；marker 分类因输入无名称且拓扑有歧义而保持 `UNCLASSIFIED`，未进入建网/SU2/pvbatch

### 本轮目标

针对当前唯一且保持只读的 `Model1-计算区域.STEP`，新增一个与正式 `pipeline run` 隔离的几何审查入口。通过 Gmsh Python API 仅导入、同步并提取曲面/体拓扑和几何特征，所有证据只写入 `runs/real_connection/geometry_inspection`，用于生成后续 `config/markers.toml` 所需的可审计候选信息。本轮不把根目录 STEP 静默当作正式 `geometry/raw/model1.step`，不复制、移动或修改 STEP，不创建 Physical Groups，不生成网格，不启动 SU2、MPI、GPU、pvbatch 或 GUI。

### 验收标准

- 新入口只接受仓库内现有普通 `.step/.stp` 文件，要求只读，输出严格限制在 `runs/real_connection/geometry_inspection`；它是输入恢复诊断，不改变正式流水线仍要求 `geometry/raw` 与 `config/markers.toml` 的质量门；
- 采用局部 `import gmsh`、`gmsh.initialize()`、`try/finally`、`gmsh.finalize()`；导入前设置 `Geometry.OCCTargetUnit = "M"`，只调用 `gmsh.model.occ.importShapes(...)` 与 `synchronize()`，不得调用 mesh generate/write、Physical Group、FLTK 或任何外部 Gmsh shell 命令；
- 运行前后重新计算 STEP SHA-256、大小和只读属性；任何变化均为 FAIL。初始化、导入、特征读取或 finalize 异常保留完整 traceback、Gmsh 模块路径和 logger 文本；
- 生成 `surface_catalog.csv`，至少含 entity tag、STEP 名称、面积、质心、bounding box、法向抽样、相邻体、推断边界角色和置信度；同时生成 `volume_catalog.csv`、`marker_candidates.json`、`geometry_manifest.json` 和 `gmsh_geometry.log`；
- marker 候选只依据可复核的 STEP 名称、拓扑和几何特征分组。缺少明确名称时角色必须保持 `unclassified`，只可给出 topology/extrema candidate，不得自动映射 farfield、wall、rear outlet 或内部测量面，不得写 `config/markers.toml`；
- 单元测试覆盖生命周期、异常 finalize、单位设置先于导入、目录字段、法向/邻接容错、源哈希不变、禁止建网/写网格和 CLI 路径边界；先运行专项测试，再运行完整标准库测试；
- 真实执行仅运行一次只读目录命令。执行后审查实体数量、单位尺度、名称覆盖率、拓扑歧义和所有输出哈希；只有证据足以唯一分类时才提出下一质量门，否则明确列出仍需人工确认的候选，不伪造 PASS；
- 完成后更新本阶段结果并停止；不得自动进入真实网格、SU2 配置/求解或 ParaView。

### 实施记录

- 2026-08-01：按仓库顺序重读 `AGENTS.md`、完整 `PLAN.md`、`config/project.toml`、`config/cases.csv`，随后确认 `config/markers.toml` 仍不存在。复核现有正式流水线会在缺 marker 和规范 STEP 路径时零外部调用安全停止。
- 2026-08-01：确定最小安全解阻方案为独立只读几何目录化：允许 Gmsh API 读取根目录唯一 STEP，但不把它复制或隐式传给正式流水线；所有新工件只写入用户已授权的 `runs/real_connection`。
- 2026-08-01：在 `GmshBridge` 增加 `inspect_step_geometry(...)`。入口在 import 前检查 STEP 后缀、普通非 reparse 文件、只读属性和允许输出根；记录输入前后大小/SHA-256/只读状态。Gmsh 会话为新建且由本方法拥有，导入前设置 `Geometry.OCCTargetUnit="M"`，导入后 OCC synchronize；成功初始化后的所有异常路径都 finalize。
- 2026-08-01：目录化只调用 OCC/model 几何查询，逐面记录名称、面积、质心、包围盒、参数域内有限非零法向、相邻体和边；逐体记录体积、质心、包围盒及边界面。代码没有创建 Physical Group、没有 `mesh.generate`、没有 `gmsh.write`，也不构造 Toolchain/CommandRunner。
- 2026-08-01：增加固定 `gmsh catalog --step ... --output runs/real_connection/geometry_inspection` CLI；拒绝 `..`、仓库外 STEP、错误后缀或任何其它输出目录。新标准库 mock 测试证明单位设置先于 import、异常仍 finalize、可写输入和输出越界在 import 前失败，以及无建网/写网格/Physical Group/GUI 调用。
- 2026-08-01：专项测试 26/26 PASS、完整测试 151/151 PASS 后，仅执行一次真实只读目录命令。未运行 Gmsh CLI、SU2_CFD、SU2_SOL、MPI、GPU、pvbatch 或 GUI；未生成 `.msh`、`.su2` 或可视化文件。
- 2026-08-01：真实目录揭示 2 个独立 OCC 体和 59 个面；Gmsh/STEP 面名称覆盖率为 0/59，两个体名称也为空。所有 59 面各只邻接一个体，体 1 有 53 面、体 2 有 6 面，当前不存在共享拓扑面。几何特征还显示 `1↔54`、`3↔58` 两对跨体面包围盒相同、面积相对差约 `1.255e-6`、质心相距约 `1.344e-5 m`，只能标记为近重合/拓扑审查候选，不能自动合并或赋边界角色。
- 2026-08-01：因此 `geometry_manifest.json` 的目录状态为 PASS，但分类状态明确为 `UNCLASSIFIED`、`marker_configuration_ready=false`、`next_stage_allowed=false`；没有创建或修改 `config/markers.toml`，正式 `pipeline run` 的输入门保持不变。

### 验证结果

- 真实命令：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe gmsh catalog --step 'Model1-计算区域.STEP' --output runs/real_connection/geometry_inspection`，返回码 0，耗时约 8.1 秒。Gmsh Python API 版本 4.15.2，模块为仓库 `.venv\Lib\site-packages\gmsh.py`；logger 只有 STEP label 信息，无 error/warning。
- 输入 STEP 在运行前后均为只读、2,553,767 字节，SHA-256 均为 `D7D1CBEC76CCC6B45AAD5689D61A28C41E1BA41B9CDA6C90D46EB6FF891C1A6B`，`source_unchanged=true`。导入后的米制全局 bounds 为 `[-0.5218171313,-3.6059005308,-7.2118009616,6.3053624018,3.6059005308,7.2118009616] m`。
- 体 1：体积 `15.6934375418 m³`，bounds `[-0.1711972163,-1.2488864886,-2.4977728772,6.3053624018,1.2488864886,2.4977728772] m`，53 个边界面；体 2：体积 `89.5119226971 m³`，bounds `[-0.5218171313,-3.6059005308,-7.2118009616,5.2000001,3.6059005308,7.2118009616] m`，6 个边界面。
- STEP 文本独立复核有 59 条 `ADVANCED_FACE('NONE',...)`，与目录的 59 个空 surface name 一致；只有一个 solid 级名称和一个 `BREP_WITH_VOIDS('NONE',...)`，不足以把实体唯一映射到项目要求的 farfield、wall、两个 rear outlet 和内部 measurement surface。
- 输出哈希：`surface_catalog.csv`=`3034B62A8341C25E047981913C9ACA02D8DA2C4534207C30B301E698633EEAF3`，`volume_catalog.csv`=`54334395393860551EB73E3FB27F7C9503FFB337C48F5234F9CC5B25477C5F0E`，`marker_candidates.json`=`F8BC95A8CE622458F429139057357023B145A54918DCD630935AF1478B58F2BB`，`geometry_manifest.json`=`A02F8E82C07CBCABC6689443F7C60A31C9FD4DA16844C58863770B4D2516DDF3`，`gmsh_geometry.log`=`4DB707B8EC3C2DE921E826CB2D1F3E59F223D83B1246048C3B51362D168954B0`。
- 专项命令 `python -m unittest tests.test_geometry_inspection tests.test_cli -v` 为 26/26 PASS；最终 `python -m unittest discover -s tests -v` 为 151/151 PASS、0 skipped、0 failures，耗时 4.552 秒。`py_compile` 通过；静态进程策略测试继续证明唯一外部进程入口显式 `shell=False`，本目录没有新增 solver/pvbatch 命令或日志。

### 剩余风险与后续前置条件

- 当前 STEP 没有面级名称，数字 tag 也不稳定；必须由客户/几何负责人依据目录特征确认实体角色，或提供带持久面名的 STEP。不得由坐标或面积“猜”出 `config/markers.toml`。
- 两个体没有共享拓扑，但存在两对高相似跨体面；在边界分类前必须判断它们是预期接口、间隙还是重复面。任何修复都应另设授权阶段并写入 `geometry/derived`，不能修改原 STEP，也不能在本轮静默 `fragment/removeAllDuplicates`。
- 根目录 STEP 仍不等同于配置要求的 `geometry/raw/model1.step`。用户/数据管理员需明确建立规范只读输入或修订配置；诊断入口不会让正式流水线绕过这一真源检查。
- 后部出口模式仍为 `TBD_AFTER_SUPERSONIC_PILOT`，即使 STEP/marker 后续冻结，SU2 仍必须在边界条件质量门停止，除非客户定义另行确认；缺失的 RANS/SST 模板同样仍需当前 SU2 版本证据。
- 本轮到此停止：真实 STEP → Gmsh 的只读导入与目录连接已经成功，但真实项目 Gmsh 网格、SU2 和 pvbatch 流水线尚未打通，不能把目录 PASS 解释为 CFD 连接 PASS。

---

## 当前阶段：真实项目输入的小型软件连接流水线

状态：已实现并安全停止于真实输入质量门（未启动 Gmsh、SU2 或 pvbatch）

### 本轮目标

把已验证的 Gmsh、SU2_CFD 和 pvbatch 连接模块接到 `config/project.toml`、`config/cases.csv`、`config/markers.toml` 与 `geometry/raw` 中的只读 STEP；只为指定 `M50_H21_A8_B0` 生成不超过 30 万单元的 smoke 网格并最多运行 5 步串行连接检查，输出全部阶段 manifest、日志和六段 `real_connection_report.json`。不生成生产网格，不判断收敛或解释物理结果，不运行其它工况、MPI、GPU 或 GUI。

### 验收标准

- CLI 精确支持 `pipeline run --step ... --project ... --cases ... --markers ... --case M50_H21_A8_B0 --mesh-level smoke --nproc 1 --max-iterations 5 --output runs/real_connection`；输出必须严格等于仓库内 `runs/real_connection`，所有生成文件均留在该根目录；
- 第一质量门只用 Python 标准库解析并校验三份配置、选择唯一工况、核对 STEP 路径/只读前后 SHA-256、SI 单位转换声明、smoke/串行/5 步/无 GPU 约束；`markers.toml` 缺失、语法错误、关键 marker 缺失、实体映射歧义或 STEP 不在 `geometry/raw` 时立即停止，且不得 import/initialize Gmsh 或调用任何外部程序；
- 阶段严格为 config → Gmsh STEP import/单位转换/Physical Groups/smoke mesh → SU2 网格校验 → case/配置检查 → SU2_CFD → visualization manifest handoff → pvbatch → report；任一阶段失败后所有下游保持未运行；
- Gmsh 只通过 Python API 读取原始 STEP，导入前按配置设置 OCCTargetUnit 为 m，marker 只来自外部配置且同时校验名称、实体和几何特征，不硬编码真实 surface tag；生成 `mesh/mesh.msh`、`mesh/mesh.su2`、几何/网格 manifest、日志及 `surface_catalog.csv`；单元数大于 30 万、负体积/负 Jacobian、marker 遗漏或歧义均不得启动 SU2；
- Python 独立解析 `mesh.su2` 并证明所有配置 marker 存在、计数可解析、无非有限值；只从 `cases.csv` 取 `M50_H21_A8_B0`，不硬编码项目物理常数；
- SU2 配置仅使用当前 8.5.0 安装证据与项目/工况/marker 真源；后部出口仍为 `TBD_AFTER_SUPERSONIC_PILOT` 或缺少经过冻结的求解边界条件时，允许且必须停在 SU2 配置检查阶段，写明阻塞，绝不猜静压/背压或改变客户定义；只有配置质量门 PASS 才以 `[SU2_CFD, case.cfg]` 串行执行最多 5 步；
- SU2 PASS 后只从本轮 SU2 manifest 的 `visualization_file` 交给 pvbatch；普通 Python 不导入 ParaView，pvbatch 无界面打开并输出 `solution_inventory.json`，所有关键命令使用参数列表和统一 `CommandRunner`，保留原始 stdout/stderr、返回码、cwd、时间和哈希；
- `real_connection_report.json` 始终生成并分别报告 `STEP → Gmsh`、`Gmsh → mesh.su2`、`mesh.su2 → SU2`、`SU2 → 可视化文件`、`可视化文件 → pvbatch`、`pvbatch → JSON`；未运行阶段不得伪造 PASS，必须记录阻塞原因和当前输入/输出证据；
- 标准库测试覆盖配置与工况解析、missing/ambiguous markers 的零外部调用、STEP/输出越界、串行与 smoke/5 步/30 万上限、后部出口未冻结的配置阶段停止、严格阶段顺序、报告六段状态和下游 fail-fast；先运行短测试，再决定真实命令能推进到哪个质量门；
- 检查所有日志、manifest、输入输出哈希、原始 STEP 未变和源码进程策略，更新本阶段；当前目录若仍不是 Git 仓库，记录限制并用逐文件检查替代 diff。本轮完成或在真实输入质量门安全停止后即结束。

### 实施记录

- 2026-08-01：依次重读 `AGENTS.md`、完整 `PLAN.md`、`config/project.toml`、`config/cases.csv`，随后检查 `config/markers.toml`；该 marker 真源当前不存在。
- 2026-08-01：检查真实输入结构：仓库没有 `geometry/` 或 `geometry/raw/` 目录；唯一 STEP 是仓库根目录只读的 `Model1-计算区域.STEP`，而项目配置声明 `geometry/raw/model1.step`。按用户本轮“输入来自 geometry/raw”和“生成文件只能写 runs/real_connection”的限制，不能移动、复制、链接或静默回退到根目录 STEP。
- 2026-08-01：因此当前真实运行必须在纯 Python 输入预检阶段停止；在 `markers.toml` 和规范 STEP 路径补齐前，不允许导入 STEP、初始化 Gmsh 或调用 SU2/pvbatch。继续实现可测试的流水线和 FAIL 报告，使阻塞可复现且不会误触下游软件。
- 2026-08-01：重构 `pipeline run` CLI，加入并严格转发用户要求的 STEP/project/cases/markers/case/mesh-level/nproc/max-iterations/output 参数；移除旧 dry-run 探针语义。CLI 在构造工具链或 runner 前拒绝非 smoke、`nproc != 1`、超过 5 步、非有限 timeout、`..` 或不精确等于仓库 `runs/real_connection` 的输出。
- 2026-08-01：实现纯标准库真实输入加载器：只接受 `config` 与 `geometry/raw` 受信目录中的普通非 reparse 文件，STEP 必须只读且与 `project.geometry_file` 精确一致；校验 SI、mm → m 声明、CSV 列/数值/唯一 case，以及 marker 的 physical name、role、维度、entity tag、STEP 名称与 area/centroid/bounding-box 几何指纹。流体、远场、壁面、两个后出口和内部测量面角色缺一即停，测量面 solver-boundary 标志必须与项目一致。
- 2026-08-01：增加真实三维 SU2 文本网格质量门，独立验证非空/有限、`NDIME=3`、正的 NELEM/NPOIN/NMARK、多 marker 名称与元素数、marker 计数一致和 `NELEM <= 300000`；不再错误复用二维单一 `farfield` smoke 检查器。
- 2026-08-01：实现真实项目阶段编排和六段报告：input → Gmsh STEP → 三维网格检查 → case/config → SU2 → visualization → pvbatch。每一阶段预建独立 NOT_RUN manifest/log；失败阶段写完整 traceback，后续保持未运行。Gmsh mock 测试证明异常时原 STEP 不变且 SU2/pvbatch 零调用；有效三维 mesh mock 证明后出口 TBD 时前两链 PASS、SU2 及下游 FAIL 并零调用、不生成 `case.cfg`。
- 2026-08-01：当前真实命令仅运行 Python 输入质量门并按预期返回非零；生成的报告和全部阶段占位 manifest/log 都严格位于 `runs/real_connection`。没有解析工具路径、import/initialize Gmsh、调用 SU2_CFD、MPI、GPU、pvbatch 或 GUI。

### 验证结果

- 实际命令：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe pipeline run --step geometry/raw/model1.step --project config/project.toml --cases config/cases.csv --markers config/markers.toml --case M50_H21_A8_B0 --mesh-level smoke --nproc 1 --max-iterations 5 --output runs/real_connection`。CLI 返回 1，并明确报告 `MISSING_MARKERS`、`MISSING_STEP`；这是按限制要求的安全 FAIL，不是软件执行失败。
- `real_connection_report.json` 的六段 `step_to_gmsh`、`gmsh_to_su2_mesh`、`mesh_to_su2`、`su2_to_visualization_file`、`visualization_file_to_pvbatch`、`pvbatch_to_json` 全为 FAIL，`overall=FAIL`；只允许的 `input=true`，Gmsh、mesh、case config、SU2、visualization、ParaView attempted 均为 false。报告 SHA-256 为 `8D584DB775A704D5663109EA03183EEB7EDD5193DAFEF9FA53A213641389E03A`。
- 输出审计：`input/input_manifest.json` 与 `pipeline_manifest.json` 均为 FAIL；mesh、SU2、ParaView 目录只含明确 `NOT_RUN_DUE_TO_UPSTREAM_FAILURE` 的 manifest/log。全目录 `.command.json` 数为 0，solver/pvbatch stdout/stderr 日志数为 0，证明没有外部程序启动。
- 根目录现有 `Model1-计算区域.STEP` 仍为只读、2,553,767 字节，SHA-256 保持 `D7D1CBEC76CCC6B45AAD5689D61A28C41E1BA41B9CDA6C90D46EB6FF891C1A6B`；没有移动、复制、链接、导入或修改它。`config/project.toml` 与 `config/cases.csv` SHA-256 仍分别为 `96EC212CAEA5B76816ADB5089AB77B97D77D131461AD854615940D52E4B3BD1C`、`C27CB7B8B70B04FBFCEC1F2CEF4BE66510672377258F09DC13BA4F93AF137435`。
- 专项测试 34/34 PASS；最终完整命令 `$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v` 为 145/145 PASS、0 skipped、0 failures，耗时 4.634 秒。`compileall` 通过；静态进程策略仍证明普通 Python 不导入 ParaView、无 GUI 调用，外部进程唯一入口显式 `shell=False`。
- 当前目录仍不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`；无法生成 Git diff，已用逐文件审查、145 项测试、输入/产物哈希和零命令元数据审计替代。

### 剩余风险与后续前置条件

- `config/markers.toml` 必须由经过几何目录/特征审查且无歧义的边界分类结果提供，至少覆盖流体域、远场、壁面、两个后部出口与内部测量面角色；不得由代码或本轮执行者猜测。
- 原始 STEP 必须由用户/数据管理员放入配置声明的 `geometry/raw` 路径并保持只读，或明确修订配置与命令；本轮不会在输入目录外生成替代文件。
- 后部出口边界仍未冻结，即使 STEP 与 marker 后续补齐，真实流水线也可能在 SU2 配置质量门按设计停止，而不是擅自运行 5 步。
- 项目声明的 `templates/su2/first_order.cfg.j2` 与 `second_order.cfg.j2` 当前也不存在；后出口冻结后仍需建立并按当前 SU2 8.5.0 验证真实 RANS/SST 模板，不能复用二维 Euler smoke 配置。
- 本轮没有取得真实项目 STEP → Gmsh → SU2 → pvbatch PASS；当前可交付结果是可重复的安全质量门及带证据的 FAIL 报告。解阻后必须从同一命令重新开始，不能把本轮 NOT_RUN manifest 当作连接成功。
- 本轮到此停止，不自动创建 marker、不整理 raw STEP、不运行任何 CFD 软件。

---

## 当前阶段：真实小型端到端 connect-test 与统一连接报告

状态：已完成（真实单命令 tools → Gmsh → SU2_CFD → pvbatch 六段连接均为 PASS）

### 本轮目标

把已经分别真实验证的 Gmsh、SU2 和 ParaView 连接模块串成单一、可重复、失败即停的 `connect-test` 命令：先检查控制 Python、Gmsh Python 模块和全部必需/可选工具，再由 Gmsh Python API 新建二维矩形 smoke 网格，交给 SU2_CFD 串行运行 5 步，最后只从本轮 SU2 manifest 获取可视化文件并由 pvbatch 输出 JSON/CSV。生成机器可读与 Markdown 连接报告。本轮不读取真实 STEP、不运行生产网格、正式 CFD、MPI 或 ParaView GUI。

### 验收标准

- CLI 精确支持 `connect-test --output runs/connection --clean --nproc 1 --timeout 300`；当前质量门只允许 `nproc=1`，timeout 必须为有限正数，输出根必须严格限制为仓库 `runs/connection`；
- `--clean` 只处理连接测试保留的 `tools/`、`gmsh/`、`su2/`、`paraview/`、连接报告和本命令自身记录；删除前验证绝对根目录、拒绝符号链接/目录联接及任何越界目标，不删除根目录内未知文件或项目其它内容；
- 阶段严格为 tools → gmsh → su2 → paraview → report；工具检查记录当前 Python、真实 `import gmsh`、Gmsh CLI、SU2_CFD、pvbatch，以及可选 SU2_SOL/MPI；任一必需项失败立即停止；
- 每个程序阶段复用已经验收的 bridge：Gmsh 在独立目录生成并验证 `smoke.msh/smoke.su2` 与 `farfield`；SU2 只读取本轮新网格，串行执行 5 步并生成 history/VTU；ParaView 只读取本轮 SU2 manifest 的 `visualization_file`，普通 Python 不导入 ParaView；
- 每阶段使用 `runs/connection/tools|gmsh|su2|paraview` 独立目录和独立 manifest；Gmsh 失败不得构造或调用 SU2，SU2 失败不得构造或调用 pvbatch；失败时仍允许在异常处理路径写连接报告，但不得调用任何下游外部程序；
- `connection_report.json` 精确包含六段连接状态和 `overall`，PASS 必须由当前 manifest、文件存在性、当前 SHA-256、marker、迭代数、可视化校验、合法 JSON/CSV 和所有关键命令返回码共同证明；能力帮助探测等非关键警告必须原样保留且不能被后续成功掩盖；
- 六段 evidence 至少保存相关绝对程序路径/版本、实际参数列表命令/返回码、输入输出绝对路径、SHA-256、关键日志行及 UTC 起止时间；`connection_report.md` 与 JSON 状态一致；任何缺失、过期、哈希不符或不可解析证据均不得生成伪 PASS；
- 标准库测试覆盖 CLI、受限清理、严格阶段顺序、工具必需/可选语义、Gmsh/SU2 fail-fast、报告质量门和 Markdown/JSON 输出；先运行短测试，再运行完整测试与真实端到端 connect-test；
- 最终检查全部阶段 manifest、命令日志、关键日志行、输入输出哈希、配置真源哈希和源码静态进程策略；当前目录若仍非 Git 仓库，记录限制并用逐文件审查替代；完成后停止。

### 实施记录

- 2026-08-01：确认此前三段真实连接均已完成：Gmsh 4.15.2 API 生成 994 点/1866 三角形及 `farfield`，SU2_CFD 8.5.0 读取网格并完成 5 步，pvbatch/ParaView 6.2 打开 VTU 并输出 JSON/CSV；完整测试当时为 112/112 PASS。
- 2026-08-01：依次重读 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`；确认 `config/markers.toml` 尚未生成且真实 STEP 不在本轮范围。现有 `connect-test` 仅运行早期 import/CLI 探针，缺少真实工件传递、工具 manifest、安全 clean 和统一报告，故本轮集成验证必要。
- 2026-08-01：把 `connect-test` 改为严格的 tools → gmsh → su2 → paraview 流程；每个阶段直接复用已经验收的 bridge，并只把本轮返回的 manifest/路径交给下游。任何阶段异常立即由 `Pipeline` 截断，报告中未运行阶段明确写 `NOT_RUN_DUE_TO_UPSTREAM_FAILURE`，不读取旧工件伪造 PASS。
- 2026-08-01：增加当前控制 Python、真实 `import gmsh`、Gmsh CLI、SU2_CFD、pvbatch 和可选 SU2_SOL/MPI 的工具 manifest；必需工具解析失败即停。CLI 把输出精确限制在仓库 `runs/connection`，本质量门只接受 `nproc=1` 和有限正 timeout。
- 2026-08-01：实现受管工件账本和安全 `--clean`：逐组件拒绝 symlink/junction/reparse，账本校验 schema/root/规范化去重/size/SHA-256，删除前全量预检并通过同文件系统随机 quarantine 原子搬移，staging 失败整体回滚。首次迁移只处理固定连接工件；`gmsh/pip_install.log`、`su2/discovery/**`、旧 command archive 和未知文件不被接管。
- 2026-08-01：实现六段 JSON/Markdown 报告质量门。报告从当前运行的工具/阶段状态、实际命令、返回码、时间、日志关键行、输入输出及当前哈希交叉验证；开始外部阶段前先落盘 FAIL 报告，最终 PASS 报告成对事务写入，且只有账本成功保存后才允许发布 PASS。
- 2026-08-01：新增端到端纯单元 fixture 与 CLI 回归，覆盖安全 clean/未知文件保留、账本篡改、祖先 reparse、可选工具、Gmsh fail-fast、完整六链 PASS，以及 ParaView 帮助探测非关键超时不能掩盖或阻断真实关键命令。
- 2026-08-01：安全复审确认当前真实普通目录和单进程运行条件下无阻止执行的 P0/P1；随后只运行一次串行小型端到端连接测试。未读取 STEP，未运行 MPI、SU2_SOL、ParaView GUI、生产网格或正式 CFD。

### 验证结果

- Windows 等价命令：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe connect-test --output runs/connection --clean --nproc 1 --timeout 300`，返回码 0；报告六段 `python_to_gmsh`、`gmsh_to_su2_mesh`、`python_to_su2`、`su2_to_paraview_file`、`python_to_pvbatch`、`pvbatch_to_results` 全部为 PASS，`overall=PASS`。
- 工具与 Gmsh：控制 Python 3.13.14 成功导入 Gmsh Python 4.15.2；Gmsh CLI 4.15.2 返回 0。API 生成 994 点、1866 个三角形、120 条 `farfield` 边及 `fluid` 域；`smoke.su2` SHA-256 为 `5050bda713d1afd39d80dc12d3b306211e1e0f2c6ef7d09f7ddd1642ad5a3dd8`。
- SU2：SU2_CFD 8.5.0 以参数列表 `[SU2_CFD.exe, smoke.cfg]` 串行返回 0，明确读取 994 点、1866 三角形、1 个 marker/120 个 `farfield` 边，完成迭代 0～4 共 5 步并记录 `Exit Success (SU2_CFD)`；`history.csv` 和 `connection_solution.vtu` 均存在，VTU SHA-256 为 `9f0d7d66b233b55b25962785c6438ab08339333217a2fc5f87ce3c6ab92c8aa1`。
- ParaView：`pvbatch.exe` 实际最小启动探针、`inspect_solution.py` 和 `smoke_postprocess.py` 三个关键命令均返回 0；ParaView `(6, 2)` 成功打开该 VTU，识别 `Unstructured Grid`、994 点、1866 单元，以及 `Density`、`Momentum`、`Energy`、`Pressure`、`Temperature`、`Mach`、`Pressure_Coefficient`、`Velocity` 八组 point data；输出 inventory JSON、postprocess JSON 和含 1 行数据的 CSV。
- 报告与清理复核：`connection_report.json` SHA-256 为 `397c236a48e97932138f8a024d95e7f6906d3771aa0dd0596fb98cbf43fb88d0`，Markdown 为 `bc0f7528464f7c029c13a3242e3fda27572735cb57b7e269b50d93caf518ce7d`；41 项账本工件逐项重新计算 size/SHA-256，0 个不匹配，全部要求输出存在。clean 移除 33 个旧受管工件、0 警告；`gmsh/pip_install.log`、`su2/discovery/**` 和未知旧记录仍保留。
- 最终完整测试：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`，126/126 通过、0 skipped、0 failures，耗时 3.796 秒；`compileall` 通过，进程策略测试确认只有统一 `CommandRunner` 创建外部进程且显式 `shell=False`，普通控制层没有导入 `paraview.simple`。
- 当前目录仍不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`；无法执行 Git diff，已用逐文件审查、126 项测试、静态进程策略、阶段日志、产物清单和 SHA-256 复核替代。

### 剩余风险与后续前置条件

- 当前 ParaView Windows 构建的 `pvbatch --help` 仍会触发 10 秒能力探测超时并返回 `3221225786`；报告把它保留为 `critical=false` 的 WARN，未使用未经证明的 offscreen 参数。实际 pvbatch 启动、VTU 读取和后处理三个关键命令均独立返回 0。
- SU2 日志保留连接域没有两个压力截面、未启用时间收敛监视、二维 dual-control-volume 正交性统计为 `nan`、单核运行着色效率低四项警告；独立网格/VTU/history 有限性检查和全部关键返回码均通过，因此这些警告没有被隐藏，也不改变本轮程序连接结论。
- 受管输出目录设计为单写者；后续重复执行同一 `--clean` 命令时不得并发启动第二个写入 `runs/connection` 的连接测试进程。
- 本轮只验证小型二维 Euler 程序连接，不代表真实 STEP、边界分类、RANS、y+、MPI、生产网格或物理收敛完成。
- 本轮到此停止，不自动进入真实 STEP、生产网格、正式 CFD 或其它阶段。

---

## 当前阶段：普通 Python → pvbatch → 现有 SU2 解 → JSON/CSV

状态：已完成（真实 pvbatch 启动、现有 SU2 VTU 读取与 JSON/CSV 输出均为 PASS）

### 本轮目标

只复用 `runs/connection/su2/run_manifest.json` 的 `visualization_file`，由普通 Python 通过统一 `CommandRunner` 和参数列表启动 `pvbatch`，让 pvbatch 脚本无界面打开现有 SU2 可视化文件并输出数据清单、积分连接测试 CSV 与完整运行 manifest。本轮不启动新的 SU2、MPI、SU2_SOL、Gmsh 或 ParaView GUI；普通 Python 不导入 `paraview.simple`。

### 验收标准

- `pvbatch` 严格按 CLI、`CFD_PVBATCH`、`config/tools.json`、隔离 PATH 中 `shutil.which("pvbatch")` 的优先级解析，保存绝对路径，继续拒绝静默 Windows 系统目录与未经配置允许的 `/mnt/c/.../*.exe`；不得回退为 `paraview.exe` 或 GUI；
- 在运行数据脚本前，用 `CommandRunner` 分别执行受控帮助探测和最小 pvbatch 脚本；只有帮助文本明确支持时才添加 offscreen 参数，所有命令均为参数列表、`shell=False`，并记录绝对可执行路径、参数、cwd、UTC 起止时间、返回码和原始 stdout/stderr；
- `inspect_solution.py` 只能由 pvbatch 执行，使用 `OpenDataFile` 和 `UpdatePipeline` 打开显式 `.vtk/.vtu/.pvtu/.vtm`，递归统计数据集类型、block/point/cell、bounds，以及 point/cell/field 数组的名称、分量数和可用范围；空数组不伪造，失败保留完整 traceback 并返回非零；
- `smoke_postprocess.py` 只能由 pvbatch 执行，打开同一输入并调用当前版本可用的 `IntegrateVariables`，输出 `smoke_integral.csv` 与 `smoke_postprocess.json`，至少记录总点/单元、bounds 和可积分数组；只验证后处理链路，不做物理解读或复杂可视化；
- 普通 Python 只从用户指定 SU2 manifest 的 `visualization_file` 获取输入，要求 manifest 为 PASS、字段为绝对或相对 manifest 的真实受支持文件，并记录输入和脚本 SHA-256；不得搜索整个项目或改写输入文件；
- CLI 支持并真实执行 `paraview inspect --manifest runs/connection/su2/run_manifest.json --output runs/connection/paraview`，生成 `solution_inventory.json`、`smoke_postprocess.json`、`smoke_integral.csv`、固定 pvbatch stdout/stderr 日志及 `paraview_manifest.json`；任一阶段失败立即停止且 manifest 为 FAIL；
- `paraview_manifest.json` 至少记录 pvbatch 绝对路径/版本/帮助与最小脚本证据、两个脚本路径和 SHA-256、输入路径和 SHA-256、各命令完整参数/返回码、输出文件列表、点/单元数及 PASS/FAIL；点和单元同时为 0 时至少 WARN 并说明原因，本连接数据预期应直接 FAIL；
- 标准库测试覆盖命令构造、manifest 输入解析、缺字段/不存在输入、JSON 解析、timeout、非零返回码、固定日志和静态禁止普通控制层导入 `paraview.simple`；完整测试 0 failure、0 skipped；
- 检查日志、JSON/CSV、哈希、静态进程策略和配置真源哈希后更新本阶段；当前目录若仍不是 Git 仓库，记录无法执行 git diff 并以逐文件审查替代；本轮结束后停止。

### 实施记录

- 2026-08-01：首先确认 Python → Gmsh → SU2_CFD 真实连接阶段为 PASS；现有 SU2 manifest 指向运行目录内 `connection_solution.vtu`，994 点、1866 单元，输入 SHA-256 为 `9f0d7d66b233b55b25962785c6438ab08339333217a2fc5f87ce3c6ab92c8aa1`，本轮只读复用该文件。
- 2026-08-01：依次重读 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`；确认 `config/markers.toml` 尚未生成。开始只读检查现有 ParaView 桥、两个 pvbatch 脚本、CLI、工具解析与本机安装，不运行任何 exe。
- 2026-08-01：定位现有 `C:\Program Files\ParaView 6.2.0\bin\pvbatch.exe`，将该明确绝对路径写入 `config/tools.json`；工具解析继续遵守 CLI、`CFD_PVBATCH`、配置、隔离 PATH 的优先级，并新增 basename 限制，明确拒绝 `paraview.exe`、`pvpython.exe` 或其它 GUI/解释器替代品。
- 2026-08-01：完善普通 Python 桥和 CLI。控制层只读取 SU2 PASS manifest 的 `visualization_file`，校验后缀、文件非空及已有 SHA-256 证据；所有外部进程均经统一 `CommandRunner` 以参数列表和 `shell=False` 启动，逐阶段记录固定日志、命令元数据、超时、返回码及原始 stderr，失败立即停止。
- 2026-08-01：实现三个仅供 pvbatch 解释的脚本：最小启动探针、`OpenDataFile`/`UpdatePipeline` 数据清单、`IntegrateVariables`/`SaveData` 积分连接测试。普通 Python 源码没有导入 `paraview`；脚本不创建视图、不渲染、不打开 GUI，也不修改输入 VTU。
- 2026-08-01：首次真实执行发现当前 Windows 构建的 `pvbatch --help` 不返回；该次运行在任何 ParaView 脚本或 VTU 读取前被精确终止并写出 FAIL 证据。随后把帮助检查限定为 10 秒的可选能力探测：超时即不添加任何 offscreen 参数，但最小脚本启动仍是严格可用性质量门；新增回归测试防止帮助探测阻塞正式连接。
- 2026-08-01：最终按用户指定 CLI 真实执行成功；帮助能力探测按预期超时并保留 WARN，最小启动探针、解文件检查和积分脚本均返回 0。未运行新的 SU2、SU2_SOL、MPI、Gmsh 或 ParaView GUI。

### 验证结果

- 最终命令：`& .\scripts\cfdpipe.ps1 paraview inspect --manifest runs/connection/su2/run_manifest.json --output runs/connection/paraview --timeout 300`，CLI 返回 0，`paraview_manifest.json` 为 PASS、总返回码 0。
- `pvbatch` 绝对路径为 `C:\Program Files\ParaView 6.2.0\bin\pvbatch.exe`，可执行文件 SHA-256 为 `91054bf2a1c0bd078ebd4e8a467fec755fab3e89cc2b34c019867358c0832a94`；启动脚本报告 ParaView 版本 `(6, 2)`，实际最小探针返回 0。
- `OpenDataFile` 成功打开 SU2 生成的 `connection_solution.vtu`：数据集类型 `Unstructured Grid`、1 个 block、994 点、1866 单元、bounds `[0, 1, 0, 0.5, 0, 0]`。检测到 point arrays：`Density`、`Momentum`、`Energy`、`Pressure`、`Temperature`、`Mach`、`Pressure_Coefficient`、`Velocity`；cell/field arrays 为空且未伪造字段，所有可用范围均为有限值。
- `IntegrateVariables` 成功，`smoke_integral.csv` 含表头和 1 行结果；`solution_inventory.json` SHA-256 为 `21ffad4a30a2d4efde556a300034f11f9d10d7b13cf496992206312f0f72a798`，`smoke_postprocess.json` 为 `1a6d6c0c7d31e023f851801d70789f9f63e099934518ba3dde071d644f475549`，CSV 为 `770ae7675d07917fc22bc9e150c9c38e5219c952965d909f9db0fadc80be6d67`。
- 最终 `pvbatch.stdout.log` 与 `pvbatch.stderr.log` 均为 0 字节；所有 ParaView 阶段日志未匹配 `error/fatal/traceback/segmentation/NaN`。帮助探测超时单独记录为非致命能力警告，实际启动、读取和后处理均有独立返回码 0 证据。
- 输入 VTU 运行前后 SHA-256 均为 `9f0d7d66b233b55b25962785c6438ab08339333217a2fc5f87ce3c6ab92c8aa1`，确认未改写；输入严格来自 `runs/connection/su2/run_manifest.json`，没有搜索或误选其它算例。
- 完整测试：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`，112/112 通过、0 skipped、0 failures，耗时 3.589 秒；包含命令构造、manifest 解析、缺失/不存在输入、JSON、timeout、非零返回码、固定日志、帮助超时和普通控制层禁止导入 ParaView 的测试。
- `compileall` 通过，静态扫描未发现 `shell=True` 或 Gmsh GUI 调用；`config/project.toml` 与 `config/cases.csv` SHA-256 仍分别为 `96ec212caea5b76816adb5089ab77b97d77d131461ad854615940d52e4b3bd1c`、`c27cb7b8b70b04fbfcec1f2cef4be66510672377258f09dc13ba4f93af137435`。当前目录不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`，已用逐文件检查、静态策略测试、日志与产物哈希替代 diff。

### 剩余风险与后续前置条件

- 当前 ParaView 6.2.0 Windows 构建的 `pvbatch --help` 在 10 秒内不返回，因此本轮无法从帮助文本证明 offscreen 参数；工作流按规则没有添加该参数。连接脚本不渲染且实际无界面成功，未来若需渲染应另设能力验证，不能凭版本猜测参数。
- 代码支持 `.vtk/.vtu/.pvtu/.vtm`，但本轮真实验证对象仅为 SU2 生成的单块 `.vtu`；多块 `.pvtu/.vtm` 仍需在出现真实输入时单独验收。
- 本轮只处理连接测试数据，不对 Euler smoke 的积分量作物理解释，也不扩展到正式后处理、真实 STEP、RANS、生产网格或新求解。
- 本轮到此停止，不自动进入物理后处理、云图生成或其它 CFD 阶段。

---

## 当前阶段：连接诊断与执行策略收口

状态：已完成（严格诊断收口、真实 Gmsh/SU2 smoke 为 PASS、测试 0 skipped）

### 本轮目标

处理上一阶段尚未闭合的连接层问题，但不跨入新的 CFD 阶段：查明并严格界定 SU2 二维网格质量表中的 `Orthogonality Angle = NaN`；增加防止外部工具绕过统一 `CommandRunner` 的可执行策略检查；消除当前测试套件唯一的平台条件跳过；为 Windows 上失效的 `python3.exe` 别名提供仓库内、无需安装或系统修改的可复现入口。本轮不运行生产网格、正式 CFD、MPI、SU2_SOL、ParaView/pvbatch，也不修改原始 STEP。

### 验收标准

- 先以现有 Gmsh/SU2 manifest、日志、网格文本和源码做只读诊断；不把二维质量统计中的不适用值与求解 history/解场 NaN 混为一谈，也不得静默忽略任何错误；
- 若无法在保持二维三角形、单一 `farfield` 外边界和 `fluid` 域的连接测试约束下消除 SU2 自身的二维统计 `NaN`，必须用独立、有限且可复现的网格几何检查证明单元面积/方向/角度有效，并在 manifest 中保存明确分类与证据；任何其它 NaN/Inf 仍立即 FAIL；
- 所有仓库 Python 外部程序启动必须集中在 `CommandRunner`，使用参数列表与 `shell=False`；增加静态回归测试，防止未来在桥接器、CLI 或脚本中直接使用 `subprocess.run/call/Popen` 绕过审计；
- 修正唯一的 WSL 条件跳过，使 `/mnt/c/.../*.exe` 拒绝与显式许可策略可在 Windows 上通过 mock/纯路径方式完整测试，最终测试 0 skipped；
- 在不安装软件、不修改 Windows App Execution Alias、不修改项目目录外文件的前提下，提供仓库内 Windows CLI 启动入口，明确选择 `.venv\Scripts\python.exe` 并设置 `PYTHONPATH=src`；
- 所有修改使用标准库测试验证；检查日志、产物、源码策略和配置真源哈希，更新本阶段结果与剩余风险；若诊断需要真实短运行，必须先证明 mock/文本检查不足，并只允许现有二维 smoke，禁止 MPI、SU2_SOL、ParaView/pvbatch；
- 本轮结束后停止，不进入真实 STEP、RANS、生产网格、并行求解或后处理阶段。

### 实施记录

- 2026-08-01：依次重读 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`；确认 `config/markers.toml` 尚未生成。
- 2026-08-01：开始三路只读审查：二维 `Orthogonality Angle NaN` 的含义与最小验证方案、外部进程统一入口的防回归策略、平台 skip 与 Windows CLI 入口；审查期间不启动任何 CFD 外部程序。
- 2026-08-01：对 Gmsh 的 SU2 文本网格增加独立几何与拓扑质量检查：逐一解析 1866 个一阶三角形和 994 个点，检查有限坐标、重复点、面积方向、退化单元、角度、归一化质量、边使用次数、非流形边、`farfield` 与真实拓扑边界的一致性及单一闭合边界连通分量。
- 2026-08-01：把 SU2 二维 dual-control-volume 质量表中的唯一正交性 `nan` 改为严格、结构化分类 `SU2_2D_DUAL_ORTHOGONALITY_STAT_UNAVAILABLE`。只有日志位置和文本精确匹配、网格独立质量检查 PASS、stderr 无异常、返回码 0、history 有限且满 5 步、VTU 浮点数组全部有限时才允许最终 PASS；其它 NaN 或数值 `inf/-inf/+inf` 仍立即 FAIL。
- 2026-08-01：增加 VTU ASCII 与 appended-raw 验证，核对点/单元计数，并逐值检查 Float32/Float64 数组；当前真实 VTU 共检查 9 个数组、14,910 个浮点值。
- 2026-08-01：强化 `CommandRunner`：复用固定日志名之前将旧 stdout/stderr/command JSON 复制到带 SHA-256 索引的 `command_archive`；进程启动后的非预期等待/捕获异常会终止进程、尽力排空原始 stderr，并保存完整 runner traceback 与失败元数据。
- 2026-08-01：增加 AST 静态策略测试，确保 `src/` 和 `scripts/` 中只有 `process.py` 的唯一 `subprocess.Popen` 调用可以创建外部进程，且该调用的首参数为列表变量并显式 `shell=False`；SU2_SOL 私有入口增加仅限“当前运行目录内新非空 restart 的可视化回退”前置条件。
- 2026-08-01：修复显式允许 `/mnt/c/.../*.exe` 的纯路径策略，使拒绝和许可分支均可跨平台 dry-run 测试；移除原先唯一的 WSL 条件 skip。增加仓库内 `scripts/cfdpipe.ps1`，固定使用 `.venv\Scripts\python.exe` 并临时设置 `PYTHONPATH=src`，没有修改系统 Python alias 或 PATH。
- 2026-08-01：首次收口复验中，新增的 Inf 扫描把 SU2 正常说明 `values at infinity (freestream)` 误判为数值无穷，工作流按规则写 FAIL 并停止；随后将检测收紧为独立数值 token `inf/-inf/+inf`，增加回归测试后重新执行同一 5 步串行 smoke，最终 PASS。
- 2026-08-01：最终真实复验只运行 Gmsh Python API 小网格及 SU2_CFD 串行 5 步；未启动 MPI、SU2_SOL、ParaView/pvbatch，未读取真实 STEP，未生成生产网格或正式 CFD。

### 验证结果

- Windows 仓库入口：`& .\scripts\cfdpipe.ps1 gmsh smoke --output runs/connection/gmsh` 返回 0；Python 成功导入 `.venv\Lib\site-packages\gmsh.py`，API/CLI 版本均为 4.15.2，生成 994 点、1866 个正向一阶三角形和 120 条 `farfield` 边，manifest 为 PASS。
- 独立网格证据：总面积精确为 0.5 m²，最小三角形面积 `1.6848744472892514e-4 m²`，最小角 `41.502883°`，最大角 `91.953535°`，最小归一化质量 `0.8475526`；负面积、退化、重复坐标、非流形边均为 0，120 条 farfield 边与拓扑外边界完全一致。
- 串行命令：`& .\scripts\cfdpipe.ps1 su2 smoke --mesh runs/connection/gmsh/smoke.su2 --output runs/connection/su2 --nproc 1 --max-iterations 5 --timeout 300` 返回 0；SU2_CFD 8.5.0 明确读取上述 994 点、1866 三角形和 `farfield`，完成迭代 0～4，最终记录 `Exit Success (SU2_CFD)`。
- `run_manifest.json` 为 PASS、`return_code=0`、`iterations=5`、`fatal_matches=[]`、`su2_sol_command=null`、stderr 为 0 字节；history 路径为 `runs/connection/su2/history.csv`，可视化路径为 `runs/connection/su2/connection_solution.vtu`。
- VTU 验证为 `VTU_APPENDED_RAW`，994 点、1866 单元，9 个 Float32 数组、14,910 个值全部有限；SHA-256 为 `9f0d7d66b233b55b25962785c6438ab08339333217a2fc5f87ce3c6ab92c8aa1`。history SHA-256 为 `14e9ea29fa3088fafc1a52c27ce133653c11b186443dc18484cfa1aad8836de5`。
- 最终完整测试：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`，95/95 通过、0 skipped、0 failures，耗时 3.353 秒；MPI 与 SU2_SOL 只做命令/前置条件 mock 测试，没有启动程序。
- `config/project.toml` SHA-256 仍为 `96EC212CAEA5B76816ADB5089AB77B97D77D131461AD854615940D52E4B3BD1C`；`config/cases.csv` 仍为 `C27CB7B8B70B04FBFCEC1F2CEF4BE66510672377258F09DC13BA4F93AF137435`。当前目录不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`，已用逐文件检查、静态策略测试、日志和产物哈希替代 diff。

### 剩余风险与后续前置条件

- SU2 8.5.0 自身仍会为这一纯二维网格打印 dual-control-volume 正交性最小/最大值 `nan`；它没有出现在 history、stderr 或解场数组中，现已严格分类而非删除或静默忽略。后续任何不满足精确上下文和独立质量证据的 NaN/Inf 都会失败。
- 当前结果只证明 Python → Gmsh Python API → SU2 网格 → SU2_CFD 串行小型 Euler 连接，不代表真实 STEP、边界分类、RANS、y+、生产网格、MPI 兼容性或物理收敛已经验证。
- WindowsApps 的 `python3.exe` 系统别名没有也不应由仓库修改；Windows 复现使用 `scripts/cfdpipe.ps1` 或显式 `.venv\Scripts\python.exe`。Linux/WSL 仍可使用用户要求的 `PYTHONPATH=src python3 -m cfdpipe ...` 形式。
- 历史上已发生的无参数 SU2_SOL 误探测无法撤销；最终工作流和本轮复验均未调用 SU2_SOL，新增前置条件与静态策略测试防止其被随意绕过。
- 本轮到此停止；不得自动进入 ParaView/pvbatch、真实 STEP、RANS、生产网格、MPI 或正式 CFD 阶段。

---

## 当前阶段：Python → SU2_CFD，Gmsh smoke.su2 → SU2_CFD

状态：已完成（真实串行 SU2_CFD 五步连接算例为 PASS）

### 本轮目标

只实现并验证普通 Python 通过安全参数列表启动当前已安装的 `SU2_CFD`，让上一阶段由 Gmsh Python API 生成的 `runs/connection/gmsh/smoke.su2` 被 SU2 实际读取，并完成 5 步二维均匀自由流 Euler 连接算例；生成 history、至少一个 ParaView 可读解文件和完整运行 manifest。本轮不调用 ParaView/pvbatch、不导入真实 STEP、不运行正式 CFD。

### 验收标准

- 按 CLI、`CFD_SU2_CFD`、`config/tools.json`、`shutil.which("SU2_CFD")` 的优先级解析 `SU2_CFD`；以相同策略解析 `SU2_SOL` 和 `mpirun`/`mpiexec`，保存并验证绝对路径，拒绝静默 Windows 系统目录或 `/mnt/c/.../*.exe`；
- 只读搜索当前 SU2 安装自带的 `config_template.cfg`、QuickStart、TestCases、tutorial 或 example 配置，记录实际参考配置绝对路径和 SHA-256；配置键必须由当前安装证据确认，不凭记忆猜测；
- 使用 Gmsh 阶段已通过校验的二维 `smoke.su2`，复制或链接到 `runs/connection/su2/smoke.su2`，配置中仅写相对 `MESH_FILENAME= smoke.su2`；
- 生成当前版本支持的二维 Euler、Mach 0.3、AOA 0、侧滑 0、`MARKER_FAR=( farfield )`、5 次迭代连接配置，并请求 CSV history 与 ParaView 可读输出，文件前缀统一为 `connection_solution`；
- `run_su2(config_path, run_directory, nproc=1, timeout_seconds=300)` 首次必须串行执行 `[SU2_CFD, smoke.cfg]`；只有 `nproc>1` 才构造 `[mpirun/mpiexec, -np, nproc, SU2_CFD, smoke.cfg]`，本轮不实际启动 MPI；
- 所有外部命令继续由 `CommandRunner` 以 `subprocess.Popen(..., shell=False)` 启动，实时保存 stdout/stderr、cwd、起止时间、参数和返回码；超时安全终止后强制终止，非零返回码或致命日志模式均为 FAIL；
- 求解后只在本轮运行目录内选择新生成的 `.vtk/.vtu/.pvtu/.vtm`；若必须调用当前安装的 `SU2_SOL` 才能生成可视化文件，则同样记录命令与日志，但不得调用 ParaView；
- `run_manifest.json` 至少记录 SU2_CFD 绝对路径、版本证据、参数、cwd、参考配置路径/SHA-256、配置与网格 SHA-256、返回码、迭代数、history 路径、`visualization_file`、起止时间和 PASS/FAIL；
- CLI 支持并真实运行 `su2 smoke --mesh runs/connection/gmsh/smoke.su2 --output runs/connection/su2 --nproc 1 --max-iterations 5 --timeout 300`，生成用户指定的配置、日志、history、manifest 和可视化文件；
- 标准库测试覆盖命令构造、串行、MPI 构造但不启动、timeout、非零返回码、manifest、可视化选择和 `shell=False`；完整测试全部通过；
- 检查日志、产物、源真值哈希与 Git 状态，更新本阶段结果和剩余风险；本轮完成后停止，不调用 pvbatch、ParaView、真实 STEP 或正式 CFD。

### 实施记录

- 2026-08-01：首先确认上一阶段真实 Gmsh manifest 为 PASS：Python API/CLI 4.15.2，`smoke.su2` 含 994 个节点、1866 个二维三角形和 120 个 `farfield` 边界单元。
- 2026-08-01：依次重读 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`；确认 `config/markers.toml` 尚未生成。
- 2026-08-01：开始只读检查现有 SU2 桥、统一命令运行器、工具路径配置、本机 SU2/MPI 安装和版本自带配置；在语法证据确认前不生成或启动算例。
- 2026-08-01：定位并验证现有 `SU2_CFD.exe`/`SU2_SOL.exe` 位于 SU2 8.5.0 `win64-omp` 包；`SU2_CFD --help` 返回码 0 并给出 `Release 8.5.0 "Harrier"`。同时定位 Intel MPI `mpiexec.exe`，本轮只记录绝对路径，未启动 MPI。
- 2026-08-01：当前安装包没有独立 `config_template.cfg`、QuickStart、TestCases 或 tutorial；选用安装包内 `FADO\examples\example4_SU2\data.zip::configFlow.cfg` 作为 Euler 配置主证据，并用同包 `SU2\io\config.py` 与 `SU2_CFD.exe` 支持字符串交叉核对用户要求的键和值；未修改任何系统模板。
- 2026-08-01：更新 `config/tools.json` 中现有 SU2_CFD、SU2_SOL 和 mpiexec 的显式绝对路径；工具解析继续采用 CLI、环境变量、配置、`shutil.which` 优先级，并为 MPI PATH 回退依次检查 `mpirun`、`mpiexec`。
- 2026-08-01：完善统一 `CommandRunner` 的固定日志路径支持，使实际求解实时写入 `solver.stdout.log`、`solver.stderr.log` 和 `solver.command.json`；路径被限制在本次输出目录内，唯一进程启动点继续显式使用参数列表与 `shell=False`。
- 2026-08-01：实现真实 SU2 smoke 桥：先严格复核 Gmsh SU2 文本，按哈希一致方式复制为运行目录相对 `smoke.su2`；生成二维 Euler/Mach 0.3/AOA 0/侧滑 0/`farfield`/CSV history/PARAVIEW 输出配置；串行直接启动 SU2_CFD，仅 `nproc>1` 才构造 `-np` MPI 命令。
- 2026-08-01：实现返回码、有限 timeout、原始 stderr、致命日志、history 有限值/迭代数和本轮新生成可视化文件检查；只有本轮产生非空 restart 且没有 `connection_solution*` 可视化时才允许生成专用 `su2_sol.cfg` 并回退，SOL 的命令、返回码和原始 stderr 均单独保存。
- 2026-08-01：第一次真实 SU2_CFD 运行已成功读取网格且返回 0，但均匀流默认在 2 步提前收敛；桥按验收规则写出 FAIL manifest 并停止。随后依据当前安装已证实的 `CONV_RESIDUAL_MINVAL` 与 `CONV_STARTITER` 强制完整执行 5 步。
- 2026-08-01：SU2 对此纯二维网格的双控制体质量表打印 `Orthogonality Angle ... nan ... nan`，但其解场残差、history、网格方向和进程退出均正常。实现只对这一精确质量表行作非致命例外并写入 `nonfatal_diagnostics`；任何其它 NaN/Inf 仍判为致命错误。
- 2026-08-01：最终审查移除了 Python 中原有的压力、温度、比热比、气体常数和记忆式数值默认值；smoke 配置只发出当前安装逐项有证据的最小键。首轮最小配置由 SU2 明确返回缺少 `CONV_NUM_METHOD_FLOW`，程序以 FAIL 停止；随后仅从安装自带 Euler 配置读取其值 `JST`，没有恢复物理常数硬编码。
- 2026-08-01：串行 CLI 对 SU2_SOL/MPI 改为可选定位，显式无效路径仍立即报错；只有 `nproc>1` 才强制要求 MPI。可视化选择收紧为运行目录直属、本轮新增或变化、非空且以 `connection_solution` 开头的文件。
- 2026-08-01：审查安装目录配置时曾误把一个无参数 `SU2_SOL.exe` 探测并入只读搜索命令；该进程立即以“缺少 .cfg”退出，未读取网格/解、未生成文件、未调用 ParaView。此操作不属于最终工作流且违反了“不必要时不调用 SU2_SOL”的执行边界，已当场向用户说明，之后未再次启动 SU2_SOL。
- 2026-08-01：最终按用户参数以仓库 `.venv\Scripts\python.exe` 执行串行命令，返回 0、manifest 为 PASS；最终自动工作流未启动 MPI、SU2_SOL、ParaView 或 pvbatch。

### 验证结果

- 最终实际命令：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m cfdpipe su2 smoke --mesh runs/connection/gmsh/smoke.su2 --output runs/connection/su2 --nproc 1 --max-iterations 5 --timeout 300`，CLI 返回码 0。
- `solver.stdout.log` 明确记录输入 `smoke.su2`、二维、994 个节点、1866 个体三角形、1 个 surface marker、120 个 `farfield` 边界单元，且体/边界单元方向全部正确；迭代表为 0、1、2、3、4，最后记录 `ITER = 5` 与 `Exit Success (SU2_CFD)`。
- `run_manifest.json` 为 `PASS`：SU2 版本 8.5.0，求解命令仅为绝对 `SU2_CFD.exe` 加相对 `smoke.cfg`，cwd 为本轮运行目录，返回码 0，`iterations=5`，`fatal_matches=[]`，未超时。
- 参考配置为 `FADO\examples\example4_SU2\data.zip::configFlow.cfg`，内容 SHA-256 为 `3dcc9385847fc7be5f8af169c54c638f4e92b23ed41a614342e1d1ebc83d177e`；解析器证据 `SU2\io\config.py` SHA-256 为 `6d99924efd0918b49028f1c286920865a42dd2ee8812fbddf78ebd277fb6fc4a`；SU2_CFD SHA-256 为 `3cb60646b31c08e468441be9f3497601960d4bb31349e6329982bcdeed599248`。
- `runs/connection/su2/smoke.cfg` 为 555 字节，SHA-256 为 `0a363486a108132170ccbe051b9ccb053d37c3991cd79c15400f147772e7a110`，其中 `MESH_FILENAME= smoke.su2` 为相对路径且不含 Python 硬编码热力学常数；源网格与运行副本 SHA-256 均为 `5050bda713d1afd39d80dc12d3b306211e1e0f2c6ef7d09f7ddd1642ad5a3dd8`。
- `history.csv` 为 690 字节、`Inner_Iter=0,1,2,3,4` 共 5 行，SHA-256 为 `14e9ea29fa3088fafc1a52c27ce133653c11b186443dc18484cfa1aad8836de5`，桥内及独立检查均未发现 NaN/Inf；`connection_solution.vtu` 为有效 VTK UnstructuredGrid，含 994 点、1866 单元，大小 92,972 字节，SHA-256 为 `9f0d7d66b233b55b25962785c6438ab08339333217a2fc5f87ce3c6ab92c8aa1`。
- 固定日志均存在：`solver.stdout.log` 14,567 字节，`solver.stderr.log` 0 字节，`solver.command.json` 完整记录绝对程序路径、参数列表、cwd、起止时间、返回码和日志路径；原始 stderr 未被替换或忽略。
- 完整测试：`$env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`，共发现 82 项，81 项通过、1 项仅适用于 WSL 的条件测试跳过、0 失败，耗时 2.288 秒。SU2 专项 22 项全部通过，MPI 与 SU2_SOL 回退测试只使用 mock，没有启动程序。
- `compileall` 通过；静态检查未发现 `shell=True`、Gmsh GUI 或普通 Python 导入 `paraview.simple`。最终运行 manifest 的 `su2_sol_command=null`，运行目录中只有 SU2_CFD 的版本查询与串行求解记录，没有 MPI、SU2_SOL、ParaView/pvbatch 命令。
- `config/project.toml` SHA-256 仍为 `96EC212CAEA5B76816ADB5089AB77B97D77D131461AD854615940D52E4B3BD1C`；`config/cases.csv` 仍为 `C27CB7B8B70B04FBFCEC1F2CEF4BE66510672377258F09DC13BA4F93AF137435`。当前目录仍非 Git 仓库，无法生成 `git diff`，已用逐文件、测试、日志和产物哈希审查替代。

### 剩余风险与后续前置条件

- 当前连接质量门只证明 Gmsh 小网格可被 SU2_CFD 8.5.0 串行读取并完成 5 步 Euler；不代表物理收敛、RANS 边界条件、生产网格或正式 CFD 已验证。
- SU2 对纯二维 smoke 网格输出的双控制体 `Orthogonality Angle` 最小/最大值为 NaN；该精确诊断已完整保留。它没有进入 history 或解场，也没有阻止连接测试，但后续正式网格必须按仓库网格质量门单独校核，不能沿用此例外推断生产质量。
- 当前 SU2 包是 `win64-omp`；MPI 只完成了 `-np` 参数构造测试，Intel MPI 与此 SU2 构建的运行兼容性尚未验证，后续并行阶段必须另设质量门。本轮没有启动 MPI。
- SU2SOL 回退代码已由 mock 覆盖，但未用有效配置实际处理 restart；最终 workflow 不需要它，因为 SU2_CFD 已直接生成 VTU。审查期间的无参数误探测已记录为执行偏差，不能作为回退连接成功证据；未来若确需回退，必须单独获得授权并验证。
- `config/tools.json` 使用 Downloads/Program Files 中的显式绝对路径；目录移动后必须通过 CLI、环境变量或配置重新给出经验证路径，解析器不会静默选择其它 Windows exe。
- 当前机器的 WindowsApps `python3.exe` 别名失效；Windows 可复现命令继续使用仓库 `.venv\Scripts\python.exe` 作为等价解释器。
- 本轮到此停止；不得自动进入 ParaView/pvbatch、真实 STEP、RANS、生产网格或正式 CFD 阶段。

---

## 当前阶段：Python → Gmsh Python API → SU2 网格文件

状态：已完成（真实 Python API 连接与 SU2 网格导出均为 PASS）

### 本轮目标

真实接通普通 Python 与 Gmsh Python API，使用 API 创建 1.0 m × 0.5 m 的二维三角形连接测试网格，同时写出 Gmsh MSH 与 SU2 网格文件并验证 SU2 文本结构。补齐可配置的 STEP 网格入口，但本轮不导入真实 STEP、不生成生产网格，也不运行 SU2 或 ParaView。

### 验收标准

- 先确认当前 Python、环境变量、`config/tools.json`、PATH 及本机已有 Gmsh SDK/其他 Python 环境的状态；依据用户在 2026-08-01 的明确授权，优先在仓库内创建隔离 Python 环境并安装与现有 Gmsh CLI 兼容的官方 Python SDK，不覆盖系统 Python 或已有 Gmsh；
- Gmsh 生命周期采用 `import gmsh`、`gmsh.initialize()`、`try/finally`、`gmsh.finalize()`，不调用 `gmsh.fltk.run()`；初始化失败时记录完整 traceback、模块路径，并停止后续操作；
- 记录 Gmsh Python 版本、模块绝对路径、Gmsh CLI 绝对路径及 CLI 版本；CLI 只允许用参数列表查询版本，不得用于生成网格；
- 使用 Gmsh Python API 创建 1.0 m × 0.5 m 的二维矩形域、调用 synchronize、生成约 1000～5000 个三角形单元；整个外边界命名为 `farfield`，二维区域命名为 `fluid`；
- 由 `gmsh.write` 生成 `runs/connection/gmsh/smoke.msh` 与 `runs/connection/gmsh/smoke.su2`，并生成 `gmsh_manifest.json`、`gmsh.log`；manifest 包含版本、模块路径、输入、节点数、单元数、marker、SHA-256 和 PASS/FAIL；
- SU2 文本检查器验证非空、无 NaN/Inf、`NDIME=2`、`NELEM/NPOIN/NMARK > 0`、`farfield` 存在且 `MARKER_ELEMS > 0`；失败使 CLI 返回非零；
- `build_step_mesh(step_path, output_directory, marker_mapping, mesh_options)` 在导入前设置 `Geometry.OCCTargetUnit = "M"`，使用 OCC `importShapes` 与 `synchronize`，marker 全由外部映射提供且必须命名，支持 `mesh.msh/mesh.su2`；本轮只实现接口并用 mock 测试，不调用真实 STEP；
- `PYTHONPATH=src python3 -m cfdpipe gmsh smoke --output runs/connection/gmsh`（当前 Windows 可使用等价解释器）真实成功且生成四个指定输出；
- 标准库测试覆盖初始化/关闭、矩形网格、Physical Group 名称、SU2 文本解析、输出文件、异常 finalize；随后执行完整 `unittest discover`；
- 原始 STEP、`config/project.toml`、`config/cases.csv` 保持不变，`config/markers.toml` 不生成；不运行 SU2、MPI 或 ParaView；
- 检查日志、输出和代码边界，更新本阶段记录与剩余风险；当前目录若仍非 Git 仓库，则继续记录无法生成 `git diff` 的限制。

### 实施记录

- 2026-08-01：依次重读 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`；确认 `config/markers.toml` 尚未生成。
- 2026-08-01：当前 `D:\Python\python.exe` 为 Python 3.14.0a1，`importlib.util.find_spec("gmsh")` 返回 `null`；`CFD_GMSH` 未设置，`config/tools.json` 中 `gmsh` 为 `null`，PATH 未发现 `gmsh` 可执行程序。
- 2026-08-01：用户将本阶段目标扩展为真实生成二维连接测试网格及 SU2 文本验证；重新探测后当前 Python、PATH、环境变量与 `tools.json` 状态仍未变化，开始检查本机其他已有 Python/Gmsh 运行时。
- 2026-08-01：只读检查了本机现有 Python 3.14、3.13、3.12、3.11 与 WSL Python；均无法找到或导入 `gmsh`。现有 Gmsh ZIP 仅包含 CLI，没有 `gmsh.py`、Python wheel 或 `libgmsh.dll`，因此不能在不安装/获取 SDK 的前提下建立真实 Python API 会话。
- 2026-08-01：发现并验证已有 Gmsh 4.15.2 CLI：`<LOCAL_GMSH_DIR>\gmsh.exe`，将其显式写入被 Git 忽略的 `config/tools.json`；CLI SHA-256 为 `317C43391E5B1FAB3A1DD80DC5245DAD6E2D087910F4B8EBC234BD6D4B8F41A1`。CLI 仅以参数列表执行 `--version`，没有用于建网格。
- 2026-08-01：完善 `gmsh_bridge.py`：直接、局部 `import gmsh`；完整初始化/终止生命周期；二维 OCC 矩形、`farfield`/`fluid` Physical Group、纯一阶三角形检查、双格式 `gmsh.write`、SHA-256 manifest、完整失败 traceback 与 Gmsh logger；未包含 GUI 调用。
- 2026-08-01：实现严格 SU2 文本检查器，以及采用 `Geometry.OCCTargetUnit="M"`、OCC `importShapes`、外部 marker mapping/mesh options 的 `build_step_mesh` 接口；接口拒绝空名称、修剪后重名及同维实体重复归属。本轮仅使用 mock STEP fixture 测试，未读取或导入真实 STEP。
- 2026-08-01：完善 `gmsh smoke --output ...` CLI；输出限制在 `runs/connection/` 内，CLI 版本探测或 API 任一阶段失败都会保存 FAIL 记录并立即停止。
- 2026-08-01：按指定 `python3` 形式执行时，本机 WindowsApps `python3.exe` 失效并返回 9009；使用同机可用的 `D:\Python\python.exe` 执行等价命令后，Gmsh CLI 版本探测成功，随后在直接 `import gmsh` 处以返回码 1 停止。
- 2026-08-01：用户明确授权解决缺少 Gmsh Python SDK 的阻塞；本轮只允许为该连接建立仓库内隔离运行时并重跑小型 smoke，不扩展到真实 STEP、SU2 或 ParaView。
- 2026-08-01：选择本机稳定的 64 位 Python 3.13.14，在仓库内创建 `.venv`；未覆盖系统 Python、既有 Gmsh CLI 或其他 CFD 软件。
- 2026-08-01：从官方 Python 包索引安装精确匹配的 `gmsh==4.15.2` Windows x64 wheel；新增 `requirements-gmsh.txt` 固定版本及 wheel SHA-256，并用 `.gitignore` 排除隔离环境和 Python 缓存。`pip check` 报告 `No broken requirements found`。
- 2026-08-01：真实 `import gmsh`、`initialize`、`finalize` 成功；Python distribution/API 均为 4.15.2，模块为 `.venv\Lib\site-packages\gmsh.py`，加载库为 `.venv\Lib\gmsh-4.15.dll`。
- 2026-08-01：使用隔离解释器执行 `python -m cfdpipe gmsh smoke --output runs/connection/gmsh`，返回码 0；未调用 Gmsh GUI，网格创建与 MSH/SU2 写出全部由 Python API 完成。

### 验证结果

- Gmsh 专项测试：`$env:PYTHONPATH=(Resolve-Path src).Path; python -m unittest tests.test_gmsh_bridge -v`，13/13 通过；覆盖生命周期、矩形网格、Physical Group 名称、SU2 解析、四个输出、异常 finalize、CLI 失败即停及 STEP 外部映射。
- 完整短测试：`$env:PYTHONPATH=(Resolve-Path src).Path; python -m unittest discover -s tests -v`，共 56 项，55 项通过、1 项仅适用于 WSL 的条件测试跳过、0 失败，耗时 1.651 秒。SU2 与 ParaView 测试均为 mock，没有启动外部程序。
- 授权安装 SDK 前的真实命令返回 1；当时的 FAIL manifest/log 完整保留了 `ModuleNotFoundError: No module named 'gmsh'` traceback，并确认程序在导入失败后立即停止。该失败产物随后被授权修复后的 PASS 产物替代。
- `config/project.toml` SHA-256 仍为 `96EC212CAEA5B76816ADB5089AB77B97D77D131461AD854615940D52E4B3BD1C`；`config/cases.csv` 仍为 `C27CB7B8B70B04FBFCEC1F2CEF4BE66510672377258F09DC13BA4F93AF137435`；只读原始 STEP 仍为 `D7D1CBEC76CCC6B45AAD5689D61A28C41E1BA41B9CDA6C90D46EB6FF891C1A6B`。
- 当前目录仍不是 Git 仓库，`git status --short` 返回 `fatal: not a git repository`；已用逐文件、静态调用边界和产物检查代替 diff。
- 授权修复后的真实 smoke：`gmsh_manifest.json` 为 `PASS`；Python API 与 CLI 版本均为 4.15.2；节点数 994、二维一阶三角形 1866、`farfield` 边界单元 120。
- 真实输出均存在且非空：`smoke.msh` 78,362 字节，SHA-256 `66c7d144732ce5c2919461c2b03a6919ddffd182c1d69442f53c05dd309f1565`；`smoke.su2` 76,103 字节，SHA-256 `5050bda713d1afd39d80dc12d3b306211e1e0f2c6ef7d09f7ddd1642ad5a3dd8`；实算哈希与 manifest 一致。
- SU2 文本独立复核通过：`NDIME=2`、`NELEM=1866`、`NPOIN=994`、`NMARK=1`、`MARKER_TAG=farfield`、`MARKER_ELEMS=120`，域单元类型全部为 SU2 三角形代码 5，未发现 NaN/Inf。
- Gmsh 日志无错误或警告且以 `status: PASS` 结束；最新 CLI `--version` 命令元数据包含绝对路径、参数、cwd、起止时间、返回码 0、stdout 日志和 0 字节 stderr 日志。
- 隔离解释器完整测试：`$env:PYTHONPATH=(Resolve-Path src).Path; .\.venv\Scripts\python.exe -m unittest discover -s tests -v`，共 56 项，55 项通过、1 项仅适用于 WSL 的条件测试跳过、0 失败，耗时 1.714 秒。

### 剩余风险与后续前置条件

- 本机 WindowsApps `python3.exe` 别名仍失效；仓库的可复现 Windows 命令必须显式使用 `.venv\Scripts\python.exe`。删除隔离环境后可用稳定 Python 3.13 和 `requirements-gmsh.txt` 重建。
- `config/tools.json` 中 CLI 仍是 Downloads 下的显式绝对路径；若该目录被移动，必须通过命令行、`CFD_GMSH` 或配置重新给出经验证的绝对路径，工具解析器不会静默降级。
- 当前目录没有 Git 元数据，无法生成 `git diff`；本轮以逐文件审查、源真值哈希、命令日志、产物哈希和独立文本检查替代。
- 本质量门仅证明 Python → Gmsh Python API → 小型 SU2 网格文件连接成功；没有导入真实 STEP，也没有运行 SU2、MPI、ParaView 或正式 CFD。下一阶段不得自动开始。

---

## 当前阶段：Python、Gmsh、SU2 与 ParaView 程序连接层

状态：已完成（当前目录未初始化 Git，已用逐文件与静态审查替代 `git diff`）

### 本轮目标

仅建立可测试的程序连接骨架：由普通 Python 直接导入 Gmsh；由 `pvbatch` 解释执行 ParaView 脚本；由无 `shell=True` 的参数列表启动 SU2/SU2 MPI；并通过统一 `CommandRunner` 记录完整、可追溯的命令执行信息。所有连接测试产物统一写入 `runs/connection/`。

### 验收标准

- `src/cfdpipe/`、`src/cfdpipe/bridges/`、`scripts/paraview/`、`config/tools.json` 与指定的五个标准库 `unittest` 文件存在；
- CLI 提供 `tools check`、`gmsh smoke`、`gmsh step`、`su2 smoke`、`su2 run`、`paraview inspect`、`connect-test` 和 `pipeline run` 子命令；
- 工具路径严格按“命令行参数、环境变量、`config/tools.json`、`shutil.which`”解析，拒绝未显式允许的 `/mnt/c/.../*.exe`，且不静默选择 Windows 目录中的可执行文件；
- 统一命令运行器使用参数列表与 `shell=False`，支持 `cwd`、环境变量、超时、stdout/stderr 独立日志、实时输出、返回码检查及 dry-run，并记录绝对可执行路径、参数、工作目录、起止时间与返回码；
- Gmsh 桥仅验证普通 Python 的 `import gmsh` 连接，不导入或运行真实 STEP；ParaView 桥不在普通 Python 中导入 `paraview.simple`；SU2 桥仅通过子进程调用 `SU2_CFD`，且只在 `nproc > 1` 时使用 MPI；
- 任一流水线阶段失败即停止，下游不再调用，原始 stderr 保留；
- 从仓库根目录运行 `PYTHONPATH=src python3 -m unittest discover -s tests -v`（Windows 当前终端使用等价环境变量设置）全部通过；测试只使用临时假程序或 mock，不启动真实 Gmsh、SU2、MPI、ParaView、正式网格或 CFD；
- 检查测试日志和输出、更新本阶段记录，并复核所有修改；不修改只读原始 STEP 和已有配置真源。

### 实施记录

- 2026-08-01：依次读取 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv`，确认 `config/markers.toml` 尚未生成。
- 2026-08-01：检查仓库结构；目标连接层文件均尚未创建，未发现需要保留或合并的同名实现。
- 2026-08-01：确认当前目录不是 Git 仓库；本轮将以逐文件内容检查替代 `git diff`，并将缺少 Git diff 能力保留为风险。
- 2026-08-01：创建 `cfdpipe` 包、统一 `CommandRunner`、严格工具路径解析器、fail-fast 流水线、Gmsh/SU2/ParaView 三个桥接器、两个 `pvbatch` 脚本及标准库单元测试。
- 2026-08-01：`CommandRunner` 仅接受绝对可执行路径，显式使用 `shell=False`，并记录命令参数、cwd、UTC 起止时间、返回码及原始字节 stdout/stderr；支持环境覆盖、实时输出、超时、返回码检查和 dry-run。POSIX 使用新 session/进程组清理，Windows 使用新进程组、`CTRL_BREAK_EVENT` 与有界 kill 回退，输出线程等待有上限。
- 2026-08-01：工具路径严格按 CLI、环境变量、`config/tools.json`、隔离 `PATH` 中的 `shutil.which` 解析；高优先级无效时不降级；拒绝静默 Windows 系统目录可执行文件；默认在规范化前后拒绝 `/mnt/c/.../*.exe`，只接受 `tools.json` 中的显式许可。
- 2026-08-01：Gmsh smoke 仅在方法内部普通导入 `gmsh`，不打开 STEP，并避免初始化或终止调用方已有的 Gmsh 会话；STEP 入口明确停止并报告本阶段未实现。
- 2026-08-01：SU2 仅由 `CommandRunner` 启动 `SU2_CFD`，串行时不使用 MPI，仅 `nproc > 1` 时构造 `mpiexec -n ... SU2_CFD ...`；ParaView 普通控制层不导入 ParaView，所有后处理脚本均由 `pvbatch` 参数列表启动。
- 2026-08-01：CLI 已提供 `tools check`、`gmsh smoke`、`gmsh step`、`su2 smoke`、`su2 run`、`paraview inspect`、`connect-test` 和 `pipeline run`；连接输出目录固定为仓库内 `runs/connection/`。
- 2026-08-01：补充 CLI 与 Pipeline 回归测试；Pipeline 失败测试确认调用顺序为 `first → fail`，未执行 downstream，并保留原始异常为 `cause`。
- 2026-08-01：按用户指定的 `python3` 形式两次尝试完整测试；本机 `<WINDOWS_USER_PROFILE>\AppData\Local\Microsoft\WindowsApps\python3.exe` 为空的失效别名，均返回 1 且无输出。随后使用同机可用的 `<LOCAL_PYTHON>\python.exe`（Python 3.14.0a1）执行 Windows 等价命令完成验证。
- 2026-08-01：最终单元测试共 47 项，46 项通过、1 项仅适用于 POSIX/WSL 的 `/mnt/c` 显式许可测试在 Windows 按条件跳过；耗时 1.441 秒。
- 2026-08-01：最终 `connect-test` 与 `pipeline run` 均以 dry-run 完成 `gmsh-import → su2-cfd → paraview-pvbatch` 三阶段；未导入真实 Gmsh，未启动真实 SU2、MPI 或 ParaView。
- 2026-08-01：对 19 个 Python 文件完成 AST 解析和调用策略检查；唯一真实 subprocess 启动点为 `process.py` 中的 `Popen(..., shell=False)`；ParaView 导入只存在于两个 `scripts/paraview/` 脚本；未发现 GUI 或网络调用。
- 2026-08-01：检查 `runs/connection/` 中 10 份永久命令记录；全部为 dry-run、返回码均为 0，引用的 stdout/stderr 日志均存在，stderr 日志均为 0 字节。

### 验证结果

- 单元测试：`$env:PYTHONPATH=(Resolve-Path 'src').Path; python -m unittest discover -s tests -v`，结果为 47 项、46 通过、1 项平台条件跳过、0 失败。
- 指定命令兼容性：源码支持 `PYTHONPATH=src python3 -m cfdpipe ...`；当前机器的 `python3` WindowsApps 别名不可运行，等价 `python -m cfdpipe ...` 的 CLI 帮助树、版本入口和全部叶子命令解析测试通过。
- 最终 dry-run 流水线记录：`runs/connection/connect-test-4344-7fb6c2b86c.json` 与 `runs/connection/pipeline-run-22324-cd658205e2.json`，三阶段均标记完成；命令元数据包含绝对路径、参数、cwd、时间、返回码和日志路径。
- 失败即停记录：`runs/connection/fail-fast-selftest-28344-0c5cad6285.json` 只包含已完成的 `first` 和失败的 `fail`，没有 downstream；自动单元测试重复验证该行为。
- `config/project.toml` SHA-256 仍为 `96EC212CAEA5B76816ADB5089AB77B97D77D131461AD854615940D52E4B3BD1C`；`config/cases.csv` SHA-256 仍为 `C27CB7B8B70B04FBFCEC1F2CEF4BE66510672377258F09DC13BA4F93AF137435`。
- 原始 `Model1-计算区域.STEP` 未被连接代码导入、处理或修改；仅为完整性核对执行只读哈希检查。文件仍为只读，大小 2,553,767 字节，SHA-256 为 `D7D1CBEC76CCC6B45AAD5689D61A28C41E1BA41B9CDA6C90D46EB6FF891C1A6B`。
- Git 检查：`git status --short` 返回 `fatal: not a git repository`；已逐文件确认目标文件、静态边界、日志和配置哈希，但无法生成仓库级 diff。

### 剩余风险与后续前置条件

- `config/tools.json` 当前有意使用空路径占位；按本轮限制未连接或查询真实 Gmsh、SU2、MPI、ParaView 版本。下一轮实际连接测试前必须由 CLI、环境变量或配置给出受验证的绝对路径。
- 两个 ParaView 脚本只完成静态解析和 mock 桥接测试，尚未在目标 `pvbatch` 版本中执行；实际版本 API 兼容性仍待连接测试确认。
- Gmsh Python 模块只做了 mock 单测；实际 `import gmsh`、初始化和版本查询仍待连接测试确认，但不得在该确认中进入 STEP 导入或几何处理。
- Windows Python 标准库不提供 Job Object 树终止接口；当前实现保证新进程组、软中断、直接进程 kill 和输出线程等待均有界，但主动脱离进程组的更深后代仍可能需要未来的受控平台适配。
- 当前目录没有 Git 元数据，无法满足字面意义上的 `git diff` 质量门；若后续建立 Git 基线，应先审查并纳入本轮文件，再继续下一阶段。
- 本机 `python3` App Execution Alias 无效；在不安装或修改系统软件的前提下，本轮只能使用 `python` 等价命令。早期检查生成的 `__pycache__` 仍在仓库内，清理操作被当前执行策略拒绝，未尝试绕过。
- 本轮到此停止；未进入真实 Gmsh/STEP 连接、网格或 CFD 阶段。

---

## 当前阶段：工况清单

状态：已完成

### 本轮目标

将用户提供的 5 个马赫数、高度、攻角、侧滑角和角色组合保存为 `config/cases.csv`，作为工况配置真源。

### 验收标准

- `config/cases.csv` 存在，表头及 5 行记录与用户提供内容一致；
- CSV 可由 Python 标准库读取，所有行列数正确且 `case_id` 唯一；
- 数值字段可解析为预期数值类型；
- `project.toml` 指定的设计点和网格无关性工况均存在且角色为 `design`；
- 不修改项目常数配置或只读原始 STEP 文件。

### 实施记录

- 2026-08-01：按顺序读取 `AGENTS.md`、`PLAN.md`、`config/project.toml`，确认 `config/cases.csv` 不存在且 `config/markers.toml` 尚未生成。
- 2026-08-01：已创建 `config/cases.csv`，表头和 5 行工况保持用户提供的顺序与取值。
- 2026-08-01：使用 Python 3.14.0a1 标准库 `csv` 与 `tomllib` 完成验证；8/8 项检查通过。
- 2026-08-01：确认 `project.toml` 的设计点和网格无关性工况均为 `M50_H21_A8_B0`，且该记录角色为 `design`。
- 2026-08-01：确认原始 `Model1-计算区域.STEP` 仍为只读，文件大小保持为 2,553,767 字节。

### 验证结果

- `config/cases.csv`：验证通过；5 条记录；226 字节；SHA-256 为 `C27CB7B8B70B04FBFCEC1F2CEF4BE66510672377258F09DC13BA4F93AF137435`。
- CSV 检查：8/8 通过，包括精确表头、精确记录、行数、唯一标识、数值可解析性及生产工况交叉引用。
- 原始 STEP：未修改，仍为只读。

### 剩余风险与后续前置条件

- `altitude_km` 按 `project.toml` 的输入单位保存为 km；后续大气与求解配置生成程序必须显式转换为 SI 长度单位。
- 本阶段只建立工况真源，尚未生成美国标准大气状态量或 SU2 算例配置。

---

## 已完成阶段：项目基础配置

状态：已完成

### 本轮目标

将用户提供的项目级常数、坐标约定、物理模型、网格目标、边界角色和求解策略保存为 `config/project.toml`，作为后续自动化流程的配置真源。

### 验收标准

- `config/project.toml` 存在且可由标准 TOML 解析器读取；
- 配置表结构、键名、字符串、布尔值、数组和数值与用户提供内容一致；
- 参考长度、参考面积、力矩原点及粗/中/细网格目标通过自动核对；
- 不修改只读原始 STEP 文件；
- 检查验证输出并记录结果与剩余风险。

### 实施记录

- 2026-08-01：检查仓库现状；确认已有 `AGENTS.md`，尚无 `PLAN.md`、`config/project.toml`、`config/cases.csv` 和 `config/markers.toml`；原始 STEP 文件保持只读。
- 2026-08-01：已创建 `config/project.toml`，内容保持用户提供的表结构和取值。
- 2026-08-01：使用 Python 3.14.0a1 标准库 `tomllib` 完成解析；14/14 项结构与关键值检查通过，未发现解析错误。
- 2026-08-01：确认原始 `Model1-计算区域.STEP` 仍为只读，文件大小保持为 2,553,767 字节。

### 验证结果

- `config/project.toml`：验证通过；1,670 字节；SHA-256 为 `96EC212CAEA5B76816ADB5089AB77B97D77D131461AD854615940D52E4B3BD1C`。
- 配置检查：14/14 通过，包括单位、参考量、RANS/SST、y+、网格级别、内部测量面属性、后部出口占位策略、CUDA 策略和设计工况。
- 原始 STEP：未修改，仍为只读。

### 剩余风险与后续前置条件

- `geometry_file` 指向 `geometry/raw/model1.step`，当前仓库尚无该路径；本轮未移动、重命名或复制原始 STEP。后续几何导入阶段开始前需按只读原始数据策略建立该路径。
- `config/markers.toml` 尚未生成；应在几何修复及无歧义边界分类完成后生成。
- 后部出口模式仍为 `TBD_AFTER_SUPERSONIC_PILOT`；超声速小规模试算验证前不得设置静压或背压。

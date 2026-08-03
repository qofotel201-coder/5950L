# 复现指南

本指南用于在另一台电脑上复验已经通过的能力，而不是跳过质量门启动生产计算。当前参考平台仍是 Windows x64；Ubuntu 22.04 x86-64 仅为 `CANDIDATE_PENDING_REMOTE_VERIFICATION`，不得写成 VERIFIED 或 PASS。

## Linux / AutoDL 候选部署边界

- AutoDL 网络加速只用于 GitHub clone/fetch，完成后必须关闭代理；依赖与外部工具部署需单独批准。
- 项目和大型工件应放在 `/root/autodl-tmp`，但仓库脚本不得硬编码该机器路径。
- L0/L1 不需要 CAD，也不导入或运行 Gmsh；L2/L3 必须等待 AutoDL 实机工具部署与验证。
- STEP/BREP 和历史 `runs/` 不通过 Git 传输，只能使用独立受控渠道并复核既有 SHA-256。
- 在远程 L0/L1、随后 L2/L3 质量门逐级通过前，不得进入生产网格、MPI/GPU 声明或正式 CFD。

候选机建议依次执行：

```bash
scripts/repro/bootstrap_linux.sh --dry-run
scripts/repro/check_linux_environment.sh runs/reproducibility/linux_environment.json
scripts/repro/run_linux_l0_l1.sh --output runs/reproducibility/linux_l0_l1_<UTC_TAG>
```

## 1. 复现层级

复现必须依次推进，前一层失败时停止。

| 层级 | 启动外部 CFD 工具 | 需要私有 CAD/历史证据 | 目的 |
|---|---:|---:|---|
| L0 仓库离线预检 | 否 | 否 | 文件、配置、禁止项和 Python 语法检查 |
| L1 标准库测试 | 否 | 否 | mock、fail-closed 和命令策略回归 |
| L2 工具定位 | 仅检查路径/能力 | 否 | 确认本机显式工具配置 |
| L3 通用 connect-test | 是 | 否 | 二维 Gmsh→SU2→pvbatch 五步小链 |
| L4 私有输入门 | 否 | 是 | STEP/BREP 和证据 SHA-256 绑定 |
| L5 真实项目小链 | 是 | 是 | 真实几何 topology-smoke 五步连接 |
| L6 小网格诊断门 | 是 | 是 | 15 层混合网格、串行 SST RANS、y+ |
| L7 生产门 | 尚不可用 | 是 | 粗/中/细网格及正式 RANS，当前未完成 |

L3、L5 和 L6 的 PASS 不能替代 L7。

## 2. 克隆与 Python 环境

推荐使用私有 GitHub 仓库和已验证的 Python 3.13.14。`pyproject.toml` 的最低语法支持为 Python 3.12，但 3.12 不是当前外部工具链的完整实机基线。

```powershell
git clone <REPOSITORY_URL> cfdpipe
Set-Location cfdpipe
py -3.13 -m venv .venv
```

在线环境可使用绑定哈希的依赖文件：

```powershell
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-gmsh.txt
```

完全离线环境应由可信电脑预先准备精确 wheel，并在目标机执行：

```powershell
.\.venv\Scripts\python.exe -m pip install `
  --no-index `
  --find-links <WHEEL_DIRECTORY> `
  --require-hashes `
  -r requirements-gmsh.txt
```

仓库不自动安装 SU2、ParaView、MPI 或 Gmsh CLI，也不修改系统 `PATH`、Windows App Execution Alias 或注册表。`scripts/cfdpipe.ps1` 固定使用仓库根目录的 `.venv\Scripts\python.exe` 和 `src/`。

## 3. 工具路径

复制示例，不提交生成的本机配置：

```powershell
if (-not (Test-Path config/tools.json)) {
  Copy-Item config/tools.example.json config/tools.json
}
```

路径解析优先级为：

1. CLI 参数；
2. 环境变量；
3. 被 Git 忽略的 `config/tools.json`；
4. 隔离后的 `shutil.which`。

支持的覆盖变量为：

```powershell
$env:CFD_GMSH = "C:\absolute\path\gmsh.exe"
$env:CFD_SU2_CFD = "C:\absolute\path\SU2_CFD.exe"
$env:CFD_SU2_SOL = "C:\absolute\path\SU2_SOL.exe"
$env:CFD_MPIEXEC = "C:\absolute\path\mpiexec.exe"
$env:CFD_PVBATCH = "C:\absolute\path\pvbatch.exe"
```

高优先级路径无效时程序不会静默降级。`/mnt/c/.../*.exe` 默认拒绝；不能把 `paraview.exe`、`pvpython.exe` 或其它 GUI 程序替代 `pvbatch`。

检查命令：

```powershell
& .\scripts\cfdpipe.ps1 tools check --tools-config config/tools.json
```

参考软件合同位于 `config/reproducibility.json`。版本不同时先重新执行 L3，不要直接复用旧 PASS manifest。

## 4. L0：离线仓库预检

该命令不导入 Gmsh、不启动 SU2、pvbatch、MPI 或 GUI，也不访问网络：

```powershell
.\.venv\Scripts\python.exe scripts/repro/verify_repository.py
```

它应检查必要仓库文件、TOML/CSV/JSON 可解析性、Python 语法、禁止的普通 Python ParaView 导入、GUI 调用和进程策略。任何失败均返回非零并给出具体项。

## 5. L1：纯 Python 回归

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe scripts/repro/run_cad_free_tests.py
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
```

CI 只覆盖上述显式 CAD-free L0/L1 集合。完整 `unittest discover` 还会校验哈希绑定的真实项目配置，须在 L4 私有输入门 PASS 后于本地运行。GitHub Actions 不下载客户 CAD、不启动 Gmsh、SU2、ParaView、MPI/GPU，不生成网格，也不证明目标机外部工具可用。

## 6. L3：通用小型连接

```powershell
& .\scripts\cfdpipe.ps1 connect-test `
  --output runs/connection `
  --clean `
  --nproc 1 `
  --timeout 300
```

执行顺序严格为 tools → Gmsh Python API → SU2_CFD → pvbatch。预期：

- Gmsh 生成约 1,000 点、约 1,800 个二维三角形和 `farfield`；
- SU2_CFD 串行完成 5 步并生成 history 与 VTU；
- pvbatch 打开本轮 SU2 manifest 指向的 VTU并生成合法 JSON/CSV；
- `runs/connection/connection_report.json` 六段和 `overall` 均为 `PASS`。

`--clean` 只处理经账本验证、属于连接测试的工件；未知文件不会被当作受管工件删除。不要并发运行两个写入同一 `runs/connection` 的进程。

## 7. L4：私有输入门

按 [私有输入交接](PRIVATE_INPUTS.md) 放置文件后执行：

```powershell
.\.venv\Scripts\python.exe scripts/repro/verify_repository.py --require-private-inputs
```

文件名相同但 SHA-256 不同也必须失败。不要编辑配置中的哈希来“通过”检查；几何变更应建立新的 lineage 并重新执行几何修复、目录化和 marker 重匹配。

## 8. L5：真实项目小型连接

仅在 L4 PASS 后运行：

```powershell
& .\scripts\cfdpipe.ps1 pipeline run `
  --step geometry/raw/model1.step `
  --project config/project.toml `
  --cases config/cases.csv `
  --markers config/markers.toml `
  --topology-smoke-config config/topology_smoke.toml `
  --case M50_H21_A8_B0 `
  --mesh-level smoke `
  --nproc 1 `
  --max-iterations 5 `
  --timeout 300 `
  --output runs/real_connection
```

该链使用原始 STEP 作为只读 provenance，并根据 `markers.toml` 选择已经证明保持共享拓扑的只读 BREP 作为网格几何。它只生成小型无边界层 topology-smoke 网格并运行 5 步 Euler。成功标准是 `runs/real_connection/real_connection_report.json` 六段均 PASS；结果不能用于 RANS、载荷或物理解读。

`pipeline run` 把输出根固定为 `runs/real_connection`。复验前应先通过受控方式完整保留旧目录，并确认目标目录不会混入不兼容证据；不得手工拼接不同 run 的 manifest，也不得覆盖仍需保留的 PASS/FAIL 证据。

## 9. L6：小规模边界层与诊断 RANS

这一层除了通过严格 CAD 输入门，还需要按 [已验证基线](VERIFIED_BASELINE.md) 和相关配置中的 provenance 路径/哈希交接完整历史证据。`config/reproducibility.json` 只冻结软件版本与私有输入策略，不替代运行工件清单。先在新的独立目录重建小型 15 层混合网格：

```powershell
& .\scripts\cfdpipe.ps1 pipeline boundary-layer-smoke `
  --step geometry/raw/model1.step `
  --project config/project.toml `
  --cases config/cases.csv `
  --markers config/markers.toml `
  --topology-smoke-config config/topology_smoke.toml `
  --schedule-config config/boundary_layer_trial.toml `
  --smoke-config config/boundary_layer_smoke.toml `
  --case M50_H21_A8_B0 `
  --output runs/boundary_layer_quality/<UNIQUE_RUN_NAME>
```

只有 mesh manifest 为严格 PASS、四个 marker 完整、零非正/非有限单元且全部柱列满足 15 层时，才可运行诊断 RANS。以下命令展示当前已验证的一阶 200 步 y+ 复验形态；输入 manifest 必须由显式路径和 SHA-256 选择，不能搜索旧目录：

```powershell
& .\scripts\cfdpipe.ps1 pipeline rans-smoke `
  --step geometry/raw/model1.step `
  --project config/project.toml `
  --cases config/cases.csv `
  --markers config/markers.toml `
  --topology-smoke-config config/topology_smoke.toml `
  --boundary-layer-trial-config config/boundary_layer_trial.toml `
  --boundary-layer-smoke-config config/boundary_layer_smoke.toml `
  --rear-outlet-pilot-config config/rear_outlet_pilot.toml `
  --rans-config config/rans_smoke.toml `
  --mesh-manifest <PASS_BOUNDARY_LAYER_MANIFEST> `
  --outlet-evidence <PASS_OUTLET_EVIDENCE_MANIFEST> `
  --case M50_H21_A8_B0 `
  --nproc 1 `
  --spatial-order 1 `
  --max-iterations 200 `
  --timeout 840 `
  --output runs/rans_smoke/<UNIQUE_RUN_NAME>
```

这仍是 bounded diagnostic：`diagnostic_only=true`、`production_eligible=false`、`convergence_claimed=false`。二阶诊断必须通过 `--spatial-order 2 --restart-manifest <EXPLICIT_PASS_MANIFEST>` 显式绑定一阶 restart；不要目录搜索。

设计点后部出口冻结证据可由纯 Python 重新评估，但仍依赖已交接的哈希绑定运行证据：

```powershell
& .\scripts\cfdpipe.ps1 pipeline rear-outlet-freeze-check `
  --config config/rear_outlet_freeze.toml `
  --output runs/rear_outlet_freeze/<UNIQUE_RUN_NAME>
```

冻结只适用于 `M50_H21_A8_B0`，且不把任何小网格结果提升为生产解。

## 10. 当前不可执行的生产阶段

不得把下列工作包装成“已复现”：

- 5,000,000 单元正式粗网格；
- 8,000,000～12,000,000 单元中网格及细网格；
- 两阶段正式 RANS 收敛与粗/中/细网格无关性；
- 生产载荷、内部测量面指标或最终物理结论；
- MPI 运行兼容性或 GPU 加速结论。

当前粗网格方向/日程审计仍有局部低质量 Prism，真实 v5 尝试也没有形成可消费终态。必须先通过该质量门，才能进入正式 CFD。

## 11. 可审计执行规则

所有外部工具调用都必须经过统一运行器，并记录：绝对程序路径、参数列表、cwd、UTC 起止时间、返回码、stdout、stderr 和超时/终止证据。SU2 失败后不得调用 pvbatch；Gmsh/网格门失败后不得调用 SU2。

Gmsh Python API 会话必须遵循 `initialize()` / `try` / `finally` / `finalize()`，不调用 FLTK。普通 Python 不导入 `paraview.simple`；ParaView 脚本只由 `pvbatch` 解释执行。任何新入口都应保持这些约束并增加标准库测试。

## 12. 结果保存与复核

- 每次尝试使用新的 run 目录；不得覆盖历史 PASS/FAIL 证据；
- `runs/` 被 Git 忽略，需在独立工件存储中保留；
- 交接时保留 manifest、command JSON、stdout/stderr、输入/输出 SHA-256；
- 后续阶段只能消费显式 PASS manifest 和匹配哈希；
- WARN 不得被后续 PASS 掩盖；
- 输出目录中出现未知旧文件时停止确认来源，不要随意选取。

若某个步骤失败，保留完整日志和 manifest，从新的独立目录修复后重试。不要降低 marker 完整性、非正体积/Jacobian、`y+`、层数或资源保护阈值来换取 PASS。

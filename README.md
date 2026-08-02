# cfdpipe

`cfdpipe` 是一个 Windows 优先、可审计、失败即停的 CFD 自动化仓库。普通 Python 是唯一总控层：Gmsh Python API 负责几何与网格，`SU2_CFD` 由安全子进程调用，ParaView 脚本只由 `pvbatch` 无界面执行。

当前仓库已经真实打通两条小型链路：

- 通用连接算例：Python → Gmsh → SU2_CFD → VTU → pvbatch → JSON/CSV，PASS；
- 真实项目小型连接：只读 STEP/派生 BREP → Gmsh → SU2_CFD → VTU → pvbatch → JSON/CSV，PASS。

此外，小规模 15 层边界层混合网格、串行 SST RANS 诊断和实际 `y+ < 1` 质量门已经 PASS。它们仍是诊断证据，不是生产解。正式粗网格、正式两阶段 RANS、粗/中/细网格无关性和生产结果尚未完成。

## 不变的执行边界

- 不依赖 GUI、鼠标宏或自动点击；
- 不使用 `shell=True`，所有外部命令均以参数列表执行；
- 普通 Python 不导入 `paraview.simple`；
- 原始 STEP 只读，修复几何只能写入 `geometry/derived`；
- 项目常数、工况和 marker 真源只在 `config/`；
- 任一质量门失败立即停止，不调用下游工具；
- 客户 CAD、派生几何、`runs/` 工件和本机工具绝对路径默认不进入 Git。

## 已验证版本

| 组件 | 已验证版本 | 用途 |
|---|---:|---|
| Python | 3.13.14 | 唯一总控层 |
| Gmsh Python API / CLI | 4.15.2 / 4.15.2 | STEP/BREP、Physical Groups、网格 |
| SU2_CFD | 8.5.0 | 串行 Euler 与诊断 SST RANS |
| ParaView / pvbatch | 6.2 | 无界面读取和 JSON/CSV 后处理 |
| cfdpipe | 0.1.0 | 仓库 CLI |

其它版本不自动视为兼容；版本变化后应从工具检查和小型连接测试重新取证。完整证据见 [已验证基线](docs/VERIFIED_BASELINE.md)。

## Windows 快速开始

克隆后从仓库根目录执行。仓库不会自动安装或替换系统软件；工具只按显式优先级解析，并记录实际来源。

```powershell
git clone <REPOSITORY_URL> cfdpipe
Set-Location cfdpipe

py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-gmsh.txt

if (-not (Test-Path config/tools.json)) {
  Copy-Item config/tools.example.json config/tools.json
}
# 编辑 config/tools.json，填写本机 Gmsh、SU2_CFD、SU2_SOL、MPI 和 pvbatch 的绝对路径。
```

如果目标电脑完全离线，请先通过受控介质提供 `requirements-gmsh.txt` 所绑定的 wheel，再使用 `--no-index --find-links <WHEEL_DIRECTORY>` 安装。SU2 和 ParaView 是外部程序，不属于 Python 依赖，必须由机器管理员预先部署。

首先运行不启动任何 CFD 外部程序的仓库检查：

```powershell
.\.venv\Scripts\python.exe scripts/repro/verify_repository.py
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe scripts/repro/run_cad_free_tests.py
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
```

然后检查显式工具路径：

```powershell
& .\scripts\cfdpipe.ps1 tools check --tools-config config/tools.json
```

只有上述步骤通过，才运行不含客户 CAD 的小型端到端连接测试：

```powershell
& .\scripts\cfdpipe.ps1 connect-test `
  --output runs/connection `
  --clean `
  --nproc 1 `
  --timeout 300
```

预期最终文件为 `runs/connection/connection_report.json`，且六段连接状态和 `overall` 均为 `PASS`。该命令只运行二维五步 Euler 连接算例，不代表正式 CFD。

## 私有输入与真实项目复验

Git 克隆不会包含客户 CAD、派生 BREP 或历史 `runs/` 证据。它们必须通过独立受控渠道按原相对路径交接，并用 SHA-256 验证：

```powershell
.\.venv\Scripts\python.exe scripts/repro/verify_repository.py --require-private-inputs
```

严格交接方法、当前输入哈希和公开仓库注意事项见 [私有输入交接](docs/PRIVATE_INPUTS.md)。私有输入门通过后，才可按 [复现指南](docs/REPRODUCIBILITY.md) 分级复验真实项目小链、边界层网格和诊断 RANS。

## 仓库结构

```text
config/             项目、工况、marker、质量门和版本合同
src/cfdpipe/        Python 总控、桥接器、质量门与 CLI
scripts/paraview/   仅供 pvbatch 解释的后处理脚本
scripts/repro/      不启动外部 CFD 工具的离线仓库预检
tests/              标准库 unittest
geometry/           私有输入占位说明；真实 CAD 默认被 Git 忽略
runs/               本地运行证据；默认被 Git 忽略
docs/               复现、基线与私有输入文档
```

## 当前质量门

| 阶段 | 状态 | 结论 |
|---|---|---|
| Python/Gmsh/SU2/pvbatch 通用小链 | PASS | 软件连接已打通 |
| 真实 STEP/BREP 小型端到端链 | PASS | 真实项目的软件和数据连接已打通 |
| 15 层小型边界层混合网格 | PASS | 仅小规模质量门 |
| 小型串行 SST RANS 与 y+ | PASS | `max y+ = 0.92289495`，仅诊断 |
| 设计点后部出口语义 | PASS | 仅 `M50_H21_A8_B0` 冻结，非生产解 |
| 正式粗网格 | 未完成 | 60 层局部棱柱质量门仍未通过 |
| 正式 CFD 与网格无关性 | 未开始 | 必须等待粗网格质量门 |

详细数值和证据哈希见 [已验证基线](docs/VERIFIED_BASELINE.md)。

## 安全与贡献

默认建议使用私有 GitHub 仓库；公开前必须审查 `config/markers.toml` 中的几何特征和项目工况。安全问题见 [SECURITY.md](SECURITY.md)，贡献规则见 [CONTRIBUTING.md](CONTRIBUTING.md)。本仓库尚未自动授予开源许可，未取得授权前不要公开客户输入或衍生工件。

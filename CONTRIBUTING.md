# 参与开发

本项目按证据化质量门推进。一次变更只处理一个阶段；连接成功、网格可写和物理结果可信是三个不同结论，不能互相替代。

## 开始前

1. 按顺序阅读 `AGENTS.md`、`PLAN.md`、`config/project.toml`、`config/cases.csv` 和 `config/markers.toml`。
2. 在 `PLAN.md` 写明本轮目标、验收标准和明确禁止的下游动作。
3. 使用 Python 3.12 或更高版本建立独立虚拟环境。已验证的参考版本记录在 `docs/VERIFIED_BASELINE.md`。
4. 如需 Gmsh Python API，显式安装锁定文件：

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-gmsh.txt
   ```

   SU2 与 ParaView 是外部命令行工具，不是 pip 依赖。使用 `CFD_GMSH`、`CFD_SU2_CFD`、`CFD_SU2_SOL`、`CFD_MPIEXEC`、`CFD_PVBATCH` 或本地 `config/tools.json` 显式配置路径；不要把本机绝对路径提交到版本库。

安装和外部程序运行都是贡献者主动执行的步骤；仓库预检和托管 CI 不会安装 Gmsh、SU2、ParaView，也不会启动网格或求解任务。

## 最小开发循环

在仓库根目录运行：

```powershell
$env:PYTHONPATH = (Resolve-Path 'src').Path
.\.venv\Scripts\python.exe -B scripts/repro/verify_repository.py
.\.venv\Scripts\python.exe -B -m compileall -q src tests scripts
.\.venv\Scripts\python.exe -B scripts/repro/run_cad_free_tests.py
```

根据修改范围补充相应的 CAD-free `unittest` 模块。完整发现还会校验哈希绑定的真实项目配置，因此只在恢复私有 STEP/BREP 并通过严格输入门后运行：

```powershell
.\.venv\Scripts\python.exe -B scripts/repro/verify_repository.py --require-private-inputs
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

完整回归不是外部工具授权；不得因此自动启动 Gmsh、SU2、pvbatch、MPI、GPU 或正式 CFD。

## 实现规则

- Python 是唯一总控层。Gmsh 通过 Python API 使用；SU2_CFD、SU2_SOL 和 pvbatch 只通过统一命令运行器以参数列表调用，始终 `shell=False`。
- 普通 Python 不得导入 `paraview.simple`；ParaView 脚本只能由 `pvbatch` 执行。禁止 GUI、自动点击和 `gmsh.fltk.run()`。
- 项目物理常数、工况、参考量、边界角色和质量阈值只来自 `config/`，不得在 Python、模板或 ParaView 脚本中另写副本。
- 原始 STEP 只读。修复后几何只写入 `geometry/derived/`，所有运行工件只写入对应的 `runs/` 阶段目录。
- 不用数字 entity tag 作为跨运行边界身份；使用名称、几何特征、稳定 fingerprint 和 SHA-256 证据。
- 任一阶段失败立即停止下游调用。保留完整 stderr、返回码、命令参数、cwd、起止时间、版本、输入输出哈希和 FAIL manifest。
- 不覆盖已有有效代码或工件，不提交 `.venv`、客户 CAD、派生几何、网格、restart、求解结果、日志或本机工具配置。

## Pull Request 完成条件

- 相关短测试、离线预检和 `compileall` 通过；具备私有输入时再记录完整回归结果。
- 从仓库根目录可重复执行，日志中没有被忽略的错误。
- 检查变更清单，确认没有客户数据、本机绝对路径、密钥或大文件。
- 最终更新 `PLAN.md`，写入实际命令、返回码、证据路径、SHA-256、结论和剩余风险。
- PR 描述明确区分已验证能力、诊断性结果和尚未通过的生产质量门。

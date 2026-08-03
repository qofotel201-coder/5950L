# 私有输入与运行证据交接

本仓库默认不把客户 CAD、派生几何、历史运行结果或本机工具路径提交到 Git。这样可以复制代码和配置结构，同时避免把专有模型、大文件和个人绝对路径发布到 GitHub。

默认建议创建私有 GitHub 仓库。即使 CAD 被忽略，`config/markers.toml` 仍包含几何指纹、面积、质心和 bounds，`config/cases.csv` 包含项目工况；公开前必须由数据所有者单独审查。

## 1. 不进入 Git 的内容

`.gitignore` 应持续排除：

- `.venv/`；
- `config/tools.json`；
- `geometry/raw/` 中的客户原始 CAD；
- `geometry/derived/` 中的派生 STEP/BREP；
- 仓库根目录遗留的 STEP/SLDPRT/IGES/BREP；
- `runs/` 中的网格、解、日志、图片和 manifest；
- MSH、SU2、VTK/VTU、restart 等大体积计算工件。

不要为了方便复刻而用 `git add -f` 绕过这些规则。GitHub Release、Issue、PR 附件和 CI artifact 同样不得承载客户 CAD 或未经批准的求解结果。

## 2. 精确 CAD 基线

当前项目的必需私有输入如下：

| 相对路径 | 用途 | 大小 | SHA-256 | 属性 |
|---|---|---:|---|---|
| `geometry/raw/model1.step` | 原始只读 provenance | 2,553,767 B | `d7d1cbec76ccc6b45aad5689d61a28c41e1ba41b9cda6c90d46eb6ff891c1a6b` | 只读 |
| `geometry/derived/model1_shared_topology/model1_shared_topology.brep` | 已验证 pipeline geometry | 3,028,066 B | `13698e9afb96ac21205c31d8a23f718adece802fda51ed1a67857ba73379e121` | 只读 |

派生 STEP 不能替代该 BREP：已验证的 STEP AP214 round-trip 会丢失两体共享拓扑。文件名相同但哈希不同也不能替代。

以下原生/交换文件仅用于 provenance 或 CAD 取证，不是自动 pipeline 的最低运行输入；若交接则仍需保持原样：

| 文件 | SHA-256 |
|---|---|
| `Model1-计算区域.SLDPRT` | `cb39ff7128b7a94e1e0dfd842b95ba43db7f0e5af573f9c6238536d56bf7853f` |
| `Model1-ComputeZone.IGS` | `9d66574763c785be80ceceb7d45667942c1c344d23b78bb8c87f6ecc7fb0e06d` |

这些文件也不应放入 Git。

## 3. 历史运行证据

通用 `connect-test` 可以在新电脑上从零生成，不需要交接 `runs/connection`。真实项目高级质量门则采用 fail-closed 的哈希 lineage，部分配置会引用既有 PASS manifest；若希望精确延续已验证状态，需要交接对应 `runs/` 工件。

至少应保留以下类别：

- 几何修复、marker rematch 和接口持久性 manifest；
- topology-smoke 网格、manifest 和 Gmsh 日志；
- 出口诊断 manifest 与 VTU；
- 15 层边界层网格、manifest、MSH/SU2 和日志；
- 一阶/二阶 RANS run manifest、history、restart、VTU、pvbatch JSON；
- 后部出口冻结评估所引用的全部 Euler/RANS 证据；
- 每个外部命令的 command JSON、stdout 和原始 stderr。

关键运行路径和哈希见 [已验证基线](VERIFIED_BASELINE.md) 及各质量门配置的 provenance 字段；`config/reproducibility.json` 只声明软件版本、私有输入政策和离线预检范围。不要只复制最终 JSON 而遗漏它引用的输入、日志或 restart；后续质量门会把不完整 lineage 判为 FAIL。

## 4. 推荐交接流程

交接应通过企业私有文件库、加密介质或其它已批准渠道完成。本仓库不负责上传、加密、密钥管理或远程传输。

在源电脑上：

1. 停止所有写入目标 run 目录的任务；
2. 按相对路径收集 CAD 和选定证据目录；
3. 拒绝 symlink、junction、reparse point 和指向仓库外的链接；
4. 计算每个文件的大小与 SHA-256；
5. 生成独立交接清单并由数据所有者批准；
6. 将包和清单分别通过受控渠道交付。

在目标电脑上：

1. 先克隆代码，不运行外部 CFD 工具；
2. 把文件解包到仓库内相同相对路径，禁止覆盖未审查的已有文件；
3. 将原始 STEP 和派生 BREP设为只读；
4. 逐文件核对大小和 SHA-256；
5. 运行严格离线输入门；
6. 只有全部 PASS 后才运行 `tools check` 和小型连接测试。

Windows 示例：

```powershell
attrib +R geometry\raw\model1.step
attrib +R geometry\derived\model1_shared_topology\model1_shared_topology.brep

Get-FileHash -Algorithm SHA256 geometry\raw\model1.step
Get-FileHash -Algorithm SHA256 `
  geometry\derived\model1_shared_topology\model1_shared_topology.brep

.\.venv\Scripts\python.exe `
  scripts/repro/verify_repository.py `
  --require-private-inputs
```

严格检查失败时不要启动 Gmsh、SU2 或 pvbatch。

## 5. 本机工具路径不能交接

`config/tools.json` 包含个人目录和安装位置，被 Git 忽略。目标电脑应从 `config/tools.example.json` 新建自己的文件，或使用环境变量/CLI 参数。不得复制源电脑的绝对路径并假定有效，也不得让程序静默选择 Windows 系统目录或 GUI 可执行文件。

工具路径解析顺序和变量见 [复现指南](REPRODUCIBILITY.md)。

## 6. 输入不一致时怎么办

如果 STEP 或 BREP 哈希不一致，应把它视为新的几何版本，而不是修补清单：

1. 保留旧文件和旧证据；
2. 为新输入建立独立目录和 SHA-256；
3. 重新执行只读几何目录、接口检查和受控派生修复；
4. 在修复后重新生成 surface catalog；
5. 重新完成 marker 唯一匹配与人工业务确认；
6. 从 topology-smoke 开始逐级复验；
7. 生成新的 `config/reproducibility.json` 版本合同。

禁止只修改 `markers.toml`、manifest 或 reproducibility 合同中的哈希来接受新文件。运行时 Gmsh tag 不能作为跨版本身份。

## 7. 公开发布检查

从私有仓库转为公开仓库前，至少检查：

- Git 历史中从未提交 CAD、网格、restart、VTU、图片或运行日志；
- `config/tools.json` 和个人绝对路径未进入历史；
- `config/markers.toml` 的几何描述获准公开；
- `config/cases.csv`、参考量和业务边界定义获准公开；
- README、Issue、PR、Release 与 CI artifact 没有附带私有工件；
- 已选择并获得批准的软件许可证；未授权前默认保留全部权利。

如果任何一项不明确，继续使用私有仓库，不要发布。

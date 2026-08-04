# Changelog

本文件记录 LLM Olympics 各正式版本的重要变化。

## [Unreleased]

## [0.2.0] - 2026-08-04

### 赛事审计与可靠性

- 循环赛 checkpoint 新增 SQLite 跨进程 runner lease：恢复进程会在重建 Provider 和调用模型
  前原子领取执行权；默认 60 秒租约配合 15 秒心跳及每组开赛前续租，避免同一赛事被并行执行。
- lease 使用进程内随机 capability token、数据库 token 摘要和单调 generation fencing；过期、
  释放或已被接管的 v6 runner 不能再追加 checkpoint、封存赛事或更新 ELO。`Ctrl-C` 会尽力
  立即释放，崩溃后则可在过期后安全接管。
- SQLite schema 升至 v6；v5→v6 的 lease 表迁移与版本提升处于同一事务，失败会完整回滚。
  claim、续租、保存和封存都使用短事务，不跨 Provider 网络调用持有写锁。
- 心跳丢失会取消循环赛任务；只实现同步 `chat()` 的旧第三方 Provider 已在途请求仍可能短暂
  继续，但其旧 generation 已被禁止持久化任何结果。
- 心跳会在当前租约有效窗口内重试临时 SQLite `BUSY`/`LOCKED`，避免把短暂写锁竞争误报为
  失权；完整但未封存的 checkpoint 可直接结算，无需重建 Provider 或重新调用模型。
- 升级数据库前必须先停止仍在运行的 v5 runner；已经加载的旧代码不理解 lease，v6 schema
  无法反向约束它继续调用旧写入路径。
- 新增 `audit-tournament`，以 immutable、query-only SQLite 快照严格只读审计指定的
  循环赛或 checkpoint；检查完整性、当前 schema 必需列、已声明外键、赛事关系索引、
  正式档案及 ELO 账本。
- 审计命令在数据库存在活动 journal/WAL、需要迁移或版本过新时保守拒绝，不创建、迁移、
  chmod 或修复数据库；`--json` 提供稳定、脱敏的机器可读结果。
- 若同一选手和榜单作用域存在目标赛事之外的其他计分操作，仍会完整验证该赛事的 ELO
  快照、贡献和历史，但将当前排行榜重放覆盖标为 `partial`，避免对 schema 尚未记录的
  全局提交顺序作过度保证；部分覆盖仍会核对计分历史来源、当前分候选和操作更新时间。
- `round-robin --resume` 在把正式赛事报告为已完成前会深度验证关系表与 ELO 状态；同时
  拒绝早于 checkpoint 最后更新时间的封存时间。
- checkpoint 创建与恢复会预检既有可信 `entrant_id` 的身份绑定，在首次 Provider 调用前
  拒绝最终无法封存的身份冲突。

### 发布与验证

- 新增真实子进程强制终止、租约自然过期、接管、封存及 ELO 只结算一次的端到端回归测试；
  已封存赛事恢复路径也会拒绝损坏的 ELO 快照。
- GitHub Release 的 tag 流水线会重新执行依赖审计、Ruff 与完整测试；CI 和 Release 均分别
  从 wheel 与 sdist 进行隔离安装和 CLI 冒烟，并检查版本、README、CHANGELOG 与安全支持线一致。

## [0.1.2] - 2026-08-03

### 安全

- 普通 `openai:model` 使用第三方 OpenAI 兼容端点时，不再继承 OpenAI SDK 的全局
  organization、project、admin、webhook 或自定义请求头，防止不同端点间串用凭据。
- 同步、异步客户端及设置单步超时后由 `with_options()` 创建的客户端副本使用相同
  隔离策略；`OPENAI_API_KEY` 与 `OPENAI_BASE_URL` 的既有配置方式保持不变。
- 精确的 OpenAI 官方默认端点继续保留 SDK 原有的 organization、project 和自定义头
  兼容行为；命名 Provider Profile 仍对所有端点执行完整隔离。

## [0.1.1] - 2026-08-03

### 许可

- 本仓库原创代码及 Python 发行归档以 MIT License 开源，版权归 `zhenghu` 所有。
- Python 发行包使用 SPDX `MIT` 许可证表达式，并包含完整 `LICENSE` 文件。
- 新增第三方许可说明，披露国际象棋功能依赖 GPL-3.0-or-later 的 python-chess；外部依赖
  不因本项目采用 MIT 而被重新授权。

## [0.1.0] - 2026-08-03

首个公开 GitHub Release。

### 对战与赛事

- 提供知识问答、数学问答、逻辑推理、猜谜、五子棋和国际象棋六个项目。
- 支持人类、mock、OpenAI、Ollama 与命名 Provider Profile 选手。
- 支持单局、交换顺序双局赛和 3–16 名非人类选手循环赛。
- 循环赛按完整双局对阵保存检查点；进程或机器中断后可使用赛事 ID 继续运行。

### 存档与评分

- SQLite 数据库格式升级至 v5，保存对局、双局赛、循环赛、检查点和稳定选手身份。
- 对局、双局赛和最终循环赛封存均使用事务保护；循环赛正式档案与 ELO 只结算一次。
- 支持总体和分项目 ELO 榜、历史列表以及完整 JSON 档案查询。
- 旧版 SQLite 数据库可原子迁移到当前格式，并保留既有档案和评分身份。

> 升级提示：首次打开旧数据库前应停止写入进程并制作一致备份；若数据库处于 WAL 模式，
> 直接复制时需要连同 `-wal` 与 `-shm` 文件一起处理。迁移至 v5 后，不要再使用仅支持旧
> schema 的版本打开该数据库。

### 安全与可靠性

- Provider Profile 仅记录凭据环境变量名，API Key、Key 哈希和客户端不会写入档案或检查点。
- 隔离 OpenAI 兼容端点配置，拒绝携带凭据的远程明文 HTTP，并保留回环地址兼容性。
- 对模型超时、Provider 故障、非法走法、超长输出和终端控制字符进行限制与安全处理。
- 收紧 POSIX 数据目录和 SQLite 文件权限，并对配置文件共享权限发出提醒。
- GitHub Actions 覆盖 Python 3.11–3.13、依赖审计、Ruff、pytest、CodeQL 和发布包安装冒烟。

# Changelog

本文件记录 LLM Olympics 各正式版本的重要变化。

## [Unreleased]

- 完成建议编号阶段四 4.5a：`play --human-input web` 现在可为同一场比赛中的每一名
  `HumanPlayer` 创建独立的本机浏览器席位；比赛仍需至少 2 名选手，并继续服从具体 Game
  与平台最多 16 名选手的既有人数约束。CLI 会按选手分别打印一次性参与链接，非人类席位
  仍可使用 mock、Provider Profile 或其他 LLM。
- 每个浏览器 Human 使用互不共享的 session、seat 和高熵 capability；一个席位只能读取自己的
  当前 request 并幂等提交自己的动作；公开题面与动作事件仍可在同机观战页中显示。多席位继续
  复用 Match 的并发盲答、非法重试、超时、取消和最终存档流程；双人对局沿用 ELO，三名及
  以上选手完整存档但不计分。input sidecar schema 仍为 v1，公开 API v1 只放宽了参赛者列表
  的有界长度。
- 4.5a 仍仅面向回环地址和可信本机操作者：运行命令的终端会看到全部席位链接，它不是同一
  操作系统账户之间的保密边界，也没有加入远程身份认证、授权或 TLS。浏览器输入仍只支持
  单场 `play`，不扩展到 `series`、`round-robin`、网页建赛、Provider 调用或 ELO 管理。

## [0.6.0] - 2026-08-14

- 完成阶段四 4.3：新增独立、权限收紧的 SQLite 实时事件 sidecar。`play`、`series` 与
  `round-robin` 会以后台有界队列发布已经过公开 DTO 白名单过滤的事件；Web 即使晚启动，
  仍可发现运行中的本机比赛，并通过 WebSocket 从首个缺失序号续播。
- 实时发布故障、队列背压、sidecar 忙或观战客户端断开都只会让直播安全降级，不会改变
  Provider 调用、比赛结果、ELO、预算或最终存档。只有正式档案事务成功提交后，直播才会
  公布可打开的最终 match 档案；超时租约会只读投影为中断，不产生虚假的完成状态。
- 观战大厅新增实时比赛卡片，直播详情提供跟随、暂停、逐条查看、回到最新、断线续播和
  REST 只读轮询回退；完成后可进入既有存档回放。HTTP 面仍只有 GET/WebSocket，并继续
  限制回环 Host、同源 Origin、严格 CSP，不提供远程落子、管理操作、认证或公网访问。
- 完成阶段四 4.4 的首个可玩切片：`play --human-input web` 可将恰好一名 `HumanPlayer`
  接入本机浏览器通用文本参与页。CLI 生成 fragment capability 链接，题面、非法重试、超时、
  接受事件和最终存档仍复用同一个 Match 引擎流程；网页不能创建比赛或调用 Provider。
- 新增与正式档案及直播缓存分离的 `*.input.db`：比赛进程拥有席位、租约和 request 生命周期，
  Web 只可凭 capability 以同源 JSON POST 原子提交当前 request。令牌只保存摘要，submission ID
  支持网络幂等重试；旧 request、双标签竞争、超时/取消、错误令牌和跨源请求均 fail closed。
- 参与页会立即清除 URL fragment、在标签页会话中保存 capability，并明确区分“已提交给引擎”
  与“已通过游戏规则”。首版仍限回环地址、单个 Web Human、`play` 和通用文本输入；远程多席位、
  专用棋盘、认证/TLS 与网页建赛不在本切片范围。

## [0.5.1] - 2026-08-13

- Web 观战页升级到 React 19.2.8，并从 React 19 已移除的 UMD 分发切换为同源单文件
  production bundle；构建使用精确锁定的 esbuild，CI 会从已声明源码和 npm 完整性记录
  重新构建并与发行资产逐字节比对，继续保持无 CDN、严格 CSP 和运行时不依赖 Node。

## [0.5.0] - 2026-08-12

- 启动阶段四 4.1：新增可选 `web` extra 与 `llmolympic web`，以 FastAPI 提供本机只读
  健康状态、项目列表、最近对局、公开档案详情和 ELO 排行榜；已完成的本地引擎对局可通过
  版本化 WebSocket 信封按事件顺序回放并从指定序号续播。
- Web 仓储使用 SQLite `mode=ro`、`query_only` 与 `trusted_schema=OFF`，不复用会迁移或
  收紧权限的写入仓储。服务默认只监听回环地址，拒绝跨源请求；公开 DTO 会剔除 Provider
  路由、Profile、模型配置、凭据、请求头、原始失败详情和未知事件字段，并对档案大小、
  事件数量、语义与并发回放设定上限。
- 当前 Web 切片只回放已完成且已存档的对局，不是运行中比赛的实时广播；运行中事件
  broker、远程人类输入与锦标赛模式留给后续阶段四切片。
- 完成阶段四 4.2：`llmolympic web` 根地址新增随 wheel/sdist 离线分发的 React 观战页，
  提供项目筛选、最近对局、总体/分项目 ELO、对局详情和通用事件时间线；回放可播放、暂停、
  前后单步、拖动进度、切换速度并重新播放。
- 前端从同源 WebSocket 连续接收公开事件，异常断线会以首个缺失 `seq` 有限续播；接收进度
  与视觉播放游标相互独立，WebSocket 不可用时可降级至同一只读 REST 详情。所有存档文本均
  作为 React 文本节点渲染，不使用原始 HTML。
- 页面不依赖 CDN、外部字体、遥测、Service Worker 或运行时 Node；React 18.3.1 与许可证
  随发行包固定分发。HTML 使用仅允许同源脚本、样式和精确同源 WebSocket 的 CSP；API
  响应继续使用 `default-src 'none'`，既有回环 Host、同源 Origin 与只读数据库边界不变。
- macOS 源码仓库新增可双击的 `start_web.command` / `stop_web.command`；服务由项目路径隔离
  的用户级 `launchd` 标签管理，重复启停幂等，关闭时不会按 PID、端口或进程名误停其他程序。
- 修复观战页把合法系列赛/循环赛评级策略误报为 `archive_invalid` 的问题；Web 读取器现在按
  单局、系列赛和循环赛的实际关联严格核对策略，并可安全展示迁移后仍保留原始 JSON 的
  schema v1 历史摘要，旧档案继续保持不可回放。
- 发布流水线新增从已构建 wheel 启动服务的真实 Chromium / axe 冒烟，覆盖大厅筛选、
  WebSocket 回放、REST 降级、恶意文本纯文本渲染与 WCAG A/AA；开发依赖锁定 React 来源并
  逐字节校验随包资源，npm Dependabot 提供后续升级与安全公告信号。

## [0.4.0] - 2026-08-11

- 启动第三阶段：新增双人 `creative_writing` 开放作答项目，以及 3–9 名 LLM 组成的匿名
  评审团。每名评委分别盲评每份作品，完整评委交集达到严格多数 quorum 后，以版本化 rubric
  的加权总分中位数生成 0–1 最终比分。
- `play`、`series` 和 `round-robin` 新增可重复的 `--judge`；禁止重复评委、自评和人类评委。
  个别评委失败可在 quorum 内降级，未达 quorum 则不存档、不更新 ELO。双局赛冻结并复用
  同一评审团；循环赛在 checkpoint 中冻结无凭据快照，跨进程恢复时严格核对后再调用模型。
- 评审协议严格拒绝缺失/额外字段、布尔值、NaN、Infinity、越界分数和超长理由；作品按
  不可信数据隔离，评委题面不包含参赛者或模型身份。档案只保存安全白名单评委描述、匿名
  映射、逐维裁决与可重算聚合，不保存原始响应、凭据或端点。
- 新增与 `entrant_id` / ELO 分离的 Provider `route_id`：同一规范化端点和模型不能通过
  direct/Profile、Key、名称或采样参数变化重复担任评委或规避自评。`PanelVerdict` 升至
  schema v3，冻结完整评审团并绑定任务、rubric、匿名映射和作品正文的请求摘要；旧 v1/v2
  裁决保持可读，MatchArchive v2 不变。路由摘要不保存原始端点，但它可关联且可能被字典
  猜测，不作为保密或远端模型真实性证明。
- 创意裁决使用当前 SQLite schema v8，在 `match_finished.data.judging` 中持久化，并复用
  现有双人总榜和分项目 ELO。系列与赛事档案逐层冻结同一评审团，严格只读审计会按事件重建
  裁决请求，并复核关系表、评分账本与 ELO。`play.command` 新增完全离线的创意单场、双局赛
  和三人循环赛入口。
- 新增手动触发的 `Live Provider Smoke` GitHub Actions 工作流：使用受 Environment 保护的
  OpenRouter Secret，按顺序从 3–9 个候选中探针选出最先通过严格评审协议的三个模型，再以
  两名 mock 参赛者执行固定 6 次正式评审请求。单次运行最多 15 次可能计费调用，正式阶段仍
  要求三个 Provider 路由全部成功；它不阻塞 PR。CI 与 Release 的 wheel/sdist 隔离安装也
  新增零费用三 mock 评委冒烟。
- `play`、`series` 与 `round-robin` 新增五项 Provider 硬预算：调用总数、累计 input、
  单次 output cap、累计 output 和本地预估美元。对应 CLI 为 `--max-provider-calls`、
  `--max-input-tokens`、`--max-output-tokens-per-call`、`--max-total-output-tokens` 与
  `--max-estimated-cost-usd`；每项独立按 CLI > `LLMOLYMPIC_*` 环境变量 > `[budget]` >
  默认值解析。
- 每批并发模型/算法调用会在创建 task 前原子预留；预算不足时不发出任何部分请求。调用包含
  重试和创意评委请求，input 以规范 messages JSON 的 UTF-8 字节数保守预留，output 以实际
  下发 cap 预留。缺失/非法 usage、异常、超时及 dispatch 后取消都按完整上界计费；仅明确
  未 dispatch 的取消会释放预留。`UsageError` 直接中止，不记技术负、不保存部分结果或 ELO。
- 费用配置只接受 `Decimal` 十进制字符串并以整数纳美元记账：上限向下取整，精确 spec 的
  USD/百万 Token 价格和每次调用估价向上取整。OpenAI 与 Provider Profiles 作为云端 LLM
  必须显式冻结准确价格；Ollama 是本地 LLM、mock 是算法，两者未显式给价时按零估价。该美元
  数值只是本地估算，不能替代 Provider 账户或网关的真实费用限额。
- SQLite schema 升至 v8，新增 credential/content-free 的 `provider_budgets` 与
  `provider_call_attempts`。`round-robin` 将 checkpoint 与冻结 policy 原子创建，使用同一
  runner lease generation 保护 reserve/dispatch/settle；takeover 释放旧未调度预留，并把旧
  已调度未知调用按完整上界计费。恢复拒绝预算 CLI，忽略当前预算 env/config，只使用 SQLite
  冻结值；旧无预算 checkpoint 保持兼容且不会被追溯加预算。
- `play` 的参赛者与创意评委、`series` 的两局分别共享整次内存预算；循环赛预算跨进程持续且
  封存后不可再用。`--allow-large-tournament` 只跳过静态规模确认，不能绕过硬预算。预算表不
  保存 Key、原始端点、请求头、模型名、prompt、response 或 Provider 原始异常；只保存 opaque
  route、整数 policy/计数、状态、时间和 lease generation。

## [0.3.0] - 2026-08-06

- SQLite schema 升至 v7；每次已计分的顶层 match、series 或 tournament 都在同一事务中
  获得唯一、单调递增的全局评分操作序号。v1–v6 会按既有评分历史写入顺序原子回填，失败
  时完整回滚。
- `audit-tournament` 现在按全局评分操作提交顺序确定性重放全部 ELO、局数和胜平负；目标赛事
  前后存在其他评分操作时也能完整核对当前排行榜，不再因缺少提交顺序而降级为 `partial`。
- v7 使用完整 SQLite schema manifest 核对列、约束、外键、索引语义和额外对象；旧迁移库会
  在同一事务内把 `matches` / `series_archives` 规范化为当前结构，并保留原始归档内容。
- 同时作答项目改为先快照同轮题面、并发收答，再按报名顺序确定性推进与归档；取消会回收
  同批任务，收答阶段的终局选手调用错误也会立即取消挂起对手并丢弃其他同批结果。POSIX
  默认终端输入改用可移除 reader，超时或取消后不再遗留抢输入线程。

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

# LLM Olympics 设计方案

一个人类与 LLM 同台竞技的**多项目竞技场**：知识问答、数学、推理、下棋、创意……
比赛项目插件化，后期新增项目无需改动引擎。支持人 vs LLM、LLM vs LLM。

## 1. 领域模型

| 概念 | 职责 |
|---|---|
| **Game**（比赛项目） | 插件。定义题目/局面生成、走法校验、终局计分 |
| **Player**（选手） | 统一抽象。`LLMPlayer`（调模型 API）与 `HumanPlayer`（外部输入）对引擎透明 |
| **Match**（对局） | 通用回合循环：发题面 → 收走法 → 校验推进 → 判分 |
| **Judge**（裁判） | 客观项目由 Game 规则判分；创意项目由匿名、多评委 `LLMJudgePanel` 异步裁决 |
| **Rating**（评分） | 标准 ELO（K=32），分项目 + 总榜，SQLite 持久化 |
| **Usage Budget**（用量预算） | Provider 调用前原子预留，按可信 usage 结算；支持内存和 SQLite v8 账本 |

### 1.1 选手身份与 Provider Profile

- `name` 是 Game、事件和比分映射沿用的对局内键；`display_name` 是展示名快照，
  新档案要求二者一致；是否为同一竞技身份只由 `entrant_id` 决定。
- `entrant_id` 是跨对局稳定的选手主键，ELO、榜单和评分历史都按它关联；同一
  对局中 `name` 与 `entrant_id` 均须唯一，避免同一身份以两个名字与自己比赛。
- 命名 Provider Profile 使用 `profile:<id>[:model]`；省略模型时读取 Profile 的
  `default_model`，稳定身份为 `profile:<id>:<model>`。Profile 只保存 provider、
  端点、模型、展示名和 API Key 环境变量名，不把凭据值写入选手描述或档案。
- 没有 Profile 或显式 `entrant_id` 的兼容 Provider 调用，会以 provider、模型、
  展示名和安全采样参数生成确定性摘要；这种兼容身份重命名后会变化，需要跨名称
  延续身份时应使用命名 Profile 或显式 ID。
- SQLite 的 `entrants.identity_json` 保存身份元数据（当前为 `kind`、安全采样参数及
  可用的 `profile_id`、`provider`、`model`）。第一次可信本地引擎观察会取代此前仅由
  外部导入占用的同 ID 元数据与展示名；一旦出现可信观察，身份元数据即不可变，冲突
  会拒绝整次事务。后续只有时间更新的可信本地对局可改展示名，导入档案不能改写它。
- `route_id` 与 `entrant_id` 正交：前者只用于评委路由去重和防自评，后者继续独立承担
  ELO 身份。内置云端/本地模型按协议族、规范化端点和精确模型派生路由，mock 按算法
  策略派生；Profile ID、Key、显示名和采样参数不参与。路由摘要不会进入
  `entrants.identity_json`，因此不改变既有 ELO 或 Profile 身份语义。

### 1.2 档案来源、兼容迁移与计分信任边界

- 新对局与双局赛分别使用 archive/series schema v2，循环赛使用 tournament
  schema v1；三者的新档案均记录 `source`：引擎生成为 `local_engine`，外部构造
  为 `external`。只有旧 match/series schema v1（包括历史上省略版本号的 JSON）
  读入时标为 `legacy`；tournament schema v1 不使用 legacy 来源。
- SQLite 使用 `PRAGMA user_version = 8`，以 `entrants`、`entrant_id` 和展示名快照
  持久化对局、系列赛、循环赛、循环赛检查点、runner lease、榜单及评分历史。v1–v6
  数据库升级在单一事务内完成；v5→v6 会增加 runner lease 表，v1–v6→v7 会按既有
  `rating_history` 写入顺序回填全局评分操作，并把历史 `matches` / `series_archives`
  规范化为当前表结构且原样保留归档 JSON；v7→v8 以 additive migration 新增 Provider
  预算及无内容调用尝试账本。旧 checkpoint 不会被追溯绑定预算。所有升级失败时都回滚且
  不提升版本号。
- 历史名称映射为 `legacy:` + `SHA-256(name.encode("utf-8"))`。计算使用名称的
  **精确 UTF-8 字节**，不做 Unicode 规范化或大小写折叠；legacy 命名空间与新
  Profile 身份隔离，不能根据旧显示名猜测为新的模型身份。
- 迁移只补齐关系表、来源和身份索引，已有 `archive_json` / `series_json` 原样保留；
  兼容字段只在读取时于内存中补齐。历史对局是否已计分由现有
  `rating_history` 反推，不能重算或重复计分。
- `SQLiteStore.save_match()` / `save_series()` / `save_tournament()` 默认
  `rating_source="imported"`，只存档、不计 ELO。`rating_source="engine"` 仅供
  本进程完成对局后的可信调用路径，
  双人对局才计分；它是调用方信任声明，不是认证、签名或对来源的密码学证明。
  同一 match/series/tournament ID 也不能通过重新保存从 imported 升级为 engine。

### 1.3 严格只读赛事审计

- `audit-tournament` 只审计调用方指定的一项循环赛或 checkpoint；SQLite 的完整
  `integrity_check` 和当前 schema v8 结构 manifest 覆盖整个文件，业务语义深验则限定在
  目标赛事。manifest 通过 `table_xinfo` / `table_list` / `index_list` / `index_xinfo` /
  `foreign_key_list` 及 fail-closed SQL token 解析，完整核对列、PK、UNIQUE、CHECK、FK、
  显式索引、排序/排序规则、partial predicate、STRICT/WITHOUT ROWID 和额外对象；不依赖
  空白、大小写或 `sqlite_autoindex` 名称做逐字 DDL 匹配。
- 审计连接使用 `mode=ro&immutable=1` 与 `query_only`，不构造 `SQLiteStore`、不迁移、
  不 chmod、不创建 sidecar、不修复数据，也不创建 Provider 或访问网络。发现活动 rollback
  journal/WAL，或审计前后主文件 device/inode/size/mtime 变化时保守失败。
- 进行中的 checkpoint 必须是确定赛程的连续完整系列前缀，且其保留的 series/match ID
  不得进入正式表；已封存 checkpoint 必须完整、封存时间不早于最后更新，并与正式赛事
  一一对应。checkpoint 创建、加载和审计还会按首个可信观察规则预检既有 `entrant_id`
  身份，避免不可封存的身份冲突拖到整场完成后才暴露。正式赛事继续深验参赛者、配对、
  series/match 关系和 canonical JSON。
- 已计分赛事还会核对正式档案身份与全局 `entrants.identity_json` 的绑定，并重算赛事内的
  冻结 ELO、逐局 contribution/history 和聚合 snapshot。schema v7 为每次已计分的顶层
  match、series 或 tournament 分配唯一、单调递增的 `rating_operation_seq`；审计按该
  提交顺序重放整个数据库的 ELO、局数、胜平负和更新时间，再与当前排行榜逐项比对。目标
  赛事前后即使还有其他评分操作，也不再需要退化为 `partial` 或依赖事件时间推断提交顺序。
- 运行期的 `round-robin --resume` 在把正式赛事视为完成前也使用正式档案深验，避免顶层
  JSON 尚可解析但关系索引或 ELO 已损坏时错误跳过恢复。
- checkpoint 使用 SQLite v6 跨进程 runner lease。claim 在 `BEGIN IMMEDIATE` 短事务内
  重载 checkpoint、拒绝活动 owner，并分配随机 capability token 与单调递增 generation；
  SQLite 只持久化 token 摘要。resume 必须先 claim，再重建 Provider，避免冲突进程产生调用。
- 租约默认 60 秒、后台每 15 秒续租，每组对阵开始前也主动续租；append 与 finalize 在各自
  原子事务中校验 token + generation + 过期时间。心跳对临时 SQLite `BUSY`/`LOCKED` 在
  当前租约有效窗口内退避重试。已过期或被接管的 v6 runner 即使仍有在途 Provider 请求，
  也会被 fencing 拒绝继续保存 checkpoint、封存赛事或更新 ELO。
- release 使用 compare-and-set，不能释放后来执行者的 generation；崩溃后由过期接管惰性
  清理，显式 expiry 也保留 generation 单调性。任何 lease 事务都不跨 Provider 网络调用；
  只实现同步 `chat()` 的旧适配器在线程中发出的单次请求仍可能无法立即取消。
- v5→v6 升级前必须停止旧版 runner；已加载的 v5 代码没有 lease 写入校验，新版 schema
  无法反向为旧进程注入 fencing。

### 1.4 Provider 用量与硬预算

- 五个配置字段是 `max_provider_calls`、`max_input_tokens`、
  `max_output_tokens_per_call`、`max_total_output_tokens` 和
  `max_estimated_cost_usd`；每个字段独立按 CLI > `LLMOLYMPIC_*` 环境变量 >
  `[budget]` > 默认值解析。总预算默认关闭，单次输出 cap 默认 1024 Token；改变 cap
  本身也会启用预算执行。
- 每个 Provider transport attempt 预留一次调用，包括非法输出重试和每份创意作品的独立
  评审。input 上界是 system/user messages 以固定字段排序、无多余空白 JSON 序列化后的
  UTF-8 字节数，保证不依赖供应商 tokenizer；output 上界是 Provider 能真正下发的 cap。
  Provider 报告有效 usage 时按实际 input/output Token 结算。未报告或非法 usage、超时、
  Provider 异常及 dispatch 后取消都按完整上界计费；只允许明确未 dispatch 的预留释放。
- Match 同时作答和 Judge 独立盲评在创建任何 task 前调用共享账本的 `reserve_many()`。
  整批调用、input、output 和估价先作一次原子校验；任一维度超限则整批不预留、不建 task、
  不调用 Provider。报告值超过预留时记录 violation、毒化账本并禁止后续 dispatch，不能以
  Provider 错误或技术负吞掉 `UsageError`。
- 金额只用整数纳美元。CLI、环境变量、TOML 与 `[pricing]` 的金额先以 `Decimal` 读取；
  USD 上限向下取整到纳美元，USD/百万 Token 价格向上取整到纳美元/百万 Token，每次调用
  的 input + output 估价再向上取整到 1 纳美元。启用费用上限时，所有云端 route 必须按
  精确 `openai:<model>` 或 `profile:<id>:<model>` spec 显式给价；同一 `route_id` 的冲突
  价格和动态 OpenRouter 路由均 fail closed。OpenAI 与 Provider Profiles 是云端 LLM，
  Ollama 是本地 LLM，mock 是算法，human 是外部人类；Ollama/mock 未显式给价时按零估价。
- `play` 的参赛者和评委共享一个内存 `UsageBudget`；`series` 两局共享同一个内存账本。
  `round-robin` 在创建 checkpoint 的同一事务中写入 SQLite v8 budget，冻结限额、output cap、
  input-bound/cost-rounding 版本及 `route_id → price` policy。每次 reserve/dispatch/settle
  都受 runner lease generation fencing；takeover 会释放旧 generation 的 reserved attempt，
  并把 dispatched attempt 按完整上界记为 unknown，然后才允许新 runner 继续。
- `round-robin --resume` 拒绝所有显式预算 CLI 选项，并只读取 SQLite 中的冻结 policy；当前
  `[budget]`、`[pricing]` 及预算环境变量不会改变旧赛事。历史无预算 checkpoint 继续无预算
  恢复。`--allow-large-tournament` 只跳过静态规模确认，绝不改变硬预算状态机或额度。
- 账本只持久化 opaque `route_id`、整数 policy/限额/累计值、attempt 状态、时间和 lease
  generation，不保存 API Key、Key 哈希、原始端点、请求头、模型名、prompt、response 或
  Provider 原始异常。美元限额只是按本地冻结价格得到的估算，不能替代 Provider 账户、组织
  或网关侧的真实支付限额。

## 2. 统一 Game 接口（核心设计）

```python
class Game(Protocol):
    name: str
    def new_state(self, players: list[str], seed: int) -> GameState: ...   # 同 seed 同局面，可复现
    def current_players(self, state) -> list[str]: ...                    # 轮到谁
    def prompt_for(self, state, player) -> str: ...                       # 题面/局面
    def apply_move(self, state, player, move) -> None: ...                # 校验+推进，非法抛 IllegalMoveError
    def is_over(self, state) -> bool: ...
    def score(self, state) -> dict[str, float]: ...                       # 1.0 胜 / 0.5 平 / 0.0 负，或按比例
```

项目可以额外声明 `min_players` / `max_players` 人数元数据；未声明的旧插件
默认兼容为至少一名选手且不设上限。五子棋与国际象棋声明为恰好两名选手。

**核心洞察：单轮问答只是"每个选手恰好只有一步"的多轮对局特例。**
接口按通用回合制设计后，数学/问答（每人一步）、下棋（交替多步）、
猜谜（多题竞答）都套进同一个 Match 循环，引擎对项目类型一无所知。

**后期新增项目 = 在 `games/` 加一个实现该接口的模块并登记注册表**，
引擎、判分、ELO、CLI/Web 界面零改动。

## 3. 项目分类与判分

| 类型 | 交互模式 | 判分 | 状态 |
|---|---|---|---|
| 数学 math_quiz | 单轮 | 规则（数值提取+容差） | ✅ 已实现 |
| 知识问答 knowledge_quiz | 单轮 | 规则（选项匹配） | ✅ 已实现 |
| 逻辑推理 reasoning_quiz | 多题问答 | 规则（唯一解 + 选项匹配） | ✅ 已实现 |
| 猜谜 riddle_quiz | 多题问答 | 规则（结构化线索 + 选项/别名匹配） | ✅ 已实现 |
| 五子棋 gomoku | 多轮有状态 | 15×15 自由规则（五连胜负） | ✅ 已实现 |
| 国际象棋 chess | 多轮有状态 | 标准规则（SAN/UCI、胜负与和棋） | ✅ 已实现 |
| 创意写作 creative_writing | 单轮开放作答 | LLM 评审团（匿名、独立盲评、加权中位数） | ✅ 阶段三已实现 |

创意写作的每局限定两名参赛者。Game 在全部作品收齐后提供版本化
`JudgingRequest`；Match 在同步 `score()` 路径之前调用异步评审团，并把完整可重算裁决
放入 `match_finished.data.judging`。评委规模为 3–9 名，法定人数采用严格多数；同一评委
必须完整评完全部有效作品才能参与聚合。每份作品单独送审，题面只含匿名标签，不含参赛者
或模型身份；正文被视为不可信数据。`PanelVerdict` schema v3 冻结完整 `panel`，要求
评委 ID 和 `route_id` 均唯一，正常裁决的成功/失败记录必须精确覆盖该快照；全固定分路径
虽然不调用评委，也仍保留同一快照。v3 额外绑定规范化请求摘要，将任务、rubric、匿名映射
和作品正文与裁决证据连接；深度审计按事件重建请求并复核摘要。旧 v1/v2 裁决兼容读取，
但 v1 不具备已验证的路由独立性。嵌套裁决版本升级不改变 MatchArchive v2；当前 SQLite
schema v8 另行承载 Provider 预算账本。

创意 `series` 的两局复用一个 `JudgePanelSnapshot` 并原子计分；`round-robin` 在零进度
checkpoint 中冻结同一快照，恢复时由当前凭据重建评委并在模型调用前验证安全描述与路由。
每个系列、最终赛事档案和所有非技术负裁决都必须与顶层快照一致。参赛者和评委共享预算；
SQLite v8 账本、runner lease、接管封账与深度审计覆盖整项创意循环赛。

持久化的 `route_id` 是对配置路由的稳定伪名，不包含 Key、请求头或原始端点，但常见端点
仍可能被字典枚举，不能把它当作保密机制。规范化不会执行 DNS 查询，也不会合并 CNAME、
回环地址别名或供应商模型别名；它保证的是配置请求路由去重，不是远端模型版本真实性。

非法走法规则：解析失败/走法非法给有限次重试（默认 3 次），重试题面会附上
上次输出与拒绝原因。达到上限后，问答判该题不得分；双人棋类立即技术判负。

五子棋首版固定玩家列表第一位执黑、第二位执白；坐标为 `A1` 到 `O15`，
四个方向连续 5 子或以上获胜，不实现禁手或交换开局。赛事层已支持双方
交换先后手各赛一局，降低黑方先手优势。

国际象棋固定玩家列表第一位执白、第二位执黑，使用 `chess` 规则引擎校验
SAN/UCI、将军、易位、吃过路兵、升变和所有标准终局。状态保存初始 FEN 与
规范 UCI 历史，每次重放得到棋盘；不能只保存当前 FEN，否则会丢失重复局面
历史。由于当前 Game 接口没有单独的申请和棋动作，三次重复与五十回合的可
申请和棋自动执行；该策略及规则引擎版本会写入 `game_config`。双局赛交换完整
玩家顺序，因此双方各执白一次。

## 4. 事件驱动架构（为 Web / 手机端留路）

Match 循环的每一步产出**结构化事件**（`match_started` / `turn_prompt` /
`move_received` / `move_rejected` / `match_finished`），界面层只消费事件渲染：

```
CLI                  Web / WebSocket
        \            /
         Match 事件流 ── core 引擎（不含任何界面代码）
```

- `HumanPlayer.get_move` 是异步接口：CLI 里是键盘输入，将来是 API/WebSocket 远端提交，引擎无感。
- `LLMPlayer` 通过 Provider 的原生异步 `achat()` 调用模型；OpenAI / Ollama
  请求可在单步截止时间到达时取消，不依赖无法强制停止的工作线程。
- LLM 超时或 Provider 异常会产生 `move_rejected`，并以 `reason_code`、
  `forfeit_scope=match` 和 `technical_loss=true` 记录机器可读原因；随后仍会产生
  `match_finished`，失败方计 0 分，对手计 1 分。
- 上述字段只保证出现在新档案中；读取没有 `termination` 的 schema v1 历史档案
  时按 `unknown` 处理，不能把缺失字段解释成 `completed`。
- **手机端迁移路径**：后端（Python + FastAPI）不动，手机只是新客户端，
  通过 WebSocket 消费同一批事件。客户端优先 React Native（与 Web 前端同源）。
  API key 只存服务端，判分计时在服务端，天然防作弊。

## 5. 题目来源与公平性

- **程序动态生成为主**（数学、推理）：模板 + 随机参数现场生成，
  不在任何模型训练集里，模型间对比才有意义。元数据标 `source: generated`。
- **版本化结构化题库**（猜谜）：从对象特征库按 seed 组合线索、同类干扰项和
  选项顺序，不把静态散文谜面伪装成动态生成；元数据标
  `source: generated_from_structured_bank`，同时记录题库与生成器版本。
- **静态题库为辅**（知识竞答）：可测"知识量"，但接受模型可能见过的偏差，
  标 `source: static`，报表分开统计。
- 问答项目的所有选手拿到逐字相同的 prompt；棋类按各自角色显示同一局面；
  LLM 统一单步超时与 max_tokens；人类输入使用独立限时；
  采样参数、每步走法、完整事件流记入对局档案（pydantic，可 JSON 序列化），结果可复核。
- 同时作答项目先快照同轮全部 prompt，并发收齐结果后才按报名顺序推进状态和输出事件；
  Provider 完成先后不会改变档案，也不会把同轮答案写进其他选手的题面。收答阶段的终局
  选手调用错误会丢弃其他同批结果并取消、回收挂起任务；`apply_move` 仍按报名顺序提交，
  因而第三方同时作答 Game 若把非法走法定义为整场判负，必须自行接受该确定性提交语义，
  或提供不会在校验失败前修改状态的实现。单个共享终端仍无法为多名人类提供彼此隔离的
  私密输入，多人类实战需使用 Web/独立客户端。
- 推理/猜谜档案保存 seed、完整题面、走法和生成器/题库版本，不复制插件内部
  标准答案；审计时需使用对应版本代码按 seed 重放，档案本身尚不能脱离代码独立判分。
- 每场结束后，完整 JSON 档案、选手索引、总榜/项目榜 ELO 与评分历史在同一
  SQLite 事务中写入；`match_id` 防止重复计分。双局赛把两局放进同一事务，
  以系列开始前的同一 ELO 期望值批量累计，避免逐局更新造成顺序漂移。
  `rating_history` 仍逐局留痕，但系列赛行记录的是冻结期望值下的贡献账；审计时
  需通过 `series_matches` 关联 `series_archives.rating_policy=elo_batch_v1`，不能
  把第二局单独按其行内 `rating_before` 再算一次标准 ELO。
- 循环赛让每个无序选手对完成一次双局赛。各对阵 seed 从赛事 seed 和双方稳定
  `entrant_id` 确定性派生，同一对阵的两局共用 seed。首局前会冻结赛事 ID、
  项目配置、顺序敏感的选手描述、seed、超时和赛程；每完成一个双局对阵便以不计分
  prefix 原子追加到 checkpoint。恢复时核对 `Game.describe_config()` 与完整 Player
  descriptor，只运行未完成后缀；Profile 仅从检查点取得 ID 和已解析模型，API Key
  始终从当前进程的环境变量重新获取，不进入 checkpoint、哈希或档案。
  全部完成后才在最终封存事务开始时读取并冻结当前 ELO，批量计算所有贡献，再在
  同一事务中封存正式赛事、系列、对局和评分历史；中断期间先落库的其他计分对局
  会先进入该基准分。这样可避免
  同一组已完成对局在处理或保存顺序不同时产生等级分漂移。有状态或随机
  Provider 的输出仍可受实际执行顺序影响，不应把 ELO 性质误读为模型输出确定性。
- 人类选手限时作答；同一模型跑 N 局取平均，降低采样运气成分。

## 6. 比赛模式

- **单挑**：人 vs LLM 或 LLM vs LLM，同题同时作答（已实现）。
- **交换先手双局赛**：两名非人类选手交换顺序各赛一局（已实现）。
- **循环赛**：3–16 个非人类选手两两进行交换顺序双局赛，依次按总局分、胜局数、
  较少技术负排名，完全同绩时按 `entrant_id` 确定展示顺序，并展示 ELO 净变化
  （已实现）。当前串行执行，每完成一组双局对阵就保存检查点；`--resume`
  跳过已完成 prefix，全部完成时才封存正式档案并更新一次 ELO。超过默认对局/
  调用规模阈值需显式使用 `--allow-large-tournament`；该确认不绕过 Provider 硬预算。
- **锦标赛**：单场多题总分制（阶段四）。

## 7. 技术栈

- **语言**：Python 3.11+（各家 LLM SDK 最全，引擎到 Web 一门语言贯通）
- **建模**：Pydantic v2（状态、事件、档案）
- **CLI**：Typer + Rich
- **Web（阶段四）**：FastAPI + WebSocket；前端 React
- **存储**：SQLite（对局记录、总榜/项目榜、ELO 历史；题库待接入）
- **棋类（阶段二）**：纯 Python 15×15 五子棋；标准国际象棋由 `chess` 规则引擎校验
- **模型接入**：OpenAI 官方异步 SDK / Ollama 异步 HTTP / Mock（离线演示与测试）；
  同步 `chat()` 仅作为无硬超时的第三方 Provider 兼容接口
- **工具**：uv 或 pip、pytest、ruff

## 8. 路线图

1. **MVP** ✅：core 引擎（回合循环 + 事件流）+ 数学/知识问答两个单轮项目 +
   provider 抽象（openai/ollama/mock）+ CLI，LLM vs LLM 与人类入场。
2. **阶段二（已完成）**：ELO + SQLite 持久化 ✅；五子棋多轮状态机 ✅；
   LLM 超时、Provider 异常技术判负与失败档案 ✅；交换先手双局赛与公平批量
   ELO ✅；逻辑推理与结构化猜谜项目 ✅；标准国际象棋与换色双局赛 ✅；
   稳定 entrant 身份、命名 Provider Profiles 与 SQLite v3 迁移 ✅；
   公平循环赛与 SQLite v4 迁移 ✅；逐对阵 checkpoint/resume 与 SQLite v5 迁移 ✅；
   循环赛严格只读深度审计与恢复完成态校验 ✅；跨进程 runner lease、fencing 与
   SQLite v6 迁移 ✅；全局评分操作账本、确定性 ELO 重放与 SQLite v7 迁移 ✅。
3. **创意 + LLM 评审团（已完成）**：双人创意写作与单场 CLI ✅；逐作品匿名盲评、
   3–9 名评委多数 quorum、严格 JSON 与加权中位数聚合 ✅；双人档案与 ELO ✅；
   Provider 硬预算与参赛者/评委共享预算 ✅；SQLite v8 跨进程预算账本 ✅；冻结评审团的
   创意双局赛与循环赛 ✅；checkpoint/resume、runner lease、请求证据绑定及深度审计 ✅。
4. **Web 化 + 锦标赛（进行中）**：4.1 以 FastAPI 提供仅限回环地址的只读 REST、
   排行榜与已完成档案的 WebSocket 事件回放 ✅；4.2 提供随 Python 发行包离线分发的
   React 观战大厅、ELO 榜、对局详情和可控制事件时间线 ✅。读取层使用 SQLite `mode=ro`，
   公开协议与内部档案模型隔离。后续切片再加入运行中事件 broker、远程人类输入和锦标赛
   模式；之后新增项目继续保持纯插件接入。

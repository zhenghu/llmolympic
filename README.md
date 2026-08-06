# LLM Olympics

人类与 LLM 同台竞技的多项目竞技场：知识问答、数学、推理、下棋、创意……
比赛项目插件化，支持人 vs LLM、LLM vs LLM。设计细节见 [DESIGN.md](DESIGN.md)。

## 安装

### GitHub Release

v0.3.0 通过 GitHub Release 提供 Python wheel，可直接从发布地址安装：

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install \
  https://github.com/zhenghu/llmolympic/releases/download/v0.3.0/llmolympic-0.3.0-py3-none-any.whl
```

安装后可核对版本并检查本地运行环境；`doctor` 不会连接模型服务或显示 API Key：

```bash
llmolympic --version
llmolympic doctor
llmolympic games
```

本次 v0.3.0 的发布范围是 GitHub Release，不包含 PyPI 发布或独立 macOS 应用包。
wheel 提供 `llmolympic` 命令；双击启动器 `play.command` 随源码仓库和 GitHub 自动生成的
源码归档提供，不包含在 wheel 中。

本仓库原创代码及 `llmolympic` 发行归档采用 [MIT License](LICENSE)，允许在保留版权与
许可声明的前提下使用、修改、分发和商业使用。外部依赖保留各自许可证；国际象棋功能
依赖 GPL-3.0-or-later 的 python-chess，组合使用或再分发时还须遵守其条款。详见
[第三方许可说明](THIRD_PARTY_NOTICES.md)。

### 源码开发安装

从源码运行或参与开发时，在仓库根目录创建 editable 环境：

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
```

用 OpenAI 模型对战需设置 `OPENAI_API_KEY`（见 `.env.example`）；
本地模型可用 [Ollama](https://ollama.com)；没有任何 key 时用内置 mock 选手即可体验。

## 配置模型

源码安装时，可在项目根目录复制模板生成自己的配置（`config.toml` 含密钥，已被 git
忽略）：

```bash
cp config.example.toml config.toml
```

编辑这个固定的项目配置，启动时会自动加载：

```toml
[openai]
api_key = "sk-..."                        # 对应环境变量 OPENAI_API_KEY
# base_url = "https://api.deepseek.com/v1" # DeepSeek/Kimi 等兼容接口填这里

[ollama]
# base_url = "http://localhost:11434"

# 命名 Profile 可让多个 OpenAI 兼容端点同场对战。
# 只填环境变量名，不要把 Key 写到 Profile 中。
[profiles.kimi]
provider = "openai"
default_model = "moonshot-v1-128k"
base_url = "https://api.moonshot.cn/v1"
api_key_env = "KIMI_API_KEY"
display_name = "Kimi"

[profiles.deepseek]
provider = "openai"
default_model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
display_name = "DeepSeek"

[profiles.local]
provider = "ollama"
default_model = "llama3.1:8b"
base_url = "http://localhost:11434"
display_name = "Local Llama"

[storage]
# database = "~/.llmolympic/llmolympic.db"

[match]
# llm_timeout_seconds = 120.0 # 对应环境变量 LLMOLYMPIC_LLM_TIMEOUT
```

wheel 安装不会把 `config.example.toml` 或用户配置写入 site-packages，也不会自动读取
当前工作目录中的配置。请从源码仓库取得 [config.example.toml](config.example.toml)，
保存到自己的配置目录，并显式指定路径：

```bash
mkdir -p "$HOME/.config/llmolympic"
# 将 config.example.toml 复制到下面的路径并按需编辑
export LLMOLYMPIC_CONFIG="$HOME/.config/llmolympic/config.toml"
```

源码安装仍可自动读取仓库根目录的 `config.toml`；也可以使用同一个
`LLMOLYMPIC_CONFIG` 方式覆盖它。

查找顺序为：`LLMOLYMPIC_CONFIG` 指定的文件 > 源码项目根目录的
`config.toml`。程序不会扫描任意当前工作目录中的 `config.toml`，避免环境中的
API Key 被意外发送到不可信配置指定的兼容端点。配置项取值优先级仍为：
环境变量 > 选中的配置文件 > 默认值。含密钥的配置文件建议设置为仅本人可读：

```bash
chmod 600 config.toml
# 使用显式配置路径时：chmod 600 "$LLMOLYMPIC_CONFIG"
```

### 升级已有数据库

首次用当前源码版本打开旧版 SQLite 存档时，程序会在事务内将 schema 升级到
v7 并保留既有档案和 ELO。v7 会按既有 `rating_history` 的 SQLite 写入顺序回填全局
评分操作序号，并把历史迁移产生的 `matches` / `series_archives` 表规范化成与新建 v7
数据库相同的结构，同时原样保留归档 JSON。升级前请停止所有正在写入该数据库的赛事进程，
并使用 SQLite 备份机制制作一致备份；如果直接复制文件，必须同时处理同名的 `-wal` 和
`-shm` 文件。升级后的数据库不应再交给只支持旧 schema 的版本打开。可先运行
`llmolympic doctor --db 路径` 进行只读检查；`doctor` 不执行迁移。
若旧库包含非标准或被篡改的表、额外 view/trigger，或者 ELO 历史缺失、交错、孤立或无法
确定性重放，升级会安全拒绝；迁移事务会整体回滚，原 schema 与数据保持不变。

Profile ID 只允许字母、数字、点、下划线和连字符。`provider`
目前支持 `openai` 和 `ollama`。OpenAI 兼容 Profile 必须声明
`api_key_env`，程序只在创建该 Provider 时读取对应环境变量；
不会隐式复用另一个端点的 Key，也不会继承全局 OpenAI SDK 的组织、项目或
自定义请求头，更不会把 Key 写入对局档案。
所有携带 API Key 的远程 OpenAI 兼容端点都必须使用 HTTPS；明文 HTTP
只允许 `localhost`、`127.0.0.0/8` 或 `::1` 回环地址。

普通 `openai:model` 语法仍从 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 或 `[openai]`
读取所选 Key 与端点。当端点不是精确的 OpenAI 官方默认地址时，客户端不会把
`OPENAI_ORG_ID`、`OPENAI_PROJECT_ID`、`OPENAI_CUSTOM_HEADERS` 等全局 SDK 设置
转发到兼容服务，避免不同端点的请求头或凭据发生串用。官方默认端点继续保留 SDK
对这些环境设置的兼容行为；多端点比赛仍推荐使用隔离边界更明确的命名 Profile。

```bash
export KIMI_API_KEY="..."
export DEEPSEEK_API_KEY="..."

# 使用各 Profile 的 default_model
llmolympic play --game math_quiz --players profile:kimi,profile:deepseek --seed 42

# 只覆盖某个 Profile 的模型（模型名中可继续包含冒号）
llmolympic play --game chess --players profile:local:llama3.1:8b,mock:fixed
```

命名 Profile 选手的稳定身份为 `profile:<id>:<model>`；`display_name`
作为对局内 `name` 和界面展示使用，但不参与身份或 ELO 关联。更改显示名不会
创建新的 ELO 身份，更换 Profile ID 或模型则会。
同场出现重复展示名时，CLI 会附加 Profile 和模型进行消歧。

## 运行

macOS 可以在 Finder 中双击 `play.command`。菜单可直接启动五子棋、国际象棋、
数学、知识、逻辑推理和猜谜竞答；全部六个项目都提供三个 mock 的离线循环赛入口，
并保留已有的人类对战、两个 mock 自动演示和棋类换先手双局赛。

```bash
# 两个 mock 选手演示（离线，无需 key）
llmolympic play --game math_quiz --players mock:random,mock:fixed --rounds 5

# 人对战 GPT
llmolympic play --game knowledge_quiz --players human:我,openai:gpt-4o-mini

# 两个模型对战（同 seed 同题，公平对比）
llmolympic play --game math_quiz --players openai:gpt-4o-mini,ollama:llama3.1 --seed 42

# 两个命名兼容端点对战（推荐）
llmolympic play --game math_quiz --players profile:kimi,profile:deepseek --seed 42

# 动态逻辑推理：排序约束与三位密码题都会先由程序穷举确认唯一解
llmolympic play --game reasoning_quiz --players mock:random,mock:fixed --rounds 5 --seed 42

# 猜谜竞答：按 seed 从版本化结构化题库组合线索与同类干扰项
llmolympic play --game riddle_quiz --players human:我,mock:random --rounds 5 --seed 42

# 覆盖默认的 LLM 单步限时
llmolympic play --players openai:gpt-4o-mini,mock:fixed --llm-timeout 90

# 五子棋：第一个选手执黑先行，第二个选手执白
llmolympic play --game gomoku --players human:我,openai:gpt-4o-mini

# 五子棋离线演示（mock 会读取棋盘并选择空位）
llmolympic play --game gomoku --players mock:random,mock:fixed

# 公平双局赛：同一 seed，各执黑一次，两局一起存档并批量更新 ELO
llmolympic series --game gomoku --players mock:random,mock:fixed --seed 42

# 三名以上非人类选手循环赛：每一对选手交换顺序各赛一局
llmolympic round-robin --game knowledge_quiz \
  --players profile:kimi,profile:deepseek,profile:local --rounds 5 --seed 42

# 从开赛时显示的赛事 ID 恢复中断的循环赛（自定义数据库需再次指定）
llmolympic round-robin --resume <TOURNAMENT_ID> --db ~/.llmolympic/llmolympic.db

# 严格只读审计一项进行中或已封存的循环赛；--json 适合 CI/脚本
llmolympic audit-tournament <TOURNAMENT_ID> --db ~/.llmolympic/llmolympic.db
llmolympic audit-tournament <TOURNAMENT_ID> --db ~/.llmolympic/llmolympic.db --json

# 国际象棋：第一个选手执白；接受 SAN（e4、O-O）或 UCI（e2e4）
llmolympic play --game chess --players human:我,mock:random

# 国际象棋离线双局赛：交换颜色后一起存档并批量更新 ELO
llmolympic series --game chess --players mock:fixed,mock:illegal --seed 42

# 列出所有比赛项目
llmolympic games

# 查看总体 / 分项目 ELO 榜
llmolympic leaderboard
llmolympic leaderboard --game math_quiz

# 查看对局历史与完整档案
llmolympic history
llmolympic archive <MATCH_OR_SERIES_OR_TOURNAMENT_ID>
```

五子棋采用 15×15 自由规则：黑棋先行，横、竖或斜线连续 5 子或以上获胜，
没有禁手；满盘无人获胜则和棋。坐标为 `A1` 到 `O15`，`A1` 在左上角，
中心是 `H8`。选手连续 3 次非法落子，或人类选手超时未落子，会立即判负。
`--rounds` 用于数学、知识、逻辑推理和猜谜项目，不适用于棋类（包括双局赛和循环赛）。

国际象棋采用标准初始局面，玩家列表第一位执白、第二位执黑。规则引擎完整校验
将军、将死、王车易位、吃过路兵与升变；输入严格接受一个 SAN 或 UCI 走法。
局面保存规范 UCI 历史，因此三次/五次重复仍可准确复核。当前终端交互没有单独
的“申请和棋”动作，所以有权按三次重复或五十回合申请和棋时，竞技场会自动申请；
逼和、子力不足、五次重复和七十五回合仍按标准自动终局。连续 3 次非法走法、
超时或 Provider 故障立即判负。

`reasoning_quiz` 的题目由本地程序动态生成，目前包含排序约束和三位密码推理；
生成器会枚举完整候选空间，只有唯一解的题目才会进入比赛，最多 50 题。
`riddle_quiz` 从 12 个版本化结构化对象中选择目标，再按 seed 随机组合三条线索、
同类别干扰项和选项顺序；单场不重复目标，因此最多 12 题。两者均使用 A–D
客观判分，不调用 LLM 出题或评审。逻辑题记录 `source=generated`，谜题记录
`source=generated_from_structured_bank`，生成器和题库版本写入对局配置供审计。
答案可以是选项字母或完整选项；猜谜还接受题库登记的同义名称。

同时作答的项目会先快照同轮所有题面，并发收齐答案后，再按选手报名
顺序校验、推进状态和输出事件。Provider 响应快慢不会改变归档顺序，后完成的
选手也不会看到同轮对手的答案或由其导致的状态变化。非法答案会进入下一个
并发重试批次；若收答阶段出现结束整场的选手调用错误，引擎会丢弃其他同批结果，并立即
取消、回收尚未完成的选手任务。取消整场对局时，同批未完成的任务也会一并取消和回收。
原生异步 Provider 会收到取消；POSIX 上使用内置 `input` 的终端 `HumanPlayer`
通过事件循环的可移除 stdin reader 收答，超时或取消后不会留下抢占下一次输入的
后台线程。自定义 `input_fn`、不可选择的 stdin 以及不支持 reader 的事件循环会安全
回退到工作线程，仍受线程无法强制终止的限制；不启用硬超时的旧同步 Provider 也有
同样限制。

这保证了引擎与远程/独立客户端的盲答语义。单个共享终端仍不能为多名人类
提供彼此隔离的私密输入界面；本地 reader 只会串行化共享 stdin 的提示，不会隐藏
已经输入的内容。多人类实战应使用后续 Web/独立客户端。

`series` 固定进行两局：第一局按命令中的选手顺序，第二局完整交换顺序；两局
使用相同 seed。五子棋中这表示双方各执黑一次，国际象棋中表示双方各执白一次。
两局会在一个 SQLite 事务中
原子存档，并基于系列赛开始前的同一 ELO 期望值批量计分，所以各胜一局不会
因保存顺序产生积分漂移。榜单场次和胜平负仍按两局分别累计。
问答项目也可使用 `series`：两局题目条件相同，但模型各自重新采样，用于观察
输出波动；它不代表问答项目存在先后手优势。

当前终端版 `series` 仍只接受 LLM/mock，以便在所有支持的平台和自定义输入环境中
保持一致的可取消保证。库调用在 POSIX 内置 stdin 或其他可取消输入后端上可以安全
复用 `HumanPlayer` 跑双局赛；若走工作线程 fallback，超时后仍不得贸然开始第二局。
单局 `play` 的人类对战不受影响。

`round-robin` 接受 3–16 名 LLM/mock/Profile 选手，不接受人类选手。每个无序
选手对运行一次双局赛，因此 N 名选手会产生 `N*(N-1)/2` 个系列、`N*(N-1)`
场对局。每个对阵的 seed 由赛事 seed 和双方稳定 `entrant_id` 确定性派生，
同一对阵的两局共用派生 seed 并完整交换选手顺序。最终表汇总局分、胜平负、
技术负和 ELO 净变化。排名依次按总局分、胜局数、较少技术负排序；完全同绩时
仅用稳定 `entrant_id` 生成确定的展示顺序。

循环赛会在首次 Provider 调用前创建 SQLite 检查点并显示赛事 ID、当前进度和
完整恢复命令。每完成一组交换顺序双局赛就立即追加一个检查点；`Ctrl-C`、
进程或机器中断后，使用 `round-robin --resume <TOURNAMENT_ID>` 会跳过已保存的完整
对阵，从下一组继续。中断时尚未完成的当前一组不会保存，恢复时会整组重跑。

恢复时不能重新指定 `--game`、`--players`、`--rounds`、`--seed` 或超时选项：
项目配置、顺序敏感的选手身份和模型、seed、超时及赛程已由检查点冻结。Profile
恢复规格会使用开赛时已解析的显式模型，因此之后修改 `default_model` 不会偷换参赛
模型。检查点只保存无密钥的选手描述，不保存 API Key、Key 哈希或 Provider 客户端；
恢复进程会从当前的环境变量和 Profile 配置重建 Provider，所需 Key 必须仍可用。如果新赛事
使用了自定义 `--db`，恢复时也必须指向同一数据库。
创建和恢复 checkpoint 时会先核对已有可信 `entrant_id` 的身份绑定；若同一稳定 ID 已绑定
到不同模型/Profile 身份，会在首次或下一次 Provider 调用前拒绝，避免跑完整场后才封存失败。

每个进行中的 checkpoint 都由 SQLite 跨进程 runner lease 保护。新赛事会在首次模型调用前
创建空 checkpoint 并领取执行权；恢复进程则会先在短写事务中原子领取执行权并重载 checkpoint。
恢复时若已有未过期执行者，会在重建 Provider 和发起任何模型调用前退出；如果 checkpoint
已经完整，则无需重建 Provider，直接原子封存并结算 ELO。租约默认有效 60 秒，每 15 秒
后台续租，并在每组双局赛开赛前再次续租；checkpoint 追加也会在同一事务中校验并延长租约。
租约 token 只在领取进程内持有，
数据库仅保存摘要；单调递增的 generation 会 fencing 已过期或已被接管的旧执行者，使其不能
继续保存进度、封存赛事或更新 ELO。

`Ctrl-C` 和正常错误路径会尽力立即释放租约；进程或机器崩溃后，其他进程可在租约过期后接管，
无需手工清理。心跳丢失会中止当前循环赛任务；临时 SQLite `BUSY`/`LOCKED` 会在当前租约
有效窗口内退避重试。所有领取、续租、保存和封存操作都只使用短 SQLite 事务，不会跨
Provider 网络调用持有数据库写锁。只实现同步 `chat()` 的旧第三方
Provider 请求在线程中开始后无法被 Python 强制终止，因此极端情况下已在途的一次请求可能
短暂继续；旧 runner 仍会被 generation 阻止写入任何 checkpoint 或 ELO。

从 SQLite v5 升级前应先停止仍在运行的旧版 `round-robin` 进程；旧进程本身不理解 v6 lease，
不能依靠升级后的数据库反向约束已经加载的旧代码。

### 严格只读赛事审计

`audit-tournament` 不创建 Provider、不访问网络，也不经 `SQLiteStore` 的初始化、迁移或
权限收紧路径。它以 immutable、query-only 快照执行完整 SQLite integrity check、当前
schema v7 结构 manifest 与外键完整性检查，再深度核对指定赛事的 checkpoint 连续前缀、
正式赛事、参赛者/配对/系列/对局关系索引、checkpoint 与既有可信身份的可封存性、已计分
赛事的稳定身份绑定，以及赛事 ELO 快照、逐局贡献和评分历史。进行中的赛事会报告可恢复进度；
已封存赛事会验证正式档案。manifest 会核对表与列、PK、UNIQUE、CHECK、FK、显式索引及
列顺序、DESC、collation、partial predicate、STRICT / WITHOUT ROWID，并拒绝额外的表、
视图或触发器。CHECK 与 partial predicate 使用 fail-closed 的规范化 SQL token 解析，
不会因无关空白、大小写或 SQLite 自动索引名不同而误报。命令只报告、不修复数据。

活动 runner 持有未过期租约时，进行中 checkpoint 仍会展示已保存进度，但
`resumable=false`；租约释放或过期后才会报告为可恢复。审计不会顺手释放或过期租约。

为避免把不一致快照误报为健康，审计前或审计过程中出现同名 `-journal` / `-wal`、主文件
发生变化时会退出失败；请先停止写入该数据库的比赛进程再重试。旧 schema 也只报告需要
迁移，不会原地升级或 chmod。退出码 `0` 表示指定赛事通过，`1` 表示数据库/赛事不一致，
Typer 参数错误使用退出码 `2`；`--json` 输出不包含选手、模型、题面、端点或原始异常。

赛事自身的 ELO 快照、贡献和 history 始终逐项验证。schema v7 的 `rating_operations`
为每次已计分的顶层 match、series 或 tournament 分配唯一、单调递增的全局序号；审计会按
该提交顺序重算每个作用域的 ELO、局数和胜平负，并与当前排行榜逐项比对。因此，即使目标
赛事前后还有其他评分操作，`checks.leaderboard` 也能给出完整 PASS，而不依赖可能倒序的
事件时间。`round-robin --resume` 也会在宣告正式赛事“已完成”前执行同一套关系与 ELO 深验。

问答赛估算为
`2*N*(N-1)*rounds` 个选手回合；16 人 × 100 轮为 48,000 回合，默认最多
重试 3 次时理论上限为 144,000 次选手调用。超过保守阈值会在建库和调用前拒绝；
确认费用和超时风险后才使用 `--allow-large-tournament`，并先在 Provider
账户或网关设置整场费用上限。

LLM 每步默认限时 120 秒，可用 `--llm-timeout`、环境变量
`LLMOLYMPIC_LLM_TIMEOUT` 或 `[match] llm_timeout_seconds` 调整。OpenAI、Ollama
和 mock 都使用可取消的原生异步调用；模型超时或 Provider 运行期异常会立即判
技术负，不会让整场程序崩溃。人类限时仍由独立的 `--timeout` 控制。
只实现同步 `chat()` 的旧第三方 Provider 可用 `--no-llm-timeout` 兼容运行，
但该选项只禁用比赛层截止时间，Provider 自身的网络超时仍可能生效；同时程序
无法强制终止卡住的同步请求，建议尽快实现原生异步 `achat()`。

为限制失控模型或极端参数对终端、内存和数据库的影响，平台统一限制：单场最多
16 名选手，题目型项目最多 100 轮，单回合最多重试 10 次，单次模型/选手输出
最多 4096 字符，历史与榜单查询最多返回 1000 条。逻辑推理和猜谜仍受各自更小的
题库上限约束。超长或非文本输出会安全判技术负，原始内容不会写入档案；终端显示
还会过滤控制字符和双向文本控制符。`archive` 命令的终端输出最多显示 100000
字符，但 SQLite 中的合法档案不受这个显示上限影响。
内置 OpenAI 和 Ollama Provider 默认分别请求最多 1024 个输出 Token；恶意兼容
端点仍可能忽略请求限制，且 4096 字符检查发生在响应接收后，因此生产部署仍应
在 Provider 账户和网关侧设置费用、响应体及整场调用预算。

每场 `play` 完成后会自动写入 SQLite，并在同一事务中更新总榜与分项目 ELO；
`series` 把两局和批量 ELO 作为一个原子事务保存；`round-robin` 把每个已完成的双局
对阵作为不计分检查点原子追加，全部赛程完成后再在一个事务中封存正式赛事、
系列、对局、总局分和 ELO 变化。检查点封存前不会出现在对局历史或榜单中，重复恢复/
封存同一赛事也不会重复计分。循环赛在最终封存事务开始时读取并冻结当前 ELO，
再为所有对局计算期望值；因此中断期间先落库的其他计分对局会先进入基准分。在给定
同一组已完成对局结果和同一封存基准分时，ELO 聚合不受处理或保存顺序影响。对阵执行
顺序仍可能影响有状态或随机 Provider 的实际输出。
直接使用 Python 存储 API 时，`SQLiteStore.save_match()` / `save_series()` /
`save_tournament()` 默认按外部导入处理，只存档、不计分；只有本地比赛引擎应显式传入
`rating_source="engine"`，该参数是本进程内的信任声明，并非来源认证或数字签名。
`history` 会标出循环赛 ID、对阵号、系列赛 ID 与局号；`archive` 可用对局、系列赛
或循环赛 ID 读取对应的完整档案。
完整事件流、每步作答、选手配置和最终比分均保存在档案中。默认数据库位于
`~/.llmolympic/llmolympic.db`；可用 `LLMOLYMPIC_DB`、`[storage] database`
或各命令的 `--db` 覆盖。
在 POSIX 系统上，新建的数据目录默认为 `0700`，数据库及 SQLite sidecar 文件为
`0600`；打开已有数据库时也会收紧文件权限。档案只记录温度、top-p、Token 上限等
安全白名单采样参数，未知参数统一脱敏。漏洞报告方式见 [SECURITY.md](SECURITY.md)。
推理与猜谜档案记录 seed、题面、走法以及生成器/题库版本，但不额外复制内部
标准答案；独立复核需要用对应版本代码按 seed 重放生成器。

技术负也会生成完整档案并正常更新双人 ELO。事件中的 `reason_code`、
`forfeit_scope`、`termination`、`forfeited_by` 等字段可供程序稳定统计；CLI
显示中文原因，但不会把 Provider 的原始异常文本或凭据写入档案。
这些机器字段从本版本的新档案开始写入；旧档案没有相应字段时应按
`termination=unknown` 处理，不能反推为正常结束。

ELO 目前适用于双人对局：比较双方最终比分后按胜 / 平 / 负更新。单人或多人
对局仍会完整存档，但不会计入 ELO。正确率差距不改变单场 ELO 调整幅度；
榜单使用档案中的稳定 `entrant_id`，显示名只是对局时快照。
旧的 `openai:model` / `ollama:model` / `mock:strategy` 语法仍会生成确定性身份；
新建多端点比赛建议使用命名 Profile。

## 测试

```bash
pytest
ruff check .
```

## Release 资产

每个正式 GitHub Release 提供以下可校验资产：

- `llmolympic-0.3.0-py3-none-any.whl`：Python 3.11 及以上版本的通用 wheel。
- `llmolympic-0.3.0.tar.gz`：Python 源码发行包（sdist）。
- `SHA256SUMS`：上述 wheel 与 sdist 的 SHA-256 校验和。

GitHub 页面还会自动生成仓库源码的 zip/tar.gz 快照；它们与 Python sdist 是不同文件。
发布记录见 [CHANGELOG.md](CHANGELOG.md)，安全支持范围见 [SECURITY.md](SECURITY.md)。

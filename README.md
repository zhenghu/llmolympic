# LLM Olympics

人类与 LLM 同台竞技的多项目竞技场：知识问答、数学、推理、下棋、创意……
比赛项目插件化，支持人 vs LLM、LLM vs LLM。设计细节见 [DESIGN.md](DESIGN.md)。

## 安装

### GitHub Release

v0.9.0 通过 GitHub Release 提供 Python wheel，可直接从发布地址安装：

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install \
  https://github.com/zhenghu/llmolympic/releases/download/v0.9.0/llmolympic-0.9.0-py3-none-any.whl
```

安装后可核对版本并检查本地运行环境；`doctor` 不会连接模型服务或显示 API Key：

```bash
llmolympic --version
llmolympic doctor
llmolympic games
```

本次 v0.9.0 的发布范围是 GitHub Release，不包含 PyPI 发布或独立 macOS 应用包。
wheel 提供 `llmolympic` 命令；双击启动器 `play.command`、`championship.command`、
`start_web.command` 和 `stop_web.command` 随源码仓库和 GitHub 自动生成的源码归档提供，不包含在
wheel 中。

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

开发阶段四的本地 Web API 时，另外安装受上界约束的可选依赖：

```bash
python -m pip install -e ".[dev,web]"
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
v8 并保留既有档案和 ELO。v7 会按既有 `rating_history` 的 SQLite 写入顺序回填全局
评分操作序号，并把历史迁移产生的 `matches` / `series_archives` 表规范化；v8 新增无内容的
Provider 预算与调用尝试账本。旧 checkpoint 不会被追溯附加预算。升级前请停止所有正在写入
该数据库的赛事进程，
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

# 本地 LLM 直接使用 Ollama spec（模型名中可继续包含冒号）
llmolympic play --game chess --players ollama:llama3.1:8b,mock:fixed
```

命名 Profile 选手的稳定身份为 `profile:<id>:<model>`；`display_name`
作为对局内 `name` 和界面展示使用，但不参与身份或 ELO 关联。更改显示名不会
创建新的 ELO 身份，更换 Profile ID 或模型则会。
同场出现重复展示名时，CLI 会附加 Profile 和模型进行消歧。

`entrant_id` 只表示竞技身份；评委独立性另由 Provider 派生的 `route_id`
判断。OpenAI/Ollama 的路由由协议族、规范化端点和精确模型组成，mock 的路由
由算法策略组成。因此，同一端点和模型即使改用另一个 Profile、API Key、展示名、
采样参数或 direct 语法，也不能在同一评审团重复投票，参赛者也不能通过更换这些
字段规避自评检查。`route_id` 不参与 ELO，也不会写入 `entrants.identity_json`。
第三方可配置端点的 Provider 应覆盖 `route_id_for()`，否则默认实现会保守地把
同一适配器类型和模型视为同一路由。

### Provider 硬预算

`play`、`series` 和 `round-robin` 提供五个逐字段解析的 Provider 预算项；优先级均为
CLI > 环境变量 > `[budget]` > 默认值：

| 语义 | CLI | 环境变量 | `config.toml` |
|---|---|---|---|
| 调用总数 | `--max-provider-calls` | `LLMOLYMPIC_MAX_PROVIDER_CALLS` | `max_provider_calls` |
| 累计输入 | `--max-input-tokens` | `LLMOLYMPIC_MAX_INPUT_TOKENS` | `max_input_tokens` |
| 单次输出上限 | `--max-output-tokens-per-call` | `LLMOLYMPIC_MAX_OUTPUT_TOKENS_PER_CALL` | `max_output_tokens_per_call` |
| 累计输出 | `--max-total-output-tokens` | `LLMOLYMPIC_MAX_TOTAL_OUTPUT_TOKENS` | `max_total_output_tokens` |
| 本地预估美元 | `--max-estimated-cost-usd` | `LLMOLYMPIC_MAX_ESTIMATED_COST_USD` | `max_estimated_cost_usd` |

总预算默认不启用；单次输出默认仍限制为 1024 Token。调用数按实际 Provider transport
attempt 计数，包含非法答案重试和创意评委请求。调度前，input 用完整 system/user messages
的规范 JSON UTF-8 字节数作 tokenizer-independent 保守上界，output 使用 Provider 真正下发的
单次 Token cap；成功返回可信 usage 后再按报告的 input/output Token 结算。Provider 未报告
usage、返回无效 usage、超时、异常或调度后被取消时，整份预留上界都会计入；尚未调度的取消
任务才释放预留。同一并发批次先原子预留全部调用，任一上限不足就一个请求也不发出。

美元项只接受十进制字符串并使用整数纳美元记账，不经过二进制浮点：美元上限向下取整到
纳美元，用户填写的 USD/百万 Token 价格向上取整到纳美元/百万 Token，每次调用估价再向上
取整到 1 纳美元。云端价格不会联网猜测，必须按精确 spec 显式冻结：

```toml
[budget]
max_provider_calls = 200
max_input_tokens = 500000
max_output_tokens_per_call = 1024
max_total_output_tokens = 200000
max_estimated_cost_usd = "5.00"

[pricing."openai:gpt-4o-mini"]
input_usd_per_million_tokens = "0.15"
output_usd_per_million_tokens = "0.60"

[pricing."profile:kimi:moonshot-v1-128k"]
input_usd_per_million_tokens = "0.50"
output_usd_per_million_tokens = "2.00"
```

启用美元硬上限时，每条云端路由都必须有与 `openai:<model>` 或
`profile:<id>:<model>` 完全匹配的价格；动态 OpenRouter 路由会被拒绝。OpenAI 和命名
Provider Profiles 都按云端 LLM 管理，Ollama 是本地 LLM，mock 是本地算法，human 是人；
Ollama 和 mock 未显式配置价格时按零估价。美元数值只是按这份本地冻结价格得到的保守估算，
不是 Provider 最终账单或账户级支付保护；仍须在 Provider 账户或网关设置独立费用上限。

`play` 的所有参赛者与创意评委共享一个内存账本；`series` 的两局共享同一内存账本，不能
在第二局重新获得额度。`round-robin` 则使用 SQLite v8 持久化整项赛事的冻结 policy、限额、
预留与实际用量，跨进程恢复也不会重置预算。预算表只保存 opaque `route_id`、整数限额/计数、
状态、时间与 lease generation，不保存 API Key、原始端点、请求头、模型名、prompt、response
或 Provider 原始异常。

## 运行

macOS 可以在 Finder 中双击 `play.command`。菜单可直接启动五子棋、国际象棋、
数学、知识、逻辑推理、猜谜竞答和创意写作；六个客观判分项目都提供三个 mock 的
离线循环赛入口，创意写作提供单场、双局赛和“三名 mock 参赛者 + 三名匿名算法评委”
循环赛入口，并保留已有的人类对战、两个 mock 自动演示和棋类换先手双局赛。

```bash
# 两个 mock 选手演示（离线，无需 key）
llmolympic play --game math_quiz --players mock:random,mock:fixed --rounds 5

# 人对战 GPT
llmolympic play --game knowledge_quiz --players human:我,openai:gpt-4o-mini

# 两个模型对战（同 seed 同题，公平对比）
llmolympic play --game math_quiz --players openai:gpt-4o-mini,ollama:llama3.1 --seed 42

# 两个命名兼容端点对战（推荐）
llmolympic play --game math_quiz --players profile:kimi,profile:deepseek --seed 42

# 创意写作离线演示：Mock 是确定性算法，不是 LLM
llmolympic play --game creative_writing \
  --players mock:random,mock:fixed \
  --judge mock:strict --judge mock:balanced --judge mock:lenient --seed 42

# 创意写作云端实战：参赛模型与三名评委使用不同的稳定身份
llmolympic play --game creative_writing \
  --players profile:writer-a,profile:writer-b \
  --judge profile:judge-a --judge profile:judge-b --judge profile:judge-c --seed 42

# 创意写作双局赛：两局复用同一冻结评审团与整次预算
llmolympic series --game creative_writing \
  --players profile:writer-a,profile:writer-b \
  --judge profile:judge-a --judge profile:judge-b --judge profile:judge-c --seed 42

# 创意写作循环赛：checkpoint 会冻结评审团，恢复时无需也不允许重传 --judge
llmolympic round-robin --game creative_writing \
  --players profile:writer-a,profile:writer-b,profile:writer-c \
  --judge profile:judge-a --judge profile:judge-b --judge profile:judge-c --seed 42

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
  --players profile:kimi,profile:deepseek,ollama:llama3.1:8b --rounds 5 --seed 42

# 从开赛时显示的赛事 ID 恢复中断的循环赛（自定义数据库需再次指定）
llmolympic round-robin --resume <TOURNAMENT_ID> --db ~/.llmolympic/llmolympic.db

# 严格只读审计一项进行中或已封存的循环赛；--json 适合 CI/脚本
llmolympic audit-tournament <TOURNAMENT_ID> --db ~/.llmolympic/llmolympic.db
llmolympic audit-tournament <TOURNAMENT_ID> --db ~/.llmolympic/llmolympic.db --json

# 4/8/16 名非人类选手单淘汰制锦标赛：每场交换先后手双局赛，胜者晋级
llmolympic championship --game knowledge_quiz \
  --players mock:random,mock:fixed,mock:illegal,mock:balanced --rounds 5 --seed 42

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
llmolympic archive <MATCH_OR_SERIES_OR_TOURNAMENT_OR_CHAMPIONSHIP_ID>
```

### 本机 React 比赛控制、参与与观战页（阶段四 4.4 / 4.5a / 4.5b）

阶段 4.2 自 v0.5.0 起随正式 wheel/sdist 发布；阶段 4.3/4.4 自 v0.6.0 起在此基础上加入
运行中事件 broker 与单个本机浏览器人类输入；阶段 4.5a/4.5b 自 v0.7.0 起加入多席位
本机浏览器参与与本机全 Web 控制台。Web 服务端依赖仍通过可选的
`web` extra 安装；若上面只安装了基础 wheel，可使用同一发布资产补齐依赖：

```bash
python -m pip install \
  "llmolympic[web] @ https://github.com/zhenghu/llmolympic/releases/download/v0.9.0/llmolympic-0.9.0-py3-none-any.whl"
```

安装后可以打开本机 Web 服务。正式存档和实时 sidecar 对 Web 进程始终只读；只有持席位
链接的浏览器才可向独立 input sidecar 提交人类走法，只有持启动时管理链接的可信本机浏览器
才可操作独立 jobs 控制面：

```bash
llmolympic web --db ~/.llmolympic/llmolympic.db
```

macOS 源码仓库也提供两个可双击脚本：`start_web.command` 通过项目专属的用户级 `launchd`
服务启动控制台并用临时管理 fragment 打开默认浏览器，`stop_web.command` 只卸载这个精确
服务并删除管理凭证文件。关闭脚本不会按
端口或进程名终止其他程序；如果 8000 端口已由手工启动的服务占用，启动脚本会提示先回到
原终端按 `Ctrl+C`，不会接管或误停该进程。

服务默认仅监听 `127.0.0.1:8000`，当前实现明确拒绝 `0.0.0.0`、局域网地址和公网地址；
当前没有远程认证或 TLS，不能将它直接暴露到网络。启动后在浏览器打开
`http://127.0.0.1:8000/`，可以看到同一数据库上正在运行的 `play`、`series` 或
`round-robin`，也可按项目筛选最近对局和总体/分项目 ELO。实时详情支持跟随、暂停、逐条
查看和回到最新；完成存档详情仍可播放、前后单步、拖动进度、切换速度或重新播放。五子棋
和国际象棋暂时都使用通用事件时间线，不提供专用棋盘。

`llmolympic web` 会生成一条只显示给当前终端的 `/#admin=...` 本机管理链接；macOS 双击
启动器会通过项目专属 `0700` 状态目录中的 `0600` 临时文件交付同一凭证，而不会把它写入
launchd 日志。浏览器会立即从地址栏清除 fragment，并只在当前标签页会话保存凭证。管理页
可选择 `play`、`series` 或 `round-robin`、内置 Human/mock 或已经配置的 Provider Profile，
再设置 rounds、seed、超时和硬预算。创建操作只生成不可变的准备态与工作量预览；必须再次
确认才会启动 worker 或产生 Provider 调用。当前同一数据库最多运行一个 Web 任务；重复请求
使用幂等键返回原任务，不会因双击或响应丢失重复启动和计费。

浏览器不能录入 API Key、环境变量、Provider endpoint、数据库路径、shell 命令或任意模型
覆盖；Profile 必须先在本机 `config.toml` 配置，页面只显示脱敏名称、Provider 类型、固定默认
模型和凭据是否就绪。由 macOS `launchd` 启动时不会把交互式终端中的云密钥复制进 plist，
因此需要云 Profile 时应在已设置相应环境变量的可信终端运行 `llmolympic web`。Human 仍只
允许用于 `play`；`series` 和 `round-robin` 保持既有非人类限制。prepare 会把受信
Profile 的无凭据安全投影和配置摘要冻结在准备态；控制器与 child 在构造 Provider 前会再次核对，如果
Profile 的 Provider、endpoint、默认模型或凭据环境变量名在确认前发生变化，任务会在任何
Provider 调用前安全失败。

中断的循环赛只能显式恢复；未过期的 runner lease 仍在活动时不会显示恢复入口。Web
只允许恢复全 Mock 且无预算的 checkpoint，或者只含命名 Profile、并已持久化冻结 Provider
预算的 checkpoint；旧式直接 Provider checkpoint 仍须从 CLI 恢复；
含非 Mock Provider 却没有冻结预算的旧 checkpoint 仍可使用 CLI 按既有规则处理，但 Web 不会
事后附加预算或启动它。`play` 与 `series` 不会自动重跑，以免重复 Provider 调用。

要从浏览器真正参加一场比赛，先保持 Web 服务运行，再在另一个终端启动单场 `play`：

```bash
llmolympic play \
  --game gomoku \
  --players human:我,mock:fixed \
  --human-input web \
  --timeout 120 \
  --db ~/.llmolympic/llmolympic.db
```

当前源码还可启动两个本机浏览器 Human 的五子棋；CLI 会分别标注并打印两条链接：

```bash
llmolympic play \
  --game gomoku \
  --players human:黑方,human:白方 \
  --human-input web \
  --timeout 120 \
  --db ~/.llmolympic/llmolympic.db
```

CLI 会为每名浏览器 Human 打印形如
`/participate/<SESSION>/<SEAT>#capability=...` 的独立一次性本机链接。每条链接只能交给其标注的
席位使用；浏览器读取 fragment 中的高熵 capability 后立即从地址栏移除，只在当前标签页会话
中保存，服务端数据库只保存摘要。参与页显示引擎生成的原始文本题面，并把最多 4096 个
Unicode 字符的文本提交给对应的 `HumanPlayer` 异步接口。提交成功只代表“引擎已收到”，
合法性仍由具体 Game 判断；非法走法会生成新的 request ID 和修正题面，不会把旧提交串到
下一轮，也不会把一个席位的提交送入另一个席位。

4.5a 只放宽单场 `play` 的本机浏览器席位数量：比赛需有 2–16 名选手且至少一名 Human，
最终人数还必须满足具体 Game 的约束；每名 Human 都获得独立的 session、seat 和 capability，
其余选手仍可为 mock/LLM。三名及以上选手的单场会完整存档但不计入 ELO，只有双人对局参与
现有 ELO 更新。每个 capability 只允许读取并提交对应席位的当前 request；对局的
公开题面和动作事件仍会进入同机的实时观战页，因此它不是隐藏题目或公开事件的保密通道。
所有项目继续使用通用文本输入（五子棋坐标、国际象棋 SAN/UCI、问答答案或创意正文）。
4.5b 的管理页可以从安全目录选择既有 Profile 并控制三种现有比赛模式，但不会读取凭据、
直接调用 Provider、写正式档案或任意修改 ELO；这些操作仍由固定参数启动的比赛 worker 通过
现有事务执行。它也不开放局域网访问；专用棋盘要等 Game 提供结构化 UI 状态后再实现。

这是可信本机操作者场景，不是同一 macOS/Linux 账户内不同用户之间的保密边界：运行命令的
终端会显示全部 capability 链接，拥有该操作系统账户文件访问权的人也不属于威胁模型。远程
多席位必须在后续切片加入正式身份认证、席位授权、TLS、限流和安全审计，不能通过反向代理
或修改监听地址直接暴露当前服务。

React 19.2.8、ReactDOM 19.2.8 与 Scheduler 0.27.0 通过锁定依赖和可复现构建合并为同源
生产 bundle，随 wheel/sdist 离线分发；打开页面不访问 CDN，运行时也不需要 Node、外部
字体、遥测或 Service Worker。REST 前缀是 `/api/v1`，提供健康
状态、项目能力、最近对局、单场公开详情和 ELO 排行榜。已完成对局可通过
`/ws/v1/matches/<MATCH_ID>?from_seq=0` 按版本化信封回放公开事件。
WebSocket 握手必须带与监听 Host 和端口完全同源的浏览器 `Origin`；例如默认地址使用
`Origin: http://127.0.0.1:8000`。缺失、`null` 或跨源 Origin 会在连接接受前拒绝。
浏览器异常断线时会从首个缺失事件序号有限续播；若 WebSocket 不可用，则降级读取同一份
只读 REST 详情。接收进度与视觉播放游标独立，不会因网络重连重复显示事件。

阶段 4.3 的运行中事件由比赛进程写入与主档案同路径派生的独立 SQLite sidecar；同步事件
回调只做公开投影与非阻塞入队，后台线程负责短事务、心跳和有界保留。队列满、sidecar 忙、
Web 未启动或客户端过慢都不会改变比赛结果、Provider 预算、ELO 或最终存档。只有正式档案
成功提交后，页面才会提供完整回放链接；进程崩溃或租约过期会显示为中断。

默认主档案为 `example.db` 时，实时 sidecar 是同目录的 `example.db.live.db`；它是可删除的
观战缓存，不是正式比赛档案。停止相关比赛与 Web 服务后可以删除它，之后运行新比赛会按需
重新创建；不要把它提交到 Git，也不要用它替代主数据库备份。

浏览器人类输入使用另一份 `example.db.input.db`。它以 `0600` 保存短期席位、题面和尚未
消费的提交；每名浏览器 Human 拥有独立 session、seat 和 capability，比赛进程持有各席位的
owner fencing 与租约，Web 仅能凭对应 seat capability 对当前 request 做一次原子提交。
比赛与 Web 服务均停止后可删除；它不是正式档案，不应提交、同步或备份。

本机控制任务使用 `example.db.jobs.db`，以 `0600` 保存脱敏配置、准备态摘要、状态、
子进程引用和最终档案引用。过期的未启动准备态会在 30 分钟后取消，终态记录在 24 小时后
在服务重新启动或后续准备/启动任务时清理。`example.db.jobs.db.lock` 是权限同样收紧的独立
manager lock，只用于防止同一数据库被两个 Web 控制器同时管理；它不是任务租约，也不替代
循环赛 checkpoint 中的
runner lease。Web 服务或受控 worker 运行时不要删除这两个文件；完全停止后可一并删除，
之后会按需重建。它们都不是正式存档，不应代替主数据库备份。

Web 层对主档案和实时 sidecar 都使用独立的 SQLite `mode=ro` / `query_only` 连接，不创建、
迁移或修改它们，也不直接更新 ELO。参与写端点只能推进已有 input request；管理写端点只
推进独立 jobs sidecar 中经过 admin capability 授权的准备/启动/停止状态。两者都使用同源
POST、Bearer capability、严格 JSON/体积上限和幂等标识。正式主库写入仅发生在受控比赛
worker 的既有原子存档路径。jobs sidecar 和公开 DTO 只保留 Profile ID、脱敏显示名、Provider
类型、默认模型与无凭据配置摘要，不包含 endpoint、凭据环境变量名或值、请求头或原始失败详情。
旧版、外部导入、超大或语义不一致的档案不会经 Web 详情接口原样
公开。带正式身份认证、资源授权和 TLS 的远程使用，以及独立的单场多题总分制锦标赛模式，
仍留给后续阶段四切片。

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
已经输入的内容。4.5a 的本机浏览器后端通过每名 Human 独立 capability 席位隔离 request
读取与提交，但同机公开观战页仍会显示公开事件；它仍依赖可信本机操作者。跨设备或不可信
参与者实战仍需后续的远程客户端与正式认证、授权和 TLS 边界。

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

恢复时不能重新指定 `--game`、`--players`、`--rounds`、`--seed`、超时选项、
`--allow-large-tournament` 或任何 `--max-provider-*` / `--max-*-tokens` / 费用预算选项：
项目配置、顺序敏感的选手身份和模型、seed、超时及赛程已由检查点冻结。Profile
恢复规格会使用开赛时已解析的显式模型，因此之后修改 `default_model` 不会偷换参赛
模型。检查点只保存无密钥的选手描述，不保存 API Key、Key 哈希或 Provider 客户端；
恢复进程会从当前的环境变量和 Profile 配置重建 Provider，所需 Key 必须仍可用。如果新赛事
使用了自定义 `--db`，恢复时也必须指向同一数据库。
创建和恢复 checkpoint 时会先核对已有可信 `entrant_id` 的身份绑定；若同一稳定 ID 已绑定
到不同模型/Profile 身份，会在首次或下一次 Provider 调用前拒绝，避免跑完整场后才封存失败。
若新赛事启用了预算，checkpoint 与 SQLite v8 预算 policy 会在同一事务中创建；恢复只读取
已冻结的限额、精确 route 价格和单次输出 cap，明确忽略当前进程中的 `[budget]`、`[pricing]`
及相应预算环境变量。旧版创建的无预算 checkpoint 仍可恢复，并继续保持无预算，不会因当前
配置新增预算而改变赛事合同。

每个进行中的 checkpoint 都由 SQLite 跨进程 runner lease 保护。新赛事会在首次模型调用前
创建空 checkpoint 并领取执行权；恢复进程则会先在短写事务中原子领取执行权并重载 checkpoint。
恢复时若已有未过期执行者，会在重建 Provider 和发起任何模型调用前退出；如果 checkpoint
已经完整，则无需重建 Provider，直接原子封存并结算 ELO。租约默认有效 60 秒，每 15 秒
后台续租，并在每组双局赛开赛前再次续租；checkpoint 追加也会在同一事务中校验并延长租约。
租约 token 只在领取进程内持有，
数据库仅保存摘要；单调递增的 generation 会 fencing 已过期或已被接管的旧执行者，使其不能
继续保存进度、封存赛事或更新 ELO。预算调用尝试也绑定同一 generation：接管时，旧 runner
尚未调度的预留会释放，已标记调度但无法确认 usage 的调用会按完整上界计费，然后新 runner
才可继续使用剩余额度。

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
schema v8 结构 manifest 与外键完整性检查，再深度核对指定赛事的 checkpoint 连续前缀、
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
账户或网关设置整场费用上限。该选项只绕过静态赛事规模确认，不能提高、禁用或绕过任何
已配置的 Provider 硬预算。

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

### 创意写作与匿名评审团

`creative_writing` 是第三阶段的首个主观判分项目：两名参赛者在同一轮盲写一篇
20–2000 字的微型故事。它需要用可重复的 `--judge` 提供 3–9 名唯一 LLM 评委；
人类不能担任评委，同一稳定身份也不能同时参赛和评审。命名 Provider Profile、
普通 OpenAI/Ollama 以及离线 mock 都复用现有模型接入，其中 OpenAI 和 Provider
Profiles 都是云端 LLM，Ollama 是本地 LLM，mock 只是离线算法。

每名评委会分别收到每份作品，题面只使用 A/B 匿名标签，不包含参赛者姓名、
`entrant_id`、Provider、Profile 或模型信息。作品正文按不可信数据隔离，不能向评委
发出系统指令；但平台无法阻止作者在正文中主动透露身份或通过文风被识别。只有完整
评完全部有效作品的评委才进入裁决，达到严格多数 quorum 后，系统按版本化 rubric
计算各评委的加权总分，再取中位数并归一化到 0–1。个别评委失败会被安全记录；未达到
quorum 时命令失败，不写入对局，也不更新 ELO。

成功裁决的安全评委描述、匿名映射、逐维分数、理由、失败摘要、quorum 与聚合版本都
保存在 `match_finished.data.judging`。新裁决使用 `PanelVerdict` schema v3，在
`panel` 中冻结完整评审团及每名评委的 `route_id`；即使全部参赛者都已技术放弃、没有
实际评委调用，也能复核评委路由唯一性。v3 还绑定规范化 `JudgingRequest` 摘要，使题面、
rubric、匿名映射和作品正文不能在不破坏深度审计的情况下被替换。旧 schema v1/v2 裁决仍
可读取；其中 v1 因没有路由快照，不能被视为已验证路由独立。SQLite 当前使用 schema v8，
最终双人比分继续进入总榜和 `creative_writing` 项目榜。

评委原始响应、API Key、原始端点和请求头不会进入档案。`route_id` 是稳定、可跨档案
关联的端点伪名；常见端点可能通过字典枚举被猜出，因此它不是加密或保密边界。路由检查
只证明本地配置的请求路径不同，无法通过 DNS/CNAME、供应商模型别名或动态后端证明底层
基础模型必然不同。
`play`、`series` 和 `round-robin` 均支持创意项目。双局赛的两局复用同一评审团快照并
原子保存；循环赛在零进度 checkpoint 中冻结无凭据的评审团描述，恢复时用当前配置重建并
逐项核对 `entrant_id`、`route_id`、模型、Profile 与超时配置。缺失或漂移会在首次 Provider 调用
前拒绝。赛事、系列和每局裁决必须引用同一快照；只读深度审计同时验证事件重放、裁决请求
摘要、关系表、评分账本与 ELO。参赛者和评委共享同一个预算，循环赛的调用估算包含每局
两份作品乘评委人数，并由 SQLite v8 账本跨进程累计。

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

### 真实 Provider 自动化冒烟

真实评委测试默认不随本地测试或 PR CI 运行，避免网络波动、第三方模型变化和调用费用阻塞
日常合并。需要实测时，可提供 3–9 个按优先级排列的 OpenRouter 云端评委候选；测试会先
逐个探针，再用最先通过的三个模型运行正式冒烟：

```bash
OPENAI_API_KEY="$OPENROUTER_API_KEY" \
OPENAI_BASE_URL="https://openrouter.ai/api/v1" \
LLMOLYMPIC_RUN_LIVE=1 \
LLMOLYMPIC_LIVE_JUDGE_CANDIDATES="openai:openai/gpt-5.6-luna,openai:deepseek/deepseek-v4-flash-0731,openai:mistralai/mistral-medium-3-5,openai:x-ai/grok-4.3,openai:google/gemini-3.5-flash-lite" \
python -m pytest -m live_provider -q -s
```

这些变量只对该命令生效，避免同一终端后续执行普通 `pytest` 时意外再次产生云端调用。
候选必须是 3–9 个唯一的 `openai:<vendor>/<model>` spec，不接受会动态改选模型的
`openrouter/auto*` 或 `openrouter/free`。探针按输入顺序逐个发送一次与正式评审相同的系统提示、
请求信封和严格 JSON 解析；“可用”只表示模型在这一次探针中成功通过完整协议，不保证后续
调用不会因网络或第三方服务变化而失败。选满三个后不会继续探测其余候选；如果最终不足三个，
正式比赛不会启动，也不会写入对局或更新 ELO。

仓库的 `Live Provider Smoke` GitHub Actions 工作流提供同一项手动实测。在仓库设置中创建
名为 `live-provider-smoke` 的 GitHub Environment，并只在该 Environment 中添加
`OPENROUTER_API_KEY` Secret；然后从 Actions 页面选择工作流并点击 **Run workflow**。
触发界面的 `candidate_models` 接受同样的候选列表；勾选 `confirm_billable` 后才会进入受
Environment 保护的计费 job。输入只作为环境变量传给校验和测试，不会拼入 shell 命令。
`-s` 会在日志中显示脱敏的模型选择与 `LIVE_PROVIDER_SMOKE` JSON 摘要，只包含模型 spec、
安全原因码和聚合结果，不包含原始响应或 Provider 异常文本；工作流不会上传数据库或档案。
必须把该 Environment 的 deployment branches 限制为 `main`，工作流也只允许 `main` 运行；
代码内的分支判断只是纵深防御，不能替代 Environment 规则。仓库有多位写入者时，还应设置
required reviewers。每次触发都要确认 **Use workflow from** 选择的是 `main`。

若输入 `N` 个候选，选择阶段最多调用 `N` 次；正式阶段固定由两名 mock 参赛者各提交一份
作品，再由选中的三个云端模型分别评审两份作品，因此固定调用 6 次。总上限为 `N + 6`，
而 `N <= 9`，所以单次工作流最多发起 15 次可能计费的模型调用。建议为专用测试 Key 设置
Provider 侧费用限额；请求次数和输出 Token 上限不是美元价格保证，实际费用取决于所选模型
及 Provider 定价。GitHub 工作流把单次模型请求限制为 60 秒且不做 Provider 重试；探针选出
的三名评委在正式阶段仍必须 3/3 全部成功，任一模型失败都会让冒烟失败。工作流只允许读取
仓库内容，缺少 Secret 时会直接失败且不会输出 Key、请求头、原始模型响应或端点详情。
CI 与 Release 仍会分别从 wheel 和 sdist 运行零费用的三 mock 评委创意写作冒烟。

## Release 资产

每个正式 GitHub Release 提供以下可校验资产：

- `llmolympic-0.9.0-py3-none-any.whl`：Python 3.11 及以上版本的通用 wheel。
- `llmolympic-0.9.0.tar.gz`：Python 源码发行包（sdist）。
- `SHA256SUMS`：上述 wheel 与 sdist 的 SHA-256 校验和。

GitHub 页面还会自动生成仓库源码的 zip/tar.gz 快照；它们与 Python sdist 是不同文件。
发布记录见 [CHANGELOG.md](CHANGELOG.md)，安全支持范围见 [SECURITY.md](SECURITY.md)。

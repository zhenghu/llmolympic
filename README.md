# LLM Olympics

人类与 LLM 同台竞技的多项目竞技场：知识问答、数学、推理、下棋、创意……
比赛项目插件化，支持人 vs LLM、LLM vs LLM。设计细节见 [DESIGN.md](DESIGN.md)。

## 安装

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

用 OpenAI 模型对战需设置 `OPENAI_API_KEY`（见 `.env.example`）；
本地模型可用 [Ollama](https://ollama.com)；没有任何 key 时用内置 mock 选手即可体验。

## 配置模型

在源码项目根目录复制模板生成自己的配置（`config.toml` 含密钥，已被 git 忽略）：

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

也可以显式指定其他位置的配置文件，适合在项目目录外运行已安装的命令：

```bash
export LLMOLYMPIC_CONFIG="$HOME/.config/llmolympic/config.toml"
```

查找顺序为：`LLMOLYMPIC_CONFIG` 指定的文件 > 源码项目根目录的
`config.toml`。程序不会扫描任意当前工作目录中的 `config.toml`，避免环境中的
API Key 被意外发送到不可信配置指定的兼容端点。配置项取值优先级仍为：
环境变量 > 选中的配置文件 > 默认值。含密钥的配置文件建议设置为仅本人可读：

```bash
chmod 600 config.toml
# 使用显式配置路径时：chmod 600 "$LLMOLYMPIC_CONFIG"
```

Profile ID 只允许字母、数字、点、下划线和连字符。`provider`
目前支持 `openai` 和 `ollama`。OpenAI 兼容 Profile 必须声明
`api_key_env`，程序只在创建该 Provider 时读取对应环境变量；
不会隐式复用另一个端点的 Key，也不会继承全局 OpenAI SDK 的组织、项目或
自定义请求头，更不会把 Key 写入对局档案。
所有携带 API Key 的远程 OpenAI 兼容端点都必须使用 HTTPS；明文 HTTP
只允许 `localhost`、`127.0.0.0/8` 或 `::1` 回环地址。

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

终端界面会按选手顺序收答并立即显示已接受的答案；模型收到的 prompt 不包含
对手答案，但同一终端里排在后面的人类可能看到前一人的输出。启动器的人机模式
固定把人类放在第一位；多人类盲答需等待后续 Web/独立客户端的批量收答能力。

`series` 固定进行两局：第一局按命令中的选手顺序，第二局完整交换顺序；两局
使用相同 seed。五子棋中这表示双方各执黑一次，国际象棋中表示双方各执白一次。
两局会在一个 SQLite 事务中
原子存档，并基于系列赛开始前的同一 ELO 期望值批量计分，所以各胜一局不会
因保存顺序产生积分漂移。榜单场次和胜平负仍按两局分别累计。
问答项目也可使用 `series`：两局题目条件相同，但模型各自重新采样，用于观察
输出波动；它不代表问答项目存在先后手优势。

当前终端版 `series` 只接受 LLM/mock。`HumanPlayer` 的终端输入超时后，底层
输入线程无法可靠取消，贸然开始第二局可能抢占输入；待可取消输入链路完成后
再开放人类双局赛。单局 `play` 的人类对战不受影响。

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

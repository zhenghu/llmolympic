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

复制模板生成自己的配置（`config.toml` 含密钥，已被 git 忽略）：

```bash
cp config.example.toml config.toml
```

编辑项目根目录的 `config.toml`，启动时自动加载：

```toml
[openai]
api_key = "sk-..."                        # 对应环境变量 OPENAI_API_KEY
# base_url = "https://api.deepseek.com/v1" # DeepSeek/Kimi 等兼容接口填这里

[ollama]
# base_url = "http://localhost:11434"

[storage]
# database = "~/.llmolympic/llmolympic.db"

[match]
# llm_timeout_seconds = 120.0 # 对应环境变量 LLMOLYMPIC_LLM_TIMEOUT
```

取值优先级：环境变量 > `config.toml` > 默认值。


## 运行

macOS 可以在 Finder 中双击 `play.command`。菜单可直接启动五子棋、国际象棋、
数学、知识、逻辑推理和猜谜竞答，并提供人类对战或两个 mock 自动演示。

```bash
# 两个 mock 选手演示（离线，无需 key）
llmolympic play --game math_quiz --players mock:random,mock:fixed --rounds 5

# 人对战 GPT
llmolympic play --game knowledge_quiz --players human:我,openai:gpt-4o-mini

# 两个模型对战（同 seed 同题，公平对比）
llmolympic play --game math_quiz --players openai:gpt-4o-mini,ollama:llama3.1 --seed 42

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
llmolympic archive <MATCH_OR_SERIES_ID>
```

五子棋采用 15×15 自由规则：黑棋先行，横、竖或斜线连续 5 子或以上获胜，
没有禁手；满盘无人获胜则和棋。坐标为 `A1` 到 `O15`，`A1` 在左上角，
中心是 `H8`。选手连续 3 次非法落子，或人类选手超时未落子，会立即判负。
`--rounds` 用于数学、知识、逻辑推理和猜谜项目，不适用于棋类（包括双局赛）。

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

LLM 每步默认限时 120 秒，可用 `--llm-timeout`、环境变量
`LLMOLYMPIC_LLM_TIMEOUT` 或 `[match] llm_timeout_seconds` 调整。OpenAI、Ollama
和 mock 都使用可取消的原生异步调用；模型超时或 Provider 运行期异常会立即判
技术负，不会让整场程序崩溃。人类限时仍由独立的 `--timeout` 控制。
只实现同步 `chat()` 的旧第三方 Provider 可用 `--no-llm-timeout` 兼容运行，
但该选项只禁用比赛层截止时间，Provider 自身的网络超时仍可能生效；同时程序
无法强制终止卡住的同步请求，建议尽快实现原生异步 `achat()`。

每场 `play` 完成后会自动写入 SQLite，并在同一事务中更新总榜与分项目 ELO；
`series` 则把两局和批量 ELO 作为一个原子事务保存。
`history` 会标出系列赛 ID 与局号；`archive` 既可读取单局 ID，也可用系列赛 ID
读取包含两局的完整档案。
完整事件流、每步作答、选手配置和最终比分均保存在档案中。默认数据库位于
`~/.llmolympic/llmolympic.db`；可用 `LLMOLYMPIC_DB`、`[storage] database`
或各命令的 `--db` 覆盖。
推理与猜谜档案记录 seed、题面、走法以及生成器/题库版本，但不额外复制内部
标准答案；独立复核需要用对应版本代码按 seed 重放生成器。

技术负也会生成完整档案并正常更新双人 ELO。事件中的 `reason_code`、
`forfeit_scope`、`termination`、`forfeited_by` 等字段可供程序稳定统计；CLI
显示中文原因，但不会把 Provider 的原始异常文本或凭据写入档案。
这些机器字段从本版本的新档案开始写入；旧档案没有相应字段时应按
`termination=unknown` 处理，不能反推为正常结束。

ELO 目前适用于双人对局：比较双方最终比分后按胜 / 平 / 负更新。单人或多人
对局仍会完整存档，但不会计入 ELO。正确率差距不改变单场 ELO 调整幅度；
榜单身份目前使用档案中的选手名称。

## 测试

```bash
pytest
ruff check .
```

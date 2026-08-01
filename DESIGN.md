# LLM Olympics 设计方案

一个人类与 LLM 同台竞技的**多项目竞技场**：知识问答、数学、推理、下棋、创意……
比赛项目插件化，后期新增项目无需改动引擎。支持人 vs LLM、LLM vs LLM。

## 1. 领域模型

| 概念 | 职责 |
|---|---|
| **Game**（比赛项目） | 插件。定义题目/局面生成、走法校验、终局计分 |
| **Player**（选手） | 统一抽象。`LLMPlayer`（调模型 API）与 `HumanPlayer`（外部输入）对引擎透明 |
| **Match**（对局） | 通用回合循环：发题面 → 收走法 → 校验推进 → 判分 |
| **Judge**（裁判） | 规则判分内嵌在各 Game 的 `score()`；`LLMJudgePanel` 接口预留给创意类 |
| **Rating**（评分） | 标准 ELO（K=32），分项目 + 总榜，SQLite 持久化 |

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
默认兼容为至少一名选手且不设上限。五子棋声明为恰好两名选手。

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
| 国际象棋 chess | 多轮有状态 | 规则（胜负） | 后续再做 |
| 创意 | 单轮开放作答 | LLM 评审团（匿名、多评委防偏置） | 阶段三 |

非法走法规则：解析失败/走法非法给有限次重试（默认 3 次），重试题面会附上
上次输出与拒绝原因。达到上限后，问答判该题不得分；双人棋类立即技术判负。

五子棋首版固定玩家列表第一位执黑、第二位执白；坐标为 `A1` 到 `O15`，
四个方向连续 5 子或以上获胜，不实现禁手或交换开局。赛事层已支持双方
交换先后手各赛一局，降低黑方先手优势。

## 4. 事件驱动架构（为 Web / 手机端留路）

Match 循环的每一步产出**结构化事件**（`match_started` / `turn_prompt` /
`move_received` / `move_rejected` / `match_finished`），界面层只消费事件渲染：

```
CLI（今天）          WebSocket（将来）
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
- 当前 CLI 逐个收答并立即渲染走法：LLM 不会从 prompt 看到对手答案，但同一
  终端里排在后面的人类可能看到前一人的输出，因此尚不属于多人类盲答界面。
- 推理/猜谜档案保存 seed、完整题面、走法和生成器/题库版本，不复制插件内部
  标准答案；审计时需使用对应版本代码按 seed 重放，档案本身尚不能脱离代码独立判分。
- 每场结束后，完整 JSON 档案、选手索引、总榜/项目榜 ELO 与评分历史在同一
  SQLite 事务中写入；`match_id` 防止重复计分。双局赛把两局放进同一事务，
  以系列开始前的同一 ELO 期望值批量累计，避免逐局更新造成顺序漂移。
  `rating_history` 仍逐局留痕，但系列赛行记录的是冻结期望值下的贡献账；审计时
  需通过 `series_matches` 关联 `series_archives.rating_policy=elo_batch_v1`，不能
  把第二局单独按其行内 `rating_before` 再算一次标准 ELO。
- 人类选手限时作答；同一模型跑 N 局取平均，降低采样运气成分。

## 6. 比赛模式

- **单挑**：人 vs LLM 或 LLM vs LLM，同题同时作答（已实现）。
- **交换先手双局赛**：两名非人类选手交换顺序各赛一局（已实现）。
- **循环赛**：N 个模型两两对战，胜场 + ELO 排名（阶段二）。
- **锦标赛**：单场多题总分制（阶段四）。

## 7. 技术栈

- **语言**：Python 3.11+（各家 LLM SDK 最全，引擎到 Web 一门语言贯通）
- **建模**：Pydantic v2（状态、事件、档案）
- **CLI**：Typer + Rich
- **Web（阶段四）**：FastAPI + WebSocket；前端 React
- **存储**：SQLite（对局记录、总榜/项目榜、ELO 历史；题库待接入）
- **棋类（阶段二）**：首个项目为纯 Python 规则实现的 15×15 五子棋；国际象棋后续接入
- **模型接入**：OpenAI 官方异步 SDK / Ollama 异步 HTTP / Mock（离线演示与测试）；
  同步 `chat()` 仅作为无硬超时的第三方 Provider 兼容接口
- **工具**：uv 或 pip、pytest、ruff

## 8. 路线图

1. **MVP** ✅：core 引擎（回合循环 + 事件流）+ 数学/知识问答两个单轮项目 +
   provider 抽象（openai/ollama/mock）+ CLI，LLM vs LLM 与人类入场。
2. **阶段二（进行中）**：ELO + SQLite 持久化 ✅；五子棋多轮状态机 ✅；
   LLM 超时、Provider 异常技术判负与失败档案 ✅；交换先手双局赛与公平批量
   ELO ✅；逻辑推理与结构化猜谜项目 ✅；后续增加循环赛，国际象棋暂不接入。
3. **创意 + LLM 评审团**：主观判分链路（匿名、多评委）。
4. **Web 化 + 锦标赛**：FastAPI 暴露 core，前端对局/观战/排行榜；循环赛与锦标赛模式。
   之后新增项目继续保持纯插件接入。

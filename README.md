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
```

取值优先级：环境变量 > `config.toml` > 默认值。


## 运行

```bash
# 两个 mock 选手演示（离线，无需 key）
llmolympic play --game math_quiz --players mock:random,mock:fixed --rounds 5

# 人对战 GPT
llmolympic play --game knowledge_quiz --players human:我,openai:gpt-4o-mini

# 两个模型对战（同 seed 同题，公平对比）
llmolympic play --game math_quiz --players openai:gpt-4o-mini,ollama:llama3.1 --seed 42

# 列出所有比赛项目
llmolympic games

# 查看总体 / 分项目 ELO 榜
llmolympic leaderboard
llmolympic leaderboard --game math_quiz

# 查看对局历史与完整档案
llmolympic history
llmolympic archive <MATCH_ID>
```

每场 `play` 完成后会自动写入 SQLite，并在同一事务中更新总榜与分项目 ELO。
完整事件流、每步作答、选手配置和最终比分均保存在档案中。默认数据库位于
`~/.llmolympic/llmolympic.db`；可用 `LLMOLYMPIC_DB`、`[storage] database`
或各命令的 `--db` 覆盖。

ELO 目前适用于双人对局：比较双方最终比分后按胜 / 平 / 负更新。单人或多人
对局仍会完整存档，但不会计入 ELO。榜单身份目前使用档案中的选手名称。

## 测试

```bash
pytest
ruff check .
```

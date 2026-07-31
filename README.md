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

编辑项目根目录的 `config.toml`，启动时自动加载：

```toml
[openai]
api_key = "sk-..."                        # 对应环境变量 OPENAI_API_KEY
# base_url = "https://api.deepseek.com/v1" # DeepSeek/Kimi 等兼容接口填这里

[ollama]
# base_url = "http://localhost:11434"
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
```

## 测试

```bash
pytest
```

#!/bin/bash
# LLM Olympics 启动器 —— 双击即可运行。
# 首次运行如被 Gatekeeper 拦截：右键该文件 → 打开。

cd "$(dirname "$0")" || exit 1

if [ ! -d .venv ]; then
    echo "未找到 .venv，请先运行: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
    read -r -p "按回车退出..."
    exit 1
fi
source .venv/bin/activate

# 用程序自己的配置逻辑判断（环境变量 > config.toml）：key 是否已配、默认模型是什么
HAS_LLM=$(python -c "from llmolympic.config import get; print('1' if get('openai', 'api_key', env='OPENAI_API_KEY') else '')")
LLM_MODEL=$(python -c "from llmolympic.config import get; print(get('openai', 'default_model') or '')")

echo "=============================="
echo "       LLM Olympics"
echo "=============================="
echo "  1) 五子棋      你（黑）vs mock"
echo "  2) 五子棋      观看 mock 对战"
echo "  3) 数学问答    你 vs mock"
echo "  4) 知识竞答    你 vs mock"
echo "  5) 知识竞答    观看 mock 对战"
if [ -n "$HAS_LLM" ]; then
    label=${LLM_MODEL:-未设默认模型}
    echo "  6) 五子棋      你（黑）vs LLM ($label)"
    echo "  7) 知识竞答    你 vs LLM ($label)"
    echo "  8) 数学问答    你 vs LLM ($label)"
fi
echo "=============================="
read -r -p "请选择: " choice

case "$choice" in
    1) llmolympic play --game gomoku         --players human:我,mock:random ;;
    2) llmolympic play --game gomoku         --players mock:random,mock:fixed ;;
    3) llmolympic play --game math_quiz      --players human:我,mock:random --rounds 5 ;;
    4) llmolympic play --game knowledge_quiz --players human:我,mock:random --rounds 5 ;;
    5) llmolympic play --game knowledge_quiz --players mock:random,mock:fixed --rounds 5 ;;
    6) [ -n "$HAS_LLM" ] && llmolympic play --game gomoku         --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" ;;
    7) [ -n "$HAS_LLM" ] && llmolympic play --game knowledge_quiz --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" --rounds 5 ;;
    8) [ -n "$HAS_LLM" ] && llmolympic play --game math_quiz      --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" --rounds 5 ;;
    *) echo "无效选择" ;;
esac

echo ""
read -r -p "对局结束，按回车关闭窗口..."

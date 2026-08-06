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
echo "  6) 五子棋      双局换先手 mock 对战"
echo "  7) 逻辑推理    你 vs mock"
echo "  8) 逻辑推理    观看 mock 对战"
echo "  9) 猜谜竞答    你 vs mock"
echo " 10) 猜谜竞答    观看 mock 对战"
echo " 11) 国际象棋    你（白）vs mock"
echo " 12) 国际象棋    观看 mock 对战"
echo " 13) 国际象棋    双局换先手 mock 对战"
echo " 14) 知识竞答    3 个 mock 循环赛"
echo " 15) 数学问答    3 个 mock 循环赛"
echo " 16) 逻辑推理    3 个 mock 循环赛"
echo " 17) 猜谜竞答    3 个 mock 循环赛"
echo " 18) 五子棋      3 个 mock 循环赛"
echo " 19) 国际象棋    3 个 mock 循环赛"
echo " 20) 创意写作    2 个 mock + 3 个匿名算法评委"
if [ -n "$HAS_LLM" ]; then
    label=${LLM_MODEL:-未设默认模型}
    echo " 21) 五子棋      你（黑）vs LLM ($label)"
    echo " 22) 知识竞答    你 vs LLM ($label)"
    echo " 23) 数学问答    你 vs LLM ($label)"
    echo " 24) 逻辑推理    你 vs LLM ($label)"
    echo " 25) 猜谜竞答    你 vs LLM ($label)"
    echo " 26) 国际象棋    你（白）vs LLM ($label)"
fi
echo "=============================="
read -r -p "请选择: " choice

case "$choice" in
    1) llmolympic play --game gomoku         --players human:我,mock:random ;;
    2) llmolympic play --game gomoku         --players mock:random,mock:fixed ;;
    3) llmolympic play --game math_quiz      --players human:我,mock:random --rounds 5 ;;
    4) llmolympic play --game knowledge_quiz --players human:我,mock:random --rounds 5 ;;
    5) llmolympic play --game knowledge_quiz --players mock:random,mock:fixed --rounds 5 ;;
    6) llmolympic series --game gomoku       --players mock:random,mock:fixed ;;
    7) llmolympic play --game reasoning_quiz --players human:我,mock:random --rounds 5 ;;
    8) llmolympic play --game reasoning_quiz --players mock:random,mock:fixed --rounds 5 ;;
    9) llmolympic play --game riddle_quiz    --players human:我,mock:random --rounds 5 ;;
   10) llmolympic play --game riddle_quiz    --players mock:random,mock:fixed --rounds 5 ;;
   11) llmolympic play --game chess          --players human:我,mock:random ;;
   12) llmolympic play --game chess          --players mock:random,mock:fixed ;;
   13) llmolympic series --game chess        --players mock:random,mock:fixed ;;
   14) llmolympic round-robin --game knowledge_quiz --players mock:random,mock:fixed,mock:illegal --rounds 5 ;;
   15) llmolympic round-robin --game math_quiz      --players mock:random,mock:fixed,mock:illegal --rounds 5 ;;
   16) llmolympic round-robin --game reasoning_quiz --players mock:random,mock:fixed,mock:illegal --rounds 5 ;;
   17) llmolympic round-robin --game riddle_quiz    --players mock:random,mock:fixed,mock:illegal --rounds 5 ;;
   18) llmolympic round-robin --game gomoku         --players mock:random,mock:fixed,mock:illegal ;;
   19) llmolympic round-robin --game chess          --players mock:random,mock:fixed,mock:illegal ;;
   20) llmolympic play --game creative_writing --players mock:random,mock:fixed --judge mock:strict --judge mock:balanced --judge mock:lenient ;;
   21) [ -n "$HAS_LLM" ] && llmolympic play --game gomoku         --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" ;;
   22) [ -n "$HAS_LLM" ] && llmolympic play --game knowledge_quiz --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" --rounds 5 ;;
   23) [ -n "$HAS_LLM" ] && llmolympic play --game math_quiz      --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" --rounds 5 ;;
   24) [ -n "$HAS_LLM" ] && llmolympic play --game reasoning_quiz --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" --rounds 5 ;;
   25) [ -n "$HAS_LLM" ] && llmolympic play --game riddle_quiz    --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" --rounds 5 ;;
   26) [ -n "$HAS_LLM" ] && llmolympic play --game chess          --players "human:我,openai${LLM_MODEL:+:$LLM_MODEL}" ;;
    *) echo "无效选择" ;;
esac

echo ""
read -r -p "对局结束，按回车关闭窗口..."

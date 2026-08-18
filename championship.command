#!/bin/bash
# LLM Olympics 锦标赛启动器 —— 双击即可运行。
# 首次运行如被 Gatekeeper 拦截：右键该文件 → 打开。

cd "$(dirname "$0")" || exit 1

if [ ! -d .venv ]; then
    echo "未找到 .venv，请先运行: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'"
    read -r -p "按回车退出..."
    exit 1
fi
source .venv/bin/activate

echo "======================================"
echo "       LLM Olympics · 锦标赛"
echo "   4 名选手单淘汰制 · 每场交换先手双局赛"
echo "======================================"
echo "  1) 知识竞答    4 个 mock 锦标赛"
echo "  2) 数学问答    4 个 mock 锦标赛"
echo "  3) 逻辑推理    4 个 mock 锦标赛"
echo "  4) 猜谜竞答    4 个 mock 锦标赛"
echo "  5) 五子棋      4 个 mock 锦标赛"
echo "  6) 国际象棋    4 个 mock 锦标赛"
echo "  7) 创意写作    4 个 mock 锦标赛 + 3 个匿名算法评委"
echo "======================================"
echo "  使用云端 Profile 选手时，可在终端运行："
echo "  llmolympic championship --game <项目> \\"
echo "    --players profile:a,profile:b,profile:c,profile:d --seed 42"
echo "======================================"
read -r -p "请选择: " choice

case "$choice" in
    1) llmolympic championship --game knowledge_quiz --players mock:random,mock:fixed,mock:illegal,mock:balanced --rounds 5 --seed 42 ;;
    2) llmolympic championship --game math_quiz      --players mock:random,mock:fixed,mock:illegal,mock:balanced --rounds 5 --seed 42 ;;
    3) llmolympic championship --game reasoning_quiz --players mock:random,mock:fixed,mock:illegal,mock:balanced --rounds 5 --seed 42 ;;
    4) llmolympic championship --game riddle_quiz    --players mock:random,mock:fixed,mock:illegal,mock:balanced --rounds 5 --seed 42 ;;
    5) llmolympic championship --game gomoku         --players mock:random,mock:fixed,mock:illegal,mock:balanced --seed 42 ;;
    6) llmolympic championship --game chess          --players mock:random,mock:fixed,mock:illegal,mock:balanced --seed 42 ;;
    7) llmolympic championship --game creative_writing --players mock:random,mock:fixed,mock:illegal,mock:balanced --judge mock:strict --judge mock:balanced --judge mock:lenient --seed 42 ;;
    *) echo "无效选择" ;;
esac

echo ""
read -r -p "锦标赛结束，按回车关闭窗口..."

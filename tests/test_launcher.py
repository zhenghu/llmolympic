"""macOS 双击启动器的静态回归测试。"""

import re
from pathlib import Path

from llmolympic.games import list_games


def test_launcher_exposes_gomoku_human_and_demo_modes() -> None:
    launcher = (Path(__file__).parent.parent / "play.command").read_text()

    assert "1) 五子棋      你（黑）vs mock" in launcher
    assert "2) 五子棋      观看 mock 对战" in launcher
    assert "--game gomoku         --players human:我,mock:random" in launcher
    assert "--game gomoku         --players mock:random,mock:fixed" in launcher
    assert "6) 五子棋      双局换先手 mock 对战" in launcher
    assert "llmolympic series --game gomoku" in launcher


def test_launcher_exposes_reasoning_and_riddle_modes() -> None:
    launcher = (Path(__file__).parent.parent / "play.command").read_text()

    assert "7) 逻辑推理    你 vs mock" in launcher
    assert "8) 逻辑推理    观看 mock 对战" in launcher
    assert "9) 猜谜竞答    你 vs mock" in launcher
    assert "10) 猜谜竞答    观看 mock 对战" in launcher
    assert "--game reasoning_quiz --players human:我,mock:random --rounds 5" in launcher
    assert "--game reasoning_quiz --players mock:random,mock:fixed --rounds 5" in launcher
    assert "--game riddle_quiz    --players human:我,mock:random --rounds 5" in launcher
    assert "--game riddle_quiz    --players mock:random,mock:fixed --rounds 5" in launcher
    assert '--game reasoning_quiz --players "human:我,openai' in launcher
    assert '--game riddle_quiz    --players "human:我,openai' in launcher


def test_launcher_exposes_chess_human_demo_series_and_llm_modes() -> None:
    launcher = (Path(__file__).parent.parent / "play.command").read_text()

    assert "11) 国际象棋    你（白）vs mock" in launcher
    assert "12) 国际象棋    观看 mock 对战" in launcher
    assert "13) 国际象棋    双局换先手 mock 对战" in launcher
    assert "--game chess          --players human:我,mock:random" in launcher
    assert "--game chess          --players mock:random,mock:fixed" in launcher
    assert "llmolympic series --game chess" in launcher
    assert "26) 国际象棋    你（白）vs LLM" in launcher
    assert '--game chess          --players "human:我,openai' in launcher


def test_launcher_exposes_all_three_mock_round_robin_modes() -> None:
    launcher = (Path(__file__).parent.parent / "play.command").read_text()

    menu_entries = {
        14: ("知识竞答", "knowledge_quiz"),
        15: ("数学问答", "math_quiz"),
        16: ("逻辑推理", "reasoning_quiz"),
        17: ("猜谜竞答", "riddle_quiz"),
        18: ("五子棋", "gomoku"),
        19: ("国际象棋", "chess"),
    }
    for number, (label, game) in menu_entries.items():
        assert f"{number}) {label}" in launcher
        assert "3 个 mock 循环赛" in next(
            line for line in launcher.splitlines() if f"{number}) {label}" in line
        )
        command_line = next(
            line
            for line in launcher.splitlines()
            if line.strip().startswith(f"{number}) llmolympic round-robin")
        )
        assert f"--game {game}" in command_line

    command_lines = [
        line.strip() for line in launcher.splitlines() if "llmolympic round-robin" in line
    ]
    command_games = [
        match.group(1)
        for line in command_lines
        if (match := re.search(r"--game ([a-z_]+)", line)) is not None
    ]
    assert sorted(command_games) == list_games("round_robin")
    assert len(command_games) == len(set(command_games))

    for line in command_lines:
        assert "--players mock:random,mock:fixed,mock:illegal" in line
        if "--game gomoku" in line or "--game chess" in line:
            assert "--rounds" not in line
        else:
            assert "--rounds 5" in line

    assert "21) 五子棋      你（黑）vs LLM" in launcher


def test_launcher_exposes_offline_creative_writing_with_three_judges() -> None:
    launcher = (Path(__file__).parent.parent / "play.command").read_text()

    assert "20) 创意写作    2 个 mock + 3 个匿名算法评委" in launcher
    command = next(
        line for line in launcher.splitlines() if line.strip().startswith("20) llmolympic play")
    )
    assert "--game creative_writing" in command
    assert "--players mock:random,mock:fixed" in command
    assert command.count("--judge") == 3

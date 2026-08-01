"""macOS 双击启动器的静态回归测试。"""

from pathlib import Path


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
    assert "--game reasoning_quiz --players \"human:我,openai" in launcher
    assert "--game riddle_quiz    --players \"human:我,openai" in launcher


def test_launcher_exposes_chess_human_demo_series_and_llm_modes() -> None:
    launcher = (Path(__file__).parent.parent / "play.command").read_text()

    assert "11) 国际象棋    你（白）vs mock" in launcher
    assert "12) 国际象棋    观看 mock 对战" in launcher
    assert "13) 国际象棋    双局换先手 mock 对战" in launcher
    assert "--game chess          --players human:我,mock:random" in launcher
    assert "--game chess          --players mock:random,mock:fixed" in launcher
    assert "llmolympic series --game chess" in launcher
    assert "19) 国际象棋    你（白）vs LLM" in launcher
    assert "--game chess          --players \"human:我,openai" in launcher

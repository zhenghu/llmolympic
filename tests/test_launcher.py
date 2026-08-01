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

"""Creative-writing adjudication persistence and ELO integration tests."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from llmolympic.core.archive import MatchArchive
from llmolympic.core.events import EventType
from llmolympic.core.judge import JudgePanelError, LLMJudgePanel, PanelVerdict
from llmolympic.core.match import play_match
from llmolympic.core.player import LLMPlayer, Player
from llmolympic.core.storage import SCHEMA_VERSION, SQLiteStore
from llmolympic.games.creative_writing import CreativeWriting
from llmolympic.providers.mock import MockProvider


def _contestants() -> list[LLMPlayer]:
    return [
        LLMPlayer(
            "Contestant A",
            MockProvider("fixed"),
            "contestant-fixed",
            move_timeout_seconds=None,
        ),
        LLMPlayer(
            "Contestant B",
            MockProvider("random", seed=0),
            "contestant-random",
            move_timeout_seconds=None,
        ),
    ]


def _judge(name: str, strategy: str, model: str) -> LLMPlayer:
    return LLMPlayer(
        name,
        MockProvider(strategy),
        model,
        move_timeout_seconds=None,
    )


def _healthy_panel() -> LLMJudgePanel:
    return LLMJudgePanel(
        [
            _judge("Judge Strict", "strict", "judge-strict"),
            _judge("Judge Balanced", "balanced", "judge-balanced"),
            _judge("Judge Lenient", "lenient", "judge-lenient"),
        ]
    )


class _AlwaysInvalidPlayer(Player):
    kind = "scripted"

    async def get_move(self, prompt: str) -> str:
        return "too short"


def test_creative_match_round_trips_and_updates_elo_once(tmp_path) -> None:
    game = CreativeWriting()
    contestants = _contestants()
    archive = asyncio.run(
        play_match(
            game,
            contestants,
            seed=7,
            judge_panel=_healthy_panel(),
        )
    )

    finished = archive.events[-1]
    assert finished.type == EventType.MATCH_FINISHED
    assert finished.data["termination"] == "completed"
    verdict = PanelVerdict.model_validate(finished.data["judging"])
    contestant_names = {contestant.name for contestant in contestants}
    assert verdict.schema_version == 1
    assert verdict.panel_size == 3
    assert verdict.quorum == 2
    assert verdict.successful_judges == 3
    assert verdict.failures == []
    assert verdict.scores == archive.scores == finished.data["scores"]
    assert len(verdict.verdicts) == 3
    assert {item.judge.model for item in verdict.verdicts} == {
        "judge-strict",
        "judge-balanced",
        "judge-lenient",
    }
    for item in verdict.verdicts:
        assert set(item.label_map) == {"A", "B"}
        assert set(item.label_map.values()) == contestant_names
        assert set(item.scores) == contestant_names
        assert set(item.rationales) == contestant_names

    assert archive.scores["Contestant A"] > archive.scores["Contestant B"]

    path = tmp_path / "creative.db"
    store = SQLiteStore(path)
    with sqlite3.connect(path) as connection:
        schema_before = connection.execute("PRAGMA user_version").fetchone()[0]

    first = store.save_match(archive, rating_source="engine")

    assert first.inserted is True
    assert first.rated is True
    assert {
        (change.game, change.player, change.outcome)
        for change in first.rating_changes
    } == {
        (None, "Contestant A", 1.0),
        (None, "Contestant B", 0.0),
        ("creative_writing", "Contestant A", 1.0),
        ("creative_writing", "Contestant B", 0.0),
    }

    loaded = store.get_match(archive.match_id)
    assert loaded is not None
    assert loaded.model_dump(mode="json") == archive.model_dump(mode="json")
    assert loaded.events[-1].data["judging"] == finished.data["judging"]

    overall = {entry.player: entry for entry in store.leaderboard()}
    creative = {entry.player: entry for entry in store.leaderboard(game="creative_writing")}
    assert overall["Contestant A"].rating == pytest.approx(1516.0)
    assert overall["Contestant B"].rating == pytest.approx(1484.0)
    assert creative["Contestant A"].rating == pytest.approx(1516.0)
    assert creative["Contestant B"].rating == pytest.approx(1484.0)

    repeated = store.save_match(archive, rating_source="engine")
    assert repeated.inserted is False
    assert repeated.rated is True
    assert repeated.rating_changes == ()
    assert {
        entry.player: entry.rating for entry in store.leaderboard()
    } == pytest.approx({"Contestant A": 1516.0, "Contestant B": 1484.0})
    assert {
        entry.player: entry.rating for entry in store.leaderboard(game="creative_writing")
    } == pytest.approx({"Contestant A": 1516.0, "Contestant B": 1484.0})

    with sqlite3.connect(path) as connection:
        schema_after = connection.execute("PRAGMA user_version").fetchone()[0]
    assert schema_before == schema_after == SCHEMA_VERSION == 7


def test_creative_quorum_failure_is_not_saved(tmp_path) -> None:
    panel = LLMJudgePanel(
        [
            _judge("Invalid Judge A", "illegal", "invalid-judge-a"),
            _judge("Invalid Judge B", "illegal", "invalid-judge-b"),
            _judge("Valid Judge", "balanced", "valid-judge"),
        ]
    )
    store = SQLiteStore(tmp_path / "quorum-failure.db")

    with pytest.raises(JudgePanelError, match="法定人数") as error:
        asyncio.run(
            play_match(
                CreativeWriting(),
                _contestants(),
                seed=11,
                judge_panel=panel,
            )
        )

    assert error.value.reason_code == "judge_quorum_not_met"
    assert store.list_matches() == []
    assert store.leaderboard() == []
    assert store.leaderboard(game="creative_writing") == []


def test_creative_archive_rejects_missing_or_tampered_judging() -> None:
    archive = asyncio.run(
        play_match(
            CreativeWriting(),
            _contestants(),
            seed=5,
            judge_panel=_healthy_panel(),
        )
    )
    payload = archive.model_dump(mode="python")
    payload["events"][-1]["data"].pop("judging")
    with pytest.raises(ValueError, match="必须包含评审团裁决"):
        MatchArchive.model_validate(payload)

    tampered = archive.model_dump(mode="python")
    judging = tampered["events"][-1]["data"]["judging"]
    judging["scores"]["Contestant A"] = 0.0
    tampered["events"][-1]["data"]["scores"]["Contestant A"] = 0.0
    tampered["scores"]["Contestant A"] = 0.0
    with pytest.raises(ValueError, match="无效的评审团裁决"):
        MatchArchive.model_validate(tampered)


def test_creative_archive_rejects_forged_forfeit_score() -> None:
    contestants: list[Player] = [
        _AlwaysInvalidPlayer("Forfeiter"),
        LLMPlayer(
            "Writer",
            MockProvider("fixed"),
            "writer-fixed",
            move_timeout_seconds=None,
        ),
    ]
    archive = asyncio.run(
        play_match(
            CreativeWriting(),
            contestants,
            seed=8,
            judge_panel=_healthy_panel(),
        )
    )
    assert archive.scores["Forfeiter"] == 0.0

    tampered = archive.model_dump(mode="python")
    tampered["events"][-1]["data"]["judging"]["fixed_scores"]["Forfeiter"] = 1.0
    tampered["events"][-1]["data"]["judging"]["scores"]["Forfeiter"] = 1.0
    tampered["events"][-1]["data"]["scores"]["Forfeiter"] = 1.0
    tampered["scores"]["Forfeiter"] = 1.0

    with pytest.raises(ValueError, match="放弃者固定分必须为 0"):
        MatchArchive.model_validate(tampered)


def test_creative_archive_rejects_self_human_judge_and_rubric_drift() -> None:
    archive = asyncio.run(
        play_match(
            CreativeWriting(),
            _contestants(),
            seed=13,
            judge_panel=_healthy_panel(),
        )
    )

    self_judged = archive.model_dump(mode="python")
    self_judged["events"][-1]["data"]["judging"]["verdicts"][0]["judge"][
        "judge_id"
    ] = archive.players[0]["entrant_id"]
    with pytest.raises(ValueError, match="不能同时担任评委"):
        MatchArchive.model_validate(self_judged)

    human_judged = archive.model_dump(mode="python")
    human_judged["events"][-1]["data"]["judging"]["verdicts"][0]["judge"][
        "kind"
    ] = "human"
    with pytest.raises(ValueError, match="无效的评审团裁决"):
        MatchArchive.model_validate(human_judged)

    rubric_drift = archive.model_dump(mode="python")
    rubric_drift["events"][-1]["data"]["judging"]["rubric_version"] = "forged-v2"
    with pytest.raises(ValueError, match="冻结的 rubric 不一致"):
        MatchArchive.model_validate(rubric_drift)

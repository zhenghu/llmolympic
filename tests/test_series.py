"""交换先后手双局赛核心编排测试。"""

from __future__ import annotations

import asyncio

import pytest

from llmolympic.core.archive import legacy_entrant_id
from llmolympic.core.events import EventType
from llmolympic.core.match import play_match
from llmolympic.core.player import Player, PlayerProviderError
from llmolympic.core.series import (
    SeriesArchive,
    head_to_head_point,
    play_two_leg_series,
    series_from_legs,
)
from llmolympic.games import create_game


class _SequencePlayer(Player):
    kind = "scripted"

    def __init__(self, name: str, moves: list[str]) -> None:
        super().__init__(name)
        self._moves = iter(moves)

    async def get_move(self, prompt: str) -> str:
        return next(self._moves)


class _FailingPlayer(Player):
    kind = "scripted"

    async def get_move(self, prompt: str) -> str:
        raise PlayerProviderError("模型服务调用失败，判技术负", technical_loss=True)


def test_two_leg_gomoku_swaps_order_with_same_seed_and_aggregates_points() -> None:
    player_a = _SequencePlayer(
        "甲",
        ["A1", "B1", "C1", "D1", "E1", "A4", "B4", "C4", "D4"],
    )
    player_b = _SequencePlayer(
        "乙",
        ["A2", "B2", "C2", "D2", "A3", "B3", "C3", "D3", "E3"],
    )
    players = [player_a, player_b]
    rendered: list[tuple[int, object]] = []

    series = asyncio.run(
        play_two_leg_series(
            create_game("gomoku"),
            players,
            seed=2**63 - 1,
            on_event=lambda leg, event: rendered.append((leg, event)),
        )
    )

    assert players == [player_a, player_b]
    assert [[descriptor["name"] for descriptor in leg.players] for leg in series.legs] == [
        ["甲", "乙"],
        ["乙", "甲"],
    ]
    assert [leg.seed for leg in series.legs] == [2**63 - 1, 2**63 - 1]
    assert series.points == {"甲": 1.0, "乙": 1.0}
    assert (series.standings["甲"].wins, series.standings["甲"].losses) == (1, 1)
    assert series.legs[0].scores == {"甲": 1.0, "乙": 0.0}
    assert series.legs[1].scores == {"乙": 1.0, "甲": 0.0}
    assert {leg for leg, _ in rendered} == {1, 2}
    for leg_number, archive in enumerate(series.legs, start=1):
        events = [event for leg, event in rendered if leg == leg_number]
        assert events == archive.events
        assert [event.seq for event in events] == list(range(len(events)))


def test_first_leg_technical_loss_does_not_prevent_second_leg() -> None:
    bad = _FailingPlayer("bad")
    good = _SequencePlayer("good", ["H8"])

    series = asyncio.run(play_two_leg_series(create_game("gomoku"), [bad, good]))

    assert len(series.legs) == 2
    assert [leg.events[-1].data["termination"] for leg in series.legs] == [
        "technical_loss",
        "technical_loss",
    ]
    assert [leg.events[-1].data["forfeited_by"] for leg in series.legs] == [
        "bad",
        "bad",
    ]
    assert series.points == {"bad": 0.0, "good": 2.0}
    assert series.standings["bad"].technical_losses == 2


def test_caller_list_mutation_during_first_leg_cannot_change_schedule() -> None:
    player_a = _SequencePlayer(
        "甲",
        ["A1", "B1", "C1", "D1", "E1", "A4", "B4", "C4", "D4"],
    )
    player_b = _SequencePlayer(
        "乙",
        ["A2", "B2", "C2", "D2", "A3", "B3", "C3", "D3", "E3"],
    )
    players = [player_a, player_b]

    def mutate_caller_list(leg: int, event) -> None:
        if leg == 1 and event.type == EventType.MATCH_STARTED:
            players.reverse()

    series = asyncio.run(
        play_two_leg_series(
            create_game("gomoku"),
            players,
            on_event=mutate_caller_list,
        )
    )

    assert players == [player_b, player_a]
    assert [[descriptor["name"] for descriptor in leg.players] for leg in series.legs] == [
        ["甲", "乙"],
        ["乙", "甲"],
    ]


@pytest.mark.parametrize("count", [1, 3])
def test_two_leg_series_requires_exactly_two_players(count: int) -> None:
    players = [_SequencePlayer(str(index), []) for index in range(count)]

    with pytest.raises(ValueError, match="恰好 2"):
        asyncio.run(play_two_leg_series(create_game("math_quiz"), players))


@pytest.mark.parametrize("invalid_name", ["", None])
def test_invalid_player_name_is_rejected_before_any_event(invalid_name) -> None:
    bad = _SequencePlayer("placeholder", [])
    bad.name = invalid_name
    rendered = []

    with pytest.raises(ValueError, match="非空字符串"):
        asyncio.run(
            play_two_leg_series(
                create_game("knowledge_quiz", rounds=1),
                [bad, _SequencePlayer("good", [])],
                on_event=lambda leg, event: rendered.append((leg, event)),
            )
        )

    assert rendered == []


def test_duplicate_names_and_reused_player_are_rejected_before_any_event() -> None:
    reused = _SequencePlayer("same", [])
    cases = [
        [_SequencePlayer("same", []), _SequencePlayer("same", [])],
        [reused, reused],
    ]
    for players in cases:
        rendered = []
        with pytest.raises(ValueError, match="唯一"):
            asyncio.run(
                play_two_leg_series(
                    create_game("knowledge_quiz", rounds=1),
                    players,
                    on_event=lambda leg, event, _rendered=rendered: _rendered.append(
                        (leg, event)
                    ),
                )
            )
        assert rendered == []


def test_two_draws_are_distinct_from_one_win_and_one_loss_in_standings() -> None:
    series = asyncio.run(
        play_two_leg_series(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A", "A"]), _SequencePlayer("乙", ["A", "A"])],
        )
    )

    assert series.points == {"甲": 1.0, "乙": 1.0}
    assert (series.standings["甲"].wins, series.standings["甲"].draws) == (0, 2)
    assert (series.standings["甲"].losses, series.standings["甲"].technical_losses) == (
        0,
        0,
    )


@pytest.mark.parametrize(
    "scores,error",
    [
        ({"甲": float("nan"), "乙": 0.0}, "有限数值"),
        ({"甲": 1.0}, "完全一致"),
    ],
)
def test_head_to_head_point_rejects_invalid_scores(scores, error: str) -> None:
    series = asyncio.run(
        play_two_leg_series(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A", "A"]), _SequencePlayer("乙", ["A", "A"])],
        )
    )
    invalid = series.legs[0].model_copy(update={"scores": scores})

    with pytest.raises(ValueError, match=error):
        head_to_head_point(invalid, "甲")


def test_series_rejects_technical_loss_that_rewards_forfeiting_player() -> None:
    series = asyncio.run(
        play_two_leg_series(
            create_game("gomoku"),
            [
                _SequencePlayer("甲", ["A1", "B1", "C1", "D1", "E1", "A4", "B4", "C4", "D4"]),
                _SequencePlayer("乙", ["A2", "B2", "C2", "D2", "A3", "B3", "C3", "D3", "E3"]),
            ],
        )
    )
    first = series.legs[0].model_copy(deep=True)
    first.events[-1].data.update(
        {"termination": "technical_loss", "forfeited_by": "甲"}
    )

    with pytest.raises(ValueError, match="责任方 0 分"):
        series_from_legs(first, series.legs[1])


def test_series_rejects_legs_with_different_game_configuration() -> None:
    async def play_mismatched_legs():
        player_a = _SequencePlayer("甲", ["A", "A", "A"])
        player_b = _SequencePlayer("乙", ["A", "A", "A"])
        first = await play_match(
            create_game("knowledge_quiz", rounds=1),
            [player_a, player_b],
            seed=9,
        )
        second = await play_match(
            create_game("knowledge_quiz", rounds=2),
            [player_b, player_a],
            seed=9,
        )
        return first, second

    first, second = asyncio.run(play_mismatched_legs())

    with pytest.raises(ValueError, match="项目配置"):
        series_from_legs(first, second)


def test_series_event_callback_receives_all_batch_prompts_before_player_moves() -> None:
    prompts_seen = 0
    moves_started = 0

    class _ObservingPlayer(Player):
        async def get_move(self, prompt: str) -> str:
            nonlocal moves_started
            assert prompts_seen == (moves_started // 2 + 1) * 2
            moves_started += 1
            return "A"

    def observe(leg: int, event) -> None:
        nonlocal prompts_seen
        if event.type == EventType.TURN_PROMPT:
            prompts_seen += 1

    asyncio.run(
        play_two_leg_series(
            create_game("knowledge_quiz", rounds=1),
            [_ObservingPlayer("甲"), _ObservingPlayer("乙")],
            on_event=observe,
        )
    )

    assert prompts_seen == 4
    assert moves_started == 4


def test_series_archive_rejects_unknown_top_level_field() -> None:
    series = asyncio.run(
        play_two_leg_series(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A", "A"]), _SequencePlayer("乙", ["A", "A"])],
        )
    )
    payload = series.model_dump(mode="python")
    payload["unexpected"] = True

    with pytest.raises(ValueError, match="unexpected"):
        SeriesArchive.model_validate(payload)


def test_series_missing_schema_version_uses_legacy_compatibility() -> None:
    series = asyncio.run(
        play_two_leg_series(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A", "A"]), _SequencePlayer("乙", ["A", "A"])],
        )
    )
    payload = series.model_dump(mode="python")
    payload.pop("schema_version")
    payload.pop("source")
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    for leg in payload["legs"]:
        leg.pop("schema_version")
        leg.pop("source")
        for descriptor in leg["players"]:
            descriptor.pop("entrant_id")
            descriptor.pop("display_name")
        for descriptor in leg["events"][0]["data"]["players"]:
            descriptor.pop("entrant_id")
            descriptor.pop("display_name")

    loaded = SeriesArchive.model_validate(payload)

    assert loaded.schema_version == 1
    assert loaded.source == "legacy"
    assert [descriptor["entrant_id"] for descriptor in loaded.players] == [
        legacy_entrant_id("甲"),
        legacy_entrant_id("乙"),
    ]
    assert all(leg.schema_version == 1 and leg.source == "legacy" for leg in loaded.legs)


def test_schema_v1_series_rejects_an_explicit_nonlegacy_source() -> None:
    series = asyncio.run(
        play_two_leg_series(
            create_game("knowledge_quiz", rounds=1),
            [_SequencePlayer("甲", ["A", "A"]), _SequencePlayer("乙", ["A", "A"])],
        )
    )
    payload = series.model_dump(mode="python")
    payload["schema_version"] = 1
    payload["source"] = "external"
    for descriptor in payload["players"]:
        descriptor.pop("entrant_id")
        descriptor.pop("display_name")
    for leg in payload["legs"]:
        leg["schema_version"] = 1
        leg.pop("source")
        for descriptor in leg["players"]:
            descriptor.pop("entrant_id")
            descriptor.pop("display_name")
        for descriptor in leg["events"][0]["data"]["players"]:
            descriptor.pop("entrant_id")
            descriptor.pop("display_name")

    with pytest.raises(ValueError, match="schema v1 .*legacy"):
        SeriesArchive.model_validate(payload)

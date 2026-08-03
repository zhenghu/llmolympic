"""Round-robin tournament scheduling, archive, and standings tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from llmolympic.core.events import EventType
from llmolympic.core.player import HumanPlayer, Player, PlayerProviderError
from llmolympic.core.tournament import (
    TournamentArchive,
    play_round_robin,
    round_robin_pair_seed,
    tournament_from_series,
)
from llmolympic.games import create_game


class _FixedPlayer(Player):
    kind = "scripted"

    def __init__(
        self,
        name: str,
        move: str = "A",
        *,
        entrant_id: str | None = None,
    ) -> None:
        super().__init__(name, entrant_id=entrant_id)
        self.move = move

    async def get_move(self, prompt: str) -> str:
        return self.move


class _FailingPlayer(Player):
    kind = "scripted"

    async def get_move(self, prompt: str) -> str:
        raise PlayerProviderError("provider failed", technical_loss=True)


class _OnePlayerOnlyGame:
    name = "one_player_only"
    min_players = 1
    max_players = 1

    @staticmethod
    def describe_config() -> dict:
        return {}


def _players() -> list[Player]:
    return [_FixedPlayer("甲"), _FixedPlayer("乙"), _FixedPlayer("丙")]


@pytest.fixture
def tournament() -> TournamentArchive:
    return asyncio.run(
        play_round_robin(
            create_game("knowledge_quiz", rounds=1),
            _players(),
            seed=42,
        )
    )


def test_round_robin_uses_canonical_pair_order_and_archives_every_event() -> None:
    players = _players()
    rendered = []

    archive = asyncio.run(
        play_round_robin(
            create_game("knowledge_quiz", rounds=1),
            players,
            seed=42,
            on_event=lambda pairing, leg, event: rendered.append((pairing, leg, event)),
        )
    )

    assert archive.source == "local_engine"
    assert [pairing.player_indices for pairing in archive.pairings] == [
        (0, 1),
        (0, 2),
        (1, 2),
    ]
    assert [pairing.pairing_number for pairing in archive.pairings] == [1, 2, 3]
    assert len({pairing.series.series_id for pairing in archive.pairings}) == 3
    assert len({leg.match_id for pairing in archive.pairings for leg in pairing.series.legs}) == 6

    for pairing_number, pairing in enumerate(archive.pairings, start=1):
        first_index, second_index = pairing.player_indices
        first_name = archive.players[first_index]["name"]
        second_name = archive.players[second_index]["name"]
        assert [descriptor["name"] for descriptor in pairing.series.legs[0].players] == [
            first_name,
            second_name,
        ]
        assert [descriptor["name"] for descriptor in pairing.series.legs[1].players] == [
            second_name,
            first_name,
        ]
        assert pairing.seed == round_robin_pair_seed(
            42,
            archive.players[first_index]["entrant_id"],
            archive.players[second_index]["entrant_id"],
        )
        assert [leg.seed for leg in pairing.series.legs] == [pairing.seed, pairing.seed]
        for leg_number, leg in enumerate(pairing.series.legs, start=1):
            events = [
                event
                for rendered_pairing, rendered_leg, event in rendered
                if (rendered_pairing, rendered_leg) == (pairing_number, leg_number)
            ]
            assert events == leg.events

    assert archive.points == {"甲": 2.0, "乙": 2.0, "丙": 2.0}
    for standing in archive.standings:
        assert standing.series_played == 2
        assert standing.series_draws == 2
        assert standing.games_played == 4
        assert standing.draws == 4
        assert standing.points == 2.0


def test_four_player_round_robin_has_six_series_and_twelve_matches() -> None:
    archive = asyncio.run(
        play_round_robin(
            create_game("knowledge_quiz", rounds=1),
            [*_players(), _FixedPlayer("丁")],
            seed=7,
        )
    )

    assert len(archive.pairings) == 6
    assert sum(len(pairing.series.legs) for pairing in archive.pairings) == 12
    assert all(standing.series_played == 3 for standing in archive.standings)
    assert all(standing.games_played == 6 for standing in archive.standings)


def test_pair_seed_has_fixed_vectors_and_is_order_independent() -> None:
    assert round_robin_pair_seed(42, "entrant:a", "entrant:b") == -6407977967001699509
    assert round_robin_pair_seed(42, "entrant:b", "entrant:a") == -6407977967001699509
    assert round_robin_pair_seed(-(2**63), "entrant:a", "entrant:b") == -6745389254772604205
    assert round_robin_pair_seed(2**63 - 1, "entrant:a", "entrant:b") == 1984664365196310049


def test_mutating_caller_player_list_does_not_change_frozen_schedule() -> None:
    players = _players()

    def mutate_players(pairing: int, leg: int, event) -> None:
        if (pairing, leg, event.type) == (1, 1, EventType.MATCH_STARTED):
            players.reverse()

    archive = asyncio.run(
        play_round_robin(
            create_game("knowledge_quiz", rounds=1),
            players,
            on_event=mutate_players,
        )
    )

    assert [player.name for player in players] == ["丙", "乙", "甲"]
    assert [descriptor["name"] for descriptor in archive.players] == ["甲", "乙", "丙"]
    assert [pairing.player_indices for pairing in archive.pairings] == [
        (0, 1),
        (0, 2),
        (1, 2),
    ]


@pytest.mark.parametrize("count", [0, 1, 2, 17])
def test_player_count_is_rejected_before_any_event(count: int) -> None:
    players = [_FixedPlayer(str(index)) for index in range(count)]
    rendered = []

    with pytest.raises(ValueError, match="3 到 16"):
        asyncio.run(
            play_round_robin(
                create_game("knowledge_quiz", rounds=1),
                players,
                on_event=lambda pairing, leg, event: rendered.append((pairing, leg, event)),
            )
        )

    assert rendered == []


def test_human_and_duplicate_identity_are_rejected_before_any_event() -> None:
    cases = [
        [_FixedPlayer("甲"), _FixedPlayer("乙"), HumanPlayer("人类")],
        [
            _FixedPlayer("甲", entrant_id="same:entrant"),
            _FixedPlayer("乙", entrant_id="same:entrant"),
            _FixedPlayer("丙"),
        ],
    ]

    for players in cases:
        rendered = []
        with pytest.raises(ValueError, match="HumanPlayer|entrant_id 必须唯一"):
            asyncio.run(
                play_round_robin(
                    create_game("knowledge_quiz", rounds=1),
                    players,
                    on_event=lambda pairing, leg, event, output=rendered: output.append(
                        (pairing, leg, event)
                    ),
                )
            )
        assert rendered == []


def test_reused_player_object_is_rejected_before_any_event() -> None:
    reused = _FixedPlayer("甲")
    rendered = []

    with pytest.raises(ValueError, match="Player 对象"):
        asyncio.run(
            play_round_robin(
                create_game("knowledge_quiz", rounds=1),
                [reused, reused, _FixedPlayer("丙")],
                on_event=lambda pairing, leg, event: rendered.append((pairing, leg, event)),
            )
        )

    assert rendered == []


@pytest.mark.parametrize("seed", [True, -(2**63) - 1, 2**63])
def test_invalid_base_seed_is_rejected_before_any_event(seed) -> None:
    rendered = []

    with pytest.raises(ValueError, match="signed 64-bit"):
        asyncio.run(
            play_round_robin(
                create_game("knowledge_quiz", rounds=1),
                _players(),
                seed=seed,
                on_event=lambda pairing, leg, event: rendered.append((pairing, leg, event)),
            )
        )

    assert rendered == []


@pytest.mark.parametrize("max_attempts", [True, 0, 11])
def test_invalid_max_attempts_is_rejected_before_any_event(max_attempts) -> None:
    rendered = []

    with pytest.raises(ValueError, match="max_attempts"):
        asyncio.run(
            play_round_robin(
                create_game("knowledge_quiz", rounds=1),
                _players(),
                max_attempts=max_attempts,
                on_event=lambda pairing, leg, event: rendered.append((pairing, leg, event)),
            )
        )

    assert rendered == []


def test_game_that_does_not_support_two_players_fails_preflight() -> None:
    rendered = []

    with pytest.raises(ValueError, match="恰好 1"):
        asyncio.run(
            play_round_robin(
                _OnePlayerOnlyGame(),
                _players(),
                on_event=lambda pairing, leg, event: rendered.append((pairing, leg, event)),
            )
        )

    assert rendered == []


def test_tournament_from_series_rebuilds_archive_and_json_round_trips(
    tournament: TournamentArchive,
) -> None:
    rebuilt = tournament_from_series(
        tournament.players,
        [pairing.series for pairing in tournament.pairings],
        seed=tournament.seed,
        tournament_id="rebuilt",
    )
    loaded = TournamentArchive.model_validate_json(rebuilt.to_json())

    assert rebuilt.tournament_id == "rebuilt"
    assert rebuilt.points == tournament.points
    assert [pairing.player_indices for pairing in rebuilt.pairings] == [
        pairing.player_indices for pairing in tournament.pairings
    ]
    assert loaded == rebuilt


def test_standings_include_series_results_and_use_stable_tie_break() -> None:
    good_a = _FixedPlayer("甲")
    good_b = _FixedPlayer("乙")
    bad = _FailingPlayer("坏")

    archive = asyncio.run(
        play_round_robin(
            create_game("knowledge_quiz", rounds=1),
            [good_a, good_b, bad],
        )
    )

    tied_good = sorted((good_a, good_b), key=lambda player: player.entrant_id)
    assert [standing.entrant_id for standing in archive.standings[:2]] == [
        player.entrant_id for player in tied_good
    ]
    for standing in archive.standings[:2]:
        assert (standing.series_wins, standing.series_draws, standing.series_losses) == (
            1,
            1,
            0,
        )
        assert (standing.wins, standing.draws, standing.losses) == (2, 2, 0)
        assert standing.points == 3.0
    bad_standing = archive.standings[-1]
    assert (bad_standing.series_wins, bad_standing.series_losses) == (0, 2)
    assert (bad_standing.losses, bad_standing.technical_losses, bad_standing.points) == (
        4,
        4,
        0.0,
    )


def test_archive_rejects_points_not_derived_from_series(
    tournament: TournamentArchive,
) -> None:
    payload = tournament.model_dump(mode="python")
    payload["points"]["甲"] += 0.5

    with pytest.raises(ValueError, match="总局分与"):
        TournamentArchive.model_validate(payload)


def test_archive_rejects_missing_or_out_of_order_pairing(
    tournament: TournamentArchive,
) -> None:
    missing = tournament.model_dump(mode="python")
    missing["pairings"] = missing["pairings"][:-1]
    with pytest.raises(ValueError, match="每对选手恰好"):
        TournamentArchive.model_validate(missing)

    reordered = tournament.model_dump(mode="python")
    reordered["pairings"][0]["player_indices"] = (0, 2)
    with pytest.raises(ValueError, match="输入顺序"):
        TournamentArchive.model_validate(reordered)


def test_archive_rejects_tampered_pair_seed_and_duplicate_ids(
    tournament: TournamentArchive,
) -> None:
    wrong_seed = tournament.model_dump(mode="python")
    wrong_seed["pairings"][0]["seed"] = 0
    with pytest.raises(ValueError, match="seed 与稳定身份"):
        TournamentArchive.model_validate(wrong_seed)

    duplicate_series = tournament.model_dump(mode="python")
    duplicate_series["pairings"][1]["series"]["series_id"] = duplicate_series["pairings"][0][
        "series"
    ]["series_id"]
    with pytest.raises(ValueError, match="series_id 必须全局唯一"):
        TournamentArchive.model_validate(duplicate_series)

    duplicate_match = tournament.model_dump(mode="python")
    duplicate_match["pairings"][1]["series"]["legs"][0]["match_id"] = duplicate_match["pairings"][
        0
    ]["series"]["legs"][0]["match_id"]
    with pytest.raises(ValueError, match="match_id 必须全局唯一"):
        TournamentArchive.model_validate(duplicate_match)


def test_archive_rejects_cross_series_config_and_time_mismatch(
    tournament: TournamentArchive,
) -> None:
    config = tournament.model_dump(mode="python")
    for leg in config["pairings"][1]["series"]["legs"]:
        leg["events"][0]["data"]["game_config"]["tampered"] = True
    with pytest.raises(ValueError, match="相同的项目配置"):
        TournamentArchive.model_validate(config)

    time = tournament.model_dump(mode="python")
    time["started_at"] += timedelta(microseconds=1)
    with pytest.raises(ValueError, match="首尾双局赛边界"):
        TournamentArchive.model_validate(time)


def test_archive_rejects_tampered_nested_event_semantics(
    tournament: TournamentArchive,
) -> None:
    started_seed = tournament.model_dump(mode="python")
    started_seed["pairings"][0]["series"]["legs"][0]["events"][0]["data"]["seed"] = 999
    with pytest.raises(ValueError, match="match_started.*seed"):
        TournamentArchive.model_validate(started_seed)

    finished_scores = tournament.model_dump(mode="python")
    finished_scores["pairings"][0]["series"]["legs"][0]["events"][-1]["data"]["scores"]["甲"] = 0.25
    with pytest.raises(ValueError, match="match_finished 比分"):
        TournamentArchive.model_validate(finished_scores)

    reversed_time = tournament.model_dump(mode="python")
    first_leg_events = reversed_time["pairings"][0]["series"]["legs"][0]["events"]
    first_leg_events[1]["timestamp"] = first_leg_events[0]["timestamp"] - timedelta(microseconds=1)
    with pytest.raises(ValueError, match="事件时间不能逆序"):
        TournamentArchive.model_validate(reversed_time)


def test_archive_rejects_unknown_field_and_legacy_source(
    tournament: TournamentArchive,
) -> None:
    unexpected = tournament.model_dump(mode="python")
    unexpected["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        TournamentArchive.model_validate(unexpected)

    legacy = tournament.model_dump(mode="python")
    legacy["source"] = "legacy"
    with pytest.raises(ValueError, match="local_engine|external"):
        TournamentArchive.model_validate(legacy)

"""_TournamentMixin mixin for SQLiteStore."""

from __future__ import annotations

import json
import math
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from llmolympic.core._storage_types import (
    DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    SQLITE_INT_MAX,
    MatchIdCollisionError,
    ProviderBudgetCollisionError,
    ProviderBudgetSnapshot,
    RatingChange,
    RatingSource,
    SaveResult,
    SeriesIdCollisionError,
    StorageError,
    TournamentCheckpointCollisionError,
    TournamentCheckpointSaveResult,
    TournamentIdCollisionError,
    TournamentRatingChange,
    TournamentRunnerClaim,
    TournamentRunnerLease,
    TournamentRunnerLeaseBusyError,
    TournamentRunnerLeaseLostError,
    TournamentSaveResult,
    _canonical_json,
    _EntrantRef,
    _runner_lease_token_digest,
    _TournamentAggregate,
    _validate_durable_budget_definition,
    _validate_runner_lease_handle,
    _validate_runner_lease_seconds,
    _validate_usage_ledger_id,
)
from llmolympic.core.archive import MatchArchive
from llmolympic.core.elo import DEFAULT_RATING, K_FACTOR, expected_score, update_ratings
from llmolympic.core.series import SeriesArchive, head_to_head_point
from llmolympic.core.tournament import (
    TOURNAMENT_SCHEMA_VERSION,
    TournamentArchive,
    TournamentCheckpoint,
    tournament_from_series,
)
from llmolympic.core.usage import BudgetLimits, ProviderBudgetPolicy


class _TournamentMixin:
    def _verify_tournament_ratings(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        entrants: tuple[_EntrantRef, ...],
        *,
        rated: bool,
    ) -> bool:
        history_rows = connection.execute(
            """
            SELECT rh.match_id, rh.rating_scope, rh.game, rh.entrant_id,
                   rh.display_name, rh.opponent_entrant_id,
                   rh.opponent_display_name, rh.outcome, rh.rating_before,
                   rh.rating_after, rh.created_at
            FROM rating_history AS rh
            JOIN series_matches AS sm ON sm.match_id = rh.match_id
            JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
            WHERE tp.tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchall()
        snapshot_rows = connection.execute(
            """
            SELECT rating_scope, game, entrant_id, display_name, rating_before,
                   rating_after, games_added, wins_added, draws_added,
                   losses_added
            FROM tournament_rating_snapshots
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchall()
        contribution_rows = connection.execute(
            """
            SELECT sequence, match_id, rating_scope, game, entrant_id,
                   opponent_entrant_id, frozen_rating, opponent_frozen_rating,
                   expected_score, rating_delta
            FROM tournament_rating_contributions
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchall()
        if not rated:
            if history_rows or snapshot_rows or contribution_rows:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的未计分状态已损坏"
                )
            return True

        snapshot_map = {
            (row["rating_scope"], row["game"], row["entrant_id"]): row for row in snapshot_rows
        }
        expected_snapshot_keys = {
            (rating_scope, game_key, entrant.entrant_id)
            for rating_scope, game_key in (("overall", ""), ("game", tournament.game))
            for entrant in entrants
        }
        if len(snapshot_map) != len(snapshot_rows) or set(snapshot_map) != expected_snapshot_keys:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
            )
        try:
            frozen_ratings = {
                key: self._finite_database_float(row["rating_before"])
                for key, row in snapshot_map.items()
            }
        except (TypeError, ValueError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
            ) from exc

        expected_contributions, expected_aggregates = self._tournament_rating_ledger(
            tournament,
            entrants,
            frozen_ratings,
        )
        history = {
            (row["match_id"], row["rating_scope"], row["game"], row["entrant_id"]): row
            for row in history_rows
        }
        contributions = {
            (row["match_id"], row["rating_scope"], row["game"], row["entrant_id"]): row
            for row in contribution_rows
        }
        expected_keys = {
            (
                contribution.archive.match_id,
                contribution.rating_scope,
                contribution.game_key,
                contribution.player.entrant_id,
            )
            for contribution in expected_contributions
        }
        if (
            len(history) != len(history_rows)
            or len(contributions) != len(contribution_rows)
            or set(history) != expected_keys
            or set(contributions) != expected_keys
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 历史已损坏"
            )
        for expected in expected_contributions:
            key = (
                expected.archive.match_id,
                expected.rating_scope,
                expected.game_key,
                expected.player.entrant_id,
            )
            history_row = history[key]
            contribution_row = contributions[key]
            try:
                stored_outcome = self._finite_database_float(history_row["outcome"])
                stored_before = self._finite_database_float(history_row["rating_before"])
                stored_after = self._finite_database_float(history_row["rating_after"])
                stored_frozen = self._finite_database_float(contribution_row["frozen_rating"])
                stored_opponent_frozen = self._finite_database_float(
                    contribution_row["opponent_frozen_rating"]
                )
                stored_expected = self._finite_database_float(contribution_row["expected_score"])
                stored_delta = self._finite_database_float(contribution_row["rating_delta"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 历史已损坏"
                ) from exc
            if (
                history_row["display_name"] != expected.player.display_name
                or history_row["opponent_entrant_id"] != expected.opponent.entrant_id
                or history_row["opponent_display_name"] != expected.opponent.display_name
                or stored_outcome != expected.outcome
                or stored_before != expected.before
                or stored_after != expected.after
                or not self._timestamp_matches(
                    history_row["created_at"], expected.archive.finished_at
                )
                or contribution_row["sequence"] != expected.sequence
                or contribution_row["opponent_entrant_id"] != expected.opponent.entrant_id
                or stored_frozen != expected.frozen_rating
                or stored_opponent_frozen != expected.opponent_frozen_rating
                or stored_expected != expected.expected
                or stored_delta != expected.delta
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 历史已损坏"
                )

        for expected in expected_aggregates:
            key = (
                expected.rating_scope,
                expected.game_key,
                expected.player.entrant_id,
            )
            row = snapshot_map[key]
            wins = sum(outcome == 1.0 for outcome in expected.outcomes)
            draws = sum(outcome == 0.5 for outcome in expected.outcomes)
            losses = sum(outcome == 0.0 for outcome in expected.outcomes)
            try:
                stored_after = self._finite_database_float(row["rating_after"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
                ) from exc
            if (
                row["display_name"] != expected.player.display_name
                or stored_after != expected.after
                or row["games_added"] != len(expected.outcomes)
                or row["wins_added"] != wins
                or row["draws_added"] != draws
                or row["losses_added"] != losses
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的 ELO 快照已损坏"
                )

        return self._verify_current_tournament_ratings_if_latest(
            connection,
            tournament,
            expected_aggregates,
        )

    def _verify_global_rating_operation_replay(
        self,
        connection: sqlite3.Connection,
    ) -> bool:
        """Replay every committed ELO operation in its v7 sequence."""

        operation_rows = connection.execute(
            """
            SELECT rating_operation_seq, match_id, series_id, tournament_id
            FROM rating_operations
            ORDER BY rating_operation_seq
            """
        ).fetchall()
        expected_candidates = self._rating_operation_candidates(connection)
        if any(row["first_history_row"] is None for row in expected_candidates):
            raise StorageError("SQLite 全局评分操作账本缺少 ELO 历史")
        expected_keys = {
            (row["operation_kind"], row["operation_id"]) for row in expected_candidates
        }

        actual_keys: list[tuple[str, str]] = []
        sequences: list[int] = []
        for row in operation_rows:
            sequence = row["rating_operation_seq"]
            identifiers = [
                ("match", row["match_id"]),
                ("series", row["series_id"]),
                ("tournament", row["tournament_id"]),
            ]
            populated = [(kind, identifier) for kind, identifier in identifiers if identifier]
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
                or len(populated) != 1
                or not isinstance(populated[0][1], str)
            ):
                raise StorageError("SQLite 全局评分操作账本已损坏")
            sequences.append(sequence)
            actual_keys.append((populated[0][0], populated[0][1]))
        if (
            sequences != list(range(1, len(sequences) + 1))
            or len(actual_keys) != len(set(actual_keys))
            or set(actual_keys) != expected_keys
        ):
            raise StorageError("SQLite 全局评分操作账本覆盖不完整")
        orphaned_tournament_rating_data = connection.execute(
            """
            SELECT 1
            FROM tournament_rating_snapshots AS trs
            JOIN tournament_archives AS ta
              ON ta.tournament_id = trs.tournament_id
            LEFT JOIN rating_operations AS ro
              ON ro.tournament_id = trs.tournament_id
            WHERE ta.rated <> 1 OR ro.rating_operation_seq IS NULL

            UNION ALL

            SELECT 1
            FROM tournament_rating_contributions AS trc
            JOIN tournament_archives AS ta
              ON ta.tournament_id = trc.tournament_id
            LEFT JOIN rating_operations AS ro
              ON ro.tournament_id = trc.tournament_id
            WHERE ta.rated <> 1 OR ro.rating_operation_seq IS NULL
            LIMIT 1
            """
        ).fetchone()
        if orphaned_tournament_rating_data is not None:
            raise StorageError("SQLite 循环赛 ELO 数据没有对应的全局评分操作")

        replay: dict[tuple[str, str, str], dict[str, object]] = {}
        replayed_history_rows = 0
        for operation_kind, operation_id in actual_keys:
            if operation_kind == "match":
                operation_metadata = connection.execute(
                    """
                    SELECT game, finished_at, 1 AS expected_steps
                    FROM matches WHERE match_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                history_rows = connection.execute(
                    """
                    SELECT 1 AS step, rh.rating_scope, rh.game, rh.entrant_id,
                           rh.opponent_entrant_id, rh.outcome, rh.rating_before,
                           rh.rating_after,
                           CASE WHEN mp.position = 0 THEN 1 ELSE 0 END AS is_player_a
                    FROM rating_history AS rh
                    JOIN match_players AS mp
                      ON mp.match_id = rh.match_id
                     AND mp.entrant_id = rh.entrant_id
                    WHERE rh.match_id = ?
                    """,
                    (operation_id,),
                ).fetchall()
            elif operation_kind == "series":
                operation_metadata = connection.execute(
                    """
                    SELECT game, finished_at, 2 AS expected_steps
                    FROM series_archives WHERE series_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                history_rows = connection.execute(
                    """
                    SELECT sm.leg_number AS step, rh.rating_scope, rh.game,
                           rh.entrant_id, rh.opponent_entrant_id, rh.outcome,
                           rh.rating_before, rh.rating_after,
                           CASE WHEN rh.entrant_id = first_mp.entrant_id
                                THEN 1 ELSE 0 END AS is_player_a
                    FROM series_matches AS sm
                    JOIN rating_history AS rh ON rh.match_id = sm.match_id
                    JOIN series_matches AS first_sm
                      ON first_sm.series_id = sm.series_id
                     AND first_sm.leg_number = 1
                    JOIN match_players AS first_mp
                      ON first_mp.match_id = first_sm.match_id
                     AND first_mp.position = 0
                    WHERE sm.series_id = ?
                    """,
                    (operation_id,),
                ).fetchall()
            else:
                operation_metadata = connection.execute(
                    """
                    SELECT game, finished_at, pairing_count * 2 AS expected_steps
                    FROM tournament_archives WHERE tournament_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
                history_rows = connection.execute(
                    """
                    SELECT trc.sequence AS step, rh.rating_scope, rh.game,
                           rh.entrant_id, rh.opponent_entrant_id, rh.outcome,
                           rh.rating_before, rh.rating_after,
                           CASE WHEN rh.entrant_id = tp.entrant_a_id
                                THEN 1 ELSE 0 END AS is_player_a
                    FROM tournament_rating_contributions AS trc
                    JOIN rating_history AS rh
                      ON rh.match_id = trc.match_id
                     AND rh.rating_scope = trc.rating_scope
                     AND rh.game = trc.game
                     AND rh.entrant_id = trc.entrant_id
                    JOIN series_matches AS sm ON sm.match_id = trc.match_id
                    JOIN tournament_pairings AS tp
                      ON tp.tournament_id = trc.tournament_id
                     AND tp.series_id = sm.series_id
                    WHERE trc.tournament_id = ?
                    """,
                    (operation_id,),
                ).fetchall()
            if operation_metadata is None or not history_rows:
                raise StorageError("SQLite 全局评分操作账本关联的档案或历史已损坏")
            game = operation_metadata["game"]
            expected_step_count = operation_metadata["expected_steps"]
            if (
                not isinstance(game, str)
                or not game
                or isinstance(expected_step_count, bool)
                or not isinstance(expected_step_count, int)
                or expected_step_count < 1
            ):
                raise StorageError("SQLite 全局评分操作账本关联的档案已损坏")
            try:
                finished_at = datetime.fromisoformat(operation_metadata["finished_at"])
            except (TypeError, ValueError) as exc:
                raise StorageError("SQLite 全局评分操作账本关联的时间已损坏") from exc
            if finished_at.utcoffset() is None:
                raise StorageError("SQLite 全局评分操作账本关联的时间已损坏")
            finished_at = finished_at.astimezone(UTC)

            scope_rows: dict[tuple[str, str], list[sqlite3.Row]] = {}
            for history_row in history_rows:
                scope_rows.setdefault(
                    (history_row["rating_scope"], history_row["game"]),
                    [],
                ).append(history_row)
            if set(scope_rows) != {("overall", ""), ("game", game)}:
                raise StorageError("SQLite 全局评分操作的榜单范围已损坏")

            for (rating_scope, game_key), rows in scope_rows.items():
                step_rows: dict[int, list[sqlite3.Row]] = {}
                for history_row in rows:
                    step = history_row["step"]
                    if isinstance(step, bool) or not isinstance(step, int) or step < 1:
                        raise StorageError("SQLite 全局评分操作的步骤已损坏")
                    step_rows.setdefault(step, []).append(history_row)
                if sorted(step_rows) != list(range(1, expected_step_count + 1)):
                    raise StorageError("SQLite 全局评分操作的步骤覆盖不完整")

                entrants = {
                    history_row["entrant_id"]
                    for history_row in rows
                    if isinstance(history_row["entrant_id"], str) and history_row["entrant_id"]
                }
                if len(entrants) < 2:
                    raise StorageError("SQLite 全局评分操作的参赛者已损坏")
                frozen = {
                    entrant_id: float(
                        replay.get(
                            (rating_scope, game_key, entrant_id),
                            {"rating": DEFAULT_RATING},
                        )["rating"]
                    )
                    for entrant_id in entrants
                }
                running = dict(frozen)
                deltas: dict[str, list[float]] = {entrant_id: [] for entrant_id in entrants}
                operation_outcomes: dict[str, list[float]] = {
                    entrant_id: [] for entrant_id in entrants
                }
                for step in range(1, expected_step_count + 1):
                    paired_rows = step_rows[step]
                    if len(paired_rows) != 2:
                        raise StorageError("SQLite 全局评分操作的对手配对已损坏")
                    player_a_rows = [row for row in paired_rows if row["is_player_a"] == 1]
                    player_b_rows = [row for row in paired_rows if row["is_player_a"] == 0]
                    if len(player_a_rows) != 1 or len(player_b_rows) != 1:
                        raise StorageError("SQLite 全局评分操作的玩家顺序已损坏")
                    player_a_row = player_a_rows[0]
                    player_b_row = player_b_rows[0]
                    player_a_id = player_a_row["entrant_id"]
                    player_b_id = player_b_row["entrant_id"]
                    if (
                        player_a_id == player_b_id
                        or player_a_id not in entrants
                        or player_b_id not in entrants
                        or player_a_row["opponent_entrant_id"] != player_b_id
                        or player_b_row["opponent_entrant_id"] != player_a_id
                    ):
                        raise StorageError("SQLite 全局评分操作的对手配对已损坏")
                    try:
                        player_a_outcome = self._finite_database_float(player_a_row["outcome"])
                        player_b_outcome = self._finite_database_float(player_b_row["outcome"])
                        player_a_before = self._finite_database_float(player_a_row["rating_before"])
                        player_b_before = self._finite_database_float(player_b_row["rating_before"])
                        player_a_after = self._finite_database_float(player_a_row["rating_after"])
                        player_b_after = self._finite_database_float(player_b_row["rating_after"])
                    except (TypeError, ValueError) as exc:
                        raise StorageError("SQLite 全局评分操作的 ELO 数值已损坏") from exc
                    if (
                        player_a_outcome not in (0.0, 0.5, 1.0)
                        or player_b_outcome != 1.0 - player_a_outcome
                        or player_a_before != running[player_a_id]
                        or player_b_before != running[player_b_id]
                    ):
                        raise StorageError("SQLite 全局评分操作的 ELO 链已损坏")

                    player_a_delta = K_FACTOR * (
                        player_a_outcome - expected_score(frozen[player_a_id], frozen[player_b_id])
                    )
                    if operation_kind == "match":
                        expected_player_a_after, expected_player_b_after = update_ratings(
                            running[player_a_id],
                            running[player_b_id],
                            player_a_outcome,
                        )
                    elif operation_kind == "series":
                        expected_player_a_after = running[player_a_id] + player_a_delta
                        expected_player_b_after = running[player_b_id] - player_a_delta
                    else:
                        deltas[player_a_id].append(player_a_delta)
                        deltas[player_b_id].append(-player_a_delta)
                        expected_player_a_after = frozen[player_a_id] + math.fsum(
                            deltas[player_a_id]
                        )
                        expected_player_b_after = frozen[player_b_id] + math.fsum(
                            deltas[player_b_id]
                        )
                    if (
                        player_a_after != expected_player_a_after
                        or player_b_after != expected_player_b_after
                    ):
                        raise StorageError("SQLite 全局评分操作的 ELO 算法重放失败")
                    # Continue from the canonical recomputation, never from a
                    # tolerance-accepted database value.  Otherwise small changes
                    # can be propagated through history and into the leaderboard.
                    running[player_a_id] = expected_player_a_after
                    running[player_b_id] = expected_player_b_after
                    operation_outcomes[player_a_id].append(player_a_outcome)
                    operation_outcomes[player_b_id].append(player_b_outcome)

                for entrant_id, outcomes in operation_outcomes.items():
                    key = (rating_scope, game_key, entrant_id)
                    state = replay.setdefault(
                        key,
                        {
                            "rating": DEFAULT_RATING,
                            "games_played": 0,
                            "wins": 0,
                            "draws": 0,
                            "losses": 0,
                            "updated_at": None,
                        },
                    )
                    state["rating"] = running[entrant_id]
                    state["games_played"] = int(state["games_played"]) + len(outcomes)
                    state["wins"] = int(state["wins"]) + outcomes.count(1.0)
                    state["draws"] = int(state["draws"]) + outcomes.count(0.5)
                    state["losses"] = int(state["losses"]) + outcomes.count(0.0)
                    previous_updated_at = state["updated_at"]
                    if previous_updated_at is None or finished_at > previous_updated_at:
                        state["updated_at"] = finished_at
            replayed_history_rows += len(history_rows)

        total_history_rows = connection.execute("SELECT count(*) FROM rating_history").fetchone()[0]
        if replayed_history_rows != total_history_rows:
            raise StorageError("SQLite ELO 历史没有被全局评分操作账本完整覆盖")

        rating_rows = connection.execute(
            """
            SELECT rating_scope, game, entrant_id, rating, games_played,
                   wins, draws, losses, updated_at
            FROM ratings
            """
        ).fetchall()
        materialized = {
            (row["rating_scope"], row["game"], row["entrant_id"]): row for row in rating_rows
        }
        if len(materialized) != len(rating_rows) or set(materialized) != set(replay):
            raise StorageError("SQLite 当前 ELO 排行榜已损坏：与全局评分操作账本不一致")
        for key, state in replay.items():
            row = materialized[key]
            try:
                rating = self._finite_database_float(row["rating"])
            except (TypeError, ValueError) as exc:
                raise StorageError("SQLite 当前 ELO 排行榜已损坏：数值无效") from exc
            updated_at = state["updated_at"]
            if (
                rating != state["rating"]
                or row["games_played"] != state["games_played"]
                or row["wins"] != state["wins"]
                or row["draws"] != state["draws"]
                or row["losses"] != state["losses"]
                or not isinstance(updated_at, datetime)
                or not self._timestamp_matches(row["updated_at"], updated_at)
            ):
                raise StorageError("SQLite 当前 ELO 排行榜已损坏：与全局评分操作账本不一致")
        return True

    def _verify_current_tournament_ratings_if_latest(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        expected_aggregates: list[_TournamentAggregate],
    ) -> bool:
        """Verify the materialized leaderboard by replaying every v7 operation."""

        del tournament, expected_aggregates
        return self._verify_global_rating_operation_replay(connection)

    def _verify_existing_tournament(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> bool:
        expected_rated = rating_source == "engine" and tournament.source == "local_engine"
        expected_policy = "elo_tournament_batch_v1" if rated else "unrated"
        if rated != expected_rated or rating_policy != expected_policy:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的来源或计分状态已损坏"
            )
        tournament_row = connection.execute(
            """
            SELECT tournament_id, schema_version, format, pairing_policy,
                   seed_policy, game, seed, players_json, points_json,
                   pairing_count, rating_policy, k_factor, started_at,
                   finished_at, archive_source, rating_source, rated,
                   tournament_json
            FROM tournament_archives
            WHERE tournament_id = ?
            """,
            (tournament.tournament_id,),
        ).fetchone()
        if tournament_row is None:
            raise StorageError(f"数据库中 tournament_id {tournament.tournament_id!r} 已丢失")
        try:
            stored_json = self._semantic_tournament_json(tournament_row["tournament_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的档案 JSON 已损坏"
            ) from exc
        if stored_json != _canonical_json(tournament.model_dump(mode="json")):
            raise TournamentIdCollisionError(
                f"tournament_id {tournament.tournament_id!r} 已对应另一份循环赛档案"
            )
        self._verify_tournament_metadata(
            tournament_row,
            tournament,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )

        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False) for descriptor in tournament.players
        )
        standings = {standing.entrant_id: standing for standing in tournament.standings}
        entrant_rows = connection.execute(
            """
            SELECT position, entrant_id, display_name, descriptor_json, points,
                   series_played, series_wins, series_draws, series_losses,
                   games_played, wins, draws, losses, technical_losses
            FROM tournament_entrants
            WHERE tournament_id = ?
            ORDER BY position
            """,
            (tournament.tournament_id,),
        ).fetchall()
        if len(entrant_rows) != len(entrants) or [row["position"] for row in entrant_rows] != list(
            range(len(entrants))
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的参赛者索引已损坏"
            )
        payload = tournament.model_dump(mode="json")
        for row, entrant, descriptor in zip(
            entrant_rows,
            entrants,
            payload["players"],
        ):
            standing = standings[entrant.entrant_id]
            try:
                stored_descriptor = self._semantic_descriptor_json(
                    row["descriptor_json"], legacy=False
                )
                stored_points = self._finite_database_float(row["points"])
            except (StorageError, TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的参赛者索引已损坏"
                ) from exc
            if (
                row["entrant_id"] != entrant.entrant_id
                or row["display_name"] != entrant.display_name
                or stored_descriptor != _canonical_json(descriptor)
                or stored_points != standing.points
                or row["series_played"] != standing.series_played
                or row["series_wins"] != standing.series_wins
                or row["series_draws"] != standing.series_draws
                or row["series_losses"] != standing.series_losses
                or row["games_played"] != standing.games_played
                or row["wins"] != standing.wins
                or row["draws"] != standing.draws
                or row["losses"] != standing.losses
                or row["technical_losses"] != standing.technical_losses
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的参赛者索引已损坏"
                )

        if rated:
            for entrant in entrants:
                identity_row = connection.execute(
                    """
                    SELECT display_name, identity_json, created_at, updated_at
                    FROM entrants WHERE entrant_id = ?
                    """,
                    (entrant.entrant_id,),
                ).fetchone()
                if identity_row is None:
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的全局选手身份已损坏"
                    )
                try:
                    created_at = datetime.fromisoformat(identity_row["created_at"])
                    updated_at = datetime.fromisoformat(identity_row["updated_at"])
                except (TypeError, ValueError) as exc:
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的全局选手身份已损坏"
                    ) from exc
                if (
                    not isinstance(identity_row["display_name"], str)
                    or not identity_row["display_name"]
                    or identity_row["identity_json"] != entrant.identity_json
                    or created_at.utcoffset() is None
                    or updated_at.utcoffset() is None
                ):
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的全局选手身份已损坏"
                    )

        pairing_rows = connection.execute(
            """
            SELECT pairing_number, series_id, entrant_a_id, entrant_b_id
            FROM tournament_pairings
            WHERE tournament_id = ?
            ORDER BY pairing_number
            """,
            (tournament.tournament_id,),
        ).fetchall()
        if len(pairing_rows) != len(tournament.pairings):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的配对索引已损坏"
            )
        for row, pairing in zip(pairing_rows, tournament.pairings):
            player_a = entrants[pairing.player_indices[0]]
            player_b = entrants[pairing.player_indices[1]]
            if (
                row["pairing_number"] != pairing.pairing_number
                or row["series_id"] != pairing.series.series_id
                or row["entrant_a_id"] != player_a.entrant_id
                or row["entrant_b_id"] != player_b.entrant_id
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament.tournament_id!r} 的配对索引已损坏"
                )
            self._verify_tournament_series_structure(
                connection,
                pairing.series,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )

        replay_complete = self._verify_tournament_ratings(
            connection,
            tournament,
            entrants,
            rated=rated,
        )
        self._verify_top_level_rating_operation(
            connection,
            rated=rated,
            tournament_id=tournament.tournament_id,
        )
        return replay_complete

    def _load_verified_tournament(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
    ) -> tuple[TournamentArchive, bool, bool | None] | None:
        """Load one formal tournament and deeply verify all relational state."""

        row = connection.execute(
            """
            SELECT tournament_json, rating_source, rated, rating_policy
            FROM tournament_archives
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            tournament = TournamentArchive.model_validate_json(row["tournament_json"])
            tournament, _ = self._validate_tournament(tournament)
        except (TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏"
            ) from exc
        if tournament.tournament_id != tournament_id:
            raise StorageError(f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏")
        rated = bool(row["rated"])
        replay_complete = self._verify_existing_tournament(
            connection,
            tournament,
            rating_source=row["rating_source"],
            rated=rated,
            rating_policy=row["rating_policy"],
        )
        return tournament, rated, replay_complete if rated else None

    @staticmethod
    def _database_epoch(connection: sqlite3.Connection) -> int:
        value = connection.execute(
            "SELECT CAST(strftime('%s', 'now') AS INTEGER) AS epoch"
        ).fetchone()["epoch"]
        if isinstance(value, bool) or not isinstance(value, int):
            raise StorageError("SQLite 无法提供 runner lease 时钟")
        return value

    def _require_active_tournament_runner(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
        lease: TournamentRunnerLease | None,
        *,
        renew_seconds: int | None = None,
    ) -> TournamentRunnerLease:
        if lease is None:
            raise TournamentRunnerLeaseLostError("循环赛 checkpoint 写入需要有效的 runner lease")
        digest = _validate_runner_lease_handle(lease, tournament_id)
        state = self._load_tournament_runner_lease(connection, tournament_id)
        now = self._database_epoch(connection)
        if (
            state is None
            or state.token_digest is None
            or state.generation != lease.generation
            or state.token_digest != digest
            or state.expires_at_epoch is None
            or state.expires_at_epoch <= now
        ):
            raise TournamentRunnerLeaseLostError(
                "循环赛 runner lease 已过期、释放或被其他执行者接管"
            )

        if renew_seconds is None:
            return TournamentRunnerLease(
                tournament_id=tournament_id,
                generation=state.generation,
                token=lease.token,
                acquired_at_epoch=state.acquired_at_epoch,
                renewed_at_epoch=state.renewed_at_epoch,
                expires_at_epoch=state.expires_at_epoch,
            )

        duration = _validate_runner_lease_seconds(renew_seconds)
        renewed_at = max(now, state.renewed_at_epoch)
        expires_at = max(now + duration, renewed_at + 1)
        updated = connection.execute(
            """
            UPDATE tournament_runner_leases
            SET renewed_at_epoch = ?, expires_at_epoch = ?
            WHERE tournament_id = ? AND generation = ? AND token_digest = ?
            """,
            (
                renewed_at,
                expires_at,
                tournament_id,
                lease.generation,
                digest,
            ),
        )
        if updated.rowcount != 1:
            raise TournamentRunnerLeaseLostError("循环赛 runner lease 在续租时发生并发变化")
        return TournamentRunnerLease(
            tournament_id=tournament_id,
            generation=state.generation,
            token=lease.token,
            acquired_at_epoch=state.acquired_at_epoch,
            renewed_at_epoch=renewed_at,
            expires_at_epoch=expires_at,
        )

    def claim_tournament_runner(
        self,
        tournament_id: str,
        *,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> TournamentRunnerClaim:
        """Atomically reload and claim one in-progress tournament checkpoint."""

        if not isinstance(tournament_id, str) or not tournament_id.strip():
            raise ValueError("tournament_id 必须是非空字符串")
        duration = _validate_runner_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            loaded = self._load_tournament_checkpoint(connection, tournament_id)
            if loaded is None:
                raise StorageError(f"循环赛 checkpoint {tournament_id!r} 不存在")
            checkpoint, status = loaded
            if status != "in_progress":
                raise StorageError(f"循环赛 checkpoint {tournament_id!r} 已封存")

            state = self._load_tournament_runner_lease(connection, tournament_id)
            now = self._database_epoch(connection)
            if (
                state is not None
                and state.token_digest is not None
                and state.expires_at_epoch is not None
                and state.expires_at_epoch > now
            ):
                raise TournamentRunnerLeaseBusyError(
                    "循环赛 checkpoint 正由另一个执行者运行；请稍后重试"
                )

            if state is not None:
                self._close_stale_provider_attempts(
                    connection,
                    tournament_id,
                    state.generation,
                    finished_at_epoch=now,
                )
            if state is not None and state.generation >= SQLITE_INT_MAX:
                raise StorageError("循环赛 runner lease generation 已达到 SQLite 整数上限")
            generation = 1 if state is None else state.generation + 1
            token = secrets.token_hex(32)
            digest = _runner_lease_token_digest(token)
            expires_at = now + duration
            if state is None:
                connection.execute(
                    """
                    INSERT INTO tournament_runner_leases (
                        tournament_id, generation, token_digest,
                        acquired_at_epoch, renewed_at_epoch, expires_at_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (tournament_id, generation, digest, now, now, expires_at),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE tournament_runner_leases
                    SET generation = ?, token_digest = ?, acquired_at_epoch = ?,
                        renewed_at_epoch = ?, expires_at_epoch = ?
                    WHERE tournament_id = ? AND generation = ?
                    """,
                    (
                        generation,
                        digest,
                        now,
                        now,
                        expires_at,
                        tournament_id,
                        state.generation,
                    ),
                )
                if updated.rowcount != 1:
                    raise TournamentRunnerLeaseBusyError("循环赛 runner lease 在领取时发生并发变化")
            connection.commit()
            return TournamentRunnerClaim(
                checkpoint=checkpoint,
                lease=TournamentRunnerLease(
                    tournament_id=tournament_id,
                    generation=generation,
                    token=token,
                    acquired_at_epoch=now,
                    renewed_at_epoch=now,
                    expires_at_epoch=expires_at,
                ),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def renew_tournament_runner(
        self,
        lease: TournamentRunnerLease,
        *,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> TournamentRunnerLease:
        """Extend one active lease without reviving an expired generation."""

        if not isinstance(lease, TournamentRunnerLease):
            raise TypeError("必须提供 TournamentRunnerLease")
        duration = _validate_runner_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            status_row = connection.execute(
                "SELECT status FROM tournament_checkpoints WHERE tournament_id = ?",
                (lease.tournament_id,),
            ).fetchone()
            if status_row is None or status_row["status"] != "in_progress":
                raise TournamentRunnerLeaseLostError(
                    "循环赛 runner lease 对应的 checkpoint 已不存在或已封存"
                )
            renewed = self._require_active_tournament_runner(
                connection,
                lease.tournament_id,
                lease,
                renew_seconds=duration,
            )
            connection.commit()
            return renewed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def release_tournament_runner(self, lease: TournamentRunnerLease) -> bool:
        """Release only the matching fencing generation; stale releases are no-ops."""

        if not isinstance(lease, TournamentRunnerLease):
            raise TypeError("必须提供 TournamentRunnerLease")
        digest = _validate_runner_lease_handle(lease, lease.tournament_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE tournament_runner_leases
                SET token_digest = NULL, acquired_at_epoch = NULL,
                    renewed_at_epoch = NULL, expires_at_epoch = NULL
                WHERE tournament_id = ? AND generation = ? AND token_digest = ?
                """,
                (lease.tournament_id, lease.generation, digest),
            )
            connection.commit()
            return updated.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def expire_tournament_runner_leases(self) -> int:
        """Clear expired owners while preserving monotonic fencing generations."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            now = self._database_epoch(connection)
            updated = connection.execute(
                """
                UPDATE tournament_runner_leases
                SET token_digest = NULL, acquired_at_epoch = NULL,
                    renewed_at_epoch = NULL, expires_at_epoch = NULL
                WHERE token_digest IS NOT NULL AND expires_at_epoch <= ?
                """,
                (now,),
            )
            connection.commit()
            return updated.rowcount
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _load_tournament_checkpoint(
        self,
        connection: sqlite3.Connection,
        tournament_id: str,
    ) -> tuple[TournamentCheckpoint, str] | None:
        row = connection.execute(
            """
            SELECT tournament_id, schema_version, source, format,
                   pairing_policy, seed_policy, game, seed, players_json,
                   game_config_json, schedule_json, max_attempts,
                   pairing_count, created_at, updated_at, status,
                   finalized_at, final_tournament_id, config_json
            FROM tournament_checkpoints
            WHERE tournament_id = ?
            """,
            (tournament_id,),
        ).fetchone()
        if row is None:
            return None

        series_rows = connection.execute(
            """
            SELECT pairing_number, series_id, match_1_id, match_2_id,
                   completed_at, series_json
            FROM tournament_checkpoint_series
            WHERE tournament_id = ?
            ORDER BY pairing_number
            """,
            (tournament_id,),
        ).fetchall()
        try:
            semantic_config = self._semantic_checkpoint_config_json(row["config_json"])
            config_payload = json.loads(row["config_json"])
            completed_series: list[SeriesArchive] = []
            for expected_pairing_number, series_row in enumerate(series_rows, start=1):
                if series_row["pairing_number"] != expected_pairing_number:
                    raise StorageError("已完成组编号不是连续前缀")
                series = SeriesArchive.model_validate_json(series_row["series_json"])
                series, _ = self._validate_series(series)
                if (
                    self._semantic_series_json(series_row["series_json"])
                    != _canonical_json(series.model_dump(mode="json"))
                    or series_row["series_id"] != series.series_id
                    or series_row["match_1_id"] != series.legs[0].match_id
                    or series_row["match_2_id"] != series.legs[1].match_id
                    or not self._timestamp_matches(series_row["completed_at"], series.finished_at)
                ):
                    raise StorageError("已完成双局赛索引与档案不一致")
                completed_series.append(series)

            config_payload["completed_series"] = [
                series.model_dump(mode="json") for series in completed_series
            ]
            config_payload["updated_at"] = row["updated_at"]
            checkpoint = TournamentCheckpoint.model_validate(config_payload)
            checkpoint, _ = self._validate_checkpoint(checkpoint)

            payload = checkpoint.model_dump(mode="json")
            stored_players = self._semantic_players_json(row["players_json"], legacy=False)
            stored_game_config = self._semantic_json_column(row["game_config_json"])
            stored_schedule = self._semantic_json_column(row["schedule_json"])
            expected_config = _canonical_json(self._checkpoint_config_payload(checkpoint))
        except (KeyError, TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 已损坏"
            ) from exc

        if (
            semantic_config != expected_config
            or row["tournament_id"] != checkpoint.tournament_id
            or row["schema_version"] != checkpoint.schema_version
            or row["source"] != checkpoint.source
            or row["format"] != checkpoint.format
            or row["pairing_policy"] != checkpoint.pairing_policy
            or row["seed_policy"] != checkpoint.seed_policy
            or row["game"] != checkpoint.game
            or row["seed"] != checkpoint.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_game_config != _canonical_json(payload["game_config"])
            or stored_schedule != _canonical_json(payload["schedule"])
            or row["max_attempts"] != checkpoint.max_attempts
            or row["pairing_count"] != len(checkpoint.schedule)
            or not self._timestamp_matches(row["created_at"], checkpoint.created_at)
            or not self._timestamp_matches(row["updated_at"], checkpoint.updated_at)
            or len(completed_series) > row["pairing_count"]
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 元数据已损坏"
            )

        status = row["status"]
        if status == "in_progress":
            if row["finalized_at"] is not None or row["final_tournament_id"] is not None:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                )
        elif status == "finalized":
            if (
                row["final_tournament_id"] != tournament_id
                or not checkpoint.is_complete
                or not isinstance(row["finalized_at"], str)
            ):
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                )
            try:
                finalized_at = datetime.fromisoformat(row["finalized_at"])
            except ValueError as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                ) from exc
            if finalized_at.utcoffset() is None or finalized_at.astimezone(
                UTC
            ) < checkpoint.updated_at.astimezone(UTC):
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏"
                )
            final_row = connection.execute(
                """
                SELECT tournament_json FROM tournament_archives
                WHERE tournament_id = ?
                """,
                (tournament_id,),
            ).fetchone()
            if final_row is None:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已丢失"
                )
            expected_tournament = tournament_from_series(
                checkpoint.players,
                checkpoint.completed_series,
                seed=checkpoint.seed,
                tournament_id=checkpoint.tournament_id,
                judge_panel=checkpoint.judge_panel,
            )
            try:
                stored_tournament = self._semantic_tournament_json(final_row["tournament_json"])
            except StorageError as exc:
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏"
                ) from exc
            if stored_tournament != _canonical_json(expected_tournament.model_dump(mode="json")):
                raise StorageError(
                    f"数据库中 tournament_id {tournament_id!r} 的正式循环赛档案已损坏"
                )
        else:
            raise StorageError(f"数据库中 tournament_id {tournament_id!r} 的 checkpoint 状态已损坏")
        self._verify_checkpoint_entrant_bindings(connection, checkpoint)
        return checkpoint, status

    @staticmethod
    def _insert_empty_tournament_checkpoint_in_transaction(
        connection: sqlite3.Connection,
        checkpoint: TournamentCheckpoint,
        *,
        payload: dict,
        config_json: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO tournament_checkpoints (
                tournament_id, schema_version, source, format,
                pairing_policy, seed_policy, game, seed, players_json,
                game_config_json, schedule_json, max_attempts,
                pairing_count, created_at, updated_at, status,
                finalized_at, final_tournament_id, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                checkpoint.tournament_id,
                checkpoint.schema_version,
                checkpoint.source,
                checkpoint.format,
                checkpoint.pairing_policy,
                checkpoint.seed_policy,
                checkpoint.game,
                checkpoint.seed,
                _canonical_json(payload["players"]),
                _canonical_json(payload["game_config"]),
                _canonical_json(payload["schedule"]),
                checkpoint.max_attempts,
                len(checkpoint.schedule),
                checkpoint.created_at.astimezone(UTC).isoformat(),
                checkpoint.updated_at.astimezone(UTC).isoformat(),
                "in_progress",
                config_json,
            ),
        )

    def create_tournament_checkpoint_with_provider_budget(
        self,
        checkpoint: TournamentCheckpoint,
        budget_id: str,
        limits: BudgetLimits,
        policy: ProviderBudgetPolicy,
    ) -> tuple[TournamentCheckpointSaveResult, ProviderBudgetSnapshot]:
        """Atomically create an empty checkpoint and its frozen Provider budget."""

        checkpoint, _ = self._validate_checkpoint(checkpoint)
        if checkpoint.completed_series:
            raise StorageError("new tournament checkpoint must have empty progress")
        budget_id = _validate_usage_ledger_id(budget_id, "budget_id")
        limits, policy = _validate_durable_budget_definition(limits, policy)
        payload = checkpoint.model_dump(mode="json")
        config_json = _canonical_json(self._checkpoint_config_payload(checkpoint))
        pairing_count = len(checkpoint.schedule)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_checkpoint_entrant_bindings(connection, checkpoint)
            loaded = self._load_tournament_checkpoint(
                connection,
                checkpoint.tournament_id,
            )
            if loaded is None:
                if connection.execute(
                    "SELECT 1 FROM tournament_archives WHERE tournament_id = ?",
                    (checkpoint.tournament_id,),
                ).fetchone():
                    raise TournamentCheckpointCollisionError(
                        f"tournament_id {checkpoint.tournament_id!r} 已有正式循环赛档案"
                    )
                if self._provider_budget_snapshot_in_transaction(connection, budget_id):
                    raise ProviderBudgetCollisionError("budget_id is already present")
                self._insert_empty_tournament_checkpoint_in_transaction(
                    connection,
                    checkpoint,
                    payload=payload,
                    config_json=config_json,
                )
                budget = self._insert_provider_budget_in_transaction(
                    connection,
                    budget_id,
                    limits,
                    policy,
                    tournament_id=checkpoint.tournament_id,
                )
                inserted = True
            else:
                stored, status = loaded
                if (
                    status != "in_progress"
                    or stored.completed_series
                    or _canonical_json(self._checkpoint_config_payload(stored)) != config_json
                ):
                    raise TournamentCheckpointCollisionError(
                        f"tournament_id {checkpoint.tournament_id!r} 已对应另一份 checkpoint"
                    )
                budget = self._provider_budget_snapshot_in_transaction(
                    connection,
                    budget_id,
                )
                if (
                    budget is None
                    or budget.tournament_id != checkpoint.tournament_id
                    or budget.limits != limits
                    or budget.policy != policy
                ):
                    raise ProviderBudgetCollisionError(
                        "checkpoint exists without the identical frozen Provider budget"
                    )
                inserted = False
            connection.commit()
            return (
                TournamentCheckpointSaveResult(
                    inserted=inserted,
                    completed_pairing_count=0,
                    pairing_count=pairing_count,
                ),
                budget,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_tournament_checkpoint(
        self,
        checkpoint: TournamentCheckpoint,
        *,
        lease: TournamentRunnerLease | None = None,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> TournamentCheckpointSaveResult:
        """Create an empty checkpoint or append one series under an active lease."""

        checkpoint, _ = self._validate_checkpoint(checkpoint)
        payload = checkpoint.model_dump(mode="json")
        config_json = _canonical_json(self._checkpoint_config_payload(checkpoint))
        pairing_count = len(checkpoint.schedule)
        completed_count = len(checkpoint.completed_series)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_checkpoint_entrant_bindings(connection, checkpoint)
            loaded = self._load_tournament_checkpoint(
                connection,
                checkpoint.tournament_id,
            )
            if loaded is None:
                if completed_count:
                    raise StorageError("新循环赛 checkpoint 必须在第一组开始前以空进度创建")
                if lease is not None:
                    raise ValueError("新循环赛 checkpoint 必须先创建，再领取 runner lease")
                if connection.execute(
                    "SELECT 1 FROM tournament_archives WHERE tournament_id = ?",
                    (checkpoint.tournament_id,),
                ).fetchone():
                    raise TournamentCheckpointCollisionError(
                        f"tournament_id {checkpoint.tournament_id!r} 已有正式循环赛档案"
                    )
                self._insert_empty_tournament_checkpoint_in_transaction(
                    connection,
                    checkpoint,
                    payload=payload,
                    config_json=config_json,
                )
                connection.commit()
                return TournamentCheckpointSaveResult(
                    inserted=True,
                    completed_pairing_count=0,
                    pairing_count=pairing_count,
                )

            stored, status = loaded
            if status == "in_progress":
                self._require_active_tournament_runner(
                    connection,
                    checkpoint.tournament_id,
                    lease,
                    renew_seconds=lease_seconds,
                )
            stored_config_json = _canonical_json(self._checkpoint_config_payload(stored))
            if stored_config_json != config_json:
                raise TournamentCheckpointCollisionError(
                    f"tournament_id {checkpoint.tournament_id!r} 已对应另一份 checkpoint 配置"
                )
            stored_series_json = tuple(
                _canonical_json(series.model_dump(mode="json"))
                for series in stored.completed_series
            )
            incoming_series_json = tuple(
                _canonical_json(series.model_dump(mode="json"))
                for series in checkpoint.completed_series
            )
            stored_count = len(stored.completed_series)
            if incoming_series_json == stored_series_json:
                connection.commit()
                return TournamentCheckpointSaveResult(
                    inserted=False,
                    completed_pairing_count=stored_count,
                    pairing_count=pairing_count,
                )
            if status != "in_progress":
                raise TournamentCheckpointCollisionError(
                    f"tournament_id {checkpoint.tournament_id!r} 的 checkpoint 已封存"
                )
            if (
                completed_count != stored_count + 1
                or incoming_series_json[:stored_count] != stored_series_json
            ):
                raise TournamentCheckpointCollisionError(
                    "循环赛 checkpoint 只能按赛程连续追加恰好一组双局赛"
                )

            series = checkpoint.completed_series[-1]
            if (
                connection.execute(
                    "SELECT 1 FROM series_archives WHERE series_id = ?",
                    (series.series_id,),
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM tournament_checkpoint_series WHERE series_id = ?",
                    (series.series_id,),
                ).fetchone()
            ):
                raise SeriesIdCollisionError(f"series_id {series.series_id!r} 已存档")
            match_ids = (series.legs[0].match_id, series.legs[1].match_id)
            if (
                connection.execute(
                    "SELECT 1 FROM matches WHERE match_id IN (?, ?)",
                    match_ids,
                ).fetchone()
                or connection.execute(
                    """
                SELECT 1 FROM tournament_checkpoint_series
                WHERE match_1_id IN (?, ?)
                   OR match_2_id IN (?, ?)
                """,
                    (*match_ids, *match_ids),
                ).fetchone()
            ):
                raise MatchIdCollisionError("循环赛 checkpoint 的 match_id 已存档")

            connection.execute(
                """
                INSERT INTO tournament_checkpoint_series (
                    tournament_id, pairing_number, series_id, match_1_id,
                    match_2_id, completed_at, series_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.tournament_id,
                    completed_count,
                    series.series_id,
                    match_ids[0],
                    match_ids[1],
                    series.finished_at.astimezone(UTC).isoformat(),
                    incoming_series_json[-1],
                ),
            )
            updated = connection.execute(
                """
                UPDATE tournament_checkpoints
                SET updated_at = ?
                WHERE tournament_id = ? AND status = 'in_progress'
                """,
                (
                    checkpoint.updated_at.astimezone(UTC).isoformat(),
                    checkpoint.tournament_id,
                ),
            )
            if updated.rowcount != 1:
                raise StorageError("循环赛 checkpoint 状态发生并发变化")
            connection.commit()
            return TournamentCheckpointSaveResult(
                inserted=True,
                completed_pairing_count=completed_count,
                pairing_count=pairing_count,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_tournament_checkpoint(
        self,
        tournament_id: str,
    ) -> TournamentCheckpoint | None:
        """Load and deeply validate one resumable tournament checkpoint."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                loaded = self._load_tournament_checkpoint(connection, tournament_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return None if loaded is None else loaded[0]

    def finalize_tournament_checkpoint(
        self,
        tournament_id: str,
        *,
        lease: TournamentRunnerLease | None = None,
    ) -> TournamentSaveResult:
        """Atomically promote a complete checkpoint and apply tournament ELO once."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            loaded = self._load_tournament_checkpoint(connection, tournament_id)
            if loaded is None:
                raise StorageError(f"循环赛 checkpoint {tournament_id!r} 不存在")
            checkpoint, status = loaded
            if not checkpoint.is_complete:
                raise StorageError("循环赛 checkpoint 尚未完成，不能封存")
            if status == "in_progress":
                self._require_active_tournament_runner(
                    connection,
                    tournament_id,
                    lease,
                    renew_seconds=DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
                )
            self._finalize_tournament_provider_budgets(connection, tournament_id)
            tournament = tournament_from_series(
                checkpoint.players,
                checkpoint.completed_series,
                seed=checkpoint.seed,
                tournament_id=checkpoint.tournament_id,
                judge_panel=checkpoint.judge_panel,
            )
            if (
                status == "in_progress"
                and connection.execute(
                    "SELECT 1 FROM tournament_archives WHERE tournament_id = ?",
                    (tournament_id,),
                ).fetchone()
            ):
                raise StorageError("进行中的 checkpoint 已存在同 ID 正式循环赛档案")

            result = self._save_tournament_in_transaction(
                connection,
                tournament,
                rating_source="engine",
                checkpoint_owner_id=tournament_id,
            )
            if status == "in_progress":
                if not result.inserted:
                    raise StorageError("进行中的 checkpoint 未能创建正式循环赛档案")
                finalized_at = max(
                    datetime.now(UTC),
                    checkpoint.updated_at.astimezone(UTC),
                )
                updated = connection.execute(
                    """
                    UPDATE tournament_checkpoints
                    SET status = 'finalized', finalized_at = ?,
                        final_tournament_id = ?
                    WHERE tournament_id = ? AND status = 'in_progress'
                    """,
                    (
                        finalized_at.isoformat(),
                        tournament_id,
                        tournament_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise StorageError("循环赛 checkpoint 状态发生并发变化")
                digest = _validate_runner_lease_handle(lease, tournament_id)
                deleted = connection.execute(
                    """
                    DELETE FROM tournament_runner_leases
                    WHERE tournament_id = ? AND generation = ? AND token_digest = ?
                    """,
                    (tournament_id, lease.generation, digest),
                )
                if deleted.rowcount != 1:
                    raise TournamentRunnerLeaseLostError("循环赛 runner lease 在封存时发生并发变化")
            elif result.inserted:
                raise StorageError("已封存 checkpoint 的正式循环赛档案状态已损坏")
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_tournament(
        self,
        tournament: TournamentArchive,
        *,
        rating_source: RatingSource = "imported",
    ) -> TournamentSaveResult:
        """Atomically persist and batch-rate one complete round-robin tournament."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._save_tournament_in_transaction(
                connection,
                tournament,
                rating_source=rating_source,
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _save_tournament_in_transaction(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        *,
        rating_source: RatingSource,
        checkpoint_owner_id: str | None = None,
    ) -> TournamentSaveResult:
        """Persist a complete tournament using the caller's active write transaction."""

        if not connection.in_transaction:
            raise StorageError("保存循环赛需要调用方先开启 SQLite 写事务")
        rating_source = self._validate_rating_source(rating_source)
        tournament, entrants = self._validate_tournament(tournament)
        trusted_engine = (
            rating_source == "engine"
            and tournament.schema_version == TOURNAMENT_SCHEMA_VERSION
            and tournament.source == "local_engine"
        )
        rated = trusted_engine
        rating_policy = "elo_tournament_batch_v1" if rated else "unrated"
        tournament_payload = tournament.model_dump(mode="json")
        tournament_json = _canonical_json(tournament_payload)
        pairing_count = len(tournament.pairings)
        match_count = pairing_count * 2

        def persist() -> TournamentSaveResult:
            checkpoint_row = connection.execute(
                """
                SELECT status FROM tournament_checkpoints
                WHERE tournament_id = ?
                """,
                (tournament.tournament_id,),
            ).fetchone()
            if (
                checkpoint_row is not None
                and checkpoint_row["status"] == "in_progress"
                and checkpoint_owner_id != tournament.tournament_id
            ):
                raise TournamentIdCollisionError(
                    f"tournament_id {tournament.tournament_id!r} 已由进行中的循环赛 checkpoint 保留"
                )
            existing = connection.execute(
                """
                SELECT tournament_json, archive_source, rating_source, rated,
                       rating_policy
                FROM tournament_archives
                WHERE tournament_id = ?
                """,
                (tournament.tournament_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = self._semantic_tournament_json(existing["tournament_json"])
                except StorageError as exc:
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != tournament_json:
                    raise TournamentIdCollisionError(
                        f"tournament_id {tournament.tournament_id!r} 已对应另一份循环赛档案"
                    )
                stored_rated = bool(existing["rated"])
                expected_stored_rated = (
                    existing["rating_source"] == "engine"
                    and existing["archive_source"] == "local_engine"
                )
                expected_stored_policy = "elo_tournament_batch_v1" if stored_rated else "unrated"
                if (
                    stored_rated != expected_stored_rated
                    or existing["rating_policy"] != expected_stored_policy
                ):
                    raise StorageError(
                        f"数据库中 tournament_id {tournament.tournament_id!r} "
                        "的计分来源或策略已损坏"
                    )
                read_only_downgrade = (
                    existing["rating_source"] == "engine" and rating_source == "imported"
                )
                exact_policy_match = (
                    existing["rating_source"] == rating_source
                    and stored_rated == rated
                    and existing["rating_policy"] == rating_policy
                )
                if existing["archive_source"] != tournament.source or not (
                    read_only_downgrade or exact_policy_match
                ):
                    raise TournamentIdCollisionError(
                        f"tournament_id {tournament.tournament_id!r} "
                        "已以不同来源或计分策略存档，不能通过幂等重存升级"
                    )
                self._verify_existing_tournament(
                    connection,
                    tournament,
                    rating_source=existing["rating_source"],
                    rated=stored_rated,
                    rating_policy=existing["rating_policy"],
                )
                return TournamentSaveResult(
                    inserted=False,
                    rated=stored_rated,
                    pairing_count=pairing_count,
                    match_count=match_count,
                )

            for pairing in tournament.pairings:
                series = pairing.series
                checkpoint_series_owner = connection.execute(
                    """
                    SELECT tcs.tournament_id
                    FROM tournament_checkpoint_series AS tcs
                    JOIN tournament_checkpoints AS tc
                      ON tc.tournament_id = tcs.tournament_id
                    WHERE tc.status = 'in_progress' AND tcs.series_id = ?
                    """,
                    (series.series_id,),
                ).fetchone()
                if (
                    checkpoint_series_owner is not None
                    and checkpoint_series_owner["tournament_id"] != checkpoint_owner_id
                ):
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已由进行中的循环赛 checkpoint 保留"
                    )
                if connection.execute(
                    "SELECT 1 FROM series_archives WHERE series_id = ?",
                    (series.series_id,),
                ).fetchone():
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已存档，不能重复归入循环赛"
                    )
                for leg in series.legs:
                    checkpoint_match_owner = connection.execute(
                        """
                        SELECT tcs.tournament_id
                        FROM tournament_checkpoint_series AS tcs
                        JOIN tournament_checkpoints AS tc
                          ON tc.tournament_id = tcs.tournament_id
                        WHERE tc.status = 'in_progress'
                          AND (tcs.match_1_id = ? OR tcs.match_2_id = ?)
                        """,
                        (leg.match_id, leg.match_id),
                    ).fetchone()
                    if (
                        checkpoint_match_owner is not None
                        and checkpoint_match_owner["tournament_id"] != checkpoint_owner_id
                    ):
                        raise MatchIdCollisionError(
                            f"match_id {leg.match_id!r} 已由进行中的循环赛 checkpoint 保留"
                        )
                    if connection.execute(
                        "SELECT 1 FROM matches WHERE match_id = ?",
                        (leg.match_id,),
                    ).fetchone():
                        raise MatchIdCollisionError(
                            f"match_id {leg.match_id!r} 已存档，不能重复归入循环赛"
                        )

            for entrant in entrants:
                self._upsert_entrant(
                    connection,
                    entrant,
                    observed_at=tournament.started_at,
                    trusted_engine=trusted_engine,
                )

            connection.execute(
                """
                INSERT INTO tournament_archives (
                    tournament_id, schema_version, format, pairing_policy,
                    seed_policy, game, seed, players_json, points_json,
                    pairing_count, rating_policy, k_factor, started_at,
                    finished_at, archive_source, rating_source, rated,
                    tournament_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament.tournament_id,
                    tournament.schema_version,
                    tournament.format,
                    tournament.pairing_policy,
                    tournament.seed_policy,
                    tournament.game,
                    tournament.seed,
                    _canonical_json(tournament_payload["players"]),
                    _canonical_json(tournament_payload["points"]),
                    pairing_count,
                    rating_policy,
                    K_FACTOR if rated else None,
                    tournament.started_at.astimezone(UTC).isoformat(),
                    tournament.finished_at.astimezone(UTC).isoformat(),
                    tournament.source,
                    rating_source,
                    int(rated),
                    tournament_json,
                ),
            )
            standings = {standing.entrant_id: standing for standing in tournament.standings}
            for position, (entrant, descriptor) in enumerate(
                zip(entrants, tournament_payload["players"])
            ):
                standing = standings[entrant.entrant_id]
                connection.execute(
                    """
                    INSERT INTO tournament_entrants (
                        tournament_id, position, entrant_id, display_name,
                        descriptor_json, points, series_played, series_wins,
                        series_draws, series_losses, games_played, wins, draws,
                        losses, technical_losses
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tournament.tournament_id,
                        position,
                        entrant.entrant_id,
                        entrant.display_name,
                        _canonical_json(descriptor),
                        standing.points,
                        standing.series_played,
                        standing.series_wins,
                        standing.series_draws,
                        standing.series_losses,
                        standing.games_played,
                        standing.wins,
                        standing.draws,
                        standing.losses,
                        standing.technical_losses,
                    ),
                )

            for pairing in tournament.pairings:
                series = pairing.series
                self._insert_series_structure(
                    connection,
                    series,
                    rating_source=rating_source,
                    rated=rated,
                    rating_policy=rating_policy,
                )
                player_a = entrants[pairing.player_indices[0]]
                player_b = entrants[pairing.player_indices[1]]
                connection.execute(
                    """
                    INSERT INTO tournament_pairings (
                        tournament_id, pairing_number, series_id,
                        entrant_a_id, entrant_b_id
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        tournament.tournament_id,
                        pairing.pairing_number,
                        series.series_id,
                        player_a.entrant_id,
                        player_b.entrant_id,
                    ),
                )

            rating_changes: list[TournamentRatingChange] = []
            if rated:
                self._record_rating_operation(
                    connection,
                    tournament_id=tournament.tournament_id,
                )
                rating_changes = self._record_tournament_ratings(
                    connection,
                    tournament,
                    entrants,
                )

            return TournamentSaveResult(
                inserted=True,
                rated=rated,
                pairing_count=pairing_count,
                match_count=match_count,
                rating_changes=tuple(rating_changes),
            )

        return persist()

    def save_series(
        self,
        series: SeriesArchive,
        *,
        rating_source: RatingSource = "imported",
    ) -> SaveResult:
        """原子保存交换顺序的两局档案，并按总局分更新一次 ELO。

        默认 ``rating_source="imported"`` 只存档；只有可信本地引擎来源且显式
        指定 ``rating_source="engine"`` 时计分。
        两局都会出现在普通对局历史中。每局都基于系列开始前的同一 ELO
        期望值计算贡献，最后一次写入榜单，因此各胜一局时双方积分不漂移。
        """

        rating_source = self._validate_rating_source(rating_source)
        series, entrants = self._validate_series(series)
        trusted_engine = (
            rating_source == "engine"
            and series.schema_version == 2
            and series.source == "local_engine"
        )
        rated = trusted_engine
        rating_policy = "elo_batch_v1" if rated else "unrated"
        series_payload = series.model_dump(mode="json")
        series_json = _canonical_json(series_payload)
        semantic_series_json = series_json
        serialized_legs = [self._serialize_archive(leg) for leg in series.legs]

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint_owner = connection.execute(
                """
                SELECT tcs.tournament_id
                FROM tournament_checkpoint_series AS tcs
                JOIN tournament_checkpoints AS tc
                  ON tc.tournament_id = tcs.tournament_id
                WHERE tc.status = 'in_progress' AND tcs.series_id = ?
                """,
                (series.series_id,),
            ).fetchone()
            if checkpoint_owner is not None:
                raise SeriesIdCollisionError(
                    f"series_id {series.series_id!r} 已由进行中的循环赛 checkpoint 保留"
                )
            for leg in series.legs:
                checkpoint_match_owner = connection.execute(
                    """
                    SELECT tcs.tournament_id
                    FROM tournament_checkpoint_series AS tcs
                    JOIN tournament_checkpoints AS tc
                      ON tc.tournament_id = tcs.tournament_id
                    WHERE tc.status = 'in_progress'
                      AND (tcs.match_1_id = ? OR tcs.match_2_id = ?)
                    """,
                    (leg.match_id, leg.match_id),
                ).fetchone()
                if checkpoint_match_owner is not None:
                    raise MatchIdCollisionError(
                        f"match_id {leg.match_id!r} 已由进行中的循环赛 checkpoint 保留"
                    )
            existing = connection.execute(
                """
                SELECT series_json, archive_source, rating_source, rated, rating_policy
                FROM series_archives WHERE series_id = ?
                """,
                (series.series_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = self._semantic_series_json(existing["series_json"])
                except StorageError as exc:
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != semantic_series_json:
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已对应另一份系列赛档案"
                    )
                tournament_result = self._verify_existing_tournament_child(
                    connection,
                    requested_rating_source=rating_source,
                    series_id=series.series_id,
                )
                if tournament_result is not None:
                    connection.commit()
                    return SaveResult(
                        inserted=False,
                        rated=tournament_result.rated,
                    )
                stored_rated = bool(existing["rated"])
                expected_stored_rated = existing["rating_source"] == "engine" and existing[
                    "archive_source"
                ] in ("local_engine", "legacy")
                expected_stored_policy = "elo_batch_v1" if stored_rated else "unrated"
                if (
                    stored_rated != expected_stored_rated
                    or existing["rating_policy"] != expected_stored_policy
                ):
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的计分来源或策略已损坏"
                    )
                read_only_downgrade = (
                    existing["rating_source"] == "engine" and rating_source == "imported"
                )
                exact_policy_match = (
                    existing["rating_source"] == rating_source
                    and stored_rated == rated
                    and existing["rating_policy"] == rating_policy
                )
                historical_engine_repeat = (
                    series.source == "legacy"
                    and existing["rating_source"] == "engine"
                    and rating_source == "engine"
                )
                if existing["archive_source"] != series.source or not (
                    read_only_downgrade or exact_policy_match or historical_engine_repeat
                ):
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已以不同来源或计分策略存档，"
                        "不能通过幂等重存升级"
                    )
                self._verify_existing_series(
                    connection,
                    series,
                    existing["rating_policy"],
                    rated=stored_rated,
                    archive_source=existing["archive_source"],
                    rating_source=existing["rating_source"],
                )
                self._verify_top_level_rating_operation(
                    connection,
                    rated=stored_rated,
                    series_id=series.series_id,
                )
                connection.commit()
                return SaveResult(inserted=False, rated=stored_rated)

            for leg in series.legs:
                if connection.execute(
                    "SELECT 1 FROM matches WHERE match_id = ?", (leg.match_id,)
                ).fetchone():
                    raise MatchIdCollisionError(
                        f"match_id {leg.match_id!r} 已存档，不能重复归入新的系列赛"
                    )

            for entrant in entrants:
                self._upsert_entrant(
                    connection,
                    entrant,
                    observed_at=series.started_at,
                    trusted_engine=trusted_engine,
                )

            connection.execute(
                """
                INSERT INTO series_archives (
                    series_id, schema_version, game, seed, players_json, points_json,
                    rating_policy, started_at, finished_at, series_json
                    , archive_source, rating_source, rated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    series.series_id,
                    series.schema_version,
                    series.game,
                    series.seed,
                    _canonical_json(series_payload["players"]),
                    _canonical_json(series_payload["points"]),
                    rating_policy,
                    series.started_at.astimezone(UTC).isoformat(),
                    series.finished_at.astimezone(UTC).isoformat(),
                    series_json,
                    series.source,
                    rating_source,
                    int(rated),
                ),
            )
            for leg_number, (leg, serialized) in enumerate(
                zip(series.legs, serialized_legs), start=1
            ):
                archive_payload, archive_json = serialized
                leg_entrants = self._validate_archive(leg)
                self._insert_match(
                    connection,
                    leg,
                    leg_entrants,
                    archive_payload,
                    archive_json,
                    rating_source=rating_source,
                    rated=rated,
                    rating_policy=rating_policy,
                )
                connection.execute(
                    """
                    INSERT INTO series_matches (series_id, leg_number, match_id)
                    VALUES (?, ?, ?)
                    """,
                    (series.series_id, leg_number, leg.match_id),
                )

            outcomes_a = tuple(
                head_to_head_point(leg, entrants[0].display_name) for leg in series.legs
            )
            changes: list[RatingChange] = []
            if rated:
                self._record_rating_operation(connection, series_id=series.series_id)
                changes.extend(
                    self._record_series_ratings(
                        connection,
                        series,
                        entrants[0],
                        entrants[1],
                        outcomes_a,
                        None,
                    )
                )
                changes.extend(
                    self._record_series_ratings(
                        connection,
                        series,
                        entrants[0],
                        entrants[1],
                        outcomes_a,
                        series.game,
                    )
                )

            connection.commit()
            return SaveResult(inserted=True, rated=rated, rating_changes=tuple(changes))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _record_tournament_ratings(
        self,
        connection: sqlite3.Connection,
        tournament: TournamentArchive,
        entrants: tuple[_EntrantRef, ...],
    ) -> list[TournamentRatingChange]:
        frozen_ratings = {
            (rating_scope, game_key, entrant.entrant_id): self._current_rating(
                connection,
                rating_scope,
                game_key,
                entrant.entrant_id,
            )
            for rating_scope, game_key in (("overall", ""), ("game", tournament.game))
            for entrant in entrants
        }
        contributions, aggregates = self._tournament_rating_ledger(
            tournament,
            entrants,
            frozen_ratings,
        )
        for contribution in contributions:
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    contribution.archive.match_id,
                    contribution.rating_scope,
                    contribution.game_key,
                    contribution.player.entrant_id,
                    contribution.player.display_name,
                    contribution.opponent.entrant_id,
                    contribution.opponent.display_name,
                    contribution.outcome,
                    contribution.before,
                    contribution.after,
                    contribution.archive.finished_at.astimezone(UTC).isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO tournament_rating_contributions (
                    tournament_id, sequence, match_id, rating_scope, game,
                    entrant_id, opponent_entrant_id, frozen_rating,
                    opponent_frozen_rating, expected_score, rating_delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament.tournament_id,
                    contribution.sequence,
                    contribution.archive.match_id,
                    contribution.rating_scope,
                    contribution.game_key,
                    contribution.player.entrant_id,
                    contribution.opponent.entrant_id,
                    contribution.frozen_rating,
                    contribution.opponent_frozen_rating,
                    contribution.expected,
                    contribution.delta,
                ),
            )

        result: list[TournamentRatingChange] = []
        for aggregate in aggregates:
            self._upsert_rating(
                connection,
                rating_scope=aggregate.rating_scope,
                game_key=aggregate.game_key,
                entrant_id=aggregate.player.entrant_id,
                rating=aggregate.after,
                outcomes=aggregate.outcomes,
                updated_at=tournament.finished_at,
            )
            wins = sum(outcome == 1.0 for outcome in aggregate.outcomes)
            draws = sum(outcome == 0.5 for outcome in aggregate.outcomes)
            losses = sum(outcome == 0.0 for outcome in aggregate.outcomes)
            connection.execute(
                """
                INSERT INTO tournament_rating_snapshots (
                    tournament_id, rating_scope, game, entrant_id, display_name,
                    rating_before, rating_after, games_added, wins_added,
                    draws_added, losses_added
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    tournament.tournament_id,
                    aggregate.rating_scope,
                    aggregate.game_key,
                    aggregate.player.entrant_id,
                    aggregate.player.display_name,
                    aggregate.before,
                    aggregate.after,
                    len(aggregate.outcomes),
                    wins,
                    draws,
                    losses,
                ),
            )
            result.append(
                TournamentRatingChange(
                    entrant_id=aggregate.player.entrant_id,
                    display_name=aggregate.player.display_name,
                    game=None if aggregate.rating_scope == "overall" else aggregate.game_key,
                    before=aggregate.before,
                    after=aggregate.after,
                    games_added=len(aggregate.outcomes),
                    wins_added=wins,
                    draws_added=draws,
                    losses_added=losses,
                )
            )
        return result

    def _record_ratings(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        player_a: _EntrantRef,
        player_b: _EntrantRef,
        outcome_a: float,
        game: str | None,
    ) -> list[RatingChange]:
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        before_a = self._current_rating(connection, rating_scope, game_key, player_a.entrant_id)
        before_b = self._current_rating(connection, rating_scope, game_key, player_b.entrant_id)
        after_a, after_b = update_ratings(before_a, before_b, outcome_a)
        changes = [
            RatingChange(
                player=player_a.display_name,
                opponent=player_b.display_name,
                game=game,
                outcome=outcome_a,
                before=before_a,
                after=after_a,
                entrant_id=player_a.entrant_id,
                opponent_entrant_id=player_b.entrant_id,
            ),
            RatingChange(
                player=player_b.display_name,
                opponent=player_a.display_name,
                game=game,
                outcome=1.0 - outcome_a,
                before=before_b,
                after=after_b,
                entrant_id=player_b.entrant_id,
                opponent_entrant_id=player_a.entrant_id,
            ),
        ]
        for change in changes:
            self._upsert_rating(
                connection,
                rating_scope=rating_scope,
                game_key=game_key,
                entrant_id=change.entrant_id,
                rating=change.after,
                outcomes=(change.outcome,),
                updated_at=archive.finished_at,
            )
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive.match_id,
                    rating_scope,
                    game_key,
                    change.entrant_id,
                    change.display_name,
                    change.opponent_entrant_id,
                    change.opponent_display_name,
                    change.outcome,
                    change.before,
                    change.after,
                    archive.finished_at.astimezone(UTC).isoformat(),
                ),
            )
        return changes


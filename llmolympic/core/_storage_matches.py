"""_MatchesMixin mixin for SQLiteStore."""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from llmolympic.core._storage_types import (
    SQLITE_INT_MAX,
    SQLITE_INT_MIN,
    MatchSummary,
    RatingChange,
    RatingEntry,
    RatingSource,
    StorageError,
    _canonical_json,
    _EntrantRef,
    _TournamentAggregate,
    _TournamentContribution,
    _validate_query_limit,
)
from llmolympic.core.archive import MatchArchive
from llmolympic.core.elo import DEFAULT_RATING, K_FACTOR, expected_score
from llmolympic.core.events import EventType
from llmolympic.core.series import SERIES_SCHEMA_VERSION, SeriesArchive, head_to_head_point
from llmolympic.core.tournament import (
    TOURNAMENT_CHECKPOINT_SCHEMA_VERSION,
    TOURNAMENT_SCHEMA_VERSION,
    TournamentArchive,
    TournamentCheckpoint,
)


class _MatchesMixin:
    @staticmethod
    def _serialize_archive(archive: MatchArchive) -> tuple[dict, str]:
        archive_payload = archive.model_dump(mode="json")
        return archive_payload, _canonical_json(archive_payload)

    @staticmethod
    def _insert_match(
        connection: sqlite3.Connection,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
        archive_payload: dict,
        archive_json: str,
        *,
        rating_source: RatingSource,
        rated: bool,
        rating_policy: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO matches (
                match_id, schema_version, game, seed, players_json, scores_json,
                started_at, finished_at, archive_source, rating_source, rated,
                rating_policy, archive_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                archive.match_id,
                archive.schema_version,
                archive.game,
                archive.seed,
                _canonical_json(archive_payload["players"]),
                _canonical_json(archive_payload["scores"]),
                archive.started_at.astimezone(UTC).isoformat(),
                archive.finished_at.astimezone(UTC).isoformat(),
                archive.source,
                rating_source,
                int(rated),
                rating_policy,
                archive_json,
            ),
        )
        for position, (entrant, descriptor) in enumerate(zip(entrants, archive_payload["players"])):
            connection.execute(
                """
                INSERT INTO match_players (
                    match_id, position, player, entrant_id, display_name,
                    descriptor_json, score
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive.match_id,
                    position,
                    entrant.display_name,
                    entrant.entrant_id,
                    entrant.display_name,
                    _canonical_json(descriptor),
                    archive.scores[entrant.display_name],
                ),
            )

    def _insert_series_structure(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        *,
        rating_source: RatingSource,
        rated: bool,
        rating_policy: str,
    ) -> None:
        series_payload = series.model_dump(mode="json")
        connection.execute(
            """
            INSERT INTO series_archives (
                series_id, schema_version, game, seed, players_json, points_json,
                rating_policy, started_at, finished_at, series_json,
                archive_source, rating_source, rated
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
                _canonical_json(series_payload),
                series.source,
                rating_source,
                int(rated),
            ),
        )
        for leg_number, leg in enumerate(series.legs, start=1):
            archive_payload, archive_json = self._serialize_archive(leg)
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

    def _verify_tournament_series_structure(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        series_row = connection.execute(
            """
            SELECT series_id, schema_version, game, seed, players_json, points_json,
                   started_at, finished_at, archive_source, rating_source, rated,
                   rating_policy, series_json
            FROM series_archives
            WHERE series_id = ?
            """,
            (series.series_id,),
        ).fetchone()
        if series_row is None:
            raise StorageError(f"循环赛中的 series_id {series.series_id!r} 已丢失")
        try:
            stored_series_json = self._semantic_series_json(series_row["series_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的档案 JSON 已损坏"
            ) from exc
        if stored_series_json != _canonical_json(series.model_dump(mode="json")):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的循环赛档案已损坏")
        self._verify_series_metadata(
            series_row,
            series,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )
        rows = connection.execute(
            """
            SELECT sm.leg_number, sm.match_id, m.schema_version, m.game, m.seed,
                   m.players_json, m.scores_json, m.started_at, m.finished_at,
                   m.archive_json, m.archive_source, m.rating_source, m.rated,
                   m.rating_policy
            FROM series_matches AS sm
            JOIN matches AS m ON m.match_id = sm.match_id
            WHERE sm.series_id = ?
            ORDER BY sm.leg_number
            """,
            (series.series_id,),
        ).fetchall()
        if [row["leg_number"] for row in rows] != [1, 2] or [row["match_id"] for row in rows] != [
            leg.match_id for leg in series.legs
        ]:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的循环赛对局映射已损坏")
        for row, leg in zip(rows, series.legs):
            self._verify_match_metadata(
                row,
                leg,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )
            try:
                stored_match_json = self._semantic_match_json(row["archive_json"])
            except StorageError as exc:
                raise StorageError(
                    f"数据库中 match_id {leg.match_id!r} 的档案 JSON 已损坏"
                ) from exc
            if stored_match_json != _canonical_json(leg.model_dump(mode="json")):
                raise StorageError(f"数据库中 match_id {leg.match_id!r} 的循环赛档案已损坏")
            self._verify_match_players(
                connection,
                leg,
                self._validate_archive(leg),
            )

    def _verify_existing_series(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        rating_policy: str,
        *,
        rated: bool,
        archive_source: str,
        rating_source: str,
    ) -> None:
        expected_policy = "elo_batch_v1" if rated else "unrated"
        expected_rated = rating_source == "engine" and archive_source in (
            "local_engine",
            "legacy",
        )
        if (
            archive_source != series.source
            or rated != expected_rated
            or rating_policy != expected_policy
        ):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的来源或计分状态已损坏")
        series_row = connection.execute(
            """
            SELECT series_id, schema_version, game, seed, players_json, points_json,
                   started_at, finished_at, archive_source, rating_source, rated,
                   rating_policy
            FROM series_archives
            WHERE series_id = ?
            """,
            (series.series_id,),
        ).fetchone()
        if series_row is None:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏")
        self._verify_series_metadata(
            series_row,
            series,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )
        rows = connection.execute(
            """
            SELECT sm.leg_number, sm.match_id, m.schema_version, m.game, m.seed,
                   m.players_json, m.scores_json, m.started_at, m.finished_at,
                   m.archive_json, m.archive_source, m.rating_source, m.rated,
                   m.rating_policy
            FROM series_matches AS sm
            JOIN matches AS m ON m.match_id = sm.match_id
            WHERE sm.series_id = ?
            ORDER BY sm.leg_number
            """,
            (series.series_id,),
        ).fetchall()
        expected_ids = [leg.match_id for leg in series.legs]
        if [row["leg_number"] for row in rows] != [1, 2] or [
            row["match_id"] for row in rows
        ] != expected_ids:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的对局映射已损坏")
        for row, leg in zip(rows, series.legs):
            self._verify_match_metadata(
                row,
                leg,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )
            if (
                row["archive_source"] != archive_source
                or row["rating_source"] != rating_source
                or bool(row["rated"]) != rated
                or row["rating_policy"] != rating_policy
            ):
                raise StorageError(
                    f"数据库中 series_id {series.series_id!r} 的对局来源或计分状态已损坏"
                )
            try:
                stored_json = self._semantic_match_json(row["archive_json"])
            except StorageError as exc:
                raise StorageError(
                    f"数据库中 match_id {leg.match_id!r} 的档案 JSON 已损坏"
                ) from exc
            expected_json = _canonical_json(leg.model_dump(mode="json"))
            if stored_json != expected_json:
                raise StorageError(f"数据库中 series_id {series.series_id!r} 的对局档案已损坏")
            leg_entrants = self._validate_archive(leg)
            self._verify_match_players(connection, leg, leg_entrants)
        history_rows = connection.execute(
            """
            SELECT match_id, rating_scope, game, entrant_id, display_name,
                   opponent_entrant_id, opponent_display_name, outcome,
                   rating_before, rating_after, created_at
            FROM rating_history
            WHERE match_id IN (?, ?)
            """,
            expected_ids,
        ).fetchall()
        if not rated:
            if history_rows:
                raise StorageError(f"数据库中 series_id {series.series_id!r} 的未计分状态已损坏")
            return
        if len(history_rows) != 8:
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
        history = {
            (row["match_id"], row["rating_scope"], row["game"], row["entrant_id"]): row
            for row in history_rows
        }
        if len(history) != len(history_rows):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
        player_a = self._entrant_ref(series.players[0], legacy=series.schema_version == 1)
        player_b = self._entrant_ref(series.players[1], legacy=series.schema_version == 1)
        outcomes_a = tuple(head_to_head_point(leg, player_a.display_name) for leg in series.legs)
        for rating_scope, game_key in (("overall", ""), ("game", series.game)):
            first_a_key = (expected_ids[0], rating_scope, game_key, player_a.entrant_id)
            first_b_key = (expected_ids[0], rating_scope, game_key, player_b.entrant_id)
            if first_a_key not in history or first_b_key not in history:
                raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
            try:
                running_a = self._finite_database_float(history[first_a_key]["rating_before"])
                running_b = self._finite_database_float(history[first_b_key]["rating_before"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
                ) from exc
            frozen_expectation = expected_score(running_a, running_b)
            for leg, outcome_a in zip(series.legs, outcomes_a):
                row_a = history.get((leg.match_id, rating_scope, game_key, player_a.entrant_id))
                row_b = history.get((leg.match_id, rating_scope, game_key, player_b.entrant_id))
                if row_a is None or row_b is None:
                    raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
                delta_a = K_FACTOR * (outcome_a - frozen_expectation)
                next_a = running_a + delta_a
                next_b = running_b - delta_a
                try:
                    stored_outcome_a = self._finite_database_float(row_a["outcome"])
                    stored_before_a = self._finite_database_float(row_a["rating_before"])
                    stored_after_a = self._finite_database_float(row_a["rating_after"])
                    stored_outcome_b = self._finite_database_float(row_b["outcome"])
                    stored_before_b = self._finite_database_float(row_b["rating_before"])
                    stored_after_b = self._finite_database_float(row_b["rating_after"])
                except (TypeError, ValueError) as exc:
                    raise StorageError(
                        f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏"
                    ) from exc
                if (
                    row_a["opponent_entrant_id"] != player_b.entrant_id
                    or row_a["display_name"] != player_a.display_name
                    or row_a["opponent_display_name"] != player_b.display_name
                    or stored_outcome_a != outcome_a
                    or stored_before_a != running_a
                    or stored_after_a != next_a
                    or not self._timestamp_matches(row_a["created_at"], leg.finished_at)
                    or row_b["opponent_entrant_id"] != player_a.entrant_id
                    or row_b["display_name"] != player_b.display_name
                    or row_b["opponent_display_name"] != player_a.display_name
                    or stored_outcome_b != 1.0 - outcome_a
                    or stored_before_b != running_b
                    or stored_after_b != next_b
                    or not self._timestamp_matches(row_b["created_at"], leg.finished_at)
                ):
                    raise StorageError(f"数据库中 series_id {series.series_id!r} 的 ELO 历史已损坏")
                running_a = next_a
                running_b = next_b

    def _validate_checkpoint(
        self, checkpoint: TournamentCheckpoint
    ) -> tuple[TournamentCheckpoint, tuple[_EntrantRef, ...]]:
        if checkpoint.schema_version != TOURNAMENT_CHECKPOINT_SCHEMA_VERSION:
            raise StorageError(
                f"不支持循环赛 checkpoint 版本 {checkpoint.schema_version}；"
                f"当前支持 {TOURNAMENT_CHECKPOINT_SCHEMA_VERSION}"
            )
        try:
            validated = TournamentCheckpoint.model_validate(checkpoint.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"循环赛 checkpoint 无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False) for descriptor in validated.players
        )
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("循环赛 checkpoint 中的 entrant_id 必须唯一")
        for series in validated.completed_series:
            self._validate_series(series)
        return validated, entrants

    def _validate_tournament(
        self, tournament: TournamentArchive
    ) -> tuple[TournamentArchive, tuple[_EntrantRef, ...]]:
        if tournament.schema_version != TOURNAMENT_SCHEMA_VERSION:
            raise StorageError(
                f"不支持循环赛档案版本 {tournament.schema_version}；"
                f"当前支持 {TOURNAMENT_SCHEMA_VERSION}"
            )
        try:
            validated = TournamentArchive.model_validate(tournament.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"循环赛档案无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False) for descriptor in validated.players
        )
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("循环赛档案中的 entrant_id 必须唯一")
        for pairing in validated.pairings:
            self._validate_series(pairing.series)
        return validated, entrants

    def _validate_series(
        self, series: SeriesArchive
    ) -> tuple[SeriesArchive, tuple[_EntrantRef, _EntrantRef]]:
        if series.schema_version not in (1, SERIES_SCHEMA_VERSION):
            raise StorageError(
                f"不支持系列赛档案版本 {series.schema_version}；"
                f"当前支持 1 和 {SERIES_SCHEMA_VERSION}"
            )
        try:
            validated = SeriesArchive.model_validate(series.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"系列赛档案无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(
                descriptor,
                legacy=validated.schema_version == 1,
            )
            for descriptor in validated.players
        )
        for leg in validated.legs:
            self._validate_archive(leg)
        if len(entrants) != 2:
            raise StorageError("系列赛必须包含恰好两个 entrant_id")
        return validated, (entrants[0], entrants[1])

    def _validate_archive(self, archive: MatchArchive) -> list[_EntrantRef]:
        if archive.schema_version not in (1, 2):
            raise StorageError(f"不支持对局档案版本 {archive.schema_version}；当前支持 1 和 2")
        if archive.schema_version == 1 and archive.source != "legacy":
            raise StorageError("schema v1 档案来源必须是 legacy")
        if archive.schema_version == 2 and archive.source not in (
            "local_engine",
            "external",
        ):
            raise StorageError("schema v2 档案来源必须是 local_engine 或 external")
        try:
            MatchArchive.model_validate(archive.model_dump(mode="python"))
        except (TypeError, ValueError) as exc:
            raise StorageError(f"对局档案无效：{exc}") from exc
        if not SQLITE_INT_MIN <= archive.seed <= SQLITE_INT_MAX:
            raise StorageError(
                f"seed 必须在 SQLite 有符号 64 位整数范围内：{SQLITE_INT_MIN} 到 {SQLITE_INT_MAX}"
            )
        if archive.started_at.utcoffset() is None or archive.finished_at.utcoffset() is None:
            raise StorageError("对局开始和结束时间必须包含时区")
        if archive.finished_at < archive.started_at:
            raise StorageError("对局结束时间不能早于开始时间")
        entrants = [
            self._entrant_ref(descriptor, legacy=archive.schema_version == 1)
            for descriptor in archive.players
        ]
        player_names = [entrant.display_name for entrant in entrants]
        if len(set(player_names)) != len(player_names):
            raise StorageError("对局档案中的选手名字必须唯一")
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("对局档案中的 entrant_id 必须唯一")
        if set(player_names) != set(archive.scores):
            raise StorageError("对局档案中的选手与 scores 必须完全一致")
        for player, score in archive.scores.items():
            if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                raise StorageError(f"{player} 的比分必须是 0.0 到 1.0 之间的有限数值")
        if not archive.events:
            raise StorageError("对局档案必须包含事件流")
        if [event.seq for event in archive.events] != list(range(len(archive.events))):
            raise StorageError("对局事件 seq 必须从 0 开始且连续递增")
        started_events = [
            event for event in archive.events if event.type == EventType.MATCH_STARTED
        ]
        finished_events = [
            event for event in archive.events if event.type == EventType.MATCH_FINISHED
        ]
        if len(started_events) != 1 or archive.events[0] is not started_events[0]:
            raise StorageError("对局事件流必须以唯一的 match_started 开始")
        if len(finished_events) != 1 or archive.events[-1] is not finished_events[0]:
            raise StorageError("对局事件流必须以唯一的 match_finished 结束")
        started_data = started_events[0].data
        if started_data.get("game") != archive.game or started_data.get("seed") != archive.seed:
            raise StorageError("match_started 的项目或 seed 与档案不一致")
        if "game_config" in started_data and not isinstance(started_data["game_config"], dict):
            raise StorageError("match_started 的 game_config 必须是对象")
        if started_data.get("players") != archive.players:
            raise StorageError("match_started 的选手描述与档案不一致")
        finished_scores = finished_events[0].data.get("scores")
        if not isinstance(finished_scores, dict):
            raise StorageError("match_finished 的比分与档案不一致")
        if any(
            not isinstance(name, str)
            or isinstance(score, bool)
            or not isinstance(score, (int, float))
            for name, score in finished_scores.items()
        ):
            raise StorageError("match_finished 的比分必须是选手名到数值的映射")
        normalized_finished_scores = {name: float(score) for name, score in finished_scores.items()}
        if normalized_finished_scores != archive.scores:
            raise StorageError("match_finished 的比分与档案不一致")

        finished_data = finished_events[0].data
        termination = finished_data.get("termination")
        technical_loss_events = [
            event
            for event in archive.events
            if event.type == EventType.MOVE_REJECTED and event.data.get("technical_loss") is True
        ]
        technical_control_fields = {
            "reason_code",
            "forfeited_by",
            "cause_event_seq",
            "failure_details",
        }
        has_technical_controls = any(field in finished_data for field in technical_control_fields)
        if termination is None:
            if technical_loss_events or has_technical_controls:
                raise StorageError("技术负事件必须包含结构化 termination")
            return entrants  # schema v1 历史档案没有结构化终局原因
        if termination not in ("completed", "technical_loss"):
            raise StorageError("match_finished 的 termination 无效")
        if termination == "completed":
            if technical_loss_events or has_technical_controls:
                raise StorageError("正常结束的档案不能包含技术负控制字段")
            return entrants
        failure_details = finished_data.get("failure_details")
        if failure_details is not None and not isinstance(failure_details, dict):
            raise StorageError("match_finished 的 failure_details 必须是对象")
        forfeited_by = finished_data.get("forfeited_by")
        reason_code = finished_data.get("reason_code")
        reason = finished_data.get("reason")
        cause_event_seq = finished_data.get("cause_event_seq")
        if forfeited_by not in player_names:
            raise StorageError("技术负的 forfeited_by 必须是参赛选手")
        if not isinstance(reason_code, str) or not reason_code:
            raise StorageError("技术负必须包含非空 reason_code")
        if not isinstance(reason, str) or not reason:
            raise StorageError("技术负必须包含非空 reason")
        if (
            isinstance(cause_event_seq, bool)
            or not isinstance(cause_event_seq, int)
            or not 0 <= cause_event_seq < len(archive.events)
        ):
            raise StorageError("技术负必须包含有效 cause_event_seq")
        cause_event = archive.events[cause_event_seq]
        if (
            cause_event.type != EventType.MOVE_REJECTED
            or cause_event.player != forfeited_by
            or cause_event.data.get("reason_code") != reason_code
            or cause_event.data.get("reason") != reason
            or cause_event.data.get("forfeit") is not True
            or cause_event.data.get("forfeit_scope") != "match"
            or cause_event.data.get("technical_loss") is not True
            or cause_event.data.get("failure_details") != failure_details
            or len(technical_loss_events) != 1
            or technical_loss_events[0] is not cause_event
        ):
            raise StorageError("技术负的原因事件与 match_finished 不一致")
        if archive.scores[forfeited_by] != 0.0 or any(
            score != 1.0 for player, score in archive.scores.items() if player != forfeited_by
        ):
            raise StorageError("技术负必须记为责任方 0 分、其他选手 1 分")
        return entrants

    @staticmethod
    def _tournament_rating_ledger(
        tournament: TournamentArchive,
        entrants: tuple[_EntrantRef, ...],
        frozen_ratings: dict[tuple[str, str, str], float],
    ) -> tuple[list[_TournamentContribution], list[_TournamentAggregate]]:
        contributions: list[_TournamentContribution] = []
        aggregates: list[_TournamentAggregate] = []
        for rating_scope, game_key in (("overall", ""), ("game", tournament.game)):
            outcomes: dict[str, list[float]] = {entrant.entrant_id: [] for entrant in entrants}
            deltas: dict[str, list[float]] = {entrant.entrant_id: [] for entrant in entrants}
            for pairing in tournament.pairings:
                player_a = entrants[pairing.player_indices[0]]
                player_b = entrants[pairing.player_indices[1]]
                frozen_a = frozen_ratings[(rating_scope, game_key, player_a.entrant_id)]
                frozen_b = frozen_ratings[(rating_scope, game_key, player_b.entrant_id)]
                expected_a = expected_score(frozen_a, frozen_b)
                for leg_number, leg in enumerate(pairing.series.legs, start=1):
                    sequence = (pairing.pairing_number - 1) * 2 + leg_number
                    outcome_a = head_to_head_point(leg, player_a.display_name)
                    delta_a = K_FACTOR * (outcome_a - expected_a)
                    before_a = frozen_a + math.fsum(deltas[player_a.entrant_id])
                    before_b = frozen_b + math.fsum(deltas[player_b.entrant_id])
                    deltas[player_a.entrant_id].append(delta_a)
                    deltas[player_b.entrant_id].append(-delta_a)
                    after_a = frozen_a + math.fsum(deltas[player_a.entrant_id])
                    after_b = frozen_b + math.fsum(deltas[player_b.entrant_id])
                    outcomes[player_a.entrant_id].append(outcome_a)
                    outcomes[player_b.entrant_id].append(1.0 - outcome_a)
                    contributions.extend(
                        (
                            _TournamentContribution(
                                sequence=sequence,
                                archive=leg,
                                rating_scope=rating_scope,
                                game_key=game_key,
                                player=player_a,
                                opponent=player_b,
                                outcome=outcome_a,
                                frozen_rating=frozen_a,
                                opponent_frozen_rating=frozen_b,
                                expected=expected_a,
                                delta=delta_a,
                                before=before_a,
                                after=after_a,
                            ),
                            _TournamentContribution(
                                sequence=sequence,
                                archive=leg,
                                rating_scope=rating_scope,
                                game_key=game_key,
                                player=player_b,
                                opponent=player_a,
                                outcome=1.0 - outcome_a,
                                frozen_rating=frozen_b,
                                opponent_frozen_rating=frozen_a,
                                expected=1.0 - expected_a,
                                delta=-delta_a,
                                before=before_b,
                                after=after_b,
                            ),
                        )
                    )
            for entrant in entrants:
                before = frozen_ratings[(rating_scope, game_key, entrant.entrant_id)]
                aggregates.append(
                    _TournamentAggregate(
                        rating_scope=rating_scope,
                        game_key=game_key,
                        player=entrant,
                        before=before,
                        after=before + math.fsum(deltas[entrant.entrant_id]),
                        outcomes=tuple(outcomes[entrant.entrant_id]),
                    )
                )
        return contributions, aggregates

    def _record_series_ratings(
        self,
        connection: sqlite3.Connection,
        series: SeriesArchive,
        player_a: _EntrantRef,
        player_b: _EntrantRef,
        outcomes_a: tuple[float, ...],
        game: str | None,
    ) -> list[RatingChange]:
        """按系列开始前的同一 ELO 期望值累计各局变化。"""

        if len(outcomes_a) != len(series.legs):
            raise StorageError("系列赛 ELO 局分数量与对局数量不一致")
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        before_a = self._current_rating(connection, rating_scope, game_key, player_a.entrant_id)
        before_b = self._current_rating(connection, rating_scope, game_key, player_b.entrant_id)
        expected_a = expected_score(before_a, before_b)
        deltas_a = tuple(K_FACTOR * (outcome - expected_a) for outcome in outcomes_a)
        outcomes_b = tuple(1.0 - outcome for outcome in outcomes_a)
        running_a = before_a
        running_b = before_b
        history_rows: list[tuple[MatchArchive, _EntrantRef, _EntrantRef, float, float, float]] = []
        for leg, outcome_a, delta_a in zip(series.legs, outcomes_a, deltas_a):
            next_a = running_a + delta_a
            next_b = running_b - delta_a
            history_rows.extend(
                (
                    (leg, player_a, player_b, outcome_a, running_a, next_a),
                    (leg, player_b, player_a, 1.0 - outcome_a, running_b, next_b),
                )
            )
            running_a = next_a
            running_b = next_b
        after_a = running_a
        after_b = running_b

        self._upsert_rating(
            connection,
            rating_scope=rating_scope,
            game_key=game_key,
            entrant_id=player_a.entrant_id,
            rating=after_a,
            outcomes=outcomes_a,
            updated_at=series.finished_at,
        )
        self._upsert_rating(
            connection,
            rating_scope=rating_scope,
            game_key=game_key,
            entrant_id=player_b.entrant_id,
            rating=after_b,
            outcomes=outcomes_b,
            updated_at=series.finished_at,
        )
        for leg, player, opponent, outcome, before, after in history_rows:
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    leg.match_id,
                    rating_scope,
                    game_key,
                    player.entrant_id,
                    player.display_name,
                    opponent.entrant_id,
                    opponent.display_name,
                    outcome,
                    before,
                    after,
                    leg.finished_at.astimezone(UTC).isoformat(),
                ),
            )

        average_a = sum(outcomes_a) / len(outcomes_a)
        return [
            RatingChange(
                player=player_a.display_name,
                opponent=player_b.display_name,
                game=game,
                outcome=average_a,
                before=before_a,
                after=after_a,
                entrant_id=player_a.entrant_id,
                opponent_entrant_id=player_b.entrant_id,
            ),
            RatingChange(
                player=player_b.display_name,
                opponent=player_a.display_name,
                game=game,
                outcome=1.0 - average_a,
                before=before_b,
                after=after_b,
                entrant_id=player_b.entrant_id,
                opponent_entrant_id=player_a.entrant_id,
            ),
        ]

    @staticmethod
    def _upsert_rating(
        connection: sqlite3.Connection,
        *,
        rating_scope: str,
        game_key: str,
        entrant_id: str,
        rating: float,
        outcomes: tuple[float, ...],
        updated_at: datetime,
    ) -> None:
        wins = sum(outcome == 1.0 for outcome in outcomes)
        draws = sum(outcome == 0.5 for outcome in outcomes)
        losses = sum(outcome == 0.0 for outcome in outcomes)
        connection.execute(
            """
            INSERT INTO ratings (
                rating_scope, game, entrant_id, rating, games_played,
                wins, draws, losses, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (rating_scope, game, entrant_id) DO UPDATE SET
                rating = excluded.rating,
                games_played = ratings.games_played + excluded.games_played,
                wins = ratings.wins + excluded.wins,
                draws = ratings.draws + excluded.draws,
                losses = ratings.losses + excluded.losses,
                updated_at = MAX(ratings.updated_at, excluded.updated_at)
            """,
            (
                rating_scope,
                game_key,
                entrant_id,
                rating,
                len(outcomes),
                wins,
                draws,
                losses,
                updated_at.astimezone(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _current_rating(
        connection: sqlite3.Connection, rating_scope: str, game: str, entrant_id: str
    ) -> float:
        row = connection.execute(
            """
            SELECT rating FROM ratings
            WHERE rating_scope = ? AND game = ? AND entrant_id = ?
            """,
            (rating_scope, game, entrant_id),
        ).fetchone()
        return DEFAULT_RATING if row is None else float(row["rating"])

    def get_match(self, match_id: str) -> MatchArchive | None:
        """Load the complete archive for ``match_id``."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT archive_json FROM matches WHERE match_id = ?", (match_id,)
            ).fetchone()
        return None if row is None else MatchArchive.model_validate_json(row["archive_json"])

    def get_series(self, series_id: str) -> SeriesArchive | None:
        """Load the complete two-leg archive for ``series_id``."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT series_json FROM series_archives WHERE series_id = ?",
                (series_id,),
            ).fetchone()
        return None if row is None else SeriesArchive.model_validate_json(row["series_json"])

    def get_tournament(self, tournament_id: str) -> TournamentArchive | None:
        """Load the complete round-robin archive for ``tournament_id``."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT tournament_json FROM tournament_archives
                WHERE tournament_id = ?
                """,
                (tournament_id,),
            ).fetchone()
        return (
            None if row is None else TournamentArchive.model_validate_json(row["tournament_json"])
        )

    def get_verified_tournament(self, tournament_id: str) -> TournamentArchive | None:
        """Load one formal tournament and checkpoint from one consistent snapshot."""

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            loaded_checkpoint = self._load_tournament_checkpoint(connection, tournament_id)
            loaded = self._load_verified_tournament(connection, tournament_id)
            if loaded is not None and (
                loaded_checkpoint is not None and loaded_checkpoint[1] != "finalized"
            ):
                raise StorageError("进行中的 checkpoint 已存在同 ID 正式循环赛档案")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return None if loaded is None else loaded[0]

    def list_matches(self, *, limit: int = 20, game: str | None = None) -> list[MatchSummary]:
        """Return recent persisted matches, newest first."""

        _validate_query_limit(limit)
        sql = """
            SELECT m.match_id, m.game, m.seed, m.players_json, m.scores_json,
                   m.started_at, m.finished_at, m.rating_source, m.rated,
                   sm.series_id, sm.leg_number, tp.tournament_id,
                   tp.pairing_number, ta.pairing_count
            FROM matches AS m
            LEFT JOIN series_matches AS sm ON sm.match_id = m.match_id
            LEFT JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
            LEFT JOIN tournament_archives AS ta
                   ON ta.tournament_id = tp.tournament_id
        """
        params: list[object] = []
        if game is not None:
            sql += " WHERE m.game = ?"
            params.append(game)
        sql += " ORDER BY m.finished_at DESC, m.match_id DESC LIMIT ?"
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
            entrant_ids_by_match: dict[str, list[str]] = {row["match_id"]: [] for row in rows}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                identity_rows = connection.execute(
                    f"""
                    SELECT match_id, entrant_id
                    FROM match_players
                    WHERE match_id IN ({placeholders})
                    ORDER BY match_id, position
                    """,  # noqa: S608 - placeholders are generated, values stay parameterized
                    [row["match_id"] for row in rows],
                ).fetchall()
                for identity_row in identity_rows:
                    entrant_ids_by_match[identity_row["match_id"]].append(
                        identity_row["entrant_id"]
                    )
        return [
            MatchSummary(
                match_id=row["match_id"],
                game=row["game"],
                seed=row["seed"],
                players=tuple(player["name"] for player in json.loads(row["players_json"])),
                entrant_ids=tuple(entrant_ids_by_match[row["match_id"]]),
                scores={
                    name: float(score) for name, score in json.loads(row["scores_json"]).items()
                },
                started_at=datetime.fromisoformat(row["started_at"]),
                finished_at=datetime.fromisoformat(row["finished_at"]),
                series_id=row["series_id"],
                leg_number=row["leg_number"],
                rating_source=row["rating_source"],
                rated=bool(row["rated"]),
                tournament_id=row["tournament_id"],
                pairing_number=row["pairing_number"],
                pairing_count=row["pairing_count"],
            )
            for row in rows
        ]

    def leaderboard(self, *, game: str | None = None, limit: int = 50) -> list[RatingEntry]:
        """Return the overall leaderboard or the leaderboard for one game."""

        _validate_query_limit(limit)
        rating_scope = "overall" if game is None else "game"
        game_key = "" if game is None else game
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT r.entrant_id, e.display_name, r.rating, r.games_played,
                       r.wins, r.draws, r.losses, r.updated_at
                FROM ratings AS r
                JOIN entrants AS e ON e.entrant_id = r.entrant_id
                WHERE r.rating_scope = ? AND r.game = ?
                ORDER BY r.rating DESC, r.games_played DESC,
                         e.display_name ASC, r.entrant_id ASC
                LIMIT ?
                """,
                (rating_scope, game_key, limit),
            ).fetchall()
        return [
            RatingEntry(
                player=row["display_name"],
                entrant_id=row["entrant_id"],
                rating=float(row["rating"]),
                games_played=row["games_played"],
                wins=row["wins"],
                draws=row["draws"],
                losses=row["losses"],
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]


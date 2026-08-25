"""_EntrantsMixin mixin for SQLiteStore."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime

from llmolympic.core._storage_types import (
    _IDENTITY_SAMPLING_KEYS,
    _SAFE_PROFILE_ID,
    MatchIdCollisionError,
    RatingChange,
    RatingSource,
    SaveResult,
    SeriesIdCollisionError,
    StorageError,
    TournamentSaveResult,
    _canonical_json,
    _EntrantRef,
    _sensitive_descriptor_path,
)
from llmolympic.core.archive import (
    MatchArchive,
    legacy_entrant_id,
    normalize_player_descriptors,
    validate_entrant_id,
)
from llmolympic.core.elo import K_FACTOR, update_ratings
from llmolympic.core.series import SeriesArchive
from llmolympic.core.tournament import TournamentArchive, TournamentCheckpoint


class _EntrantsMixin:
    @staticmethod
    def _entrant_ref(descriptor: dict, *, legacy: bool) -> _EntrantRef:
        try:
            entrant_id = validate_entrant_id(descriptor.get("entrant_id"))
        except ValueError as exc:
            raise StorageError(str(exc)) from exc
        display_name = descriptor.get("display_name")
        if not isinstance(display_name, str) or not display_name:
            raise StorageError("选手描述必须包含非空 display_name")
        if descriptor.get("name") != display_name:
            raise StorageError("选手描述的 display_name 必须与 name 一致")
        if legacy:
            expected_id = legacy_entrant_id(display_name)
            if entrant_id != expected_id:
                raise StorageError("legacy entrant_id 与历史选手名称不一致")
            identity = {"kind": "legacy"}
        else:
            if entrant_id.startswith("legacy:"):
                raise StorageError("新档案不能声明保留的 legacy entrant_id")
            sensitive_path = _sensitive_descriptor_path(descriptor)
            if sensitive_path is not None:
                raise StorageError(f"选手描述不能包含凭据或连接端点字段：{sensitive_path}")
            kind = descriptor.get("kind")
            if not isinstance(kind, str) or not kind:
                raise StorageError("选手描述必须包含非空 kind")
            identity = {"kind": kind}
            for key in ("profile_id", "provider", "model"):
                if key not in descriptor:
                    continue
                value = descriptor[key]
                if not isinstance(value, str) or not value:
                    raise StorageError(f"选手描述的 {key} 必须是非空字符串")
                if key == "profile_id" and _SAFE_PROFILE_ID.fullmatch(value) is None:
                    raise StorageError("选手描述的 profile_id 格式无效")
                identity[key] = value
            if "sampling_params" in descriptor:
                sampling_params = descriptor["sampling_params"]
                if not isinstance(sampling_params, dict):
                    raise StorageError("选手描述的 sampling_params 必须是对象")
                safe_sampling_params: dict[str, object] = {}
                for key, value in sampling_params.items():
                    if not isinstance(key, str):
                        raise StorageError("选手描述的 sampling_params 键必须是字符串")
                    if key not in _IDENTITY_SAMPLING_KEYS:
                        if value != "[REDACTED]":
                            raise StorageError(f"选手描述的 sampling_params.{key} 必须已脱敏")
                        continue
                    if value == "[REDACTED]":
                        continue
                    if value is not None and not isinstance(value, (bool, int, float)):
                        raise StorageError(f"选手描述的 sampling_params.{key} 必须是标量")
                    if isinstance(value, float) and not math.isfinite(value):
                        raise StorageError(f"选手描述的 sampling_params.{key} 必须是有限数值")
                    safe_sampling_params[key] = value
                identity["sampling_params"] = safe_sampling_params
        return _EntrantRef(
            entrant_id=entrant_id,
            display_name=display_name,
            identity_json=_canonical_json(identity),
        )

    @staticmethod
    def _has_trusted_entrant_observation(
        connection: sqlite3.Connection,
        entrant_id: str,
    ) -> bool:
        return bool(
            connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM match_players AS mp
                    JOIN matches AS m ON m.match_id = mp.match_id
                    WHERE mp.entrant_id = ?
                      AND m.schema_version = 2
                      AND m.archive_source = 'local_engine'
                      AND m.rating_source = 'engine'
                )
                """,
                (entrant_id,),
            ).fetchone()[0]
        )

    def _verify_checkpoint_entrant_bindings(
        self,
        connection: sqlite3.Connection,
        checkpoint: TournamentCheckpoint,
    ) -> None:
        """Reject checkpoints that cannot become trusted tournament entrants."""

        self._verify_entrant_descriptors(connection, tuple(checkpoint.players))

    def _verify_entrant_descriptors(
        self,
        connection: sqlite3.Connection,
        descriptors: tuple[dict, ...],
    ) -> None:
        """Reject descriptors whose entrant identity conflicts with a trusted row."""

        for descriptor in descriptors:
            entrant = self._entrant_ref(descriptor, legacy=False)
            existing = connection.execute(
                """
                SELECT identity_json, updated_at
                FROM entrants WHERE entrant_id = ?
                """,
                (entrant.entrant_id,),
            ).fetchone()
            if existing is None:
                continue
            try:
                observed_at = datetime.fromisoformat(existing["updated_at"])
            except (TypeError, ValueError) as exc:
                raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏") from exc
            if observed_at.utcoffset() is None:
                raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏")
            if (
                self._has_trusted_entrant_observation(connection, entrant.entrant_id)
                and existing["identity_json"] != entrant.identity_json
            ):
                raise StorageError(f"entrant_id {entrant.entrant_id!r} 已绑定到另一份身份元数据")

    @staticmethod
    def _upsert_entrant(
        connection: sqlite3.Connection,
        entrant: _EntrantRef,
        *,
        observed_at: datetime,
        trusted_engine: bool,
    ) -> None:
        timestamp = observed_at.astimezone(UTC).isoformat()
        existing = connection.execute(
            """
            SELECT display_name, identity_json, updated_at
            FROM entrants WHERE entrant_id = ?
            """,
            (entrant.entrant_id,),
        ).fetchone()
        if existing is None:
            connection.execute(
                """
                INSERT INTO entrants (
                    entrant_id, display_name, identity_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    entrant.entrant_id,
                    entrant.display_name,
                    entrant.identity_json,
                    timestamp,
                    timestamp,
                ),
            )
            return
        try:
            existing_observed_at = datetime.fromisoformat(existing["updated_at"])
        except (TypeError, ValueError) as exc:
            raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏") from exc
        if existing_observed_at.utcoffset() is None:
            raise StorageError(f"entrant_id {entrant.entrant_id!r} 的观察时间已损坏")
        is_newer_observation = observed_at.astimezone(UTC) > existing_observed_at.astimezone(UTC)
        has_trusted_observation = _EntrantsMixin._has_trusted_entrant_observation(
            connection,
            entrant.entrant_id,
        )
        if trusted_engine and not has_trusted_observation:
            # The first trusted engine observation establishes both identity and
            # presentation. An imported archive may carry a forged future
            # timestamp, so it must not be able to pin either one.
            connection.execute(
                """
                UPDATE entrants
                SET display_name = ?, identity_json = ?, updated_at = ?
                WHERE entrant_id = ?
                """,
                (
                    entrant.display_name,
                    entrant.identity_json,
                    timestamp,
                    entrant.entrant_id,
                ),
            )
            return
        if existing["identity_json"] != entrant.identity_json:
            raise StorageError(f"entrant_id {entrant.entrant_id!r} 已绑定到另一份身份元数据")
        if (
            trusted_engine
            and is_newer_observation
            and existing["display_name"] != entrant.display_name
        ):
            connection.execute(
                """
                UPDATE entrants SET display_name = ?, updated_at = ?
                WHERE entrant_id = ?
                """,
                (entrant.display_name, timestamp, entrant.entrant_id),
            )

    @staticmethod
    def _semantic_match_json(raw_json: str) -> str:
        try:
            archive = MatchArchive.model_validate_json(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的对局档案 JSON 已损坏") from exc
        return _canonical_json(archive.model_dump(mode="json"))

    @staticmethod
    def _semantic_series_json(raw_json: str) -> str:
        try:
            series = SeriesArchive.model_validate_json(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的系列赛档案 JSON 已损坏") from exc
        return _canonical_json(series.model_dump(mode="json"))

    @staticmethod
    def _semantic_tournament_json(raw_json: str) -> str:
        try:
            tournament = TournamentArchive.model_validate_json(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的循环赛档案 JSON 已损坏") from exc
        return _canonical_json(tournament.model_dump(mode="json"))

    @staticmethod
    def _checkpoint_config_payload(checkpoint: TournamentCheckpoint) -> dict:
        payload = checkpoint.model_dump(mode="json")
        payload.pop("completed_series")
        payload.pop("updated_at")
        return payload

    @staticmethod
    def _semantic_checkpoint_config_json(raw_json: str) -> str:
        try:
            payload = json.loads(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的循环赛 checkpoint 配置 JSON 已损坏") from exc
        if (
            not isinstance(payload, dict)
            or "completed_series" in payload
            or "updated_at" in payload
        ):
            raise StorageError("数据库中的循环赛 checkpoint 配置 JSON 已损坏")
        return _canonical_json(payload)

    @staticmethod
    def _semantic_descriptor_json(raw_json: str, *, legacy: bool) -> str:
        try:
            descriptor = json.loads(raw_json)
            normalized = normalize_player_descriptors([descriptor], legacy=legacy)[0]
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的选手描述 JSON 已损坏") from exc
        return _canonical_json(normalized)

    @staticmethod
    def _semantic_json_column(raw_json: object) -> str:
        if not isinstance(raw_json, str):
            raise StorageError("数据库中的 JSON 元数据已损坏")
        try:
            value = json.loads(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的 JSON 元数据已损坏") from exc
        return _canonical_json(value)

    @staticmethod
    def _semantic_players_json(raw_json: object, *, legacy: bool) -> str:
        if not isinstance(raw_json, str):
            raise StorageError("数据库中的选手 JSON 元数据已损坏")
        try:
            players = normalize_player_descriptors(json.loads(raw_json), legacy=legacy)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的选手 JSON 元数据已损坏") from exc
        return _canonical_json(players)

    @staticmethod
    def _timestamp_matches(raw_timestamp: object, expected: datetime) -> bool:
        if not isinstance(raw_timestamp, str):
            return False
        try:
            observed = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            return False
        return observed.utcoffset() is not None and observed.astimezone(UTC) == expected.astimezone(
            UTC
        )

    @staticmethod
    def _finite_database_float(value: object) -> float:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite SQLite number")
        return number

    def _verify_match_metadata(
        self,
        row: sqlite3.Row,
        archive: MatchArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        payload = archive.model_dump(mode="json")
        try:
            stored_players = self._semantic_players_json(
                row["players_json"], legacy=archive.schema_version == 1
            )
            stored_scores = self._semantic_json_column(row["scores_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 match_id {archive.match_id!r} 的反规范化元数据已损坏"
            ) from exc
        if (
            row["match_id"] != archive.match_id
            or row["schema_version"] != archive.schema_version
            or row["game"] != archive.game
            or row["seed"] != archive.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_scores != _canonical_json(payload["scores"])
            or not self._timestamp_matches(row["started_at"], archive.started_at)
            or not self._timestamp_matches(row["finished_at"], archive.finished_at)
            or row["archive_source"] != archive.source
            or row["rating_source"] != rating_source
            or row["rated"] != int(rated)
            or row["rating_policy"] != rating_policy
        ):
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的反规范化元数据已损坏")

    def _verify_series_metadata(
        self,
        row: sqlite3.Row,
        series: SeriesArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        payload = series.model_dump(mode="json")
        try:
            stored_players = self._semantic_players_json(
                row["players_json"], legacy=series.schema_version == 1
            )
            stored_points = self._semantic_json_column(row["points_json"])
        except StorageError as exc:
            raise StorageError(
                f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏"
            ) from exc
        if (
            row["series_id"] != series.series_id
            or row["schema_version"] != series.schema_version
            or row["game"] != series.game
            or row["seed"] != series.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_points != _canonical_json(payload["points"])
            or not self._timestamp_matches(row["started_at"], series.started_at)
            or not self._timestamp_matches(row["finished_at"], series.finished_at)
            or row["archive_source"] != series.source
            or row["rating_source"] != rating_source
            or row["rated"] != int(rated)
            or row["rating_policy"] != rating_policy
        ):
            raise StorageError(f"数据库中 series_id {series.series_id!r} 的反规范化元数据已损坏")

    def _verify_tournament_metadata(
        self,
        row: sqlite3.Row,
        tournament: TournamentArchive,
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        payload = tournament.model_dump(mode="json")
        try:
            stored_players = self._semantic_players_json(row["players_json"], legacy=False)
            stored_points = self._semantic_json_column(row["points_json"])
            stored_k_factor = (
                None if row["k_factor"] is None else self._finite_database_float(row["k_factor"])
            )
        except (StorageError, TypeError, ValueError) as exc:
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的反规范化元数据已损坏"
            ) from exc
        expected_k_factor = K_FACTOR if rated else None
        if (
            row["tournament_id"] != tournament.tournament_id
            or row["schema_version"] != tournament.schema_version
            or row["format"] != tournament.format
            or row["pairing_policy"] != tournament.pairing_policy
            or row["seed_policy"] != tournament.seed_policy
            or row["game"] != tournament.game
            or row["seed"] != tournament.seed
            or stored_players != _canonical_json(payload["players"])
            or stored_points != _canonical_json(payload["points"])
            or row["pairing_count"] != len(tournament.pairings)
            or row["rating_policy"] != rating_policy
            or stored_k_factor != expected_k_factor
            or not self._timestamp_matches(row["started_at"], tournament.started_at)
            or not self._timestamp_matches(row["finished_at"], tournament.finished_at)
            or row["archive_source"] != tournament.source
            or row["rating_source"] != rating_source
            or row["rated"] != int(rated)
        ):
            raise StorageError(
                f"数据库中 tournament_id {tournament.tournament_id!r} 的反规范化元数据已损坏"
            )

    def _verify_match_players(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
    ) -> None:
        rows = connection.execute(
            """
            SELECT position, player, entrant_id, display_name, descriptor_json, score
            FROM match_players
            WHERE match_id = ?
            ORDER BY position
            """,
            (archive.match_id,),
        ).fetchall()
        payload = archive.model_dump(mode="json")
        if len(rows) != len(entrants) or [row["position"] for row in rows] != list(
            range(len(entrants))
        ):
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的选手索引已损坏")
        legacy = archive.schema_version == 1
        for row, entrant, descriptor in zip(rows, entrants, payload["players"]):
            try:
                stored_descriptor = self._semantic_descriptor_json(
                    row["descriptor_json"], legacy=legacy
                )
                stored_score = self._finite_database_float(row["score"])
            except (StorageError, TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 match_id {archive.match_id!r} 的选手索引已损坏"
                ) from exc
            if (
                row["player"] != entrant.display_name
                or row["entrant_id"] != entrant.entrant_id
                or row["display_name"] != entrant.display_name
                or stored_descriptor != _canonical_json(descriptor)
                or stored_score != archive.scores[entrant.display_name]
            ):
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的选手索引已损坏")

    def _verify_match_history(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
        *,
        rated: bool,
    ) -> None:
        rows = connection.execute(
            """
            SELECT rating_scope, game, entrant_id, display_name,
                   opponent_entrant_id, opponent_display_name, outcome,
                   rating_before, rating_after, created_at
            FROM rating_history
            WHERE match_id = ?
            """,
            (archive.match_id,),
        ).fetchall()
        if not rated:
            if rows:
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的未计分状态已损坏")
            return
        if len(entrants) != 2 or len(rows) != 4:
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")
        history = {(row["rating_scope"], row["game"], row["entrant_id"]): row for row in rows}
        if len(history) != len(rows):
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")
        player_a, player_b = entrants
        score_a = archive.scores[player_a.display_name]
        score_b = archive.scores[player_b.display_name]
        outcome_a = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
        for rating_scope, game_key in (("overall", ""), ("game", archive.game)):
            row_a = history.get((rating_scope, game_key, player_a.entrant_id))
            row_b = history.get((rating_scope, game_key, player_b.entrant_id))
            if row_a is None or row_b is None:
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")
            try:
                before_a = self._finite_database_float(row_a["rating_before"])
                before_b = self._finite_database_float(row_b["rating_before"])
                stored_outcome_a = self._finite_database_float(row_a["outcome"])
                stored_outcome_b = self._finite_database_float(row_b["outcome"])
                stored_after_a = self._finite_database_float(row_a["rating_after"])
                stored_after_b = self._finite_database_float(row_b["rating_after"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏"
                ) from exc
            after_a, after_b = update_ratings(before_a, before_b, outcome_a)
            if (
                row_a["display_name"] != player_a.display_name
                or row_a["opponent_entrant_id"] != player_b.entrant_id
                or row_a["opponent_display_name"] != player_b.display_name
                or stored_outcome_a != outcome_a
                or stored_after_a != after_a
                or not self._timestamp_matches(row_a["created_at"], archive.finished_at)
                or row_b["display_name"] != player_b.display_name
                or row_b["opponent_entrant_id"] != player_a.entrant_id
                or row_b["opponent_display_name"] != player_a.display_name
                or stored_outcome_b != 1.0 - outcome_a
                or stored_after_b != after_b
                or not self._timestamp_matches(row_b["created_at"], archive.finished_at)
            ):
                raise StorageError(f"数据库中 match_id {archive.match_id!r} 的 ELO 历史已损坏")

    def _verify_existing_match(
        self,
        connection: sqlite3.Connection,
        metadata_row: sqlite3.Row,
        archive: MatchArchive,
        entrants: list[_EntrantRef],
        *,
        rating_source: str,
        rated: bool,
        rating_policy: str,
    ) -> None:
        self._verify_match_metadata(
            metadata_row,
            archive,
            rating_source=rating_source,
            rated=rated,
            rating_policy=rating_policy,
        )
        self._verify_match_players(connection, archive, entrants)
        self._verify_match_history(connection, archive, entrants, rated=rated)

    def _verify_existing_tournament_child(
        self,
        connection: sqlite3.Connection,
        *,
        requested_rating_source: RatingSource,
        series_id: str | None = None,
        match_id: str | None = None,
    ) -> TournamentSaveResult | None:
        if (series_id is None) == (match_id is None):
            raise ValueError("series_id 与 match_id 必须且只能提供一个")
        if series_id is not None:
            row = connection.execute(
                """
                SELECT ta.tournament_id AS stored_tournament_id,
                       ta.tournament_json, ta.archive_source, ta.rating_source,
                       ta.rated, ta.rating_policy, ta.pairing_count
                FROM tournament_pairings AS tp
                JOIN tournament_archives AS ta
                  ON ta.tournament_id = tp.tournament_id
                WHERE tp.series_id = ?
                """,
                (series_id,),
            ).fetchone()
            collision_error = SeriesIdCollisionError
            identifier = series_id
            identifier_name = "series_id"
        else:
            row = connection.execute(
                """
                SELECT ta.tournament_id AS stored_tournament_id,
                       ta.tournament_json, ta.archive_source, ta.rating_source,
                       ta.rated, ta.rating_policy, ta.pairing_count
                FROM series_matches AS sm
                JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
                JOIN tournament_archives AS ta
                  ON ta.tournament_id = tp.tournament_id
                WHERE sm.match_id = ?
                """,
                (match_id,),
            ).fetchone()
            collision_error = MatchIdCollisionError
            identifier = match_id
            identifier_name = "match_id"
        if row is None:
            return None
        try:
            tournament = TournamentArchive.model_validate_json(row["tournament_json"])
            tournament, _ = self._validate_tournament(tournament)
        except (TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 {identifier_name} {identifier!r} 所属循环赛档案已损坏"
            ) from exc
        if tournament.tournament_id != row["stored_tournament_id"]:
            raise StorageError(f"数据库中 {identifier_name} {identifier!r} 所属循环赛档案已损坏")
        stored_rated = bool(row["rated"])
        expected_rated = (
            row["rating_source"] == "engine" and row["archive_source"] == "local_engine"
        )
        expected_policy = "elo_tournament_batch_v1" if stored_rated else "unrated"
        if stored_rated != expected_rated or row["rating_policy"] != expected_policy:
            raise StorageError(
                f"数据库中 {identifier_name} {identifier!r} 所属循环赛计分状态已损坏"
            )
        if requested_rating_source == "engine" and row["rating_source"] != "engine":
            raise collision_error(
                f"{identifier_name} {identifier!r} 已作为未计分循环赛子记录存档，"
                "不能通过幂等重存升级"
            )
        self._verify_existing_tournament(
            connection,
            tournament,
            rating_source=row["rating_source"],
            rated=stored_rated,
            rating_policy=row["rating_policy"],
        )
        return TournamentSaveResult(
            inserted=False,
            rated=stored_rated,
            pairing_count=row["pairing_count"],
            match_count=row["pairing_count"] * 2,
        )

    def _verify_existing_series_leg(
        self,
        connection: sqlite3.Connection,
        archive: MatchArchive,
        *,
        requested_rating_source: RatingSource,
    ) -> SaveResult | None:
        tournament_result = self._verify_existing_tournament_child(
            connection,
            requested_rating_source=requested_rating_source,
            match_id=archive.match_id,
        )
        if tournament_result is not None:
            return SaveResult(inserted=False, rated=tournament_result.rated)
        row = connection.execute(
            """
            SELECT sa.series_id AS stored_series_id,
                   sa.series_json, sa.archive_source, sa.rating_source,
                   sa.rated, sa.rating_policy
            FROM series_matches AS sm
            JOIN series_archives AS sa ON sa.series_id = sm.series_id
            WHERE sm.match_id = ?
            """,
            (archive.match_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            series = SeriesArchive.model_validate_json(row["series_json"])
            series, _ = self._validate_series(series)
        except (TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 match_id {archive.match_id!r} 所属系列赛档案已损坏"
            ) from exc
        if series.series_id != row["stored_series_id"]:
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 所属系列赛档案已损坏")
        stored_rated = bool(row["rated"])
        expected_policy = "elo_batch_v1" if stored_rated else "unrated"
        expected_rated = row["rating_source"] == "engine" and row["archive_source"] in (
            "local_engine",
            "legacy",
        )
        if row["rating_policy"] != expected_policy or stored_rated != expected_rated:
            raise StorageError(f"数据库中 match_id {archive.match_id!r} 所属系列赛计分状态已损坏")
        if row["archive_source"] != archive.source or (
            requested_rating_source == "engine" and row["rating_source"] != "engine"
        ):
            raise MatchIdCollisionError(
                f"match_id {archive.match_id!r} 已以不同来源或计分策略存档，不能通过幂等重存升级"
            )
        self._verify_existing_series(
            connection,
            series,
            row["rating_policy"],
            rated=stored_rated,
            archive_source=row["archive_source"],
            rating_source=row["rating_source"],
        )
        self._verify_top_level_rating_operation(
            connection,
            rated=stored_rated,
            series_id=series.series_id,
        )
        return SaveResult(inserted=False, rated=stored_rated)

    def save_match(
        self,
        archive: MatchArchive,
        *,
        rating_source: RatingSource = "imported",
    ) -> SaveResult:
        """Persist one completed match, rating only trusted local-engine archives.

        Re-saving an identical ``match_id`` is an idempotent no-op.  Reusing an
        id for different content raises :class:`MatchIdCollisionError`.
        ``rating_source="imported"`` is the safe default and never changes ELO.
        Matches with any player count other than two are also archived but unrated.
        """

        rating_source = self._validate_rating_source(rating_source)
        entrants = self._validate_archive(archive)
        trusted_engine = (
            rating_source == "engine"
            and archive.schema_version == 2
            and archive.source == "local_engine"
        )
        rated = trusted_engine and len(entrants) == 2
        rating_policy = "elo_v1" if rated else "unrated"
        archive_payload, archive_json = self._serialize_archive(archive)
        semantic_archive_json = _canonical_json(archive.model_dump(mode="json"))

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            checkpoint_owner = connection.execute(
                """
                SELECT tcs.tournament_id
                FROM tournament_checkpoint_series AS tcs
                JOIN tournament_checkpoints AS tc
                  ON tc.tournament_id = tcs.tournament_id
                WHERE tc.status = 'in_progress'
                  AND (tcs.match_1_id = ? OR tcs.match_2_id = ?)
                """,
                (archive.match_id, archive.match_id),
            ).fetchone()
            if checkpoint_owner is not None:
                raise MatchIdCollisionError(
                    f"match_id {archive.match_id!r} 已由进行中的循环赛 checkpoint 保留"
                )
            championship_owner = connection.execute(
                """
                SELECT ccs.championship_id
                FROM championship_checkpoint_series AS ccs
                JOIN championship_checkpoints AS cc
                  ON cc.championship_id = ccs.championship_id
                WHERE cc.status = 'in_progress'
                  AND (ccs.match_1_id = ? OR ccs.match_2_id = ?)
                """,
                (archive.match_id, archive.match_id),
            ).fetchone()
            if championship_owner is not None:
                raise MatchIdCollisionError(
                    f"match_id {archive.match_id!r} 已由进行中的锦标赛 checkpoint 保留"
                )
            existing = connection.execute(
                """
                SELECT match_id, schema_version, game, seed, players_json, scores_json,
                       started_at, finished_at, archive_json, archive_source,
                       rating_source, rated, rating_policy
                FROM matches WHERE match_id = ?
                """,
                (archive.match_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_json = self._semantic_match_json(existing["archive_json"])
                except StorageError as exc:
                    raise StorageError(
                        f"数据库中 match_id {archive.match_id!r} 的档案 JSON 已损坏"
                    ) from exc
                if existing_json != semantic_archive_json:
                    raise MatchIdCollisionError(
                        f"match_id {archive.match_id!r} 已对应另一份对局档案"
                    )
                series_result = self._verify_existing_series_leg(
                    connection,
                    archive,
                    requested_rating_source=rating_source,
                )
                if series_result is not None:
                    connection.commit()
                    return series_result
                stored_rated = bool(existing["rated"])
                expected_stored_policy = "elo_v1" if stored_rated else "unrated"
                expected_stored_rated = existing["rating_source"] == "engine" and (
                    (archive.schema_version == 1 and archive.source == "legacy")
                    or (
                        archive.schema_version == 2
                        and archive.source == "local_engine"
                        and len(entrants) == 2
                    )
                )
                if (
                    existing["rating_policy"] != expected_stored_policy
                    or stored_rated != expected_stored_rated
                    or (stored_rated and existing["rating_source"] != "engine")
                    or (
                        stored_rated
                        and existing["archive_source"] not in ("local_engine", "legacy")
                    )
                    or (stored_rated and len(entrants) != 2)
                ):
                    raise StorageError(
                        f"数据库中 match_id {archive.match_id!r} 的计分来源或策略已损坏"
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
                    archive.source == "legacy"
                    and existing["rating_source"] == "engine"
                    and rating_source == "engine"
                )
                if existing["archive_source"] != archive.source or not (
                    read_only_downgrade or exact_policy_match or historical_engine_repeat
                ):
                    raise MatchIdCollisionError(
                        f"match_id {archive.match_id!r} 已以不同来源或计分策略存档，"
                        "不能通过幂等重存升级"
                    )
                self._verify_existing_match(
                    connection,
                    existing,
                    archive,
                    entrants,
                    rating_source=existing["rating_source"],
                    rated=stored_rated,
                    rating_policy=existing["rating_policy"],
                )
                self._verify_top_level_rating_operation(
                    connection,
                    rated=stored_rated,
                    match_id=archive.match_id,
                )
                connection.commit()
                return SaveResult(inserted=False, rated=stored_rated)

            for entrant in entrants:
                self._upsert_entrant(
                    connection,
                    entrant,
                    observed_at=archive.started_at,
                    trusted_engine=trusted_engine,
                )

            self._insert_match(
                connection,
                archive,
                entrants,
                archive_payload,
                archive_json,
                rating_source=rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )

            changes: list[RatingChange] = []
            if rated:
                self._record_rating_operation(connection, match_id=archive.match_id)
                score_a = archive.scores[entrants[0].display_name]
                score_b = archive.scores[entrants[1].display_name]
                outcome_a = 1.0 if score_a > score_b else 0.0 if score_a < score_b else 0.5
                changes.extend(
                    self._record_ratings(
                        connection, archive, entrants[0], entrants[1], outcome_a, None
                    )
                )
                changes.extend(
                    self._record_ratings(
                        connection,
                        archive,
                        entrants[0],
                        entrants[1],
                        outcome_a,
                        archive.game,
                    )
                )

            connection.commit()
            return SaveResult(
                inserted=True,
                rated=rated,
                rating_changes=tuple(changes),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


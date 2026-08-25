"""_ChampionshipCheckpointMixin mixin for SQLiteStore.

A championship checkpoint is the resumable, non-rating state of a knockout
championship.  It reuses the established round-robin discipline: an empty
checkpoint is created first, a cross-process runner lease fences one writer,
and completed series are appended one whole round at a time under that lease.
The formal, unrated ``championship_archives`` rows and their child series are
written once when the checkpoint finalizes.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from llmolympic.core._storage_types import (
    DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    SQLITE_INT_MAX,
    ChampionshipCheckpointCollisionError,
    ChampionshipCheckpointSaveResult,
    ChampionshipRunnerClaim,
    ChampionshipRunnerLease,
    ChampionshipRunnerLeaseBusyError,
    ChampionshipRunnerLeaseLostError,
    MatchIdCollisionError,
    SaveResult,
    SeriesIdCollisionError,
    StorageError,
    _canonical_json,
    _EntrantRef,
    _runner_lease_token_digest,
    _validate_runner_lease_seconds,
)
from llmolympic.core.championship import (
    CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION,
    ChampionshipArchive,
    ChampionshipCheckpoint,
)
from llmolympic.core.series import SeriesArchive


class _ChampionshipCheckpointMixin:
    @staticmethod
    def _championship_checkpoint_config_payload(
        checkpoint: ChampionshipCheckpoint,
    ) -> dict:
        payload = checkpoint.model_dump(mode="json")
        payload.pop("completed_series")
        payload.pop("updated_at")
        return payload

    @staticmethod
    def _semantic_championship_checkpoint_config_json(raw_json: str) -> str:
        try:
            payload = json.loads(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的锦标赛 checkpoint 配置 JSON 已损坏") from exc
        if (
            not isinstance(payload, dict)
            or "completed_series" in payload
            or "updated_at" in payload
        ):
            raise StorageError("数据库中的锦标赛 checkpoint 配置 JSON 已损坏")
        return _canonical_json(payload)

    def _validate_championship_checkpoint(
        self, checkpoint: ChampionshipCheckpoint
    ) -> tuple[ChampionshipCheckpoint, tuple[_EntrantRef, ...]]:
        if checkpoint.schema_version != CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION:
            raise StorageError(
                f"不支持锦标赛 checkpoint 版本 {checkpoint.schema_version}；"
                f"当前支持 {CHAMPIONSHIP_CHECKPOINT_SCHEMA_VERSION}"
            )
        try:
            validated = ChampionshipCheckpoint.model_validate(
                checkpoint.model_dump(mode="python")
            )
        except (TypeError, ValueError) as exc:
            raise StorageError(f"锦标赛 checkpoint 无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False) for descriptor in validated.players
        )
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("锦标赛 checkpoint 中的 entrant_id 必须唯一")
        for series in validated.completed_series:
            self._validate_series(series)
        return validated, entrants

    @staticmethod
    def _championship_runner_lease_handle(
        lease: ChampionshipRunnerLease,
        championship_id: str,
    ) -> bytes:
        if not isinstance(lease, ChampionshipRunnerLease):
            raise TypeError("必须提供 ChampionshipRunnerLease")
        if lease.championship_id != championship_id:
            raise ValueError("runner lease 不属于该锦标赛")
        if (
            isinstance(lease.generation, bool)
            or not isinstance(lease.generation, int)
            or lease.generation < 1
        ):
            raise ValueError("runner lease generation 无效")
        return _runner_lease_token_digest(lease.token)

    def _load_championship_runner_lease(
        self,
        connection: sqlite3.Connection,
        championship_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """
            SELECT generation, token_digest, acquired_at_epoch,
                   renewed_at_epoch, expires_at_epoch
            FROM championship_runner_leases
            WHERE championship_id = ?
            """,
            (championship_id,),
        ).fetchone()
        if row is None:
            return None

        generation = row["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise StorageError("锦标赛 runner lease 已损坏")

        raw_digest = row["token_digest"]
        timestamps = (
            row["acquired_at_epoch"],
            row["renewed_at_epoch"],
            row["expires_at_epoch"],
        )
        if raw_digest is None:
            if any(value is not None for value in timestamps):
                raise StorageError("锦标赛 runner lease 已损坏")
            digest = None
        else:
            if not isinstance(raw_digest, (bytes, bytearray, memoryview)):
                raise StorageError("锦标赛 runner lease 已损坏")
            digest = bytes(raw_digest)
            if len(digest) != 32 or any(
                isinstance(value, bool) or not isinstance(value, int) for value in timestamps
            ):
                raise StorageError("锦标赛 runner lease 已损坏")
            acquired_at, renewed_at, expires_at = timestamps
            if not acquired_at <= renewed_at < expires_at:
                raise StorageError("锦标赛 runner lease 已损坏")

        return {
            "generation": generation,
            "token_digest": digest,
            "acquired_at_epoch": timestamps[0],
            "renewed_at_epoch": timestamps[1],
            "expires_at_epoch": timestamps[2],
        }

    def _require_active_championship_runner(
        self,
        connection: sqlite3.Connection,
        championship_id: str,
        lease: ChampionshipRunnerLease | None,
        *,
        renew_seconds: int | None = None,
    ) -> ChampionshipRunnerLease:
        if lease is None:
            raise ChampionshipRunnerLeaseLostError(
                "锦标赛 checkpoint 写入需要有效的 runner lease"
            )
        digest = self._championship_runner_lease_handle(lease, championship_id)
        state = self._load_championship_runner_lease(connection, championship_id)
        now = self._database_epoch(connection)
        if (
            state is None
            or state["token_digest"] is None
            or state["generation"] != lease.generation
            or state["token_digest"] != digest
            or state["expires_at_epoch"] is None
            or state["expires_at_epoch"] <= now
        ):
            raise ChampionshipRunnerLeaseLostError(
                "锦标赛 runner lease 已过期、释放或被其他执行者接管"
            )

        if renew_seconds is None:
            return ChampionshipRunnerLease(
                championship_id=championship_id,
                generation=state["generation"],
                token=lease.token,
                acquired_at_epoch=state["acquired_at_epoch"],
                renewed_at_epoch=state["renewed_at_epoch"],
                expires_at_epoch=state["expires_at_epoch"],
            )

        duration = _validate_runner_lease_seconds(renew_seconds)
        renewed_at = max(now, state["renewed_at_epoch"])
        expires_at = max(now + duration, renewed_at + 1)
        updated = connection.execute(
            """
            UPDATE championship_runner_leases
            SET renewed_at_epoch = ?, expires_at_epoch = ?
            WHERE championship_id = ? AND generation = ? AND token_digest = ?
            """,
            (
                renewed_at,
                expires_at,
                championship_id,
                lease.generation,
                digest,
            ),
        )
        if updated.rowcount != 1:
            raise ChampionshipRunnerLeaseLostError("锦标赛 runner lease 在续租时发生并发变化")
        return ChampionshipRunnerLease(
            championship_id=championship_id,
            generation=state["generation"],
            token=lease.token,
            acquired_at_epoch=state["acquired_at_epoch"],
            renewed_at_epoch=renewed_at,
            expires_at_epoch=expires_at,
        )

    def claim_championship_runner(
        self,
        championship_id: str,
        *,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> ChampionshipRunnerClaim:
        """Atomically reload and claim one in-progress championship checkpoint."""

        if not isinstance(championship_id, str) or not championship_id.strip():
            raise ValueError("championship_id 必须是非空字符串")
        duration = _validate_runner_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            loaded = self._load_championship_checkpoint(connection, championship_id)
            if loaded is None:
                raise StorageError(f"锦标赛 checkpoint {championship_id!r} 不存在")
            checkpoint, status = loaded
            if status != "in_progress":
                raise StorageError(f"锦标赛 checkpoint {championship_id!r} 已封存")

            state = self._load_championship_runner_lease(connection, championship_id)
            now = self._database_epoch(connection)
            if (
                state is not None
                and state["token_digest"] is not None
                and state["expires_at_epoch"] is not None
                and state["expires_at_epoch"] > now
            ):
                raise ChampionshipRunnerLeaseBusyError(
                    "锦标赛 checkpoint 正由另一个执行者运行；请稍后重试"
                )

            if state is not None and state["generation"] >= SQLITE_INT_MAX:
                raise StorageError("锦标赛 runner lease generation 已达到 SQLite 整数上限")
            generation = 1 if state is None else state["generation"] + 1
            token = secrets.token_hex(32)
            digest = _runner_lease_token_digest(token)
            expires_at = now + duration
            if state is None:
                connection.execute(
                    """
                    INSERT INTO championship_runner_leases (
                        championship_id, generation, token_digest,
                        acquired_at_epoch, renewed_at_epoch, expires_at_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (championship_id, generation, digest, now, now, expires_at),
                )
            else:
                updated = connection.execute(
                    """
                    UPDATE championship_runner_leases
                    SET generation = ?, token_digest = ?, acquired_at_epoch = ?,
                        renewed_at_epoch = ?, expires_at_epoch = ?
                    WHERE championship_id = ? AND generation = ?
                    """,
                    (
                        generation,
                        digest,
                        now,
                        now,
                        expires_at,
                        championship_id,
                        state["generation"],
                    ),
                )
                if updated.rowcount != 1:
                    raise ChampionshipRunnerLeaseBusyError(
                        "锦标赛 runner lease 在领取时发生并发变化"
                    )
            connection.commit()
            return ChampionshipRunnerClaim(
                checkpoint=checkpoint,
                lease=ChampionshipRunnerLease(
                    championship_id=championship_id,
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

    def renew_championship_runner(
        self,
        lease: ChampionshipRunnerLease,
        *,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> ChampionshipRunnerLease:
        """Extend one active championship lease without reviving an expired one."""

        if not isinstance(lease, ChampionshipRunnerLease):
            raise TypeError("必须提供 ChampionshipRunnerLease")
        duration = _validate_runner_lease_seconds(lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            status_row = connection.execute(
                "SELECT status FROM championship_checkpoints WHERE championship_id = ?",
                (lease.championship_id,),
            ).fetchone()
            if status_row is None or status_row["status"] != "in_progress":
                raise ChampionshipRunnerLeaseLostError(
                    "锦标赛 runner lease 对应的 checkpoint 已不存在或已封存"
                )
            renewed = self._require_active_championship_runner(
                connection,
                lease.championship_id,
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

    def release_championship_runner(self, lease: ChampionshipRunnerLease) -> bool:
        """Release only the matching fencing generation; stale releases are no-ops."""

        if not isinstance(lease, ChampionshipRunnerLease):
            raise TypeError("必须提供 ChampionshipRunnerLease")
        digest = self._championship_runner_lease_handle(lease, lease.championship_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE championship_runner_leases
                SET token_digest = NULL, acquired_at_epoch = NULL,
                    renewed_at_epoch = NULL, expires_at_epoch = NULL
                WHERE championship_id = ? AND generation = ? AND token_digest = ?
                """,
                (lease.championship_id, lease.generation, digest),
            )
            connection.commit()
            return updated.rowcount == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _load_championship_checkpoint(
        self,
        connection: sqlite3.Connection,
        championship_id: str,
    ) -> tuple[ChampionshipCheckpoint, str] | None:
        row = connection.execute(
            """
            SELECT championship_id, schema_version, source, format,
                   pairing_policy, seed_policy, tiebreak_policy, game, seed,
                   players_json, game_config_json, schedule_json, max_attempts,
                   pairing_count, created_at, updated_at, status,
                   finalized_at, final_championship_id, config_json
            FROM championship_checkpoints
            WHERE championship_id = ?
            """,
            (championship_id,),
        ).fetchone()
        if row is None:
            return None

        series_rows = connection.execute(
            """
            SELECT pairing_number, series_id, match_1_id, match_2_id,
                   completed_at, series_json
            FROM championship_checkpoint_series
            WHERE championship_id = ?
            ORDER BY pairing_number
            """,
            (championship_id,),
        ).fetchall()
        try:
            semantic_config = self._semantic_championship_checkpoint_config_json(
                row["config_json"]
            )
            config_payload = json.loads(row["config_json"])
            completed_series: list[SeriesArchive] = []
            for expected_pairing_number, series_row in enumerate(series_rows, start=1):
                if series_row["pairing_number"] != expected_pairing_number:
                    raise StorageError("锦标赛已完成组编号不是连续前缀")
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
                    raise StorageError("锦标赛已完成双局赛索引与档案不一致")
                completed_series.append(series)

            config_payload["completed_series"] = [
                series.model_dump(mode="json") for series in completed_series
            ]
            config_payload["updated_at"] = row["updated_at"]
            checkpoint = ChampionshipCheckpoint.model_validate(config_payload)
            checkpoint, _ = self._validate_championship_checkpoint(checkpoint)

            payload = checkpoint.model_dump(mode="json")
            stored_players = self._semantic_players_json(row["players_json"], legacy=False)
            stored_game_config = self._semantic_json_column(row["game_config_json"])
            stored_schedule = self._semantic_json_column(row["schedule_json"])
            expected_config = _canonical_json(
                self._championship_checkpoint_config_payload(checkpoint)
            )
        except (KeyError, TypeError, ValueError, StorageError) as exc:
            raise StorageError(
                f"数据库中 championship_id {championship_id!r} 的 checkpoint 已损坏"
            ) from exc

        if (
            semantic_config != expected_config
            or row["championship_id"] != checkpoint.championship_id
            or row["schema_version"] != checkpoint.schema_version
            or row["source"] != checkpoint.source
            or row["format"] != checkpoint.format
            or row["pairing_policy"] != checkpoint.pairing_policy
            or row["seed_policy"] != checkpoint.seed_policy
            or row["tiebreak_policy"] != checkpoint.tiebreak_policy
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
                f"数据库中 championship_id {championship_id!r} 的 checkpoint 元数据已损坏"
            )

        status = row["status"]
        if status == "in_progress":
            if row["finalized_at"] is not None or row["final_championship_id"] is not None:
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的 checkpoint 状态已损坏"
                )
        elif status == "finalized":
            if (
                row["final_championship_id"] != championship_id
                or not checkpoint.is_complete
                or not isinstance(row["finalized_at"], str)
            ):
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的 checkpoint 状态已损坏"
                )
            try:
                finalized_at = datetime.fromisoformat(row["finalized_at"])
            except ValueError as exc:
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的 checkpoint 状态已损坏"
                ) from exc
            if finalized_at.utcoffset() is None or finalized_at.astimezone(
                UTC
            ) < checkpoint.updated_at.astimezone(UTC):
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的 checkpoint 状态已损坏"
                )
            final_row = connection.execute(
                """
                SELECT championship_json FROM championship_archives
                WHERE championship_id = ?
                """,
                (championship_id,),
            ).fetchone()
            if final_row is None:
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的正式锦标赛档案已丢失"
                )
            try:
                stored = ChampionshipArchive.model_validate_json(final_row["championship_json"])
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的正式锦标赛档案已损坏"
                ) from exc
            if stored.championship_id != championship_id:
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的正式锦标赛档案已损坏"
                )
            expected = self._championship_from_checkpoint(checkpoint)
            if _canonical_json(stored.model_dump(mode="json")) != _canonical_json(
                expected.model_dump(mode="json")
            ):
                raise StorageError(
                    f"数据库中 championship_id {championship_id!r} 的正式锦标赛档案已损坏"
                )
        else:
            raise StorageError(
                f"数据库中 championship_id {championship_id!r} 的 checkpoint 状态已损坏"
            )
        self._verify_entrant_descriptors(connection, checkpoint.players)
        return checkpoint, status

    @staticmethod
    def _insert_empty_championship_checkpoint_in_transaction(
        connection: sqlite3.Connection,
        checkpoint: ChampionshipCheckpoint,
        *,
        payload: dict,
        config_json: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO championship_checkpoints (
                championship_id, schema_version, source, format,
                pairing_policy, seed_policy, tiebreak_policy, game, seed,
                players_json, game_config_json, schedule_json, max_attempts,
                pairing_count, created_at, updated_at, status,
                finalized_at, final_championship_id, config_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                checkpoint.championship_id,
                checkpoint.schema_version,
                checkpoint.source,
                checkpoint.format,
                checkpoint.pairing_policy,
                checkpoint.seed_policy,
                checkpoint.tiebreak_policy,
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

    def save_championship_checkpoint(
        self,
        checkpoint: ChampionshipCheckpoint,
        *,
        lease: ChampionshipRunnerLease | None = None,
        lease_seconds: int = DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
    ) -> ChampionshipCheckpointSaveResult:
        """Create an empty checkpoint or append one whole round under an active lease."""

        checkpoint, _ = self._validate_championship_checkpoint(checkpoint)
        payload = checkpoint.model_dump(mode="json")
        config_json = _canonical_json(self._championship_checkpoint_config_payload(checkpoint))
        pairing_count = len(checkpoint.schedule)
        completed_count = len(checkpoint.completed_series)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._verify_entrant_descriptors(connection, checkpoint.players)
            loaded = self._load_championship_checkpoint(
                connection,
                checkpoint.championship_id,
            )
            if loaded is None:
                if completed_count:
                    raise StorageError("新锦标赛 checkpoint 必须在第一轮开始前以空进度创建")
                if lease is not None:
                    raise ValueError("新锦标赛 checkpoint 必须先创建，再领取 runner lease")
                if connection.execute(
                    "SELECT 1 FROM championship_archives WHERE championship_id = ?",
                    (checkpoint.championship_id,),
                ).fetchone():
                    raise ChampionshipCheckpointCollisionError(
                        f"championship_id {checkpoint.championship_id!r} 已有正式锦标赛档案"
                    )
                self._insert_empty_championship_checkpoint_in_transaction(
                    connection,
                    checkpoint,
                    payload=payload,
                    config_json=config_json,
                )
                connection.commit()
                return ChampionshipCheckpointSaveResult(
                    inserted=True,
                    completed_pairing_count=0,
                    pairing_count=pairing_count,
                )

            stored, status = loaded
            if status == "in_progress":
                self._require_active_championship_runner(
                    connection,
                    checkpoint.championship_id,
                    lease,
                    renew_seconds=lease_seconds,
                )
            stored_config_json = _canonical_json(
                self._championship_checkpoint_config_payload(stored)
            )
            if stored_config_json != config_json:
                raise ChampionshipCheckpointCollisionError(
                    f"championship_id {checkpoint.championship_id!r} 已对应另一份 checkpoint 配置"
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
                return ChampionshipCheckpointSaveResult(
                    inserted=False,
                    completed_pairing_count=stored_count,
                    pairing_count=pairing_count,
                )
            if status != "in_progress":
                raise ChampionshipCheckpointCollisionError(
                    f"championship_id {checkpoint.championship_id!r} 的 checkpoint 已封存"
                )

            # Append exactly one whole round: the incoming prefix must extend
            # the stored prefix by the number of series in the next unplayed
            # round.  Round sizes shrink as 2^n -> 2^(n-1) -> ... -> 1, so the
            # next round's size is derived from how many rounds are already
            # stored, not from the opening-round size.
            count = len(checkpoint.players)
            if completed_count > pairing_count or completed_count <= stored_count:
                raise ChampionshipCheckpointCollisionError(
                    "锦标赛 checkpoint 只能按整轮连续追加双局赛"
                )
            if incoming_series_json[:stored_count] != stored_series_json:
                raise ChampionshipCheckpointCollisionError(
                    "锦标赛 checkpoint 只能保留既有 prefix 并追加新双局赛"
                )
            appended = completed_count - stored_count
            round_size = count >> (stored.completed_rounds + 1)
            if appended != round_size:
                raise ChampionshipCheckpointCollisionError(
                    "锦标赛 checkpoint 只能按整轮连续追加双局赛"
                )

            new_series = checkpoint.completed_series[stored_count:]

            for series in new_series:
                if (
                    connection.execute(
                        "SELECT 1 FROM series_archives WHERE series_id = ?",
                        (series.series_id,),
                    ).fetchone()
                    or connection.execute(
                        "SELECT 1 FROM championship_checkpoint_series WHERE series_id = ?",
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
                        SELECT 1 FROM championship_checkpoint_series
                        WHERE match_1_id IN (?, ?) OR match_2_id IN (?, ?)
                        """,
                        (*match_ids, *match_ids),
                    ).fetchone()
                ):
                    raise MatchIdCollisionError("锦标赛 checkpoint 的 match_id 已存档")

            for index, series in enumerate(new_series, start=stored_count + 1):
                connection.execute(
                    """
                    INSERT INTO championship_checkpoint_series (
                        championship_id, pairing_number, series_id, match_1_id,
                        match_2_id, completed_at, series_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        checkpoint.championship_id,
                        index,
                        series.series_id,
                        series.legs[0].match_id,
                        series.legs[1].match_id,
                        series.finished_at.astimezone(UTC).isoformat(),
                        incoming_series_json[index - 1],
                    ),
                )
            updated = connection.execute(
                """
                UPDATE championship_checkpoints
                SET updated_at = ?
                WHERE championship_id = ? AND status = 'in_progress'
                """,
                (
                    checkpoint.updated_at.astimezone(UTC).isoformat(),
                    checkpoint.championship_id,
                ),
            )
            if updated.rowcount != 1:
                raise StorageError("锦标赛 checkpoint 状态发生并发变化")
            connection.commit()
            return ChampionshipCheckpointSaveResult(
                inserted=True,
                completed_pairing_count=completed_count,
                pairing_count=pairing_count,
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_championship_checkpoint(
        self,
        championship_id: str,
    ) -> ChampionshipCheckpoint | None:
        """Load and deeply validate one resumable championship checkpoint."""

        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            try:
                loaded = self._load_championship_checkpoint(connection, championship_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return None if loaded is None else loaded[0]

    def finalize_championship_checkpoint(
        self,
        championship_id: str,
        *,
        lease: ChampionshipRunnerLease | None = None,
    ) -> SaveResult:
        """Atomically promote a complete checkpoint to an unrated formal archive."""

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            loaded = self._load_championship_checkpoint(connection, championship_id)
            if loaded is None:
                raise StorageError(f"锦标赛 checkpoint {championship_id!r} 不存在")
            checkpoint, status = loaded
            if not checkpoint.is_complete:
                raise StorageError("锦标赛 checkpoint 尚未完成，不能封存")
            if status == "in_progress":
                self._require_active_championship_runner(
                    connection,
                    championship_id,
                    lease,
                    renew_seconds=DEFAULT_TOURNAMENT_RUNNER_LEASE_SECONDS,
                )
            archive = self._championship_from_checkpoint(checkpoint)
            result = self._save_championship_in_transaction(
                connection,
                archive,
                rating_source="engine",
                checkpoint_owner_id=championship_id,
            )
            if status == "in_progress":
                if not result.inserted:
                    raise StorageError("进行中的 checkpoint 未能创建正式锦标赛档案")
                finalized_at = max(
                    datetime.now(UTC),
                    checkpoint.updated_at.astimezone(UTC),
                )
                updated = connection.execute(
                    """
                    UPDATE championship_checkpoints
                    SET status = 'finalized', finalized_at = ?,
                        final_championship_id = ?
                    WHERE championship_id = ? AND status = 'in_progress'
                    """,
                    (
                        finalized_at.isoformat(),
                        championship_id,
                        championship_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise StorageError("锦标赛 checkpoint 状态发生并发变化")
                digest = self._championship_runner_lease_handle(lease, championship_id)
                deleted = connection.execute(
                    """
                    DELETE FROM championship_runner_leases
                    WHERE championship_id = ? AND generation = ? AND token_digest = ?
                    """,
                    (championship_id, lease.generation, digest),
                )
                if deleted.rowcount != 1:
                    raise ChampionshipRunnerLeaseLostError(
                        "锦标赛 runner lease 在封存时发生并发变化"
                    )
            elif result.inserted:
                raise StorageError("已封存 checkpoint 的正式锦标赛档案状态已损坏")
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _championship_from_checkpoint(
        self,
        checkpoint: ChampionshipCheckpoint,
    ) -> ChampionshipArchive:
        """Reconstruct the canonical archive, deriving the champion from the bracket."""

        from llmolympic.core.championship import _champion_index, championship_from_series

        champion_index = _champion_index(checkpoint.players, checkpoint.completed_series)
        champion = checkpoint.players[champion_index]["name"]
        return championship_from_series(
            checkpoint.players,
            checkpoint.completed_series,
            seed=checkpoint.seed,
            champion=champion,
            championship_id=checkpoint.championship_id,
            judge_panel=checkpoint.judge_panel,
        )

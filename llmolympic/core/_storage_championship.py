"""_ChampionshipMixin mixin for SQLiteStore.

A championship is a multi-player knockout bracket.  Following the established
"only two-player competitions update ELO" rule, championship archives and their
child series are persisted without rating updates: the bracket outcome is a
ranking, not a sequence of independent two-player results.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC

from llmolympic.core._storage_types import (
    RatingSource,
    SaveResult,
    SeriesIdCollisionError,
    StorageError,
    _canonical_json,
    _EntrantRef,
)
from llmolympic.core.championship import (
    CHAMPIONSHIP_SCHEMA_VERSION,
    ChampionshipArchive,
)


class _ChampionshipMixin:
    def _validate_championship(
        self, championship: ChampionshipArchive
    ) -> tuple[ChampionshipArchive, tuple[_EntrantRef, ...]]:
        if championship.schema_version != CHAMPIONSHIP_SCHEMA_VERSION:
            raise StorageError(
                f"不支持锦标赛档案版本 {championship.schema_version}；"
                f"当前支持 {CHAMPIONSHIP_SCHEMA_VERSION}"
            )
        try:
            validated = ChampionshipArchive.model_validate(
                championship.model_dump(mode="python")
            )
        except (TypeError, ValueError) as exc:
            raise StorageError(f"锦标赛档案无效：{exc}") from exc
        entrants = tuple(
            self._entrant_ref(descriptor, legacy=False)
            for descriptor in validated.players
        )
        if len({entrant.entrant_id for entrant in entrants}) != len(entrants):
            raise StorageError("锦标赛中的 entrant_id 必须唯一")
        for pairing in validated.pairings:
            self._validate_series(pairing.series)
        return validated, entrants

    def save_championship(
        self,
        championship: ChampionshipArchive,
        *,
        rating_source: RatingSource = "imported",
    ) -> SaveResult:
        """Atomically persist a complete knockout championship without rating.

        The championship and every child series/match are stored unrated; the
        bracket result is a ranking and never mutates ELO.  Child two-leg series
        are therefore persisted with ``rating_source="imported"`` even when the
        trusted engine produced the championship, because a two-player series
        with ``rating_source="engine"`` would be rated by the existing invariant.
        """

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._save_championship_in_transaction(
                connection,
                championship,
                rating_source=rating_source,
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _save_championship_in_transaction(
        self,
        connection: sqlite3.Connection,
        championship: ChampionshipArchive,
        *,
        rating_source: RatingSource,
        checkpoint_owner_id: str | None = None,
    ) -> SaveResult:
        """Persist one complete championship using the caller's active write transaction."""

        if not connection.in_transaction:
            raise StorageError("保存锦标赛需要调用方先开启 SQLite 写事务")

        rating_source = self._validate_rating_source(rating_source)
        championship, entrants = self._validate_championship(championship)
        rated = False
        rating_policy = "unrated"
        payload = championship.model_dump(mode="json")
        championship_json = _canonical_json(payload)
        semantic_json = championship_json
        pairing_count = len(championship.pairings)
        child_rating_source: RatingSource = "imported"

        existing = connection.execute(
            """
            SELECT championship_json, archive_source, rating_source, rated,
                   rating_policy
            FROM championship_archives
            WHERE championship_id = ?
            """,
            (championship.championship_id,),
        ).fetchone()
        if existing is not None:
            try:
                existing_json = self._semantic_championship_json(
                    existing["championship_json"]
                )
            except StorageError as exc:
                raise StorageError(
                    f"数据库中 championship_id {championship.championship_id!r} "
                    "的档案 JSON 已损坏"
                ) from exc
            if existing_json != semantic_json:
                raise SeriesIdCollisionError(
                    f"championship_id {championship.championship_id!r} "
                    "已对应另一份锦标赛档案"
                )
            if (
                existing["archive_source"] != championship.source
                or bool(existing["rated"]) != rated
                or existing["rating_policy"] != rating_policy
            ):
                raise StorageError(
                    f"数据库中 championship_id {championship.championship_id!r} "
                    "的来源或计分状态已损坏"
                )
            return SaveResult(inserted=False, rated=rated)

        if checkpoint_owner_id is None:
            for pairing in championship.pairings:
                series = pairing.series
                if connection.execute(
                    "SELECT 1 FROM series_archives WHERE series_id = ?",
                    (series.series_id,),
                ).fetchone():
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已存档，不能重复归入锦标赛"
                    )
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
                championship_owner = connection.execute(
                    """
                    SELECT ccs.championship_id
                    FROM championship_checkpoint_series AS ccs
                    JOIN championship_checkpoints AS cc
                      ON cc.championship_id = ccs.championship_id
                    WHERE cc.status = 'in_progress' AND ccs.series_id = ?
                    """,
                    (series.series_id,),
                ).fetchone()
                if championship_owner is not None:
                    raise SeriesIdCollisionError(
                        f"series_id {series.series_id!r} 已由进行中的锦标赛 checkpoint 保留"
                    )
                for leg in series.legs:
                    if connection.execute(
                        "SELECT 1 FROM matches WHERE match_id = ?", (leg.match_id,)
                    ).fetchone():
                        raise SeriesIdCollisionError(
                            f"match_id {leg.match_id!r} 已存档，不能重复归入锦标赛"
                        )
                    if connection.execute(
                        """
                        SELECT 1 FROM championship_checkpoint_series
                        WHERE match_1_id = ? OR match_2_id = ?
                        """,
                        (leg.match_id, leg.match_id),
                    ).fetchone():
                        raise SeriesIdCollisionError(
                            f"match_id {leg.match_id!r} 已由进行中的锦标赛 checkpoint 保留"
                        )

        for entrant in entrants:
            self._upsert_entrant(
                connection,
                entrant,
                observed_at=championship.started_at,
                trusted_engine=False,
            )

        connection.execute(
            """
            INSERT INTO championship_archives (
                championship_id, schema_version, format, pairing_policy,
                seed_policy, tiebreak_policy, game, seed, players_json,
                champion, pairing_count, rating_policy, k_factor,
                started_at, finished_at, archive_source, rating_source,
                rated, championship_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                championship.championship_id,
                championship.schema_version,
                championship.format,
                championship.pairing_policy,
                championship.seed_policy,
                championship.tiebreak_policy,
                championship.game,
                championship.seed,
                _canonical_json(payload["players"]),
                championship.champion,
                pairing_count,
                rating_policy,
                None,
                championship.started_at.astimezone(UTC).isoformat(),
                championship.finished_at.astimezone(UTC).isoformat(),
                championship.source,
                rating_source,
                int(rated),
                championship_json,
            ),
        )

        standings = {standing.entrant_id: standing for standing in championship.standings}
        for position, (entrant, descriptor) in enumerate(
            zip(entrants, payload["players"])
        ):
            standing = standings[entrant.entrant_id]
            connection.execute(
                """
                INSERT INTO championship_entrants (
                    championship_id, position, entrant_id, display_name,
                    descriptor_json, rank, series_played, series_wins,
                    series_draws, series_losses, games_played, wins, draws,
                    losses, technical_losses
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    championship.championship_id,
                    position,
                    entrant.entrant_id,
                    entrant.display_name,
                    _canonical_json(descriptor),
                    standing.rank,
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

        for pairing in championship.pairings:
            series = pairing.series
            self._insert_series_structure(
                connection,
                series,
                rating_source=child_rating_source,
                rated=rated,
                rating_policy=rating_policy,
            )
            first_index = pairing.first_index
            second_index = pairing.second_index
            if first_index is None or second_index is None:
                raise StorageError("锦标赛配对的选手索引必须已解析")
            player_a = entrants[first_index]
            player_b = entrants[second_index]
            connection.execute(
                """
                INSERT INTO championship_pairings (
                    championship_id, round_number, pairing_number, series_id,
                    entrant_a_id, entrant_b_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    championship.championship_id,
                    pairing.round_number,
                    pairing.pairing_number,
                    series.series_id,
                    player_a.entrant_id,
                    player_b.entrant_id,
                ),
            )

        return SaveResult(inserted=True, rated=rated)

    def get_championship(self, championship_id: str) -> ChampionshipArchive | None:
        """Load the complete knockout archive for ``championship_id``."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT championship_json FROM championship_archives
                WHERE championship_id = ?
                """,
                (championship_id,),
            ).fetchone()
        return (
            None
            if row is None
            else ChampionshipArchive.model_validate_json(row["championship_json"])
        )

    @staticmethod
    def _semantic_championship_json(raw_json: str) -> str:
        try:
            championship = ChampionshipArchive.model_validate_json(raw_json)
        except (TypeError, ValueError) as exc:
            raise StorageError("数据库中的锦标赛档案 JSON 已损坏") from exc
        return _canonical_json(championship.model_dump(mode="json"))

"""_SchemaMixin mixin for SQLiteStore."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from functools import lru_cache

from llmolympic.core._storage_types import (
    _LEGACY_REQUIRED_COLUMNS,
    _LEGACY_SERIES_REQUIRED_COLUMNS,
    _V3_REQUIRED_COLUMNS,
    _V4_REQUIRED_COLUMNS,
    _V5_REQUIRED_COLUMNS,
    _V6_REQUIRED_COLUMNS,
    _V9_REQUIRED_COLUMNS,
    _V10_REQUIRED_COLUMNS,
    SQLITE_INT_MAX,
    RatingSource,
    StorageError,
    _canonical_json,
)
from llmolympic.core.archive import legacy_entrant_id
from llmolympic.core.sqlite_schema import (
    SchemaManifest,
    SchemaManifestError,
    TableSpec,
    introspect_schema,
    verify_schema_manifest,
)


class _SchemaMixin:
    @staticmethod
    def _verify_legacy_schema(connection: sqlite3.Connection, *, include_series: bool) -> None:
        required_tables = dict(_LEGACY_REQUIRED_COLUMNS)
        if include_series:
            required_tables.update(_LEGACY_SERIES_REQUIRED_COLUMNS)
        for table, required in required_tables.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = required - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise StorageError(f"SQLite 数据库结构不完整：{table} 缺少 {names}")

    @staticmethod
    def _create_matches_table(
        connection: sqlite3.Connection,
        *,
        table_name: str = "matches",
        if_not_exists: bool = True,
    ) -> None:
        if table_name not in {"matches", "matches_v7_canonical"}:
            raise ValueError("不安全的 matches 表名")
        conditional = "IF NOT EXISTS " if if_not_exists else ""
        connection.execute(
            f"""
            CREATE TABLE {conditional}{table_name} (
                match_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                scores_json TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_source TEXT NOT NULL
                    CHECK (archive_source IN ('local_engine', 'external', 'legacy')),
                rating_source TEXT NOT NULL
                    CHECK (rating_source IN ('engine', 'imported')),
                rated INTEGER NOT NULL CHECK (rated IN (0, 1)),
                rating_policy TEXT NOT NULL,
                archive_json TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_match_indexes(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS matches_finished_at_idx ON matches(finished_at DESC)"
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS matches_game_finished_at_idx
            ON matches(game, finished_at DESC)
            """
        )

    @staticmethod
    def _create_series_archives_table(
        connection: sqlite3.Connection,
        *,
        table_name: str = "series_archives",
        if_not_exists: bool = True,
    ) -> None:
        if table_name not in {"series_archives", "series_archives_v7_canonical"}:
            raise ValueError("不安全的 series_archives 表名")
        conditional = "IF NOT EXISTS " if if_not_exists else ""
        connection.execute(
            f"""
            CREATE TABLE {conditional}{table_name} (
                series_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                points_json TEXT NOT NULL,
                rating_policy TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_source TEXT NOT NULL
                    CHECK (archive_source IN ('local_engine', 'external', 'legacy')),
                rating_source TEXT NOT NULL
                    CHECK (rating_source IN ('engine', 'imported')),
                rated INTEGER NOT NULL CHECK (rated IN (0, 1)),
                series_json TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_series_archive_indexes(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS series_archives_finished_at_idx
            ON series_archives(finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS series_archives_game_finished_at_idx
            ON series_archives(game, finished_at DESC)
            """
        )

    @staticmethod
    def _create_base_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS entrants (
                entrant_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        _SchemaMixin._create_matches_table(connection)
        _SchemaMixin._create_match_indexes(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS match_players (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                player TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (match_id, position)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS match_players_player_idx
            ON match_players(player, match_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS match_players_entrant_idx
            ON match_players(entrant_id, match_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                rating REAL NOT NULL,
                games_played INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (rating_scope, game, entrant_id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS ratings_leaderboard_idx
            ON ratings(rating_scope, game, rating DESC, games_played DESC, entrant_id)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rating_history (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                opponent_entrant_id TEXT NOT NULL
                    REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                opponent_display_name TEXT NOT NULL,
                outcome REAL NOT NULL CHECK (outcome IN (0.0, 0.5, 1.0)),
                rating_before REAL NOT NULL,
                rating_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (match_id, rating_scope, game, entrant_id)
            )
            """
        )

    @staticmethod
    def _create_series_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._create_series_archives_table(connection)
        _SchemaMixin._create_series_archive_indexes(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS series_matches (
                series_id TEXT NOT NULL
                    REFERENCES series_archives(series_id) ON DELETE CASCADE,
                leg_number INTEGER NOT NULL CHECK (leg_number IN (1, 2)),
                match_id TEXT NOT NULL UNIQUE
                    REFERENCES matches(match_id) ON DELETE RESTRICT,
                PRIMARY KEY (series_id, leg_number)
            )
            """
        )

    @staticmethod
    def _create_tournament_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_archives (
                tournament_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                format TEXT NOT NULL CHECK (format = 'round_robin_two_leg'),
                pairing_policy TEXT NOT NULL,
                seed_policy TEXT NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                points_json TEXT NOT NULL,
                pairing_count INTEGER NOT NULL CHECK (pairing_count >= 1),
                rating_policy TEXT NOT NULL
                    CHECK (rating_policy IN ('unrated', 'elo_tournament_batch_v1')),
                k_factor REAL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_source TEXT NOT NULL
                    CHECK (archive_source IN ('local_engine', 'external')),
                rating_source TEXT NOT NULL
                    CHECK (rating_source IN ('engine', 'imported')),
                rated INTEGER NOT NULL CHECK (rated IN (0, 1)),
                tournament_json TEXT NOT NULL,
                CHECK (
                    (rated = 0 AND rating_policy = 'unrated' AND k_factor IS NULL)
                    OR
                    (rated = 1 AND rating_policy = 'elo_tournament_batch_v1'
                     AND k_factor IS NOT NULL AND k_factor > 0)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_archives_finished_at_idx
            ON tournament_archives(finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_archives_game_finished_at_idx
            ON tournament_archives(game, finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_entrants (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                position INTEGER NOT NULL CHECK (position >= 0),
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                points REAL NOT NULL,
                series_played INTEGER NOT NULL CHECK (series_played >= 0),
                series_wins INTEGER NOT NULL CHECK (series_wins >= 0),
                series_draws INTEGER NOT NULL CHECK (series_draws >= 0),
                series_losses INTEGER NOT NULL CHECK (series_losses >= 0),
                games_played INTEGER NOT NULL CHECK (games_played >= 0),
                wins INTEGER NOT NULL CHECK (wins >= 0),
                draws INTEGER NOT NULL CHECK (draws >= 0),
                losses INTEGER NOT NULL CHECK (losses >= 0),
                technical_losses INTEGER NOT NULL CHECK (technical_losses >= 0),
                PRIMARY KEY (tournament_id, position),
                UNIQUE (tournament_id, entrant_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_pairings (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                pairing_number INTEGER NOT NULL CHECK (pairing_number >= 1),
                series_id TEXT NOT NULL UNIQUE
                    REFERENCES series_archives(series_id) ON DELETE RESTRICT,
                entrant_a_id TEXT NOT NULL,
                entrant_b_id TEXT NOT NULL,
                PRIMARY KEY (tournament_id, pairing_number),
                UNIQUE (tournament_id, entrant_a_id, entrant_b_id),
                CHECK (entrant_a_id <> entrant_b_id),
                FOREIGN KEY (tournament_id, entrant_a_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (tournament_id, entrant_b_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_rating_snapshots (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                rating_before REAL NOT NULL,
                rating_after REAL NOT NULL,
                games_added INTEGER NOT NULL CHECK (games_added >= 0),
                wins_added INTEGER NOT NULL CHECK (wins_added >= 0),
                draws_added INTEGER NOT NULL CHECK (draws_added >= 0),
                losses_added INTEGER NOT NULL CHECK (losses_added >= 0),
                PRIMARY KEY (tournament_id, rating_scope, game, entrant_id),
                FOREIGN KEY (tournament_id, entrant_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_rating_contributions (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                match_id TEXT NOT NULL,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL,
                opponent_entrant_id TEXT NOT NULL,
                frozen_rating REAL NOT NULL,
                opponent_frozen_rating REAL NOT NULL,
                expected_score REAL NOT NULL,
                rating_delta REAL NOT NULL,
                PRIMARY KEY (
                    tournament_id, match_id, rating_scope, game, entrant_id
                ),
                UNIQUE (
                    tournament_id, rating_scope, game, entrant_id, sequence
                ),
                FOREIGN KEY (match_id, rating_scope, game, entrant_id)
                    REFERENCES rating_history(match_id, rating_scope, game, entrant_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (tournament_id, entrant_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (tournament_id, opponent_entrant_id)
                    REFERENCES tournament_entrants(tournament_id, entrant_id)
                    ON DELETE RESTRICT
            )
            """
        )

    @staticmethod
    def _create_championship_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS championship_archives (
                championship_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                format TEXT NOT NULL CHECK (format = 'single_elimination_two_leg'),
                pairing_policy TEXT NOT NULL,
                seed_policy TEXT NOT NULL,
                tiebreak_policy TEXT NOT NULL,
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                champion TEXT NOT NULL,
                pairing_count INTEGER NOT NULL CHECK (pairing_count >= 1),
                rating_policy TEXT NOT NULL
                    CHECK (rating_policy IN ('unrated', 'elo_championship_batch_v1')),
                k_factor REAL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                archive_source TEXT NOT NULL
                    CHECK (archive_source IN ('local_engine', 'external')),
                rating_source TEXT NOT NULL
                    CHECK (rating_source IN ('engine', 'imported')),
                rated INTEGER NOT NULL CHECK (rated IN (0, 1)),
                championship_json TEXT NOT NULL,
                CHECK (
                    (rated = 0 AND rating_policy = 'unrated' AND k_factor IS NULL)
                    OR
                    (rated = 1 AND rating_policy = 'elo_championship_batch_v1'
                     AND k_factor IS NOT NULL AND k_factor > 0)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS championship_archives_finished_at_idx
            ON championship_archives(finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS championship_archives_game_finished_at_idx
            ON championship_archives(game, finished_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS championship_entrants (
                championship_id TEXT NOT NULL
                    REFERENCES championship_archives(championship_id) ON DELETE RESTRICT,
                position INTEGER NOT NULL CHECK (position >= 0),
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                rank INTEGER NOT NULL CHECK (rank >= 1),
                series_played INTEGER NOT NULL CHECK (series_played >= 0),
                series_wins INTEGER NOT NULL CHECK (series_wins >= 0),
                series_draws INTEGER NOT NULL CHECK (series_draws >= 0),
                series_losses INTEGER NOT NULL CHECK (series_losses >= 0),
                games_played INTEGER NOT NULL CHECK (games_played >= 0),
                wins INTEGER NOT NULL CHECK (wins >= 0),
                draws INTEGER NOT NULL CHECK (draws >= 0),
                losses INTEGER NOT NULL CHECK (losses >= 0),
                technical_losses INTEGER NOT NULL CHECK (technical_losses >= 0),
                PRIMARY KEY (championship_id, position),
                UNIQUE (championship_id, entrant_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS championship_pairings (
                championship_id TEXT NOT NULL
                    REFERENCES championship_archives(championship_id) ON DELETE RESTRICT,
                round_number INTEGER NOT NULL CHECK (round_number >= 1),
                pairing_number INTEGER NOT NULL CHECK (pairing_number >= 1),
                series_id TEXT NOT NULL UNIQUE
                    REFERENCES series_archives(series_id) ON DELETE RESTRICT,
                entrant_a_id TEXT NOT NULL,
                entrant_b_id TEXT NOT NULL,
                PRIMARY KEY (championship_id, pairing_number),
                UNIQUE (championship_id, entrant_a_id, entrant_b_id),
                CHECK (entrant_a_id <> entrant_b_id),
                FOREIGN KEY (championship_id, entrant_a_id)
                    REFERENCES championship_entrants(championship_id, entrant_id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (championship_id, entrant_b_id)
                    REFERENCES championship_entrants(championship_id, entrant_id)
                    ON DELETE RESTRICT
            )
            """
        )

    @staticmethod
    def _create_championship_checkpoint_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS championship_checkpoints (
                championship_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                source TEXT NOT NULL CHECK (source = 'local_engine'),
                format TEXT NOT NULL CHECK (format = 'single_elimination_two_leg'),
                pairing_policy TEXT NOT NULL
                    CHECK (pairing_policy = 'power_of_two_bracket_v1'),
                seed_policy TEXT NOT NULL
                    CHECK (seed_policy = 'round_seed_sha256_v1'),
                tiebreak_policy TEXT NOT NULL
                    CHECK (tiebreak_policy = 'deterministic_v1'),
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                game_config_json TEXT NOT NULL,
                schedule_json TEXT NOT NULL,
                max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
                pairing_count INTEGER NOT NULL CHECK (pairing_count >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('in_progress', 'finalized')),
                finalized_at TEXT,
                final_championship_id TEXT UNIQUE
                    REFERENCES championship_archives(championship_id) ON DELETE RESTRICT,
                config_json TEXT NOT NULL,
                CHECK (
                    (status = 'in_progress'
                     AND finalized_at IS NULL
                     AND final_championship_id IS NULL)
                    OR
                    (status = 'finalized'
                     AND finalized_at IS NOT NULL
                     AND final_championship_id = championship_id)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS championship_checkpoints_updated_at_idx
            ON championship_checkpoints(status, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS championship_checkpoint_series (
                championship_id TEXT NOT NULL
                    REFERENCES championship_checkpoints(championship_id) ON DELETE RESTRICT,
                pairing_number INTEGER NOT NULL CHECK (pairing_number >= 1),
                series_id TEXT NOT NULL UNIQUE,
                match_1_id TEXT NOT NULL UNIQUE,
                match_2_id TEXT NOT NULL UNIQUE,
                completed_at TEXT NOT NULL,
                series_json TEXT NOT NULL,
                PRIMARY KEY (championship_id, pairing_number),
                CHECK (match_1_id <> match_2_id)
            )
            """
        )

    @staticmethod
    def _create_championship_runner_lease_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS championship_runner_leases (
                championship_id TEXT PRIMARY KEY
                    REFERENCES championship_checkpoints(championship_id) ON DELETE RESTRICT,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                token_digest BLOB UNIQUE,
                acquired_at_epoch INTEGER,
                renewed_at_epoch INTEGER,
                expires_at_epoch INTEGER,
                CHECK (
                    (token_digest IS NULL
                     AND acquired_at_epoch IS NULL
                     AND renewed_at_epoch IS NULL
                     AND expires_at_epoch IS NULL)
                    OR
                    (typeof(token_digest) = 'blob'
                     AND length(token_digest) = 32
                     AND acquired_at_epoch IS NOT NULL
                     AND renewed_at_epoch IS NOT NULL
                     AND expires_at_epoch IS NOT NULL
                     AND acquired_at_epoch <= renewed_at_epoch
                     AND renewed_at_epoch < expires_at_epoch)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS championship_runner_leases_expires_at_idx
            ON championship_runner_leases(expires_at_epoch)
            WHERE token_digest IS NOT NULL
            """
        )

    @staticmethod
    def _create_checkpoint_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_checkpoints (
                tournament_id TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                source TEXT NOT NULL CHECK (source = 'local_engine'),
                format TEXT NOT NULL CHECK (format = 'round_robin_two_leg'),
                pairing_policy TEXT NOT NULL
                    CHECK (pairing_policy = 'input_order_combinations_v1'),
                seed_policy TEXT NOT NULL
                    CHECK (seed_policy = 'entrant_pair_sha256_v1'),
                game TEXT NOT NULL,
                seed INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                game_config_json TEXT NOT NULL,
                schedule_json TEXT NOT NULL,
                max_attempts INTEGER NOT NULL CHECK (max_attempts >= 1),
                pairing_count INTEGER NOT NULL CHECK (pairing_count >= 1),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('in_progress', 'finalized')),
                finalized_at TEXT,
                final_tournament_id TEXT UNIQUE
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                config_json TEXT NOT NULL,
                CHECK (
                    (status = 'in_progress'
                     AND finalized_at IS NULL
                     AND final_tournament_id IS NULL)
                    OR
                    (status = 'finalized'
                     AND finalized_at IS NOT NULL
                     AND final_tournament_id = tournament_id)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_checkpoints_updated_at_idx
            ON tournament_checkpoints(status, updated_at DESC)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_checkpoint_series (
                tournament_id TEXT NOT NULL
                    REFERENCES tournament_checkpoints(tournament_id) ON DELETE RESTRICT,
                pairing_number INTEGER NOT NULL CHECK (pairing_number >= 1),
                series_id TEXT NOT NULL UNIQUE,
                match_1_id TEXT NOT NULL UNIQUE,
                match_2_id TEXT NOT NULL UNIQUE,
                completed_at TEXT NOT NULL,
                series_json TEXT NOT NULL,
                PRIMARY KEY (tournament_id, pairing_number),
                CHECK (match_1_id <> match_2_id)
            )
            """
        )

    @staticmethod
    def _create_runner_lease_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tournament_runner_leases (
                tournament_id TEXT PRIMARY KEY
                    REFERENCES tournament_checkpoints(tournament_id) ON DELETE RESTRICT,
                generation INTEGER NOT NULL CHECK (generation >= 1),
                token_digest BLOB UNIQUE,
                acquired_at_epoch INTEGER,
                renewed_at_epoch INTEGER,
                expires_at_epoch INTEGER,
                CHECK (
                    (token_digest IS NULL
                     AND acquired_at_epoch IS NULL
                     AND renewed_at_epoch IS NULL
                     AND expires_at_epoch IS NULL)
                    OR
                    (typeof(token_digest) = 'blob'
                     AND length(token_digest) = 32
                     AND acquired_at_epoch IS NOT NULL
                     AND renewed_at_epoch IS NOT NULL
                     AND expires_at_epoch IS NOT NULL
                     AND acquired_at_epoch <= renewed_at_epoch
                     AND renewed_at_epoch < expires_at_epoch)
                )
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS tournament_runner_leases_expires_at_idx
            ON tournament_runner_leases(expires_at_epoch)
            WHERE token_digest IS NOT NULL
            """
        )

    @staticmethod
    def _create_rating_operation_schema(connection: sqlite3.Connection) -> None:
        """Create the global commit-order ledger for top-level ELO writes."""

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rating_operations (
                rating_operation_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id TEXT UNIQUE REFERENCES matches(match_id) ON DELETE RESTRICT,
                series_id TEXT UNIQUE
                    REFERENCES series_archives(series_id) ON DELETE RESTRICT,
                tournament_id TEXT UNIQUE
                    REFERENCES tournament_archives(tournament_id) ON DELETE RESTRICT,
                CHECK (
                    (match_id IS NOT NULL)
                    + (series_id IS NOT NULL)
                    + (tournament_id IS NOT NULL) = 1
                )
            )
            """
        )

    @staticmethod
    def _create_provider_usage_schema(connection: sqlite3.Connection) -> None:
        """Create the content-free, integer-only Provider budget ledger."""

        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS provider_budgets (
                budget_id TEXT PRIMARY KEY
                    CHECK (length(budget_id) BETWEEN 1 AND 128
                           AND instr(budget_id, char(0)) = 0),
                tournament_id TEXT UNIQUE
                    REFERENCES tournament_checkpoints(tournament_id) ON DELETE RESTRICT,
                policy_json TEXT NOT NULL
                    CHECK (length(policy_json) BETWEEN 1 AND 65536),
                policy_digest TEXT NOT NULL CHECK (
                    length(policy_digest) = 64
                    AND policy_digest = lower(policy_digest)
                    AND policy_digest NOT GLOB '*[^0-9a-f]*'
                ),
                limit_calls INTEGER CHECK (limit_calls IS NULL OR limit_calls >= 0),
                limit_input_tokens INTEGER
                    CHECK (limit_input_tokens IS NULL OR limit_input_tokens >= 0),
                limit_output_tokens INTEGER
                    CHECK (limit_output_tokens IS NULL OR limit_output_tokens >= 0),
                limit_estimated_cost_nanos INTEGER
                    CHECK (limit_estimated_cost_nanos IS NULL
                           OR limit_estimated_cost_nanos >= 0),
                spent_calls INTEGER NOT NULL CHECK (spent_calls >= 0),
                spent_input_tokens INTEGER NOT NULL CHECK (spent_input_tokens >= 0),
                spent_output_tokens INTEGER NOT NULL CHECK (spent_output_tokens >= 0),
                spent_estimated_cost_nanos INTEGER NOT NULL
                    CHECK (spent_estimated_cost_nanos >= 0),
                reserved_calls INTEGER NOT NULL CHECK (reserved_calls >= 0),
                reserved_input_tokens INTEGER NOT NULL CHECK (reserved_input_tokens >= 0),
                reserved_output_tokens INTEGER NOT NULL CHECK (reserved_output_tokens >= 0),
                reserved_estimated_cost_nanos INTEGER NOT NULL
                    CHECK (reserved_estimated_cost_nanos >= 0),
                poison_reason_code TEXT CHECK (
                    poison_reason_code IS NULL OR poison_reason_code IN (
                        'usage_exceeds_reservation', 'usage_counter_overflow'
                    )
                ),
                created_at_epoch INTEGER NOT NULL CHECK (created_at_epoch >= 0),
                finalized_at_epoch INTEGER CHECK (
                    finalized_at_epoch IS NULL OR finalized_at_epoch >= created_at_epoch
                ),
                CHECK (spent_calls <= {SQLITE_INT_MAX} - reserved_calls),
                CHECK (spent_input_tokens <= {SQLITE_INT_MAX} - reserved_input_tokens),
                CHECK (spent_output_tokens <= {SQLITE_INT_MAX} - reserved_output_tokens),
                CHECK (
                    spent_estimated_cost_nanos
                    <= {SQLITE_INT_MAX} - reserved_estimated_cost_nanos
                ),
                CHECK (
                    poison_reason_code IS NOT NULL OR limit_calls IS NULL
                    OR (spent_calls <= limit_calls
                        AND reserved_calls <= limit_calls - spent_calls)
                ),
                CHECK (
                    poison_reason_code IS NOT NULL OR limit_input_tokens IS NULL
                    OR (spent_input_tokens <= limit_input_tokens
                        AND reserved_input_tokens <= limit_input_tokens - spent_input_tokens)
                ),
                CHECK (
                    poison_reason_code IS NOT NULL OR limit_output_tokens IS NULL
                    OR (spent_output_tokens <= limit_output_tokens
                        AND reserved_output_tokens <= limit_output_tokens - spent_output_tokens)
                ),
                CHECK (
                    poison_reason_code IS NOT NULL OR limit_estimated_cost_nanos IS NULL
                    OR (spent_estimated_cost_nanos <= limit_estimated_cost_nanos
                        AND reserved_estimated_cost_nanos
                            <= limit_estimated_cost_nanos - spent_estimated_cost_nanos)
                )
            ) STRICT
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS provider_call_attempts (
                attempt_id TEXT PRIMARY KEY
                    CHECK (length(attempt_id) BETWEEN 1 AND 128
                           AND instr(attempt_id, char(0)) = 0),
                budget_id TEXT NOT NULL
                    REFERENCES provider_budgets(budget_id) ON DELETE RESTRICT,
                route_id TEXT NOT NULL CHECK (
                    length(route_id) = 73
                    AND substr(route_id, 1, 9) = 'route:v1:'
                    AND substr(route_id, 10) = lower(substr(route_id, 10))
                    AND substr(route_id, 10) NOT GLOB '*[^0-9a-f]*'
                ),
                state TEXT NOT NULL CHECK (state IN (
                    'reserved', 'dispatched', 'settled',
                    'released_pre_dispatch', 'charged_unknown', 'violation'
                )),
                runner_generation INTEGER
                    CHECK (runner_generation IS NULL OR runner_generation >= 1),
                bound_calls INTEGER NOT NULL CHECK (bound_calls = 1),
                bound_input_tokens INTEGER NOT NULL CHECK (bound_input_tokens >= 0),
                bound_output_tokens INTEGER NOT NULL CHECK (bound_output_tokens >= 0),
                bound_estimated_cost_nanos INTEGER NOT NULL
                    CHECK (bound_estimated_cost_nanos >= 0),
                actual_calls INTEGER CHECK (actual_calls IS NULL OR actual_calls >= 0),
                actual_input_tokens INTEGER
                    CHECK (actual_input_tokens IS NULL OR actual_input_tokens >= 0),
                actual_output_tokens INTEGER
                    CHECK (actual_output_tokens IS NULL OR actual_output_tokens >= 0),
                actual_estimated_cost_nanos INTEGER CHECK (
                    actual_estimated_cost_nanos IS NULL OR actual_estimated_cost_nanos >= 0
                ),
                charged_calls INTEGER CHECK (charged_calls IS NULL OR charged_calls >= 0),
                charged_input_tokens INTEGER CHECK (
                    charged_input_tokens IS NULL OR charged_input_tokens >= 0
                ),
                charged_output_tokens INTEGER CHECK (
                    charged_output_tokens IS NULL OR charged_output_tokens >= 0
                ),
                charged_estimated_cost_nanos INTEGER CHECK (
                    charged_estimated_cost_nanos IS NULL
                    OR charged_estimated_cost_nanos >= 0
                ),
                created_at_epoch INTEGER NOT NULL CHECK (created_at_epoch >= 0),
                dispatched_at_epoch INTEGER CHECK (
                    dispatched_at_epoch IS NULL OR dispatched_at_epoch >= created_at_epoch
                ),
                finished_at_epoch INTEGER CHECK (
                    finished_at_epoch IS NULL OR finished_at_epoch >= created_at_epoch
                ),
                CHECK (
                    (actual_calls IS NULL AND actual_input_tokens IS NULL
                     AND actual_output_tokens IS NULL
                     AND actual_estimated_cost_nanos IS NULL)
                    OR
                    (actual_calls IS NOT NULL AND actual_input_tokens IS NOT NULL
                     AND actual_output_tokens IS NOT NULL
                     AND actual_estimated_cost_nanos IS NOT NULL)
                ),
                CHECK (
                    (charged_calls IS NULL AND charged_input_tokens IS NULL
                     AND charged_output_tokens IS NULL
                     AND charged_estimated_cost_nanos IS NULL)
                    OR
                    (charged_calls IS NOT NULL AND charged_input_tokens IS NOT NULL
                     AND charged_output_tokens IS NOT NULL
                     AND charged_estimated_cost_nanos IS NOT NULL)
                ),
                CHECK (
                    (state = 'reserved'
                     AND dispatched_at_epoch IS NULL AND finished_at_epoch IS NULL
                     AND actual_calls IS NULL AND charged_calls IS NULL)
                    OR
                    (state = 'dispatched'
                     AND dispatched_at_epoch IS NOT NULL AND finished_at_epoch IS NULL
                     AND actual_calls IS NULL AND charged_calls IS NULL)
                    OR
                    (state = 'released_pre_dispatch'
                     AND dispatched_at_epoch IS NULL AND finished_at_epoch IS NOT NULL
                     AND actual_calls IS NULL AND charged_calls IS NULL)
                    OR
                    (state = 'settled'
                     AND dispatched_at_epoch IS NOT NULL
                     AND finished_at_epoch >= dispatched_at_epoch
                     AND actual_calls = 1
                     AND actual_input_tokens <= bound_input_tokens
                     AND actual_output_tokens <= bound_output_tokens
                     AND actual_estimated_cost_nanos <= bound_estimated_cost_nanos
                     AND charged_calls = actual_calls
                     AND charged_input_tokens = actual_input_tokens
                     AND charged_output_tokens = actual_output_tokens
                     AND charged_estimated_cost_nanos = actual_estimated_cost_nanos)
                    OR
                    (state = 'charged_unknown'
                     AND finished_at_epoch IS NOT NULL
                     AND (dispatched_at_epoch IS NULL
                          OR finished_at_epoch >= dispatched_at_epoch)
                     AND actual_calls IS NULL
                     AND charged_calls = bound_calls
                     AND charged_input_tokens = bound_input_tokens
                     AND charged_output_tokens = bound_output_tokens
                     AND charged_estimated_cost_nanos = bound_estimated_cost_nanos)
                    OR
                    (state = 'violation'
                     AND dispatched_at_epoch IS NOT NULL
                     AND finished_at_epoch >= dispatched_at_epoch
                     AND actual_calls IS NOT NULL AND charged_calls IS NOT NULL
                     AND (actual_calls > bound_calls
                          OR actual_input_tokens > bound_input_tokens
                          OR actual_output_tokens > bound_output_tokens
                          OR actual_estimated_cost_nanos > bound_estimated_cost_nanos)
                     AND (
                         (charged_calls = actual_calls
                          AND charged_input_tokens = actual_input_tokens
                          AND charged_output_tokens = actual_output_tokens
                          AND charged_estimated_cost_nanos = actual_estimated_cost_nanos)
                         OR
                         (charged_calls = bound_calls
                          AND charged_input_tokens = bound_input_tokens
                          AND charged_output_tokens = bound_output_tokens
                          AND charged_estimated_cost_nanos = bound_estimated_cost_nanos)
                     ))
                )
            ) STRICT
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS provider_call_attempts_budget_state_idx
            ON provider_call_attempts(budget_id, state, attempt_id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS provider_call_attempts_generation_state_idx
            ON provider_call_attempts(budget_id, runner_generation, state)
            WHERE runner_generation IS NOT NULL
            """
        )

    @staticmethod
    @lru_cache(maxsize=1)
    def _archive_table_manifest_variants() -> dict[str, frozenset[TableSpec]]:
        """Return the exact fresh and historical ALTER-based archive shapes."""

        fresh: dict[str, TableSpec] = {}
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            _SchemaMixin._create_matches_table(connection)
            _SchemaMixin._create_match_indexes(connection)
            fresh["matches"] = introspect_schema(connection).tables_by_name()["matches"]
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            _SchemaMixin._create_series_archives_table(connection)
            _SchemaMixin._create_series_archive_indexes(connection)
            fresh["series_archives"] = introspect_schema(connection).tables_by_name()[
                "series_archives"
            ]

        legacy: dict[str, TableSpec] = {}
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            connection.executescript(
                """
                CREATE TABLE matches (
                    match_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    players_json TEXT NOT NULL,
                    scores_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    archive_json TEXT NOT NULL,
                    archive_source TEXT NOT NULL DEFAULT 'legacy',
                    rating_source TEXT NOT NULL DEFAULT 'imported',
                    rated INTEGER NOT NULL DEFAULT 0,
                    rating_policy TEXT NOT NULL DEFAULT 'unrated'
                );
                CREATE INDEX matches_finished_at_idx ON matches(finished_at DESC);
                CREATE INDEX matches_game_finished_at_idx
                    ON matches(game, finished_at DESC);
                """
            )
            legacy["matches"] = introspect_schema(connection).tables_by_name()["matches"]
        with closing(sqlite3.connect(":memory:", isolation_level=None)) as connection:
            connection.executescript(
                """
                CREATE TABLE series_archives (
                    series_id TEXT PRIMARY KEY,
                    schema_version INTEGER NOT NULL,
                    game TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    players_json TEXT NOT NULL,
                    points_json TEXT NOT NULL,
                    rating_policy TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    series_json TEXT NOT NULL,
                    archive_source TEXT NOT NULL DEFAULT 'legacy',
                    rating_source TEXT NOT NULL DEFAULT 'imported',
                    rated INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX series_archives_finished_at_idx
                    ON series_archives(finished_at DESC);
                CREATE INDEX series_archives_game_finished_at_idx
                    ON series_archives(game, finished_at DESC);
                """
            )
            legacy["series_archives"] = introspect_schema(connection).tables_by_name()[
                "series_archives"
            ]
        return {
            table: frozenset((fresh[table], legacy[table]))
            for table in ("matches", "series_archives")
        }

    @staticmethod
    def _verify_archive_tables_before_canonicalization(connection: sqlite3.Connection) -> None:
        """Reject tampering instead of silently repairing it during the v7 rebuild."""

        try:
            manifest = introspect_schema(connection)
        except SchemaManifestError as exc:
            raise StorageError("SQLite v7 迁移前结构无法安全审计") from exc
        if manifest.auxiliary_objects:
            raise StorageError("SQLite v7 迁移前存在非预期 view 或 trigger")
        tables = manifest.tables_by_name()
        for table, variants in _SchemaMixin._archive_table_manifest_variants().items():
            if tables.get(table) not in variants:
                raise StorageError(f"SQLite v7 迁移前 {table} 结构不受支持")

    @staticmethod
    def _canonicalize_archive_tables(connection: sqlite3.Connection) -> None:
        """Rebuild archive parents into the single canonical v7 table shape.

        v1/v2 databases gained source/rating columns through ``ALTER TABLE``.
        Their column order, defaults, and CHECK constraints therefore differ
        from a fresh database even after later migrations.  A complete schema
        manifest cannot safely allow that weaker shape, so v7 normalizes both
        parent tables while preserving every value through explicit columns.
        """

        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise StorageError("SQLite v7 规范化必须在事务外暂时关闭外键约束")
        _SchemaMixin._verify_archive_tables_before_canonicalization(connection)

        _SchemaMixin._create_matches_table(
            connection,
            table_name="matches_v7_canonical",
            if_not_exists=False,
        )
        connection.execute(
            """
            INSERT INTO matches_v7_canonical (
                match_id, schema_version, game, seed, players_json, scores_json,
                started_at, finished_at, archive_source, rating_source, rated,
                rating_policy, archive_json
            )
            SELECT match_id, schema_version, game, seed, players_json, scores_json,
                   started_at, finished_at, archive_source, rating_source, rated,
                   rating_policy, archive_json
            FROM matches
            """
        )
        connection.execute("DROP TABLE matches")
        connection.execute("ALTER TABLE matches_v7_canonical RENAME TO matches")
        _SchemaMixin._create_match_indexes(connection)

        _SchemaMixin._create_series_archives_table(
            connection,
            table_name="series_archives_v7_canonical",
            if_not_exists=False,
        )
        connection.execute(
            """
            INSERT INTO series_archives_v7_canonical (
                series_id, schema_version, game, seed, players_json, points_json,
                rating_policy, started_at, finished_at, archive_source,
                rating_source, rated, series_json
            )
            SELECT series_id, schema_version, game, seed, players_json, points_json,
                   rating_policy, started_at, finished_at, archive_source,
                   rating_source, rated, series_json
            FROM series_archives
            """
        )
        connection.execute("DROP TABLE series_archives")
        connection.execute("ALTER TABLE series_archives_v7_canonical RENAME TO series_archives")
        _SchemaMixin._create_series_archive_indexes(connection)

    @staticmethod
    def _rating_operation_candidates(connection: sqlite3.Connection) -> list[sqlite3.Row]:
        """Return every rated top-level archive and its first history row."""

        return connection.execute(
            """
            SELECT operation_kind, operation_id, first_history_row,
                   last_history_row, history_row_count
            FROM (
                SELECT 'match' AS operation_kind,
                       m.match_id AS operation_id,
                       MIN(rh.rowid) AS first_history_row,
                       MAX(rh.rowid) AS last_history_row,
                       COUNT(rh.rowid) AS history_row_count
                FROM matches AS m
                LEFT JOIN series_matches AS sm ON sm.match_id = m.match_id
                LEFT JOIN rating_history AS rh ON rh.match_id = m.match_id
                WHERE m.rated = 1 AND sm.match_id IS NULL
                GROUP BY m.match_id

                UNION ALL

                SELECT 'series' AS operation_kind,
                       sa.series_id AS operation_id,
                       MIN(rh.rowid) AS first_history_row,
                       MAX(rh.rowid) AS last_history_row,
                       COUNT(rh.rowid) AS history_row_count
                FROM series_archives AS sa
                LEFT JOIN tournament_pairings AS tp ON tp.series_id = sa.series_id
                LEFT JOIN series_matches AS sm ON sm.series_id = sa.series_id
                LEFT JOIN rating_history AS rh ON rh.match_id = sm.match_id
                WHERE sa.rated = 1 AND tp.series_id IS NULL
                GROUP BY sa.series_id

                UNION ALL

                SELECT 'tournament' AS operation_kind,
                       ta.tournament_id AS operation_id,
                       MIN(rh.rowid) AS first_history_row,
                       MAX(rh.rowid) AS last_history_row,
                       COUNT(rh.rowid) AS history_row_count
                FROM tournament_archives AS ta
                LEFT JOIN tournament_pairings AS tp
                  ON tp.tournament_id = ta.tournament_id
                LEFT JOIN series_matches AS sm ON sm.series_id = tp.series_id
                LEFT JOIN rating_history AS rh ON rh.match_id = sm.match_id
                WHERE ta.rated = 1
                GROUP BY ta.tournament_id
            )
            ORDER BY first_history_row, operation_kind, operation_id
            """
        ).fetchall()

    @classmethod
    def _backfill_rating_operations(cls, connection: sqlite3.Connection) -> None:
        """Backfill v1-v6 commit order from SQLite's historical insert order."""

        candidates = cls._rating_operation_candidates(connection)
        if any(row["first_history_row"] is None for row in candidates):
            raise StorageError("SQLite ELO 操作缺少评分历史，无法迁移至 v7")
        if any(
            row["last_history_row"] - row["first_history_row"] + 1 != row["history_row_count"]
            for row in candidates
        ):
            raise StorageError("SQLite ELO 操作历史发生交错或断裂，无法迁移至 v7")
        orphaned_history = connection.execute(
            """
            SELECT 1
            FROM rating_history AS rh
            JOIN matches AS m ON m.match_id = rh.match_id
            LEFT JOIN series_matches AS sm ON sm.match_id = rh.match_id
            LEFT JOIN series_archives AS sa ON sa.series_id = sm.series_id
            LEFT JOIN tournament_pairings AS tp ON tp.series_id = sm.series_id
            LEFT JOIN tournament_archives AS ta
              ON ta.tournament_id = tp.tournament_id
            WHERE CASE
                WHEN ta.tournament_id IS NOT NULL THEN ta.rated
                WHEN sa.series_id IS NOT NULL THEN sa.rated
                ELSE m.rated
            END <> 1
            LIMIT 1
            """
        ).fetchone()
        if orphaned_history is not None:
            raise StorageError("SQLite ELO 历史没有对应的已计分顶层档案，无法迁移至 v7")

        for row in candidates:
            columns = {
                "match": ("match_id", row["operation_id"]),
                "series": ("series_id", row["operation_id"]),
                "tournament": ("tournament_id", row["operation_id"]),
            }
            column, operation_id = columns[row["operation_kind"]]
            connection.execute(
                f"INSERT OR IGNORE INTO rating_operations ({column}) VALUES (?)",  # noqa: S608
                (operation_id,),
            )
        actual_rows = connection.execute(
            """
            SELECT match_id, series_id, tournament_id
            FROM rating_operations
            """
        ).fetchall()
        actual_keys = {
            (kind, identifier)
            for row in actual_rows
            for kind, identifier in (
                ("match", row["match_id"]),
                ("series", row["series_id"]),
                ("tournament", row["tournament_id"]),
            )
            if identifier is not None
        }
        expected_keys = {(row["operation_kind"], row["operation_id"]) for row in candidates}
        if len(actual_rows) != len(actual_keys) or actual_keys != expected_keys:
            raise StorageError("SQLite 全局评分操作账本无法安全回填")

    @staticmethod
    def _record_rating_operation(
        connection: sqlite3.Connection,
        *,
        match_id: str | None = None,
        series_id: str | None = None,
        tournament_id: str | None = None,
    ) -> int:
        identifiers = {
            "match_id": match_id,
            "series_id": series_id,
            "tournament_id": tournament_id,
        }
        populated = [(column, value) for column, value in identifiers.items() if value is not None]
        if len(populated) != 1:
            raise ValueError("评分操作必须且只能关联一个顶层档案")
        column, operation_id = populated[0]
        cursor = connection.execute(
            f"INSERT INTO rating_operations ({column}) VALUES (?)",  # noqa: S608
            (operation_id,),
        )
        sequence = cursor.lastrowid
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise StorageError("SQLite 无法分配全局评分操作序号")
        return sequence

    @staticmethod
    def _verify_top_level_rating_operation(
        connection: sqlite3.Connection,
        *,
        rated: bool,
        match_id: str | None = None,
        series_id: str | None = None,
        tournament_id: str | None = None,
    ) -> None:
        identifiers = {
            "match_id": match_id,
            "series_id": series_id,
            "tournament_id": tournament_id,
        }
        populated = [(column, value) for column, value in identifiers.items() if value is not None]
        if len(populated) != 1:
            raise ValueError("评分操作必须且只能关联一个顶层档案")
        column, operation_id = populated[0]
        rows = connection.execute(
            f"""
            SELECT rating_operation_seq, match_id, series_id, tournament_id
            FROM rating_operations
            WHERE {column} = ?
            """,  # noqa: S608
            (operation_id,),
        ).fetchall()
        expected_count = 1 if rated else 0
        if len(rows) != expected_count:
            raise StorageError("SQLite 顶层档案的全局评分操作账本已损坏")
        if rows:
            row = rows[0]
            sequence = row["rating_operation_seq"]
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence < 1
                or row[column] != operation_id
                or sum(row[name] is not None for name in identifiers) != 1
            ):
                raise StorageError("SQLite 顶层档案的全局评分操作账本已损坏")

    @staticmethod
    def _migrate_to_v3(connection: sqlite3.Connection, *, include_series: bool) -> None:
        """Move name-keyed ratings to isolated legacy entrant identities atomically."""

        connection.execute(
            """
            CREATE TABLE entrants (
                entrant_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute("DROP INDEX IF EXISTS match_players_player_idx")
        connection.execute("ALTER TABLE match_players RENAME TO match_players_v2")
        connection.execute("DROP INDEX IF EXISTS ratings_leaderboard_idx")
        connection.execute("ALTER TABLE ratings RENAME TO ratings_v2")
        connection.execute("ALTER TABLE rating_history RENAME TO rating_history_v2")

        legacy_names = {
            row[0]
            for row in connection.execute(
                """
                SELECT player FROM match_players_v2
                UNION SELECT player FROM ratings_v2
                UNION SELECT player FROM rating_history_v2
                UNION SELECT opponent FROM rating_history_v2
                """
            )
        }
        legacy_timestamp = datetime(1970, 1, 1, tzinfo=UTC).isoformat()
        legacy_identity = _canonical_json({"kind": "legacy"})
        connection.executemany(
            """
            INSERT INTO entrants (
                entrant_id, display_name, identity_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    legacy_entrant_id(name),
                    name,
                    legacy_identity,
                    legacy_timestamp,
                    legacy_timestamp,
                )
                for name in sorted(legacy_names)
            ],
        )

        connection.execute(
            """
            CREATE TABLE match_players (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                player TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (match_id, position)
            )
            """
        )
        for row in connection.execute(
            """
            SELECT match_id, position, player, descriptor_json, score
            FROM match_players_v2
            """
        ).fetchall():
            connection.execute(
                """
                INSERT INTO match_players (
                    match_id, position, player, entrant_id, display_name,
                    descriptor_json, score
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["match_id"],
                    row["position"],
                    row["player"],
                    legacy_entrant_id(row["player"]),
                    row["player"],
                    row["descriptor_json"],
                    row["score"],
                ),
            )
        connection.execute("DROP TABLE match_players_v2")
        connection.execute(
            "CREATE INDEX match_players_player_idx ON match_players(player, match_id)"
        )
        connection.execute(
            "CREATE INDEX match_players_entrant_idx ON match_players(entrant_id, match_id)"
        )

        connection.execute(
            """
            CREATE TABLE ratings (
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                rating REAL NOT NULL,
                games_played INTEGER NOT NULL,
                wins INTEGER NOT NULL,
                draws INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (rating_scope, game, entrant_id)
            )
            """
        )
        for row in connection.execute("SELECT * FROM ratings_v2").fetchall():
            connection.execute(
                """
                INSERT INTO ratings (
                    rating_scope, game, entrant_id, rating, games_played,
                    wins, draws, losses, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["rating_scope"],
                    row["game"],
                    legacy_entrant_id(row["player"]),
                    row["rating"],
                    row["games_played"],
                    row["wins"],
                    row["draws"],
                    row["losses"],
                    row["updated_at"],
                ),
            )
        connection.execute("DROP TABLE ratings_v2")
        connection.execute(
            """
            CREATE INDEX ratings_leaderboard_idx
            ON ratings(rating_scope, game, rating DESC, games_played DESC, entrant_id)
            """
        )

        connection.execute(
            """
            CREATE TABLE rating_history (
                match_id TEXT NOT NULL REFERENCES matches(match_id) ON DELETE CASCADE,
                rating_scope TEXT NOT NULL CHECK (rating_scope IN ('overall', 'game')),
                game TEXT NOT NULL,
                entrant_id TEXT NOT NULL REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                opponent_entrant_id TEXT NOT NULL
                    REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                opponent_display_name TEXT NOT NULL,
                outcome REAL NOT NULL CHECK (outcome IN (0.0, 0.5, 1.0)),
                rating_before REAL NOT NULL,
                rating_after REAL NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (match_id, rating_scope, game, entrant_id)
            )
            """
        )
        for row in connection.execute("SELECT * FROM rating_history_v2").fetchall():
            connection.execute(
                """
                INSERT INTO rating_history (
                    match_id, rating_scope, game, entrant_id, display_name,
                    opponent_entrant_id, opponent_display_name, outcome,
                    rating_before, rating_after, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["match_id"],
                    row["rating_scope"],
                    row["game"],
                    legacy_entrant_id(row["player"]),
                    row["player"],
                    legacy_entrant_id(row["opponent"]),
                    row["opponent"],
                    row["outcome"],
                    row["rating_before"],
                    row["rating_after"],
                    row["created_at"],
                ),
            )

        connection.execute(
            "ALTER TABLE matches ADD COLUMN archive_source TEXT NOT NULL DEFAULT 'legacy'"
        )
        connection.execute(
            "ALTER TABLE matches ADD COLUMN rating_source TEXT NOT NULL DEFAULT 'imported'"
        )
        connection.execute("ALTER TABLE matches ADD COLUMN rated INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "ALTER TABLE matches ADD COLUMN rating_policy TEXT NOT NULL DEFAULT 'unrated'"
        )
        connection.execute(
            """
            UPDATE matches
            SET rated = EXISTS (
                    SELECT 1 FROM rating_history_v2 AS rh WHERE rh.match_id = matches.match_id
                ),
                rating_source = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM rating_history_v2 AS rh
                        WHERE rh.match_id = matches.match_id
                    ) THEN 'engine'
                    ELSE 'imported'
                END,
                rating_policy = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM rating_history_v2 AS rh
                        WHERE rh.match_id = matches.match_id
                    ) THEN 'elo_v1'
                    ELSE 'unrated'
                END
            """
        )

        if include_series:
            connection.execute(
                """
                ALTER TABLE series_archives
                ADD COLUMN archive_source TEXT NOT NULL DEFAULT 'legacy'
                """
            )
            connection.execute(
                """
                ALTER TABLE series_archives
                ADD COLUMN rating_source TEXT NOT NULL DEFAULT 'imported'
                """
            )
            connection.execute(
                "ALTER TABLE series_archives ADD COLUMN rated INTEGER NOT NULL DEFAULT 0"
            )
            connection.execute(
                """
                UPDATE series_archives
                SET rated = EXISTS (
                    SELECT 1
                    FROM series_matches AS sm
                    JOIN rating_history_v2 AS rh ON rh.match_id = sm.match_id
                    WHERE sm.series_id = series_archives.series_id
                ),
                    rating_source = CASE
                        WHEN EXISTS (
                            SELECT 1
                            FROM series_matches AS sm
                            JOIN rating_history_v2 AS rh ON rh.match_id = sm.match_id
                            WHERE sm.series_id = series_archives.series_id
                        ) THEN 'engine'
                        ELSE 'imported'
                    END
                """
            )
            connection.execute(
                """
                UPDATE matches
                SET rating_source = 'engine', rated = 1, rating_policy = 'elo_batch_v1'
                WHERE match_id IN (
                    SELECT sm.match_id
                    FROM series_matches AS sm
                    JOIN series_archives AS sa ON sa.series_id = sm.series_id
                    WHERE sa.rated = 1
                )
                """
            )
        connection.execute("DROP TABLE rating_history_v2")

    @staticmethod
    def _verify_required_columns(
        connection: sqlite3.Connection,
        required_columns: dict[str, set[str]],
    ) -> None:
        for table, required in required_columns.items():
            columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
            missing = required - columns
            if missing:
                names = ", ".join(sorted(missing))
                raise StorageError(f"SQLite 数据库结构不完整：{table} 缺少 {names}")

    @staticmethod
    def _verify_v3_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._verify_required_columns(connection, _V3_REQUIRED_COLUMNS)

    @staticmethod
    def _verify_v4_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._verify_required_columns(connection, _V4_REQUIRED_COLUMNS)

    @staticmethod
    def _verify_v5_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._verify_required_columns(connection, _V5_REQUIRED_COLUMNS)

    @staticmethod
    def _verify_v6_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._verify_required_columns(connection, _V6_REQUIRED_COLUMNS)
        _SchemaMixin._verify_runner_lease_schema(connection)

    @staticmethod
    def _verify_v9_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._verify_required_columns(connection, _V9_REQUIRED_COLUMNS)

    @staticmethod
    def _verify_v10_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._verify_required_columns(connection, _V10_REQUIRED_COLUMNS)
        _SchemaMixin._verify_one_runner_lease_schema(
            connection,
            table="championship_runner_leases",
            owner_table="championship_checkpoints",
            owner_column="championship_id",
        )

    @staticmethod
    def _verify_runner_lease_schema(connection: sqlite3.Connection) -> None:
        _SchemaMixin._verify_one_runner_lease_schema(
            connection,
            table="tournament_runner_leases",
            owner_table="tournament_checkpoints",
            owner_column="tournament_id",
        )

    @staticmethod
    def _verify_one_runner_lease_schema(
        connection: sqlite3.Connection,
        *,
        table: str,
        owner_table: str,
        owner_column: str,
    ) -> None:
        table_info = connection.execute(f"PRAGMA table_info({table})").fetchall()
        primary_key = [row["name"] for row in table_info if row["pk"]]
        if primary_key != [owner_column]:
            raise StorageError(f"SQLite 数据库结构不完整：{table} 主键无效")

        foreign_keys = connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        if not any(
            row["table"] == owner_table
            and row["from"] == owner_column
            and row["to"] == owner_column
            and row["on_delete"].upper() == "RESTRICT"
            for row in foreign_keys
        ):
            raise StorageError(f"SQLite 数据库结构不完整：{table} 外键无效")

        has_unique_token = False
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if not index["unique"] or index["partial"]:
                continue
            columns = [
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (index["name"],),
                ).fetchall()
            ]
            if columns == ["token_digest"]:
                has_unique_token = True
                break
        if not has_unique_token:
            raise StorageError(
                f"SQLite 数据库结构不完整：{table} 缺少 token 唯一约束"
            )

    @staticmethod
    @lru_cache(maxsize=1)
    def _expected_v7_schema_manifest() -> SchemaManifest:
        """Build the canonical pre-budget v7 manifest for migration validation."""

        with closing(sqlite3.connect(":memory:", isolation_level=None)) as reference:
            _SchemaMixin._create_base_schema(reference)
            _SchemaMixin._create_series_schema(reference)
            _SchemaMixin._create_tournament_schema(reference)
            _SchemaMixin._create_checkpoint_schema(reference)
            _SchemaMixin._create_runner_lease_schema(reference)
            _SchemaMixin._create_rating_operation_schema(reference)
            return introspect_schema(reference)

    @staticmethod
    @lru_cache(maxsize=1)
    def _expected_schema_manifest() -> SchemaManifest:
        """Build the canonical current-schema manifest with the same SQLite runtime."""

        with closing(sqlite3.connect(":memory:", isolation_level=None)) as reference:
            _SchemaMixin._create_base_schema(reference)
            _SchemaMixin._create_series_schema(reference)
            _SchemaMixin._create_tournament_schema(reference)
            _SchemaMixin._create_checkpoint_schema(reference)
            _SchemaMixin._create_runner_lease_schema(reference)
            _SchemaMixin._create_rating_operation_schema(reference)
            _SchemaMixin._create_provider_usage_schema(reference)
            _SchemaMixin._create_championship_schema(reference)
            _SchemaMixin._create_championship_checkpoint_schema(reference)
            _SchemaMixin._create_championship_runner_lease_schema(reference)
            return introspect_schema(reference)

    @staticmethod
    def _verify_v7_schema(connection: sqlite3.Connection) -> None:
        try:
            verify_schema_manifest(connection, _SchemaMixin._expected_v7_schema_manifest())
        except SchemaManifestError as exc:
            raise StorageError(f"SQLite 数据库结构与 v7 manifest 不一致：{exc}") from exc

    @staticmethod
    @lru_cache(maxsize=1)
    def _expected_v8_schema_manifest() -> SchemaManifest:
        """Build the canonical pre-championship v8 manifest for migration validation."""

        with closing(sqlite3.connect(":memory:", isolation_level=None)) as reference:
            _SchemaMixin._create_base_schema(reference)
            _SchemaMixin._create_series_schema(reference)
            _SchemaMixin._create_tournament_schema(reference)
            _SchemaMixin._create_checkpoint_schema(reference)
            _SchemaMixin._create_runner_lease_schema(reference)
            _SchemaMixin._create_rating_operation_schema(reference)
            _SchemaMixin._create_provider_usage_schema(reference)
            return introspect_schema(reference)

    @staticmethod
    def _verify_v8_schema(connection: sqlite3.Connection) -> None:
        try:
            verify_schema_manifest(connection, _SchemaMixin._expected_v8_schema_manifest())
        except SchemaManifestError as exc:
            raise StorageError(f"SQLite 数据库结构与 v8 manifest 不一致：{exc}") from exc

    @staticmethod
    @lru_cache(maxsize=1)
    def _expected_v9_schema_manifest() -> SchemaManifest:
        """Build the canonical pre-checkpoint v9 manifest for migration validation."""

        with closing(sqlite3.connect(":memory:", isolation_level=None)) as reference:
            _SchemaMixin._create_base_schema(reference)
            _SchemaMixin._create_series_schema(reference)
            _SchemaMixin._create_tournament_schema(reference)
            _SchemaMixin._create_checkpoint_schema(reference)
            _SchemaMixin._create_runner_lease_schema(reference)
            _SchemaMixin._create_rating_operation_schema(reference)
            _SchemaMixin._create_provider_usage_schema(reference)
            _SchemaMixin._create_championship_schema(reference)
            return introspect_schema(reference)

    @staticmethod
    def _verify_v9_schema_manifest(connection: sqlite3.Connection) -> None:
        try:
            verify_schema_manifest(connection, _SchemaMixin._expected_v9_schema_manifest())
        except SchemaManifestError as exc:
            raise StorageError(f"SQLite 数据库结构与 v9 manifest 不一致：{exc}") from exc

    @staticmethod
    def _verify_schema(connection: sqlite3.Connection) -> None:
        try:
            verify_schema_manifest(connection, _SchemaMixin._expected_schema_manifest())
        except SchemaManifestError as exc:
            raise StorageError(f"SQLite 数据库结构与 v10 manifest 不一致：{exc}") from exc

    @staticmethod
    def _verify_foreign_keys(connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise StorageError("SQLite 数据库外键完整性检查失败")

    @staticmethod
    def _validate_rating_source(rating_source: object) -> RatingSource:
        if rating_source not in ("engine", "imported"):
            raise ValueError("rating_source 必须是 'engine' 或 'imported'")
        return rating_source


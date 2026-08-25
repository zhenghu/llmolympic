"""SQLite v11 championship-budget schema and migration tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from llmolympic.core.storage import (
    SCHEMA_VERSION,
    SQLiteStore,
    StorageError,
    inspect_database,
)
from llmolympic.core.usage import ProviderBudgetPolicy, RouteBudgetPolicy

_LEGACY_ROUTE_ID = f"route:v1:{'0' * 64}"
_LEGACY_POLICY = ProviderBudgetPolicy(
    max_output_tokens_per_call=32,
    routes=(RouteBudgetPolicy(route_id=_LEGACY_ROUTE_ID),),
)

_LEGACY_BUDGET = (
    "legacy-budget",
    None,
    _LEGACY_POLICY.canonical_json(),
    _LEGACY_POLICY.digest,
    5,
    10,
    10,
    None,
    1,
    2,
    3,
    0,
    0,
    0,
    0,
    0,
    None,
    10,
    None,
)
_LEGACY_ATTEMPT = (
    "legacy-attempt",
    "legacy-budget",
    _LEGACY_ROUTE_ID,
    "settled",
    None,
    1,
    2,
    3,
    0,
    1,
    2,
    3,
    0,
    1,
    2,
    3,
    0,
    10,
    11,
    12,
)


def _create_historical_database(database: Path, *, version: int) -> None:
    """Build an exact v8-v10 database without first creating a v11 store."""

    assert version in {8, 9, 10}
    with sqlite3.connect(database, isolation_level=None) as connection:
        connection.execute("BEGIN")
        SQLiteStore._create_base_schema(connection)
        SQLiteStore._create_series_schema(connection)
        SQLiteStore._create_tournament_schema(connection)
        SQLiteStore._create_checkpoint_schema(connection)
        SQLiteStore._create_runner_lease_schema(connection)
        SQLiteStore._create_rating_operation_schema(connection)
        SQLiteStore._create_provider_usage_schema_v10(connection)
        if version >= 9:
            SQLiteStore._create_championship_schema(connection)
        if version >= 10:
            SQLiteStore._create_championship_checkpoint_schema(connection)
            SQLiteStore._create_championship_runner_lease_schema(connection)
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()


def _insert_legacy_provider_rows(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            INSERT INTO provider_budgets (
                budget_id, tournament_id, policy_json, policy_digest,
                limit_calls, limit_input_tokens, limit_output_tokens,
                limit_estimated_cost_nanos,
                spent_calls, spent_input_tokens, spent_output_tokens,
                spent_estimated_cost_nanos,
                reserved_calls, reserved_input_tokens, reserved_output_tokens,
                reserved_estimated_cost_nanos,
                poison_reason_code, created_at_epoch, finalized_at_epoch
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _LEGACY_BUDGET,
        )
        connection.execute(
            """
            INSERT INTO provider_call_attempts VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            _LEGACY_ATTEMPT,
        )


def _insert_budget(
    connection: sqlite3.Connection,
    budget_id: str,
    *,
    tournament_id: str | None = None,
    championship_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO provider_budgets (
            budget_id, tournament_id, championship_id, policy_json, policy_digest,
            spent_calls, spent_input_tokens, spent_output_tokens,
            spent_estimated_cost_nanos,
            reserved_calls, reserved_input_tokens, reserved_output_tokens,
            reserved_estimated_cost_nanos, created_at_epoch
        ) VALUES (?, ?, ?, '{}', ?, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        """,
        (budget_id, tournament_id, championship_id, "0" * 64),
    )


def _provider_column_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute("PRAGMA table_info(provider_budgets)"))


def test_fresh_v11_budget_schema_has_championship_scope_constraints(tmp_path: Path) -> None:
    database = tmp_path / "fresh-v11.db"
    SQLiteStore(database)

    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        championship_column = next(
            row
            for row in connection.execute("PRAGMA table_info(provider_budgets)")
            if row["name"] == "championship_id"
        )
        assert (championship_column["type"], championship_column["notnull"], championship_column["pk"]) == (
            "TEXT",
            0,
            0,
        )

        foreign_keys = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in connection.execute("PRAGMA foreign_key_list(provider_budgets)")
        }
        assert (
            "championship_id",
            "championship_checkpoints",
            "championship_id",
            "RESTRICT",
        ) in foreign_keys

        unique_columns = {
            tuple(
                column["name"]
                for column in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                    (index["name"],),
                )
            )
            for index in connection.execute("PRAGMA index_list(provider_budgets)")
            if index["unique"]
        }
        assert ("tournament_id",) in unique_columns
        assert ("championship_id",) in unique_columns

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
            _insert_budget(connection, "missing-championship", championship_id="missing")

    # Disable FK enforcement only for this constraint-isolation connection: the
    # manifest and behavioral assertion above already proved the FK contract.
    with sqlite3.connect(database) as connection:
        _insert_budget(connection, "standalone")
        _insert_budget(connection, "championship-1", championship_id="championship-scope")
        with pytest.raises(sqlite3.IntegrityError, match="championship_id"):
            _insert_budget(connection, "championship-2", championship_id="championship-scope")
        _insert_budget(connection, "tournament-1", tournament_id="tournament-scope")
        with pytest.raises(sqlite3.IntegrityError, match="tournament_id"):
            _insert_budget(connection, "tournament-2", tournament_id="tournament-scope")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            _insert_budget(
                connection,
                "two-scopes",
                tournament_id="another-tournament",
                championship_id="another-championship",
            )


def test_v10_migration_preserves_budget_attempt_and_foreign_keys(tmp_path: Path) -> None:
    database = tmp_path / "v10.db"
    _create_historical_database(database, version=10)
    _insert_legacy_provider_rows(database)

    inspection = inspect_database(database)
    assert inspection.schema_version == 10
    assert inspection.migration_required

    store = SQLiteStore(database, create=False)
    snapshot = store.load_provider_budget(_LEGACY_BUDGET[0])
    assert snapshot is not None
    assert snapshot.tournament_id is None
    assert snapshot.championship_id is None
    assert snapshot.policy == _LEGACY_POLICY
    assert snapshot.spent.calls == 1
    assert snapshot.reserved.calls == 0

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT championship_id FROM provider_budgets WHERE budget_id = ?",
            (_LEGACY_BUDGET[0],),
        ).fetchone() == (None,)
        assert connection.execute(
            """
            SELECT
                budget_id, tournament_id, policy_json, policy_digest,
                limit_calls, limit_input_tokens, limit_output_tokens,
                limit_estimated_cost_nanos,
                spent_calls, spent_input_tokens, spent_output_tokens,
                spent_estimated_cost_nanos,
                reserved_calls, reserved_input_tokens, reserved_output_tokens,
                reserved_estimated_cost_nanos,
                poison_reason_code, created_at_epoch, finalized_at_epoch
            FROM provider_budgets
            """
        ).fetchone() == _LEGACY_BUDGET
        assert connection.execute("SELECT * FROM provider_call_attempts").fetchone() == (
            _LEGACY_ATTEMPT
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        attempt_foreign_key = connection.execute(
            "PRAGMA foreign_key_list(provider_call_attempts)"
        ).fetchone()
        assert attempt_foreign_key is not None
        assert (attempt_foreign_key[2], attempt_foreign_key[3], attempt_foreign_key[4]) == (
            "provider_budgets",
            "budget_id",
            "budget_id",
        )


def test_tampered_v10_manifest_is_rejected_before_migration(tmp_path: Path) -> None:
    database = tmp_path / "tampered-v10.db"
    _create_historical_database(database, version=10)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE provider_budgets ADD COLUMN unexpected TEXT")

    with pytest.raises(StorageError, match="v10 manifest"):
        SQLiteStore(database, create=False)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert "unexpected" in _provider_column_names(connection)
        assert "championship_id" not in _provider_column_names(connection)
        assert connection.execute(
            """
            SELECT count(*) FROM sqlite_schema
            WHERE type = 'table' AND name = 'provider_budgets_v11'
            """
        ).fetchone() == (0,)


def test_failed_v10_migration_rolls_back_parent_rebuild_and_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "failed-v10.db"
    _create_historical_database(database, version=10)
    _insert_legacy_provider_rows(database)
    original_migrate = SQLiteStore._migrate_provider_usage_to_v11

    def fail_after_rebuild(connection: sqlite3.Connection) -> None:
        original_migrate(connection)
        raise RuntimeError("injected v11 migration failure")

    monkeypatch.setattr(
        SQLiteStore,
        "_migrate_provider_usage_to_v11",
        staticmethod(fail_after_rebuild),
    )

    with pytest.raises(RuntimeError, match="injected v11 migration failure"):
        SQLiteStore(database, create=False)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 10
        assert "championship_id" not in _provider_column_names(connection)
        assert connection.execute("SELECT * FROM provider_budgets").fetchone() == _LEGACY_BUDGET
        assert connection.execute("SELECT * FROM provider_call_attempts").fetchone() == (
            _LEGACY_ATTEMPT
        )
        assert connection.execute(
            """
            SELECT count(*) FROM sqlite_schema
            WHERE type = 'table' AND name = 'provider_budgets_v11'
            """
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("version", (8, 9))
def test_v8_and_v9_upgrade_paths_reach_v11_with_valid_foreign_keys(
    tmp_path: Path,
    version: int,
) -> None:
    database = tmp_path / f"v{version}.db"
    _create_historical_database(database, version=version)

    SQLiteStore(database, create=False)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        assert {
            "championship_archives",
            "championship_checkpoints",
            "championship_runner_leases",
        } <= tables
        assert "championship_id" in _provider_column_names(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

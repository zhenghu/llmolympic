"""Complete SQLite v10 schema-manifest and fail-closed parser tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from llmolympic.core.sqlite_schema import (
    SchemaManifestError,
    extract_check_expressions,
    tokenize_schema_sql,
)
from llmolympic.core.storage import (
    SCHEMA_VERSION,
    SQLiteStore,
    StorageError,
    TournamentAuditError,
    audit_tournament,
    inspect_database,
)

SchemaTamper = Callable[[sqlite3.Connection], None]


def _execute_script(script: str) -> SchemaTamper:
    def tamper(connection: sqlite3.Connection) -> None:
        connection.executescript(script)

    return tamper


def _clone_entrants_with_unicode_long_s(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'entrants'"
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    clone_sql = row[0].replace(
        "CREATE TABLE entrants",
        'CREATE TABLE "entrantſ"',
        1,
    )
    assert clone_sql != row[0]
    connection.execute(clone_sql)


def _redirect_match_players_to_unicode_long_s(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'match_players'"
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    redirected_sql = row[0].replace(
        "REFERENCES entrants(entrant_id)",
        'REFERENCES "entrantſ"(entrant_id)',
        1,
    )
    assert redirected_sql != row[0]
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE match_players")
    connection.execute(redirected_sql)
    connection.execute("CREATE INDEX match_players_player_idx ON match_players(player, match_id)")
    connection.execute(
        "CREATE INDEX match_players_entrant_idx ON match_players(entrant_id, match_id)"
    )


def _remove_provider_attempt_strict_mode(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        """
        SELECT sql FROM sqlite_schema
        WHERE type = 'table' AND name = 'provider_call_attempts'
        """
    ).fetchone()
    assert row is not None and isinstance(row[0], str)
    strict_sql = row[0].rstrip()
    assert strict_sql.endswith(" STRICT")
    non_strict_sql = strict_sql.removesuffix(" STRICT")
    connection.execute("DROP TABLE provider_call_attempts")
    connection.execute(non_strict_sql)
    connection.execute(
        """
        CREATE INDEX provider_call_attempts_budget_state_idx
        ON provider_call_attempts(budget_id, state, attempt_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX provider_call_attempts_generation_state_idx
        ON provider_call_attempts(budget_id, runner_generation, state)
        WHERE runner_generation IS NOT NULL
        """
    )


SCHEMA_TAMPERS: tuple[tuple[str, SchemaTamper, str], ...] = (
    (
        "missing-table",
        _execute_script("DROP TABLE ratings;"),
        "table set",
    ),
    (
        "extra-column",
        _execute_script("ALTER TABLE matches ADD COLUMN unexpected TEXT;"),
        "column definitions",
    ),
    (
        "primary-key",
        _execute_script(
            """
            PRAGMA foreign_keys = OFF;
            DROP TABLE entrants;
            CREATE TABLE entrants (
                entrant_id TEXT,
                display_name TEXT NOT NULL,
                identity_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        ),
        "column definitions",
    ),
    (
        "unique-constraint",
        _execute_script(
            """
            PRAGMA foreign_keys = OFF;
            DROP TABLE series_matches;
            CREATE TABLE series_matches (
                series_id TEXT NOT NULL
                    REFERENCES series_archives(series_id) ON DELETE CASCADE,
                leg_number INTEGER NOT NULL CHECK (leg_number IN (1, 2)),
                match_id TEXT NOT NULL
                    REFERENCES matches(match_id) ON DELETE RESTRICT,
                PRIMARY KEY (series_id, leg_number)
            );
            """
        ),
        "unique constraints",
    ),
    (
        "check-constraint",
        _execute_script(
            """
            PRAGMA foreign_keys = OFF;
            DROP TABLE series_matches;
            CREATE TABLE series_matches (
                series_id TEXT NOT NULL
                    REFERENCES series_archives(series_id) ON DELETE CASCADE,
                leg_number INTEGER NOT NULL CHECK (leg_number IN (1, 2, 3)),
                match_id TEXT NOT NULL UNIQUE
                    REFERENCES matches(match_id) ON DELETE RESTRICT,
                PRIMARY KEY (series_id, leg_number)
            );
            """
        ),
        "CHECK constraints",
    ),
    (
        "foreign-key-action",
        _execute_script(
            """
            PRAGMA foreign_keys = OFF;
            DROP TABLE match_players;
            CREATE TABLE match_players (
                match_id TEXT NOT NULL
                    REFERENCES matches(match_id) ON DELETE RESTRICT,
                position INTEGER NOT NULL,
                player TEXT NOT NULL,
                entrant_id TEXT NOT NULL
                    REFERENCES entrants(entrant_id) ON DELETE RESTRICT,
                display_name TEXT NOT NULL,
                descriptor_json TEXT NOT NULL,
                score REAL NOT NULL,
                PRIMARY KEY (match_id, position)
            );
            CREATE INDEX match_players_player_idx
                ON match_players(player, match_id);
            CREATE INDEX match_players_entrant_idx
                ON match_players(entrant_id, match_id);
            """
        ),
        "foreign keys",
    ),
    (
        "explicit-index-descending",
        _execute_script(
            """
            DROP INDEX matches_finished_at_idx;
            CREATE INDEX matches_finished_at_idx ON matches(finished_at ASC);
            """
        ),
        "indexes",
    ),
    (
        "explicit-index-collation",
        _execute_script(
            """
            DROP INDEX match_players_entrant_idx;
            CREATE INDEX match_players_entrant_idx
                ON match_players(entrant_id COLLATE NOCASE, match_id);
            """
        ),
        "indexes",
    ),
    (
        "partial-index-predicate",
        _execute_script(
            """
            DROP INDEX tournament_runner_leases_expires_at_idx;
            CREATE INDEX tournament_runner_leases_expires_at_idx
                ON tournament_runner_leases(expires_at_epoch)
                WHERE token_digest IS NULL;
            """
        ),
        "indexes",
    ),
    (
        "extra-table",
        _execute_script("CREATE TABLE unexpected_table (value TEXT);"),
        "table set",
    ),
    (
        "sqlite-like-extra-table",
        _execute_script("CREATE TABLE sqliteX (value TEXT);"),
        "table set",
    ),
    (
        "unicode-casefold-clone-table",
        _clone_entrants_with_unicode_long_s,
        "table set",
    ),
    (
        "unicode-casefold-foreign-key-target",
        _redirect_match_players_to_unicode_long_s,
        "foreign keys",
    ),
    (
        "provider-attempt-without-strict",
        _remove_provider_attempt_strict_mode,
        "column definitions",
    ),
    (
        "extra-view",
        _execute_script("CREATE VIEW unexpected_view AS SELECT entrant_id FROM entrants;"),
        "unsupported table kind",
    ),
    (
        "sqlite-like-extra-view",
        _execute_script("CREATE VIEW sqliteView AS SELECT entrant_id FROM entrants;"),
        "unsupported table kind",
    ),
    (
        "extra-trigger",
        _execute_script(
            """
            CREATE TRIGGER unexpected_trigger
            AFTER INSERT ON entrants
            BEGIN
                SELECT NEW.entrant_id;
            END;
            """
        ),
        "view or trigger set",
    ),
    (
        "sqlite-like-extra-trigger",
        _execute_script(
            """
            CREATE TRIGGER sqliteTrigger
            AFTER INSERT ON entrants
            BEGIN
                UPDATE entrants
                SET display_name = display_name
                WHERE entrant_id = NEW.entrant_id;
            END;
            """
        ),
        "view or trigger set",
    ),
)


def test_fresh_v8_database_passes_the_complete_manifest(tmp_path: Path) -> None:
    database = tmp_path / "fresh-v8.db"

    SQLiteStore(database)
    SQLiteStore(database, create=False)
    inspection = inspect_database(database)

    assert SCHEMA_VERSION == 10
    assert inspection.schema_version == SCHEMA_VERSION
    assert not inspection.migration_required
    with pytest.raises(TournamentAuditError) as caught:
        audit_tournament("missing", database)
    assert caught.value.code == "tournament_not_found"


@pytest.mark.parametrize(
    ("case", "tamper", "manifest_category"),
    SCHEMA_TAMPERS,
    ids=[case for case, _, _ in SCHEMA_TAMPERS],
)
def test_schema_tampering_is_rejected_by_store_and_read_only_audit(
    tmp_path: Path,
    case: str,
    tamper: SchemaTamper,
    manifest_category: str,
) -> None:
    database = tmp_path / f"{case}.db"
    SQLiteStore(database)
    with sqlite3.connect(database) as connection:
        tamper(connection)

    with pytest.raises(StorageError, match=manifest_category):
        SQLiteStore(database, create=False)

    with pytest.raises(TournamentAuditError) as caught:
        audit_tournament("missing", database)
    assert caught.value.code == "database_invalid"


def test_manifest_mapping_rejects_duplicate_normalized_table_keys() -> None:
    canonical = SQLiteStore._expected_schema_manifest()
    duplicate = type(canonical)(
        tables=(*canonical.tables, canonical.tables[0]),
        auxiliary_objects=canonical.auxiliary_objects,
    )

    with pytest.raises(SchemaManifestError, match="collide"):
        duplicate.tables_by_name()


def test_schema_tokenizer_preserves_nested_and_string_parentheses_but_ignores_comments() -> None:
    sql = """
        CREATE TABLE sample (
            value TEXT,
            CHECK (
                (instr(value, '(') > 0 AND coalesce(length(value), (1 + (2 * 3))) > 0)
                AND value <> ')'
                AND value <> '/* not a comment ( */'
                /* CHECK (ignored_block) ) */
                -- CHECK (ignored_line) (
            )
        )
    """

    tokens = tokenize_schema_sql(sql)
    checks = extract_check_expressions(sql)

    assert len(checks) == 1
    assert {token.value for token in tokens if token.kind == "string"} == {
        "(",
        ")",
        "/* not a comment ( */",
    }
    assert "ignored_block" not in {token.value for token in tokens}
    assert "ignored_line" not in {token.value for token in tokens}
    assert sum(token.value == "(" for token in checks[0]) == sum(
        token.value == ")" for token in checks[0]
    )


@pytest.mark.parametrize(
    "sql",
    (
        "CREATE TABLE sample (value TEXT DEFAULT 'unterminated)",
        'CREATE TABLE "sample (value TEXT)',
        "CREATE TABLE sample (value TEXT /* unterminated)",
    ),
    ids=("string", "identifier", "comment"),
)
def test_schema_tokenizer_rejects_unterminated_tokens(sql: str) -> None:
    with pytest.raises(SchemaManifestError, match="unterminated"):
        tokenize_schema_sql(sql)


@pytest.mark.parametrize(
    "sql",
    (
        "CREATE TABLE sample (value INTEGER CHECK ((value > 0)",
        "CREATE TABLE sample (value INTEGER CHECK value > 0)",
    ),
    ids=("unbalanced", "unparenthesized-check"),
)
def test_schema_check_parser_fails_closed_on_malformed_constraints(sql: str) -> None:
    with pytest.raises(SchemaManifestError):
        extract_check_expressions(sql)

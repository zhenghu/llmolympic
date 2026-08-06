"""SQLite schema introspection and structural manifest comparison.

SQLite exposes most schema semantics through PRAGMA table-valued functions, but
CHECK expressions and partial-index predicates remain in ``sqlite_schema.sql``.
This module combines both sources into a normalized, immutable manifest.  It
never executes schema text and never interpolates database object names into
SQL, so it is safe to use on an untrusted read-only database.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass


class SchemaManifestError(ValueError):
    """The schema cannot be introspected safely or differs from the manifest."""


@dataclass(frozen=True, order=True)
class _Token:
    kind: str
    value: str


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    declared_type: str
    not_null: bool
    default: tuple[_Token, ...] | None
    primary_key_ordinal: int
    hidden: int


@dataclass(frozen=True, order=True)
class IndexKeySpec:
    column_id: int
    name: str | None
    descending: bool
    collation: str


@dataclass(frozen=True, order=True)
class IndexSpec:
    name: str | None
    unique: bool
    origin: str
    partial: bool
    keys: tuple[IndexKeySpec, ...]
    predicate: tuple[_Token, ...] | None


@dataclass(frozen=True, order=True)
class ForeignKeySpec:
    parent_table: str
    column_pairs: tuple[tuple[str, str | None], ...]
    on_update: str
    on_delete: str
    match: str


@dataclass(frozen=True)
class TableSpec:
    columns: tuple[ColumnSpec, ...]
    primary_key: tuple[str, ...]
    unique_constraints: tuple[IndexSpec, ...]
    foreign_keys: tuple[ForeignKeySpec, ...]
    explicit_indexes: tuple[IndexSpec, ...]
    checks: tuple[tuple[_Token, ...], ...]
    definition: tuple[_Token, ...]
    without_rowid: bool
    strict: bool
    table_kind: str


@dataclass(frozen=True)
class SchemaManifest:
    tables: tuple[tuple[str, TableSpec], ...]
    auxiliary_objects: tuple[tuple[str, str, str, tuple[_Token, ...]], ...]

    def tables_by_name(self) -> dict[str, TableSpec]:
        """Return a collision-free table map keyed by SQLite-normalized names."""

        result: dict[str, TableSpec] = {}
        for name, spec in self.tables:
            if name in result:
                raise SchemaManifestError(
                    "SQLite table names collide after identifier normalization"
                )
            result[name] = spec
        return result


_NUMBER_RE = re.compile(r"(?:0[xX][0-9A-Fa-f]+|(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)")
_MULTI_CHAR_OPERATORS = ("->>", "||", "->", "<=", ">=", "<>", "!=", "==", "<<", ">>")
_ASCII_LOWER_TRANSLATION = str.maketrans(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    "abcdefghijklmnopqrstuvwxyz",
)
_ASCII_UPPER_TRANSLATION = str.maketrans(
    "abcdefghijklmnopqrstuvwxyz",
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
)


def _ascii_lower(value: str) -> str:
    """Fold only ASCII A-Z, matching SQLite identifier comparison."""

    return value.translate(_ASCII_LOWER_TRANSLATION)


def _ascii_upper(value: str) -> str:
    return value.translate(_ASCII_UPPER_TRANSLATION)


def _decode_quoted(sql: str, start: int, quote: str) -> tuple[str, int]:
    closing = "]" if quote == "[" else quote
    index = start + 1
    value: list[str] = []
    while index < len(sql):
        character = sql[index]
        if character == closing:
            if index + 1 < len(sql) and sql[index + 1] == closing:
                value.append(closing)
                index += 2
                continue
            return "".join(value), index + 1
        value.append(character)
        index += 1
    raise SchemaManifestError("SQLite schema contains an unterminated quoted token")


def tokenize_schema_sql(sql: str) -> tuple[_Token, ...]:
    """Return case/whitespace/comment-insensitive tokens for SQLite schema SQL."""

    if not isinstance(sql, str) or not sql.strip():
        raise SchemaManifestError("SQLite schema object has no SQL definition")
    tokens: list[_Token] = []
    index = 0
    while index < len(sql):
        character = sql[index]
        if character.isspace():
            index += 1
            continue
        if sql.startswith("--", index):
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                raise SchemaManifestError("SQLite schema contains an unterminated comment")
            index = end + 2
            continue
        if character == "'":
            value, index = _decode_quoted(sql, index, character)
            tokens.append(_Token("string", value))
            continue
        if character in ('"', "`", "["):
            value, index = _decode_quoted(sql, index, character)
            tokens.append(_Token("word", _ascii_lower(value)))
            continue
        number = _NUMBER_RE.match(sql, index)
        if number is not None:
            tokens.append(_Token("number", _ascii_lower(number.group(0))))
            index = number.end()
            continue
        if character.isalpha() or character in ("_", "$") or ord(character) >= 128:
            end = index + 1
            while end < len(sql):
                candidate = sql[end]
                if not (candidate.isalnum() or candidate in ("_", "$") or ord(candidate) >= 128):
                    break
                end += 1
            tokens.append(_Token("word", _ascii_lower(sql[index:end])))
            index = end
            continue
        operator = next(
            (candidate for candidate in _MULTI_CHAR_OPERATORS if sql.startswith(candidate, index)),
            None,
        )
        if operator is not None:
            tokens.append(_Token("symbol", operator))
            index += len(operator)
            continue
        tokens.append(_Token("symbol", character))
        index += 1
    return tuple(tokens)


def _strip_outer_parentheses(tokens: tuple[_Token, ...]) -> tuple[_Token, ...]:
    while len(tokens) >= 2 and tokens[0] == _Token("symbol", "("):
        depth = 0
        closes_at_end = False
        for index, token in enumerate(tokens):
            if token == _Token("symbol", "("):
                depth += 1
            elif token == _Token("symbol", ")"):
                depth -= 1
                if depth < 0:
                    raise SchemaManifestError("SQLite schema contains unbalanced parentheses")
                if depth == 0:
                    closes_at_end = index == len(tokens) - 1
                    break
        if not closes_at_end:
            break
        tokens = tokens[1:-1]
    return tokens


def _balanced_contents(tokens: tuple[_Token, ...], opening: int) -> tuple[int, tuple[_Token, ...]]:
    if tokens[opening] != _Token("symbol", "("):
        raise SchemaManifestError("SQLite schema parser expected an opening parenthesis")
    depth = 0
    for index in range(opening, len(tokens)):
        token = tokens[index]
        if token == _Token("symbol", "("):
            depth += 1
        elif token == _Token("symbol", ")"):
            depth -= 1
            if depth == 0:
                return index, tuple(tokens[opening + 1 : index])
            if depth < 0:
                break
    raise SchemaManifestError("SQLite schema contains unbalanced parentheses")


def extract_check_expressions(sql: str) -> tuple[tuple[_Token, ...], ...]:
    tokens = tokenize_schema_sql(sql)
    checks: list[tuple[_Token, ...]] = []
    index = 0
    while index < len(tokens):
        if tokens[index] == _Token("word", "check"):
            if index + 1 >= len(tokens) or tokens[index + 1] != _Token("symbol", "("):
                raise SchemaManifestError("SQLite CHECK constraint has no parenthesized expression")
            end, expression = _balanced_contents(tokens, index + 1)
            checks.append(_strip_outer_parentheses(expression))
            index = end + 1
            continue
        index += 1
    return tuple(sorted(checks))


def _table_definition(sql: str) -> tuple[_Token, ...]:
    """Normalize a CREATE TABLE body while ignoring spelling of its prefix."""

    tokens = tokenize_schema_sql(sql)
    opening = next(
        (index for index, token in enumerate(tokens) if token == _Token("symbol", "(")),
        None,
    )
    if opening is None:
        raise SchemaManifestError("SQLite table definition has no column list")
    closing, body = _balanced_contents(tokens, opening)
    trailing = tokens[closing + 1 :]
    if any(token in (_Token("symbol", "("), _Token("symbol", ")")) for token in trailing):
        raise SchemaManifestError("SQLite table definition has unexpected trailing parentheses")
    return (*body, _Token("separator", "table_suffix"), *trailing)


def _partial_index_predicate(sql: str | None, *, partial: bool) -> tuple[_Token, ...] | None:
    if not partial:
        return None
    if sql is None:
        raise SchemaManifestError("SQLite partial index has no SQL definition")
    tokens = tokenize_schema_sql(sql)
    depth = 0
    for index, token in enumerate(tokens):
        if token == _Token("symbol", "("):
            depth += 1
        elif token == _Token("symbol", ")"):
            depth -= 1
            if depth < 0:
                raise SchemaManifestError("SQLite index definition has unbalanced parentheses")
        elif depth == 0 and token == _Token("word", "where"):
            predicate = _strip_outer_parentheses(tuple(tokens[index + 1 :]))
            if not predicate:
                raise SchemaManifestError("SQLite partial index has an empty predicate")
            return predicate
    raise SchemaManifestError("SQLite partial index has no WHERE predicate")


def _normalized_default(value: object) -> tuple[_Token, ...] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SchemaManifestError("SQLite column default is not text")
    return _strip_outer_parentheses(tokenize_schema_sql(value))


def _object_sql(connection: sqlite3.Connection, object_type: str, name: str) -> str | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type = ? AND name = ?",
        (object_type, name),
    ).fetchone()
    if row is None:
        raise SchemaManifestError("SQLite schema object disappeared during introspection")
    value = row[0]
    if value is not None and not isinstance(value, str):
        raise SchemaManifestError("SQLite schema object has an invalid SQL definition")
    return value


def _introspect_indexes(connection: sqlite3.Connection, table: str) -> tuple[IndexSpec, ...]:
    specs: list[IndexSpec] = []
    rows = connection.execute(
        'SELECT seq, name, "unique", origin, partial FROM pragma_index_list(?) ORDER BY seq',
        (table,),
    ).fetchall()
    for row in rows:
        name = row[1]
        if not isinstance(name, str):
            raise SchemaManifestError("SQLite index has an invalid name")
        origin = _ascii_lower(str(row[3]))
        if origin not in {"c", "u", "pk"}:
            raise SchemaManifestError("SQLite index has an unsupported origin")
        keys: list[IndexKeySpec] = []
        xinfo = connection.execute(
            """
            SELECT seqno, cid, name, \"desc\", coll, key
            FROM pragma_index_xinfo(?)
            ORDER BY seqno
            """,
            (name,),
        ).fetchall()
        for key_row in xinfo:
            if not key_row[5]:
                continue
            column_id = int(key_row[1])
            column_name = key_row[2]
            if column_id == -2 or column_name is None:
                raise SchemaManifestError(
                    "SQLite expression indexes are not supported by this schema"
                )
            keys.append(
                IndexKeySpec(
                    column_id=column_id,
                    name=_ascii_lower(str(column_name)),
                    descending=bool(key_row[3]),
                    collation=_ascii_lower(str(key_row[4] or "binary")),
                )
            )
        partial = bool(row[4])
        sql = _object_sql(connection, "index", name) if origin == "c" else None
        specs.append(
            IndexSpec(
                name=_ascii_lower(name) if origin == "c" else None,
                unique=bool(row[2]),
                origin=origin,
                partial=partial,
                keys=tuple(keys),
                predicate=_partial_index_predicate(sql, partial=partial),
            )
        )
    return tuple(specs)


def _introspect_foreign_keys(
    connection: sqlite3.Connection,
    table: str,
) -> tuple[ForeignKeySpec, ...]:
    grouped: dict[int, list[sqlite3.Row | tuple]] = defaultdict(list)
    rows = connection.execute(
        """
        SELECT id, seq, \"table\", \"from\", \"to\", on_update, on_delete, match
        FROM pragma_foreign_key_list(?)
        ORDER BY id, seq
        """,
        (table,),
    ).fetchall()
    for row in rows:
        grouped[int(row[0])].append(row)
    specs: list[ForeignKeySpec] = []
    for rows_for_key in grouped.values():
        rows_for_key.sort(key=lambda row: int(row[1]))
        first = rows_for_key[0]
        specs.append(
            ForeignKeySpec(
                parent_table=_ascii_lower(str(first[2])),
                column_pairs=tuple(
                    (
                        _ascii_lower(str(row[3])),
                        None if row[4] is None else _ascii_lower(str(row[4])),
                    )
                    for row in rows_for_key
                ),
                on_update=_ascii_lower(str(first[5])),
                on_delete=_ascii_lower(str(first[6])),
                match=_ascii_lower(str(first[7])),
            )
        )
    return tuple(sorted(specs))


def introspect_schema(connection: sqlite3.Connection) -> SchemaManifest:
    """Build a complete normalized manifest from an open SQLite connection."""

    try:
        table_rows = connection.execute(
            """
            SELECT name, type, ncol, wr, strict
            FROM pragma_table_list
            WHERE schema = 'main'
            ORDER BY name
            """
        ).fetchall()
    except sqlite3.Error as exc:
        raise SchemaManifestError("SQLite runtime cannot introspect table metadata") from exc

    tables: list[tuple[str, TableSpec]] = []
    try:
        for table_row in table_rows:
            table = str(table_row[0])
            if _ascii_lower(table).startswith("sqlite_"):
                continue
            table_kind = _ascii_lower(str(table_row[1]))
            if table_kind not in {"table", "virtual", "shadow"}:
                raise SchemaManifestError("SQLite schema contains an unsupported table kind")
            sql = _object_sql(connection, "table", table)
            if sql is None:
                raise SchemaManifestError("SQLite user table has no SQL definition")
            column_rows = connection.execute(
                """
                SELECT cid, name, type, \"notnull\", dflt_value, pk, hidden
                FROM pragma_table_xinfo(?)
                ORDER BY cid
                """,
                (table,),
            ).fetchall()
            columns = tuple(
                ColumnSpec(
                    name=_ascii_lower(str(row[1])),
                    declared_type=" ".join(_ascii_upper(str(row[2])).split()),
                    not_null=bool(row[3]),
                    default=_normalized_default(row[4]),
                    primary_key_ordinal=int(row[5]),
                    hidden=int(row[6]),
                )
                for row in column_rows
            )
            primary_key = tuple(
                column.name
                for column in sorted(
                    (column for column in columns if column.primary_key_ordinal),
                    key=lambda column: column.primary_key_ordinal,
                )
            )
            indexes = _introspect_indexes(connection, table)
            unique_constraints = tuple(sorted(index for index in indexes if index.origin == "u"))
            explicit_indexes = tuple(sorted(index for index in indexes if index.origin == "c"))
            primary_key_indexes = tuple(sorted(index for index in indexes if index.origin == "pk"))
            # Preserve PK auto-index semantics in the UNIQUE multiset.  INTEGER PRIMARY KEY
            # correctly has no such index, whereas composite/text PKs do.
            unique_constraints = (*unique_constraints, *primary_key_indexes)
            tables.append(
                (
                    _ascii_lower(table),
                    TableSpec(
                        columns=columns,
                        primary_key=primary_key,
                        unique_constraints=tuple(sorted(unique_constraints)),
                        foreign_keys=_introspect_foreign_keys(connection, table),
                        explicit_indexes=explicit_indexes,
                        checks=extract_check_expressions(sql),
                        definition=_table_definition(sql),
                        without_rowid=bool(table_row[3]),
                        strict=bool(table_row[4]),
                        table_kind=table_kind,
                    ),
                )
            )

        auxiliary_rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_schema
            WHERE type IN ('view', 'trigger')
            ORDER BY type, name
            """
        ).fetchall()
        auxiliary = tuple(
            (
                _ascii_lower(str(row[0])),
                _ascii_lower(str(row[1])),
                _ascii_lower(str(row[2])),
                tokenize_schema_sql(row[3]),
            )
            for row in auxiliary_rows
            if not _ascii_lower(str(row[1])).startswith("sqlite_")
        )
    except (IndexError, TypeError, ValueError, sqlite3.Error) as exc:
        if isinstance(exc, SchemaManifestError):
            raise
        raise SchemaManifestError("SQLite schema metadata is invalid") from exc
    return SchemaManifest(tables=tuple(tables), auxiliary_objects=auxiliary)


def verify_schema_manifest(
    connection: sqlite3.Connection,
    expected: SchemaManifest,
) -> None:
    """Fail with a stable structural category when ``connection`` differs."""

    actual = introspect_schema(connection)
    expected_tables = expected.tables_by_name()
    actual_tables = actual.tables_by_name()
    if set(actual_tables) != set(expected_tables):
        raise SchemaManifestError("SQLite table set differs from the schema manifest")
    if actual.auxiliary_objects != expected.auxiliary_objects:
        raise SchemaManifestError("SQLite view or trigger set differs from the schema manifest")

    comparisons = (
        ("columns", "column definitions"),
        ("primary_key", "primary key"),
        ("unique_constraints", "unique constraints"),
        ("foreign_keys", "foreign keys"),
        ("explicit_indexes", "indexes"),
        ("checks", "CHECK constraints"),
        ("definition", "table clauses"),
        ("without_rowid", "WITHOUT ROWID mode"),
        ("strict", "STRICT mode"),
        ("table_kind", "table kind"),
    )
    for table in sorted(expected_tables):
        actual_table = actual_tables[table]
        expected_table = expected_tables[table]
        for attribute, label in comparisons:
            if getattr(actual_table, attribute) != getattr(expected_table, attribute):
                raise SchemaManifestError(f"SQLite table {table} has invalid {label}")

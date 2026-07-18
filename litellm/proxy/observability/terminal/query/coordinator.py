from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import duckdb
from duckdb import StatementType
from pydantic import JsonValue, TypeAdapter

from litellm.proxy.observability.terminal.archive.catalog import SegmentRecord

_ROWS = TypeAdapter(list[tuple[JsonValue, ...]])


@dataclass(frozen=True, slots=True)
class QueryRows:
    columns: tuple[str, ...]
    rows: tuple[tuple[JsonValue, ...], ...]


@dataclass(frozen=True, slots=True)
class QueryRejected:
    detail: str


@dataclass(frozen=True, slots=True)
class QueryFailed:
    detail: str


QueryResult: TypeAlias = QueryRows | QueryRejected | QueryFailed


class QueryCoordinator:
    def __init__(self, root: Path, segments: tuple[SegmentRecord, ...], extension_directory: Path) -> None:
        self._connection = duckdb.connect()
        self._connection.execute("SET autoinstall_known_extensions=false")
        self._connection.execute("SET autoload_known_extensions=false")
        self._connection.execute("SET extension_directory=?", [str(extension_directory)])
        self._connection.execute("LOAD sqlite")
        relations: tuple[str, ...] = ()
        for index, segment in enumerate(segments):
            schema = f"segment_{index}"
            path = root / segment.path
            escaped_path = str(path).replace("'", "''")
            self._connection.execute(f"ATTACH '{escaped_path}' AS {schema} (TYPE SQLITE, READ_ONLY)")
            relations = (*relations, f"SELECT * FROM {schema}.terminal_events")
        if relations:
            self._connection.execute("CREATE VIEW terminal_events AS " + " UNION ALL BY NAME ".join(relations))
        else:
            self._connection.execute(
                "CREATE VIEW terminal_events AS SELECT NULL::VARCHAR event_id,NULL::VARCHAR event_type,NULL::BLOB frame WHERE false"
            )

    def query(self, sql: str) -> QueryResult:
        try:
            statements = self._connection.extract_statements(sql)
            if len(statements) != 1 or statements[0].type != StatementType.SELECT:
                return QueryRejected("exactly one read-only SELECT statement is required")
            cursor = self._connection.execute(sql)
            columns = tuple(item[0] for item in cursor.description)
            rows = tuple(_ROWS.validate_python(cursor.fetchall()))
            return QueryRows(columns, rows)
        except duckdb.Error as exception:
            return QueryFailed(str(exception))

    def close(self) -> None:
        self._connection.close()

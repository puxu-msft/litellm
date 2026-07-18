from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import duckdb


def _make_segment(path: Path, *, version: int) -> None:
    with sqlite3.connect(path) as connection:
        if version == 1:
            connection.execute("CREATE TABLE requests(id TEXT PRIMARY KEY, model TEXT NOT NULL)")
            connection.execute("INSERT INTO requests VALUES ('r1', 'opus')")
        else:
            connection.execute(
                "CREATE TABLE requests(id TEXT PRIMARY KEY, model TEXT NOT NULL, status INTEGER)"
            )
            connection.execute("INSERT INTO requests VALUES ('r2', 'sonnet', 200)")


def _query_segments(connection: duckdb.DuckDBPyConnection, first: Path, second: Path) -> list[tuple[object, ...]]:
    connection.execute(f"ATTACH '{first}' AS s1 (TYPE SQLITE, READ_ONLY)")
    connection.execute(f"ATTACH '{second}' AS s2 (TYPE SQLITE, READ_ONLY)")
    return connection.execute(
        "SELECT id, model, NULL::INTEGER status FROM s1.requests "
        "UNION ALL BY NAME SELECT id, model, status FROM s2.requests ORDER BY id"
    ).fetchall()


def _query_segments_auto_align(
    connection: duckdb.DuckDBPyConnection,
) -> list[tuple[object, ...]]:
    return connection.execute(
        "SELECT * FROM s1.requests UNION ALL BY NAME SELECT * FROM s2.requests ORDER BY id"
    ).fetchall()


def _offline_connection(extension_directory: Path | None = None) -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect()
    connection.execute("SET autoinstall_known_extensions = false")
    connection.execute("SET autoload_known_extensions = false")
    if extension_directory is not None:
        connection.execute("SET extension_directory = ?", [str(extension_directory)])
        connection.execute("LOAD sqlite")
    return connection


def run(*, prepare_vendored: bool) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="terminal-duckdb-poc-") as temporary:
        root = Path(temporary)
        first = root / "segment-1.sqlite"
        second = root / "segment-2.sqlite"
        extension_directory = root / "extensions"
        extension_directory.mkdir()
        _make_segment(first, version=1)
        _make_segment(second, version=2)

        bundled_connection = _offline_connection()
        loaded_extensions = bundled_connection.execute(
            "SELECT extension_name, loaded, installed FROM duckdb_extensions() WHERE extension_name='sqlite'"
        ).fetchall()
        bundled_error: str | None = None
        bundled_rows: list[tuple[object, ...]] = []
        try:
            bundled_rows = _query_segments(bundled_connection, first, second)
        except Exception as exception:
            bundled_error = f"{type(exception).__name__}: {exception}"
        finally:
            bundled_connection.close()

        install_error: str | None = None
        vendored_error: str | None = None
        vendored_rows: list[tuple[object, ...]] = []
        vendored_auto_rows: list[tuple[object, ...]] = []
        if prepare_vendored:
            install_connection = duckdb.connect()
            try:
                install_connection.execute("SET extension_directory = ?", [str(extension_directory)])
                install_connection.execute("INSTALL sqlite")
            except Exception as exception:
                install_error = f"{type(exception).__name__}: {exception}"
            finally:
                install_connection.close()

            if install_error is None:
                try:
                    vendored_connection = _offline_connection(extension_directory)
                    try:
                        vendored_rows = _query_segments(vendored_connection, first, second)
                        vendored_auto_rows = _query_segments_auto_align(vendored_connection)
                    finally:
                        vendored_connection.close()
                except Exception as exception:
                    vendored_error = f"{type(exception).__name__}: {exception}"

    expected_rows = [("r1", "opus", None), ("r2", "sonnet", 200)]
    return {
        "passed": (
            vendored_rows == expected_rows and vendored_auto_rows == expected_rows
            if prepare_vendored
            else bundled_rows == expected_rows
        ),
        "duckdb_version": duckdb.__version__,
        "sqlite_extension": loaded_extensions,
        "bundled_offline": {"passed": bundled_rows == expected_rows, "rows": bundled_rows, "error": bundled_error},
        "vendored_offline": {
            "attempted": prepare_vendored,
            "passed": vendored_rows == expected_rows,
            "rows": vendored_rows,
            "auto_align_rows": vendored_auto_rows,
            "install_error": install_error,
            "load_or_query_error": vendored_error,
        },
        "offline_policy": {
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prepare-vendored", action="store_true")
    args = parser.parse_args()
    result = run(prepare_vendored=args.prepare_vendored)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
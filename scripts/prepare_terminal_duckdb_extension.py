from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
from pydantic import TypeAdapter

_PATH = TypeAdapter(Path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare DuckDB sqlite extension for offline terminal archive queries")
    parser.add_argument("--directory", type=Path, required=True)
    arguments = parser.parse_args()
    directory = _PATH.validate_python(arguments.directory)
    directory.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect()
    try:
        connection.execute("SET extension_directory=?", [str(directory)])
        connection.execute("INSTALL sqlite")
    finally:
        connection.close()


if __name__ == "__main__":
    main()

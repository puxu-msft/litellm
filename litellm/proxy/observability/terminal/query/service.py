from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from litellm.proxy.observability.terminal.archive.catalog import CatalogOpened, open_catalog
from litellm.proxy.observability.terminal.query.coordinator import QueryCoordinator, QueryFailed, QueryResult


@dataclass(frozen=True, slots=True)
class CatalogQueryService:
    root: Path
    extension_directory: Path

    def query(self, sql: str) -> QueryResult:
        opened = open_catalog(self.root / "catalog.sqlite")
        if not isinstance(opened, CatalogOpened):
            return QueryFailed(opened.detail)
        catalog = opened.catalog
        try:
            coordinator = QueryCoordinator(self.root, catalog.segments(), self.extension_directory)
            try:
                return coordinator.query(sql)
            finally:
                coordinator.close()
        finally:
            catalog.close()


def query_service_from_env() -> CatalogQueryService | None:
    root = os.getenv("LITELLM_TERMINAL_ARCHIVE_DIR")
    extension = os.getenv("LITELLM_DUCKDB_EXTENSION_DIR")
    if not root or not extension:
        return None
    return CatalogQueryService(Path(root), Path(extension))

# Terminal observability Phase 0 PoCs

Status: experiments only. These scripts do not modify production code or configuration.

The directory tests the three technical gates frozen in `docs/superpowers/specs/2026-07-18-terminal-observability-event-archive-design.md`:

1. `run_collector_poc.py`: real uvicorn direct, `Multiprocess`, and `ChangeReload` dispatches with one Unix-stream collector owned by the parent process. The multiprocess case kills one worker and verifies replacement; the reload case touches a watched Python file and verifies old/new worker lifecycles; every mode verifies graceful SIGTERM.
2. `httpx_observer_poc.py`: differential tests for an `httpx.AsyncByteStream` observer across every single cut point, injected failures, and close propagation.
3. `duckdb_segments_poc.py`: two SQLite segments with schema evolution, attached to DuckDB with extension auto-install and auto-load disabled, then queried through `UNION ALL BY NAME`.

Run from the repository root:

```bash
PYTHONPATH=. uv run --no-sync -- python exp/terminal-observability-phase0/run_collector_poc.py --repeat 3 --output exp/terminal-observability-phase0/collector-result.json
PYTHONPATH=. uv run --no-sync -- python exp/terminal-observability-phase0/httpx_observer_poc.py --output exp/terminal-observability-phase0/httpx-result.json
uv run --with duckdb -- python exp/terminal-observability-phase0/duckdb_segments_poc.py --prepare-vendored --output exp/terminal-observability-phase0/duckdb-result.json
```

The DuckDB command records two distinct gates: whether the Python wheel contains an offline SQLite scanner, and whether an official extension prepared into an isolated directory can be loaded by a fresh connection with auto-install and auto-load disabled. The checked-in JSON files are measured results, not fixtures. `CONCLUSION.md` records what each result proves and what remains unproven.

Use `run_collector_poc.py --mode direct|multiprocess|reload` to isolate one uvicorn topology while debugging.
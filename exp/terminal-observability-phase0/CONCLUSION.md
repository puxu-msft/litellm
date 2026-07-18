# Terminal observability Phase 0 conclusions

Date: 2026-07-18

Status: the three architecture gates are viable. Main-process collector ownership passed for uvicorn direct/multiprocess/reload; the httpx transport observer contract passed; DuckDB cross-segment SQLite querying passed only with a build-time prepared official sqlite extension. Production integration remains unimplemented.

## 1. Uvicorn main-process collector

Result: **PASS** for direct, `Multiprocess(workers=2)`, and `ChangeReload`, repeated three complete runs.

Measured artifact: `collector-result.json`. It contains `repeat_count=3` and all nine mode runs, not only the final run.

The PoC starts a real Unix-stream collector before uvicorn dispatch, records the collector PID on every worker event, and runs the same three topology branches used by `litellm.proxy.shutdown.uvicorn_runner`.

Observed invariants:

- direct: collector PID equals the server PID; one worker startup and one lifespan shutdown event; exit code 0
- multiprocess: collector PID equals the supervisor parent and never any worker; two initial workers plus one replacement after an intentional `os._exit(23)`; two surviving workers emit lifespan shutdown; exit code 0
- reload: collector PID equals the reload supervisor parent; modifying an existing watched Python file produces one old and one replacement worker, both with lifespan shutdown; exit code 0
- no topology leaves a collector/runner/worker process behind
- every run records all known worker PIDs and verifies `/proc/<pid>` is absent after supervisor exit; all nine runs report `leaked_worker_pids=[]`

Critical startup contract discovered by the first red run:

- `uvicorn.Server.capture_signals()` restores the previous signal handler after graceful shutdown, then re-raises captured signals
- if the previous direct-mode SIGTERM disposition is the OS default, the re-raised SIGTERM terminates the process before the runner's collector `finally` executes
- the outer runner must own a SIGTERM handler before `server.run()` and keep it installed until collector close completes. Uvicorn temporarily replaces it; after graceful shutdown the re-raised signal reaches the outer handler, `server.run()` returns, and the runner closes the collector before restoring the prior disposition
- multiprocess/reload supervisors already own their parent signal lifecycle, but the collector must still be created before supervisor dispatch and closed after supervisor return

What this proves: a collector can be uniquely owned by the uvicorn parent across all supported topologies, including worker crash replacement and reload replacement.

What remains unproven: production collector IPC/spool/SQLite work, integration with the real CLI startup options, second-signal force exit, startup failure before socket binding, and collector failure/restart.

## 2. HTTPX observer seam

Result: **PASS** for the stream primitive and a real `httpx.AsyncBaseTransport` wrapper.

Measured artifact: `httpx-result.json`.

Coverage:

- every single cut point of an SSE payload
- multi-chunk payload with upstream failures before/after each chunk
- observer callback failure at every chunk position; observation disables itself and business bytes continue unchanged
- consumer stops after each chunk count; wrapper close propagates to the inner stream
- chunked request body preserves chunk order and bytes
- final httpx JSON request serialization uses UTF-8 bytes for non-ASCII text and is observed exactly
- an outer `AsyncBaseTransport` wraps the final request stream before the inner transport and wraps the returned response stream before the client consumes it; inner transport, observer, and consumer see identical request/response chunk sequences
- an inner transport that consumes the request and then raises preserves the exact exception at the caller while the observer request wrapper is closed in `finally`; no response wrapper is created

Critical ownership contract discovered by a red run:

- the observer transport creates the request-stream wrapper, so it must close that wrapper in `finally` after inner `handle_async_request()` returns or raises; both paths now have direct transport-level oracles
- the response-stream wrapper remains owned by `httpx.Response`; `Response.aclose()` must close observer and inner response streams
- observation failure must not be raised into the business stream; it produces an anomaly/incomplete capture and disables further observation for that stream

What this proves: the proposed shared transport observer can preserve request/response bytes, chunk boundaries, upstream exceptions, early consumer close, and close propagation under the httpx transport protocol.

What remains unproven: wiring the observer around LiteLLM's concrete `LiteLLMAiohttpTransport` and `AsyncHTTPTransport`, provider gating by `github_copilot`, real HTTP connection cancellation, retry-created clients, sync `HTTPHandler`, and production spool backpressure. These belong to the integration implementation/tests, not the primitive design.

## 3. DuckDB cross-segment query

Result: **CONDITIONAL PASS**.

Measured artifact: `duckdb-result.json`.

The PoC creates two SQLite segments with schema evolution (`requests(id, model)` and `requests(id, model, status)`), then queries them through DuckDB `UNION ALL BY NAME`.

Observed facts with DuckDB Python 1.5.4:

- the Python wheel does not bundle `sqlite_scanner`; with `autoinstall_known_extensions=false` and `autoload_known_extensions=false`, `ATTACH ... TYPE SQLITE` fails because the extension file is absent
- a build/preparation connection can install the official sqlite extension into an isolated extension directory
- a new connection pointed at that directory, with auto-install and auto-load both disabled, can explicitly `LOAD sqlite`, attach both SQLite files read-only, and return the expected cross-schema rows
- `SELECT * ... UNION ALL BY NAME SELECT * ...` automatically fills the older segment's missing `status` column with `NULL`; the query adapter does not need to manually synthesize that column for this additive evolution

Frozen implementation consequence:

- runtime must not download DuckDB extensions
- packaging/deployment must prepare and ship the official sqlite extension matched to the exact DuckDB version, platform, and architecture
- startup must explicitly load that vendored extension and fail the SQL/Web query feature clearly if version/platform validation fails; collector/archive/TUI continue independently
- dependency updates must update and test the extension artifact as one unit
- runtime keeps external local-file access enabled because the feature must read SQLite segments; download prevention is enforced by disabling extension auto-install/auto-load and loading only the fixed vendored extension path

What this proves: DuckDB can provide the required cross-segment, cross-schema logical query layer offline when the official extension is packaged alongside it.

What remains unproven: the chosen Python DuckDB dependency version for this repository, packaging in all supported local environments, query cancellation/resource limits, active WAL read snapshots, many-segment planning performance, and public table type adapters beyond the two-column evolution fixture.

## 4. Initial red results retained as evidence

The PoCs intentionally retained failures that changed the design rather than hiding them:

- the first JSON request oracle wrongly expected `é` to be ASCII-escaped; the actual final httpx body is UTF-8. The oracle was corrected against the real request bytes
- the first collector close stopped an event loop before `serve_forever` completed and printed a traceback; the collector now uses `run_forever`, closes/waits the server, then shuts down async generators
- direct mode initially exited with `-15` and no worker shutdown event; this exposed uvicorn's signal restore/re-raise contract and led to the required outer runner signal ownership
- reload initially watched an empty directory and did not replace the worker; changing an existing watched Python file is the stable oracle
- subprocess PIPE inheritance made an exited reload runner look hung; runner output now goes to files and the driver waits on the direct child
- the DuckDB wheel-only offline test failed; the result is preserved separately from the successful vendored-extension test
- the first chunked request test called a nonexistent `Request.aclose()`; the corrected ownership test closes the streams at the transport boundary that created them

## 5. Decision impact

No ADR reversal is required. The frozen architecture remains viable with three implementation constraints now promoted from assumptions to verified contracts:

1. the LiteLLM uvicorn runner must own the collector and outer direct-mode signal handler
2. the shared httpx observer must own request-wrapper close and delegate response-wrapper close to `Response.aclose()`
3. DuckDB's official sqlite extension must be a vendored build artifact; the wheel alone is insufficient for offline runtime

The next artifact should be a TDD implementation plan. It must keep the current PoC as TTY owner while Phase 1 shadow archive work lands, and it must include integration tests for the unproven boundaries listed above.

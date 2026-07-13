# Upstream HTTP Client Config Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to execute this plan. Do not implement from memory; follow the referenced skill's process exactly, phase by phase, task by task, step by step.

**Goal:** Give **any provider that configures `http_client`** (per-deployment `litellm_params.http_client`, or the global `litellm_settings.http_client` covering every provider at once) a fine-grained upstream HTTP client config surface — connect/read/pool/total timeouts, `http2` reserved — that resolves per-request across global settings, per-deployment `litellm_params`, and legacy per-face defaults, and enforces a single asyncio-level absolute deadline across SDK retries and both phases of streaming, without ever leaking into the outbound wire body. This is a **universal opt-in, not a `github_copilot`-only gate**: a provider that never sets `http_client` (directly or via the global setting) keeps its exact existing timeout behavior unchanged — `github_copilot` is simply the first, motivating consumer. Per the frozen spec's updated non-goals section, honoring this for providers gated behind `supports_httpx_timeout()` (chat's `CompletionTimeout.resolve()` degrades an `httpx.Timeout` back to a bare float for providers not on that allowlist) requires `github_copilot` to be added to that allowlist **and** a safety net so an `http_client`-configured provider's resolved `httpx.Timeout` is never silently degraded merely for being absent from that list (Phase 1, new Task 4a below).

**Architecture:** A new pure-logic module (`http_client_config.py`) parses/merges/resolves the config into an `httpx.Timeout` (component-level knobs) plus an absolute deadline (`total_timeout`); a new dependency-free helper module (`asyncio_deadline.py`) enforces that deadline via `asyncio.wait_for` around both the single non-streaming await and the streaming byte-iterator, uniformly across chat/responses/messages, by attaching the resolved deadline to the already-ubiquitous `Logging` object (no new parameters threaded through existing large signatures). Router requires no production changes: it already forwards `litellm_params` (hence `http_client`) transitively into all three faces.

**Tech Stack:** Python 3.10–3.13, Pydantic v2 (frozen `BaseModel`), `httpx`, `asyncio.wait_for`, `anyio.CancelScope(shield=True)` (existing precedent in `streaming_handler.py`), pytest + `pytest-asyncio`, `respx`/`MagicMock`-based wire-body capture (matching existing `test_use_chat_completions_api_no_leak.py` pattern).

---

## Global Constraints

These are transcribed verbatim from the frozen spec (`docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md`) and the project `CLAUDE.md`. They apply to every task in every phase, not just the ones that mention them explicitly.

- **Python 3.10 compatibility**: no `asyncio.timeout()`/`asyncio.timeout_at()` (3.11+ only), no `X | Y` union syntax in runtime-evaluated positions if the module lacks `from __future__ import annotations`, no `match` on non-exhaustive unions without `assert_never`. The unified deadline helper must use `asyncio.wait_for` (available since 3.4) so the same code path runs unchanged on 3.10 through 3.13. This is a deliberate simplification: an earlier version of this design considered branching `asyncio.timeout_at` for 3.11+ vs. `asyncio.wait_for` for 3.10, but that would require two tested code paths for one behavior; **rejected in favor of a single `wait_for`-based helper for all supported versions** (recorded in "Alternatives Considered" below).
- **120-char line width** (not 88; project overrides Black's default via project config — do not reformat to 88).
- **LIT001/LIT002 immutability**: no mutable-default seeding (`x = []; x.append(...)`) — build with comprehensions/generators wrapped in `tuple()`/`frozenset()`. `# mutable-ok` is a last resort and must carry a real reason.
- **No mutation**: no reassigning parameters or module/instance state after construction where an immutable alternative exists. Frozen `dataclass(slots=True)` or frozen Pydantic `BaseModel` for all new value types.
- **Fully typed; no `Any` or coarse `dict`/`dict[str, Any]`.** New TypedDicts must mirror new Pydantic models field-for-field so untyped call sites can still opt in without introducing `Any`.
- **Pydantic boundary validation**: all new config models are parsed/validated once at the boundary (proxy config load, or Pydantic nested-model coercion via `GenericLiteLLMParams`), never re-validated ad hoc downstream.
- **Dependency injection, no monkeypatching (production code only)**: this constraint governs how *production* code is written — clock (`now`), HTTP client, and deadline are passed as parameters or attached to already-threaded objects (`logging_obj`, `litellm_params`); production code never patches class attributes or module globals to get its own behavior. It does **not** ban `unittest.mock.patch` inside test files, which is a standard, accepted test-isolation technique for exercising a unit without its real network/collaborator side effects. Where a test's assertions are actually about *whether a specific method was called with specific arguments* (e.g. Task 12/18/22's `set_http_client_deadline` capture tests), constructing the object with a fake collaborator via dependency injection and asserting on the fake is preferred over patching the method itself, and is used wherever the object under test accepts that collaborator as a constructor/call parameter (e.g. `with_deadline`'s injectable `now`, `DeadlineBoundAsyncIterator`'s injectable `on_timeout_close`). Patching a class method or module-level function is reserved for seams where the plan does *not* propose adding a new constructor parameter to an existing large signature purely for testability (e.g. capturing calls to `Logging.set_http_client_deadline` from inside `acompletion()`/`aresponses()`/`anthropic_messages()`, or replacing `AsyncHTTPHandler.post`/`.completion` at the wire boundary to capture outbound payloads) — adding such a parameter there would itself violate the "no new parameters threaded through existing large signatures" design decision recorded in "Alternatives Considered." Both styles are legitimate test code; only production code is held to "no monkeypatching."
- **Mutation-testing mindset, >90% kill rate target**: every new branch (especially `remaining <= 0`, `deadline is None`, merge-precedence ordering) needs a test that fails if that branch's logic is inverted or removed.
- **Conventional Commits** for every commit message (`feat:`, `fix:`, `test:`, `refactor:`, `docs:`, `chore:`).
- **`make pre-commit` gating**: run before every commit that touches `litellm/` (Python) — stage exactly what you intend to commit first, since it lints/type-checks the working tree, not just staged diff content in isolation.
- **Composition over inheritance, never-nester (early returns), don't throw — model failures as values where a public exception contract already exists, tagged unions + `match` for branching on API-surface shape.**
- **No file sprawl**: two new core modules (`http_client_config.py`, `asyncio_deadline.py`) house all new pure logic; everything else is a targeted edit to an existing file at an existing seam.
- **API-fragmentation-aware**: the three faces (chat/responses/messages) each get their own wiring task because their transport-layer call shapes differ, but all three share the same two core modules — no per-face reimplementation of parsing/merging/deadline logic.
- **HTTP/2 is schema-reserved only this round**: `http2: Optional[bool]` must parse/validate but must NOT be wired to any transport. A separate PoC is explicitly out of scope for this plan.
- **Private fork, no YAGNI cuts**: every clause of the frozen spec below must map to at least one task. Do not defer or narrow scope under a "not needed now" rationale — if a real fork appears, it is called out explicitly in "Open Items for Implementer," not silently dropped.
- **Test placement convention**: extend the existing mapped test file for a module before creating a new one; only create a new file when no test currently maps to that module, following that directory's naming convention.

---

## Component Naming Reference (must be used identically across every task)

| Name | Kind | Module |
|---|---|---|
| `HttpClientConfig` | frozen Pydantic `BaseModel` | `litellm/litellm_core_utils/http_client_config.py` |
| `HttpClientConfigDict` | `TypedDict, total=False` | `litellm/litellm_core_utils/http_client_config.py` |
| `parse_http_client_config(raw)` | function | `litellm/litellm_core_utils/http_client_config.py` |
| `merge_http_client_config(global_cfg, deployment_cfg)` | function | `litellm/litellm_core_utils/http_client_config.py` |
| `ResolvedHttpClientTimeout` | frozen `dataclass(slots=True)` | `litellm/litellm_core_utils/http_client_config.py` |
| `resolve_http_client_timeout(cfg, legacy_effective_timeout)` | function | `litellm/litellm_core_utils/http_client_config.py` |
| `establish_request_deadline(kwargs, *, now)` | function | `litellm/litellm_core_utils/http_client_config.py` |
| `warn_if_custom_client_bypasses_http_client_config(*, has_custom_client, http_client_config_present, context)` | function (Task 26, review finding #9) | `litellm/litellm_core_utils/http_client_config.py` |
| `warn_if_legacy_timeout_coexists_with_http_client(*, legacy_timeout, http_client, context)` | function (Task 7a, review finding F, 3rd review round) | `litellm/litellm_core_utils/http_client_config.py` |
| `DeadlineExceeded` | exception class (`TimeoutError` subclass) | `litellm/litellm_core_utils/asyncio_deadline.py` |
| `with_deadline(deadline, awaitable, *, now=None)` | async function | `litellm/litellm_core_utils/asyncio_deadline.py` |
| `DeadlineBoundAsyncIterator` | class, now with an `aclose()` method that delegates to the wrapped inner iterator's own `aclose`/`close` | `litellm/litellm_core_utils/asyncio_deadline.py` |
| `Logging.http_client_deadline` | instance attribute (`Optional[float]`) | `litellm/litellm_core_utils/litellm_logging.py` |
| `Logging.set_http_client_deadline(deadline)` | method | `litellm/litellm_core_utils/litellm_logging.py` |
| `GenericLiteLLMParams.http_client` | `Optional[HttpClientConfig]` field | `litellm/types/router.py` |
| `LiteLLMParamsTypedDict["http_client"]` | `Optional[HttpClientConfigDict]` | `litellm/types/router.py` |
| `DeadlineExceeded` -> `litellm.Timeout` mapping branch (Task 8a) | inline `isinstance(original_exception, DeadlineExceeded)` branch, placed before the existing string-matched Timeout block | `litellm/litellm_core_utils/exception_mapping_utils.py` (`exception_type()`) — one of five coordinated choke-point edits in Task 8a; see that task for the other four (chat SDK non-streaming/streaming transparency guards, the shared `_handle_error()` transparency guard, and the messages-face direct mapping) |
| `_STATUS_CODES_ELIGIBLE_FOR_MIDSTREAM_FALLBACK` (Task 16a) | `frozenset[int]` module-level constant, `{408, 429}` | `litellm/litellm_core_utils/streaming_handler.py` (`_handle_stream_fallback_error()`) |
| `ResponsesAPIStreamingIterator._any_chunk_yielded` (Task 20a) | instance attribute (`bool`) | `litellm/responses/streaming_iterator.py` |

---

## Phase 1: Config Schema, Parsing, Merging, Proxy Validation, Leak Prevention

Mirrors spec rollout step 1: establish the `http_client` config surface end-to-end (YAML → global setting → per-deployment field → parsed/merged/validated model) before any runtime behavior depends on it, and lock in that the raw config never reaches the outbound wire body.

### Task 1: `HttpClientConfig` schema + parse

**Files:**
- Create: `litellm/litellm_core_utils/http_client_config.py`
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py` (new — no existing file maps to this module)

- [ ] **Step 1: write failing test for `HttpClientConfig` field defaults, frozen-ness, and `>0` validation on every timeout field**
  ```python
  # tests/test_litellm/litellm_core_utils/test_http_client_config.py
  import pytest
  from pydantic import ValidationError

  from litellm.litellm_core_utils.http_client_config import HttpClientConfig


  def test_http_client_config_defaults_to_all_none():
      cfg = HttpClientConfig()
      assert cfg.connect_timeout is None
      assert cfg.read_timeout is None
      assert cfg.pool_timeout is None
      assert cfg.total_timeout is None
      assert cfg.http2 is None


  def test_http_client_config_is_frozen():
      cfg = HttpClientConfig(connect_timeout=5.0)
      with pytest.raises(ValidationError):
          cfg.connect_timeout = 10.0


  def test_http_client_config_rejects_unknown_keys():
      with pytest.raises(ValidationError):
          HttpClientConfig(unknown_field=1)


  @pytest.mark.parametrize("field_name", ["connect_timeout", "read_timeout", "pool_timeout", "total_timeout"])
  @pytest.mark.parametrize("bad_value", [0, 0.0, -1, -0.5])
  def test_http_client_config_rejects_non_positive_timeout_values(field_name, bad_value):
      """Every timeout field must reject 0 and negative values -- a 0s or negative
      connect/read/pool/total timeout is not a meaningful configuration and almost
      certainly indicates a misconfiguration that should fail loudly at parse time,
      not silently produce a client that times out instantly or never."""
      with pytest.raises(ValidationError):
          HttpClientConfig(**{field_name: bad_value})


  @pytest.mark.parametrize("field_name", ["connect_timeout", "read_timeout", "pool_timeout", "total_timeout"])
  def test_http_client_config_accepts_small_positive_timeout_values(field_name):
      cfg = HttpClientConfig(**{field_name: 0.001})
      assert getattr(cfg, field_name) == 0.001
  ```
  Run: `pytest tests/test_litellm/litellm_core_utils/test_http_client_config.py -x` — confirm `ModuleNotFoundError` (the module doesn't exist yet), and additionally confirm (once Step 2 below is written but before the `Field(gt=0)` constraint is added) that the non-positive-value tests specifically are the ones that fail if the constraint is ever removed — this is the mutation-testing anchor for major review finding #1.

- [ ] **Step 2: minimal implementation — `HttpClientConfig` + `HttpClientConfigDict`, with `Field(gt=0)` on every timeout field**
  ```python
  # litellm/litellm_core_utils/http_client_config.py
  """Config surface for per-provider upstream HTTP client tuning (connect/read/pool/total
  timeouts, HTTP/2 reservation). See docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.
  """

  from typing import Optional, TypedDict

  from pydantic import BaseModel, ConfigDict, Field


  class HttpClientConfig(BaseModel):
      """Parsed, validated upstream HTTP client configuration.

      All fields are optional: an absent field means "fall back to the next layer"
      (deployment -> global -> legacy per-face default), resolved by
      `merge_http_client_config` / `resolve_http_client_timeout`. Every present timeout
      field must be strictly positive (`gt=0`); 0 or negative is rejected at parse time
      rather than silently producing an instantly-expiring or infinite timeout.
      """

      model_config = ConfigDict(frozen=True, extra="forbid")

      connect_timeout: Optional[float] = Field(default=None, gt=0)
      read_timeout: Optional[float] = Field(default=None, gt=0)
      pool_timeout: Optional[float] = Field(default=None, gt=0)
      total_timeout: Optional[float] = Field(default=None, gt=0)
      http2: Optional[bool] = None  # schema-reserved only; not wired to any transport this round


  class HttpClientConfigDict(TypedDict, total=False):
      connect_timeout: Optional[float]
      read_timeout: Optional[float]
      pool_timeout: Optional[float]
      total_timeout: Optional[float]
      http2: Optional[bool]
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/http_client_config.py tests/test_litellm/litellm_core_utils/test_http_client_config.py
  git commit -m "feat: add HttpClientConfig schema for upstream http client tuning"
  ```

### Task 2: `parse_http_client_config`

**Files:**
- Modify: `litellm/litellm_core_utils/http_client_config.py`
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py`

- [ ] **Step 1: failing test — parse accepts `None`, dict, `HttpClientConfigDict`, and an already-built `HttpClientConfig`; rejects garbage types**
  ```python
  def test_parse_http_client_config_none_returns_none():
      from litellm.litellm_core_utils.http_client_config import parse_http_client_config

      assert parse_http_client_config(None) is None


  def test_parse_http_client_config_from_dict():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          parse_http_client_config,
      )

      parsed = parse_http_client_config({"connect_timeout": 5.0, "total_timeout": 30.0})
      assert parsed == HttpClientConfig(connect_timeout=5.0, total_timeout=30.0)


  def test_parse_http_client_config_passthrough_for_existing_model():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          parse_http_client_config,
      )

      cfg = HttpClientConfig(read_timeout=2.0)
      assert parse_http_client_config(cfg) is cfg


  def test_parse_http_client_config_rejects_unknown_keys():
      from pydantic import ValidationError

      from litellm.litellm_core_utils.http_client_config import parse_http_client_config

      with pytest.raises(ValidationError):
          parse_http_client_config({"not_a_real_field": 1})
  ```
  Run: confirm `ImportError`/`AttributeError` (function doesn't exist yet).

- [ ] **Step 2: implement `parse_http_client_config`**
  ```python
  from typing import Union


  def parse_http_client_config(
      raw: Optional[Union["HttpClientConfig", HttpClientConfigDict, dict]],
  ) -> Optional[HttpClientConfig]:
      """Coerce a raw `http_client` value (None, dict/TypedDict from YAML or kwargs, or an
      already-constructed HttpClientConfig) into a validated HttpClientConfig, or None."""
      if raw is None:
          return None
      if isinstance(raw, HttpClientConfig):
          return raw
      return HttpClientConfig(**raw)
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/http_client_config.py tests/test_litellm/litellm_core_utils/test_http_client_config.py
  git commit -m "feat: add parse_http_client_config coercion helper"
  ```

### Task 3: `merge_http_client_config` (deployment overrides global, field by field)

**Files:**
- Modify: `litellm/litellm_core_utils/http_client_config.py`
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py`

- [ ] **Step 1: failing tests for merge precedence**
  ```python
  def test_merge_http_client_config_both_none_returns_none():
      from litellm.litellm_core_utils.http_client_config import merge_http_client_config

      assert merge_http_client_config(None, None) is None


  def test_merge_http_client_config_deployment_only():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          merge_http_client_config,
      )

      deployment = HttpClientConfig(connect_timeout=1.0)
      assert merge_http_client_config(None, deployment) == deployment


  def test_merge_http_client_config_global_only():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          merge_http_client_config,
      )

      glob = HttpClientConfig(total_timeout=60.0)
      assert merge_http_client_config(glob, None) == glob


  def test_merge_http_client_config_deployment_field_wins_over_global_field():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          merge_http_client_config,
      )

      glob = HttpClientConfig(connect_timeout=1.0, total_timeout=60.0)
      deployment = HttpClientConfig(connect_timeout=9.0)
      merged = merge_http_client_config(glob, deployment)
      # deployment's explicit field wins; global's unset-in-deployment field carries through
      assert merged == HttpClientConfig(connect_timeout=9.0, total_timeout=60.0)


  def test_merge_http_client_config_deployment_none_field_does_not_shadow_global():
      """A field the deployment config leaves unset must fall back to global, not to None,
      even though HttpClientConfig's own default for an unset field IS None — merge must
      distinguish 'explicitly not set' via model_fields_set, not via `is None`."""
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          merge_http_client_config,
      )

      glob = HttpClientConfig(read_timeout=5.0)
      deployment = HttpClientConfig(connect_timeout=2.0)  # read_timeout left unset
      merged = merge_http_client_config(glob, deployment)
      assert merged.read_timeout == 5.0
      assert merged.connect_timeout == 2.0


  def test_merge_http_client_config_explicit_null_in_deployment_falls_back_to_global_not_cleared():
      """Regression for the updated frozen spec's null semantics (3rd-round review, new major
      finding E): the spec now explicitly states an explicit `null` in a deployment's http_client
      config (e.g. YAML `http_client: {connect_timeout: null}`) must be treated exactly like
      'connect_timeout was never set' -- falling back to global -- NOT as an instruction to clear
      the global value with None. There is no 'clear the global override' semantic in this design;
      `model_fields_set` alone cannot express this distinction because Pydantic marks a field as
      'set' whenever it is supplied to the constructor, even when the supplied value is None."""
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          merge_http_client_config,
      )

      glob = HttpClientConfig(connect_timeout=7.0)
      deployment = HttpClientConfig(connect_timeout=None, read_timeout=2.0)
      assert "connect_timeout" in deployment.model_fields_set  # confirms Pydantic tracks explicit None as "set"
      merged = merge_http_client_config(glob, deployment)
      assert merged.connect_timeout == 7.0  # falls back to global, NOT cleared to None
      assert merged.read_timeout == 2.0
  ```
  Run: confirm failure (function missing; the new null-semantics test would additionally fail
  against the *old* `model_fields_set`-only implementation shown further below in this task's
  history, since that implementation lets an explicit `None` shadow the global value).

- [ ] **Step 2: implement `merge_http_client_config` using `model_fields_set` filtered to non-`None` values, so "unset" and "explicitly null" both fall back to global — neither one clears it**
  ```python
  def merge_http_client_config(
      global_cfg: Optional[HttpClientConfig],
      deployment_cfg: Optional[HttpClientConfig],
  ) -> Optional[HttpClientConfig]:
      """Merge global and per-deployment http_client config, field by field. A field the
      deployment config explicitly sets to a non-None value wins; a field the deployment config
      leaves unset, OR explicitly sets to None (e.g. an explicit `null` in a deployment's YAML
      http_client block), falls back to the global config's value for that field. There is no
      "explicit null clears the global value" semantic — per the frozen spec, explicit null and
      "not set at all" are equivalent from the deployment's perspective."""
      if global_cfg is None and deployment_cfg is None:
          return None
      base_values = global_cfg.model_dump() if global_cfg is not None else {}
      override_values = (
          {
              field: value
              for field, value in deployment_cfg.model_dump(include=deployment_cfg.model_fields_set).items()
              if value is not None
          }
          if deployment_cfg is not None
          else {}
      )
      return HttpClientConfig(**{**base_values, **override_values})
  ```
  Run: tests pass, including the new null-semantics regression.
  Commit:
  ```
  git add litellm/litellm_core_utils/http_client_config.py tests/test_litellm/litellm_core_utils/test_http_client_config.py
  git commit -m "feat: add merge_http_client_config with field-level deployment precedence, explicit null falls back to global"
  ```

### Task 4: `ResolvedHttpClientTimeout` + `resolve_http_client_timeout` (per-face legacy fallback)

**Files:**
- Modify: `litellm/litellm_core_utils/http_client_config.py`
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py`

- [ ] **Step 1: failing tests — resolve falls back to the caller-supplied legacy timeout per component when `cfg` is `None`; when `cfg` sets a field, it wins per-axis even when the legacy timeout is itself an `httpx.Timeout` (not just a float)**
  ```python
  import httpx


  def test_resolve_http_client_timeout_no_config_uses_legacy_float():
      from litellm.litellm_core_utils.http_client_config import resolve_http_client_timeout

      resolved = resolve_http_client_timeout(None, legacy_effective_timeout=600.0)
      assert resolved.httpx_timeout == httpx.Timeout(600.0, connect=5.0)
      assert resolved.total_timeout is None


  def test_resolve_http_client_timeout_no_config_passes_through_legacy_httpx_timeout_unchanged():
      """cfg=None means 'no override at all' -- a caller with no http_client config keeps its
      exact existing httpx.Timeout, byte for byte. This is the "opt-in: unconfigured
      providers are untouched" guarantee, not a claim that httpx.Timeout legacy values are
      ever returned unmodified once cfg sets something (see the per-axis override test
      below, which is the fix for review finding #2)."""
      from litellm.litellm_core_utils.http_client_config import resolve_http_client_timeout

      legacy = httpx.Timeout(600.0, connect=10.0, read=20.0, pool=30.0)
      resolved = resolve_http_client_timeout(None, legacy_effective_timeout=legacy)
      assert resolved.httpx_timeout == legacy
      assert resolved.total_timeout is None


  def test_resolve_http_client_timeout_cfg_overrides_win_over_legacy_httpx_timeout_per_axis():
      """Regression for review finding #2: when the legacy effective timeout is ALREADY an
      httpx.Timeout (the common case for chat, whose CompletionTimeout.resolve() usually
      returns one), a configured http_client field must still win on its own axis, and an
      UNconfigured axis must fall back to legacy's value for that SAME axis (not to the
      legacy Timeout's overall default, and not silently pass the whole legacy Timeout
      through unmodified) -- otherwise http_client config is a silent no-op for every chat
      call whose legacy timeout resolves to an httpx.Timeout, which is most of them."""
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          resolve_http_client_timeout,
      )

      legacy = httpx.Timeout(600.0, connect=10.0, read=20.0, pool=30.0)
      cfg = HttpClientConfig(read_timeout=99.0)
      resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=legacy)
      assert resolved.httpx_timeout.read == 99.0  # cfg's explicit override wins
      assert resolved.httpx_timeout.connect == 10.0  # unconfigured axis falls back to legacy's OWN connect
      assert resolved.httpx_timeout.pool == 30.0  # unconfigured axis falls back to legacy's OWN pool


  def test_resolve_http_client_timeout_cfg_connect_override_falls_back_to_http_handler_default_not_legacy(): # noqa: E501
      """When cfg is provided (even empty) and BOTH cfg and the legacy httpx.Timeout leave
      connect unset, the per-axis fallback is HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS, matching the
      float-legacy branch's existing connect-default behavior -- connect is deliberately not
      defaulted to the overall/read timeout. (Rewritten in the 3rd review round -- see review
      finding A: this test previously passed `cfg=None`, which is self-contradictory under the
      corrected "cfg=None is a pure, unmodified passthrough" contract -- a `None` cfg must never
      turn a legacy `connect=None` into 5.0; that exact byte-for-byte-preservation case is
      already covered by
      `test_resolve_http_client_timeout_no_config_passes_through_legacy_httpx_timeout_unchanged`
      above. Passing an explicit, empty `HttpClientConfig()` is what actually activates
      per-axis merging and exercises this fallback.)"""
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          resolve_http_client_timeout,
      )

      legacy = httpx.Timeout(600.0, connect=None, read=20.0, pool=30.0)
      resolved = resolve_http_client_timeout(HttpClientConfig(), legacy_effective_timeout=legacy)
      assert resolved.httpx_timeout.connect == 5.0  # HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS
      assert resolved.httpx_timeout.read == 20.0  # unconfigured axis still falls back to legacy's own read
      assert resolved.httpx_timeout.pool == 30.0  # unconfigured axis still falls back to legacy's own pool


  def test_resolve_http_client_timeout_preserves_legacy_zero_connect_when_cfg_leaves_it_unset():
      """Regression for 3rd-round review finding A: per-axis merging must use `is not None`
      checks, never `or` -- `or` would incorrectly treat a legitimate legacy axis value of 0.0
      (a valid, if unusual, "no connect timeout" configuration some callers construct
      deliberately) as if it were missing, silently replacing it with
      HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS. This only manifests once cfg is non-None (activating
      the per-axis merge); the pure cfg=None passthrough case never touches this axis at all --
      see `test_resolve_http_client_timeout_no_config_passes_through_legacy_httpx_timeout_unchanged`."""
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          resolve_http_client_timeout,
      )

      legacy = httpx.Timeout(600.0, connect=0.0, read=20.0, pool=30.0)
      cfg = HttpClientConfig(read_timeout=99.0)  # cfg is non-None but leaves connect unset
      resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=legacy)
      assert resolved.httpx_timeout.connect == 0.0  # legacy's legitimate connect=0 is preserved, not replaced


  def test_resolve_http_client_timeout_partial_config_falls_back_per_component():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          resolve_http_client_timeout,
      )

      cfg = HttpClientConfig(read_timeout=15.0)
      resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=600.0)
      assert resolved.httpx_timeout.read == 15.0
      assert resolved.httpx_timeout.connect == 5.0  # HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS default
      assert resolved.httpx_timeout.pool == 600.0  # falls back to legacy total, per-component
      assert resolved.total_timeout is None


  def test_resolve_http_client_timeout_carries_total_timeout_through():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          resolve_http_client_timeout,
      )

      cfg = HttpClientConfig(total_timeout=45.0)
      resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=600.0)
      assert resolved.total_timeout == 45.0
      # total_timeout does not itself replace the per-attempt httpx timeout components
      assert resolved.httpx_timeout.connect == 5.0
  ```
  Run: confirm failure — specifically confirm `test_resolve_http_client_timeout_cfg_overrides_win_over_legacy_httpx_timeout_per_axis` fails against the OLD implementation (it must, since the old implementation returns `legacy_effective_timeout` completely unmodified whenever it is an `httpx.Timeout`, ignoring `cfg` entirely — that is exactly review finding #2); also confirm `test_resolve_http_client_timeout_preserves_legacy_zero_connect_when_cfg_leaves_it_unset` fails against the OLD implementation's `or`-based merge (it must, since `0.0 or HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS` evaluates to `HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS`, silently discarding a legitimate legacy `connect=0.0` — review finding A from the 3rd round).

- [ ] **Step 2: implement — two-mode contract (review finding A, 3rd round): `cfg=None` is a pure, unmodified passthrough of `legacy_effective_timeout` (an `httpx.Timeout` is returned as the exact same object, not reconstructed field by field, so even a legacy axis explicitly left at `None` is preserved byte for byte; a bare float is materialized into an `httpx.Timeout` only because there is no pre-existing per-axis value to preserve in that case — this is not a "modification" of anything). `cfg` non-`None` activates per-axis merging, using `is not None` checks everywhere (never `or`), so a legitimate legacy axis value of `0` is never mistaken for "unset."**
  ```python
  from dataclasses import dataclass
  from typing import Union

  import httpx

  from litellm.constants import HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS


  @dataclass(frozen=True, slots=True)
  class ResolvedHttpClientTimeout:
      httpx_timeout: httpx.Timeout
      total_timeout: Optional[float]


  def resolve_http_client_timeout(
      cfg: Optional[HttpClientConfig],
      legacy_effective_timeout: Union[float, httpx.Timeout],
  ) -> ResolvedHttpClientTimeout:
      """Resolve a validated HttpClientConfig (or None) plus the caller's face-specific
      legacy timeout (chat's 600s COMPLETION_HTTP_FALLBACK_SECONDS, responses' 6000s
      DEFAULT_REQUEST_TIMEOUT_SECONDS, messages' _default_cached_client_timeout(), or an
      explicit user-passed httpx.Timeout) into a concrete httpx.Timeout plus an optional
      absolute total_timeout.

      Two-mode contract (hard requirement, review finding A): when `cfg` is `None`, this
      function is a pure passthrough -- an already-constructed legacy `httpx.Timeout` is
      returned completely unmodified (same object, not reconstructed), and a bare legacy float
      is materialized into an `httpx.Timeout` using the existing connect-default constant
      purely because there is no pre-existing per-axis value to preserve; neither case is a
      "modification" of caller-visible behavior, since this is exactly what legacy code already
      did before this feature existed. Unconfigured providers therefore keep their EXACT
      existing behavior with zero risk of a silent regression.

      When `cfg` is non-`None`, every axis is merged using `is not None` checks: a field `cfg`
      sets (to any value, including `0`) always wins on its own axis; an axis `cfg` leaves
      unset falls back to legacy's OWN value for that same axis (never `or`, which would
      incorrectly treat a legitimate legacy axis value of `0` as missing and silently replace
      it -- review finding A from the 3rd round). `total_timeout` is only ever populated from
      `cfg` (there is no legacy equivalent to fall back to)."""
      if cfg is None:
          if isinstance(legacy_effective_timeout, httpx.Timeout):
              return ResolvedHttpClientTimeout(httpx_timeout=legacy_effective_timeout, total_timeout=None)
          return ResolvedHttpClientTimeout(
              httpx_timeout=httpx.Timeout(
                  legacy_effective_timeout, connect=HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS
              ),
              total_timeout=None,
          )

      if isinstance(legacy_effective_timeout, httpx.Timeout):
          legacy_connect = legacy_effective_timeout.connect
          legacy_read = legacy_effective_timeout.read
          legacy_pool = legacy_effective_timeout.pool
          legacy_write = legacy_effective_timeout.write
      else:
          legacy_connect = None
          legacy_read = legacy_effective_timeout
          legacy_pool = legacy_effective_timeout
          legacy_write = legacy_effective_timeout

      connect = (
          cfg.connect_timeout
          if cfg.connect_timeout is not None
          else (legacy_connect if legacy_connect is not None else HTTP_HANDLER_CONNECT_TIMEOUT_SECONDS)
      )
      read = cfg.read_timeout if cfg.read_timeout is not None else legacy_read
      pool = cfg.pool_timeout if cfg.pool_timeout is not None else legacy_pool
      return ResolvedHttpClientTimeout(
          httpx_timeout=httpx.Timeout(legacy_write, connect=connect, read=read, pool=pool),
          total_timeout=cfg.total_timeout,
      )
  ```
  Run: tests pass, including the new per-axis-override, rewritten connect-fallback, and legacy-zero-preservation tests.
  Commit:
  ```
  git add litellm/litellm_core_utils/http_client_config.py tests/test_litellm/litellm_core_utils/test_http_client_config.py
  git commit -m "fix: resolve_http_client_timeout is a pure passthrough when cfg is None, and never treats a legacy 0 as unset"
  ```

### Task 4a: `github_copilot` opt-in to `supports_httpx_timeout`, plus a chat-face safety net so an `http_client`-resolved `httpx.Timeout` is never silently degraded for a provider absent from that allowlist (review finding #3)

**Why this task exists:** `CompletionTimeout.resolve()` (`litellm/litellm_core_utils/completion_timeout.py:61`) flattens any `httpx.Timeout` back to a bare float — keeping only `.read` — for every `custom_llm_provider` not in `supports_httpx_timeout()`'s hardcoded `["openai", "azure", "bedrock"]` allowlist (`litellm/utils.py:2132-2141`). `github_copilot` is routed through the exact same `OpenAIChatCompletion` handler as `"openai"` (confirmed: Task 13's own regression test patches `litellm.main.openai_chat_completions.completion` for `model="github_copilot/gpt-4"`), so there is no technical reason for it to be excluded — the list was simply never updated for it. Per the spec's universal-opt-in decision, this task has two independent parts: (a) add `github_copilot` to the allowlist so its own `timeout=httpx.Timeout(...)` kwarg is honored on its own merits, matching `openai`/`azure`/`bedrock`; and (b) prove, with a dedicated regression test, the exact mechanism (already implicit in Task 13's Step 2 design, made explicit and pinned down here) by which an `http_client`-resolved `httpx.Timeout` reaches the provider call **unconditionally**, for **any** provider that configures `http_client` — including ones absent from `supports_httpx_timeout` — because Task 13 assigns the final `timeout` variable via `resolve_http_client_timeout(...).httpx_timeout` **after**, and unconditionally overriding, `CompletionTimeout.resolve()`'s own possibly-degraded result, whenever `http_client` is configured (deployment or global). Part (b) is the "safety net" the review demands instead of a bare allowlist entry: it does not rely on every current or future provider being added to `supports_httpx_timeout` for `http_client` to work.

**Files:**
- Modify: `litellm/utils.py:2136` (add `"github_copilot"` to `supported_providers`)
- Test: `tests/test_litellm/test_utils.py` (new tests for `supports_httpx_timeout` itself); `tests/test_litellm/test_completion_timeout_resolution.py` (extend — safety-net regression, depends on Task 13's production code already being in place)

- [ ] **Step 1: failing test — `supports_httpx_timeout("github_copilot")` is `True`**
  ```python
  # append to tests/test_litellm/test_utils.py
  def test_supports_httpx_timeout_includes_github_copilot():
      from litellm.utils import supports_httpx_timeout

      assert supports_httpx_timeout("github_copilot") is True


  def test_supports_httpx_timeout_excludes_unlisted_provider():
      from litellm.utils import supports_httpx_timeout

      # a provider never added to the allowlist keeps the existing degrade-to-float behavior
      # for its OWN explicit httpx.Timeout kwarg -- this is the "everything else keeps its
      # exact existing behavior" half of the opt-in contract, verified so Step 2 below can't
      # accidentally widen the allowlist beyond github_copilot.
      assert supports_httpx_timeout("cohere") is False
  ```
  Run: confirm failure (first assertion fails against current `["openai", "azure", "bedrock"]` list).

- [ ] **Step 2: add `github_copilot` to the allowlist**
  ```python
  # litellm/utils.py, inside def supports_httpx_timeout(...), replacing line 2136:
      supported_providers = ["openai", "azure", "bedrock", "github_copilot"]
  ```
  Run: Step 1 tests pass.

- [ ] **Step 3: failing test (depends on Task 13 already being implemented) — a provider absent from `supports_httpx_timeout` still receives a full, non-degraded `httpx.Timeout` from `completion()` when `http_client` is configured**
  ```python
  # append to tests/test_litellm/test_completion_timeout_resolution.py
  from unittest.mock import patch

  import httpx

  import litellm


  def test_completion_http_client_bypasses_supports_httpx_timeout_degrade_for_unlisted_provider():
      """Regression for review finding #3(b): http_client is a universal opt-in, not gated
      behind supports_httpx_timeout. A provider that is NOT on that allowlist (here `cohere`,
      pinned by test_supports_httpx_timeout_excludes_unlisted_provider above to stay off it)
      must still receive the full per-axis httpx.Timeout that http_client resolves to, not
      the float CompletionTimeout.resolve() would otherwise degrade it to."""
      captured_timeout = []

      def _fake_provider_call(*args, **kwargs):
          captured_timeout.append(kwargs.get("timeout"))
          return {"choices": [{"message": {"content": "ok"}}]}

      with patch("litellm.main.base_llm_http_handler.completion", side_effect=_fake_provider_call):
          litellm.completion(
              model="cohere/command-r",
              messages=[{"role": "user", "content": "hi"}],
              http_client={"connect_timeout": 2.0, "read_timeout": 9.0},
          )

      assert len(captured_timeout) == 1
      resolved = captured_timeout[0]
      assert isinstance(resolved, httpx.Timeout), (
          "http_client-resolved httpx.Timeout must not be degraded to a float merely because "
          "the provider is absent from supports_httpx_timeout's allowlist"
      )
      assert resolved.connect == 2.0
      assert resolved.read == 9.0
  ```
  Run: this passes as soon as Task 13's Step 2 is in place (no additional production code beyond Task 13 + Step 2 of this task is required — this step exists to pin the safety-net behavior down as an explicit, independently-owned regression rather than an incidental side effect of Task 13's implementation, per the review's "design an exact mechanism, don't just add one allowlist entry" demand). Patch target verified against current code: `cohere`/`cohere_chat` routes through `_complete_cohere_chat()` (`litellm/main.py:2738-2798`), which unconditionally calls `base_llm_http_handler.completion(..., timeout=timeout, ...)` (`litellm/main.py:2783-2798`) — a single shared dispatcher object imported at module level in `main.py`, so patching `litellm.main.base_llm_http_handler.completion` captures the `timeout` kwarg it receives regardless of provider. **Open item for the implementer:** re-confirm this call site is unchanged at implementation time (`grep -n "_complete_cohere_chat\|base_llm_http_handler.completion" litellm/main.py`) since `main.py` is large and under active change; if `cohere`'s dispatch path has moved, substitute any other real provider absent from `supports_httpx_timeout` that still dispatches through a patchable shared handler (e.g. `vertex_ai`, which also appears to route through `base_llm_http_handler` per a similar pattern — verify before use, this plan has not confirmed it).
  Commit:
  ```
  git add litellm/utils.py tests/test_litellm/test_utils.py tests/test_litellm/test_completion_timeout_resolution.py
  git commit -m "feat: add github_copilot to supports_httpx_timeout allowlist; pin down http_client universal-opt-in safety net"
  ```

### Task 5: `GenericLiteLLMParams.http_client` + `LiteLLMParamsTypedDict["http_client"]` fields

**Files:**
- Modify: `litellm/types/router.py:217` (add field to `GenericLiteLLMParams`), `litellm/types/router.py:368` (add key to `LiteLLMParamsTypedDict`)
- Test: `tests/test_litellm/test_router.py`

- [ ] **Step 1: failing test — `GenericLiteLLMParams` accepts and nested-validates `http_client`**
  ```python
  # append to tests/test_litellm/test_router.py
  def test_generic_litellm_params_accepts_http_client_dict():
      from litellm.litellm_core_utils.http_client_config import HttpClientConfig
      from litellm.types.router import GenericLiteLLMParams

      params = GenericLiteLLMParams(http_client={"connect_timeout": 3.0})
      assert params.http_client == HttpClientConfig(connect_timeout=3.0)


  def test_generic_litellm_params_http_client_defaults_to_none():
      from litellm.types.router import GenericLiteLLMParams

      assert GenericLiteLLMParams().http_client is None


  def test_generic_litellm_params_rejects_invalid_http_client_keys():
      import pytest
      from pydantic import ValidationError

      from litellm.types.router import GenericLiteLLMParams

      with pytest.raises(ValidationError):
          GenericLiteLLMParams(http_client={"bogus_key": 1})
  ```
  Run: confirm failure (`extra_forbidden` not raised / field absent -> `AttributeError`).

- [ ] **Step 2: add the field**
  ```python
  # litellm/types/router.py, inside class GenericLiteLLMParams, right after the existing
  # `timeout: Optional[Union[float, str, httpx.Timeout]] = None` field (line 217):
      http_client: Optional[HttpClientConfig] = None
  ```
  Add the import near the top of `litellm/types/router.py` alongside the other `litellm_core_utils` imports:
  ```python
  from litellm.litellm_core_utils.http_client_config import HttpClientConfig
  ```
  Run: tests pass.

- [ ] **Step 3: mirror the field on `LiteLLMParamsTypedDict`**
  ```python
  # litellm/types/router.py, inside class LiteLLMParamsTypedDict, right after the existing
  # `timeout: Optional[Union[float, str, httpx.Timeout]]` line (line 368):
      http_client: Optional[HttpClientConfigDict]
  ```
  Add `HttpClientConfigDict` to the same import line above.
  Run: `pytest tests/test_litellm/test_router.py -k http_client -x` passes; `basedpyright litellm/types/router.py` clean.
  Commit:
  ```
  git add litellm/types/router.py tests/test_litellm/test_router.py
  git commit -m "feat: add http_client field to GenericLiteLLMParams and LiteLLMParamsTypedDict"
  ```

### Task 6: leak prevention — `all_litellm_params` registration

**Files:**
- Modify: `litellm/types/utils.py:3140` (insert two new entries right after `"stream_timeout",`)
- Test: `tests/test_litellm/types/test_types_utils.py`

- [ ] **Step 1: failing test — `http_client` is excluded from `get_non_default_completion_params`**

  *(3rd-round review, minor #2: the original draft also registered `_http_client_deadline` here and paired it with an exclusion test. That has been deleted — `_http_client_deadline` is never a public top-level kwarg a caller passes to `completion()`/`acompletion()`/`aresponses()`/`anthropic_messages()`; it is purely an internal parameter name threaded between `chunk_processor`, `DeadlineBoundAsyncIterator`, and `ResponsesAPIStreamingIterator.__anext__`, always derived from `logging_obj.http_client_deadline` via `establish_request_deadline()`, never sourced from a user's raw kwargs dict. `all_litellm_params`/`get_non_default_completion_params` exists specifically to protect the *public* kwargs-to-wire-body boundary, so a key that can never reach that boundary in the first place has nothing to protect — the exclusion and its test were dead code that could never fail even if the exclusion were removed. `http_client` itself stays: unlike `_http_client_deadline`, it IS a legitimate public kwarg — see Task 2's YAML/kwarg examples and Task 18a's tests, which pass `http_client=` directly into `litellm.aresponses(...)`/`async_response_api_handler(...)` calls.)*
  ```python
  # append to tests/test_litellm/types/test_types_utils.py
  def test_http_client_excluded_from_non_default_completion_params():
      from litellm.utils import get_non_default_completion_params

      kwargs = {"model": "gpt-4", "messages": [], "http_client": {"connect_timeout": 1.0}}
      assert "http_client" not in get_non_default_completion_params(kwargs)
  ```
  Run: confirm currently FAILS (`http_client` currently leaks through as a non-default param).

- [ ] **Step 2: register the key in `all_litellm_params`**
  ```python
  # litellm/types/utils.py, insert immediately after the existing line:
  #     "stream_timeout",
  # (line 3140), inside the same list literal:
      "http_client",
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/types/utils.py tests/test_litellm/types/test_types_utils.py
  git commit -m "fix: exclude http_client from completion optional params"
  ```

### Task 7: proxy-level global `http_client` validation

**Files:**
- Modify: `litellm/proxy/proxy_server.py` (new `elif key == "http_client":` branch between the existing `upperbound_key_generate_params` block ending at line 4310 and the `json_logs` block starting at line 4311)
- Test: `tests/test_litellm/proxy/test_proxy_server.py`

- [ ] **Step 1: failing test — invalid global `http_client` setting raises a descriptive error when `ProxyConfig.load_config()` loads it; valid setting is applied to `litellm.http_client`** *(review finding minor #1a: `load_config()` — confirmed at `proxy_server.py:3993-4044` — reads `litellm_settings` directly out of the YAML config it loads via `self.get_config()` and loops over it inline at line 4044; there is no separately-named `_update_general_settings_or_litellm_settings` helper, so the test must go through `load_config()` itself with a real temp config file, mirroring the existing `test_load_config_max_budget_env_var_coerced_to_float` pattern already in this file at line 2557.)*
  ```python
  # append to tests/test_litellm/proxy/test_proxy_server.py
  import yaml
  from unittest.mock import MagicMock


  @pytest.mark.asyncio
  async def test_load_config_rejects_invalid_global_http_client(tmp_path):
      from litellm.proxy.proxy_server import ProxyConfig

      test_config = {
          "model_list": [],
          "litellm_settings": {"http_client": {"connect_timeout": "not-a-number"}},
      }
      config_file = tmp_path / "config.yaml"
      config_file.write_text(yaml.dump(test_config))

      proxy_config = ProxyConfig()
      with pytest.raises(Exception, match="http_client"):
          await proxy_config.load_config(router=MagicMock(), config_file_path=str(config_file))


  @pytest.mark.asyncio
  async def test_load_config_accepts_valid_global_http_client(tmp_path):
      import litellm
      from litellm.litellm_core_utils.http_client_config import HttpClientConfig
      from litellm.proxy.proxy_server import ProxyConfig

      test_config = {
          "model_list": [],
          "litellm_settings": {"http_client": {"connect_timeout": 3.0}},
      }
      config_file = tmp_path / "config.yaml"
      config_file.write_text(yaml.dump(test_config))

      original_http_client = litellm.http_client
      try:
          proxy_config = ProxyConfig()
          await proxy_config.load_config(router=MagicMock(), config_file_path=str(config_file))
          assert litellm.http_client == HttpClientConfig(connect_timeout=3.0)
      finally:
          litellm.http_client = original_http_client
  ```
  Run: confirm failure — `litellm.http_client` doesn't exist yet, and no validation branch exists (the "rejects invalid" test fails because nothing raises; the "accepts valid" test fails with `AttributeError: module 'litellm' has no attribute 'http_client'`).

- [ ] **Step 2: add `litellm.http_client: Optional[HttpClientConfig] = None` global**
  ```python
  # litellm/__init__.py, alongside the existing `aclient_session: Optional[httpx.AsyncClient] = None` (line 391):
  http_client: Optional["HttpClientConfig"] = None
  ```
  Add a `TYPE_CHECKING`-guarded import of `HttpClientConfig` at the top of `litellm/__init__.py` to avoid import-cycle risk (mirrors existing lazy-import patterns in that file).

- [ ] **Step 3: add the validation branch in `proxy_server.py`**
  ```python
  # litellm/proxy/proxy_server.py, inserted between the end of the existing
  # `elif key == "upperbound_key_generate_params":` block (line 4310) and the existing
  # `elif key == "json_logs" and value is True:` block (line 4311):
  elif key == "http_client":
      from litellm.litellm_core_utils.http_client_config import parse_http_client_config

      try:
          parsed_http_client = parse_http_client_config(value)
      except Exception as e:
          raise ValueError(f"Invalid `http_client` setting in litellm_settings: {e}") from e
      verbose_proxy_logger.debug("setting litellm.http_client=%s", parsed_http_client)
      setattr(litellm, key, parsed_http_client)
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/__init__.py litellm/proxy/proxy_server.py tests/test_litellm/proxy/test_proxy_server.py
  git commit -m "feat: validate global http_client litellm_settings at proxy config load"
  ```

### Task 7a: coexistence warning — legacy `timeout` set alongside `http_client`, at both the global and per-deployment validation boundaries (new — review finding F, 3rd review round)

**Why this task exists:** the frozen spec (line 106) states verbatim: "旧 `timeout: float` 与 `http_client` 并存时 `http_client` 优先，加载时 warning。" ("When the legacy `timeout: float` and `http_client` coexist, `http_client` takes priority, and a warning is logged at load time.") The *priority* half of this is already a free side-effect of Task 4's `resolve_http_client_timeout` (an `http_client`-configured axis always wins over whatever `legacy_effective_timeout` resolves to) and Task 3's `merge_http_client_config` (deployment overrides global, field by field) — neither of those tasks needs to change. What is still missing is the *warning*, and the spec's "组件设计" section (lines 117-122) is explicit that it must fire at **load time**, at two distinct boundaries, not lazily at request time:
  - global: `litellm_settings.http_client` + `litellm_settings.request_timeout` coexisting, checked at the proxy config-load boundary Task 7 already added (`proxy_server.py`'s `elif key == "http_client":` branch, confirmed at `proxy_server.py:799-808` once Task 7 lands — re-grep to confirm exact lines before editing, since Task 7's own line numbers may have shifted after earlier tasks landed).
  - per-deployment: `litellm_params.timeout` + `litellm_params.http_client` coexisting on the *same* deployment, checked at the `Deployment`/`LiteLLM_Params` construction boundary — confirmed via reading `litellm/router.py:7301-7420` (`Router._create_deployment`) this session: `litellm_params: LiteLLM_Params = LiteLLM_Params(**_litellm_params)` at line 7318 is the exact construction point, called once per deployment from `set_model_list()`/`add_deployment()` for every entry in `model_list`, i.e., "model-list/Deployment 构造边界" from the spec.

  Both boundaries need the exact same "both non-None → warn" logic, so this task adds ONE new pure, dependency-free function in the shared `http_client_config.py` module (consistent with this plan's "API-fragmentation-aware: no per-face/per-boundary reimplementation of shared logic" constraint) and calls it from both boundaries.

**Files:**
- Modify: `litellm/litellm_core_utils/http_client_config.py` (new `warn_if_legacy_timeout_coexists_with_http_client` function)
- Modify: `litellm/proxy/proxy_server.py` (extend Task 7's `elif key == "http_client":` branch to also check `litellm_settings.get("request_timeout")`)
- Modify: `litellm/router.py` (`Router._create_deployment`, immediately after the `litellm_params: LiteLLM_Params = LiteLLM_Params(**_litellm_params)` line)
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py` (pure-function unit tests)
- Test: `tests/test_litellm/proxy/test_proxy_server.py` (global-boundary integration test)
- Test: `tests/router_unit_tests/test_router_helper_utils.py` (per-deployment-boundary integration test, extends the existing `test_create_deployment` neighborhood)

- [ ] **Step 1: failing unit tests for the pure warning function**
  ```python
  # append to tests/test_litellm/litellm_core_utils/test_http_client_config.py
  from unittest.mock import patch


  def test_warn_if_legacy_timeout_coexists_with_http_client_warns_when_both_set():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          warn_if_legacy_timeout_coexists_with_http_client,
      )

      with patch("litellm.litellm_core_utils.http_client_config.verbose_logger") as mock_logger:
          warn_if_legacy_timeout_coexists_with_http_client(
              legacy_timeout=600.0,
              http_client=HttpClientConfig(connect_timeout=1.0),
              context="global litellm_settings",
          )
      mock_logger.warning.assert_called_once()
      warning_text = mock_logger.warning.call_args[0][0] % mock_logger.warning.call_args[0][1:]
      assert "http_client" in warning_text
      assert "global litellm_settings" in warning_text


  def test_warn_if_legacy_timeout_coexists_with_http_client_silent_when_only_timeout_set():
      from litellm.litellm_core_utils.http_client_config import (
          warn_if_legacy_timeout_coexists_with_http_client,
      )

      with patch("litellm.litellm_core_utils.http_client_config.verbose_logger") as mock_logger:
          warn_if_legacy_timeout_coexists_with_http_client(
              legacy_timeout=600.0, http_client=None, context="irrelevant"
          )
      mock_logger.warning.assert_not_called()


  def test_warn_if_legacy_timeout_coexists_with_http_client_silent_when_only_http_client_set():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          warn_if_legacy_timeout_coexists_with_http_client,
      )

      with patch("litellm.litellm_core_utils.http_client_config.verbose_logger") as mock_logger:
          warn_if_legacy_timeout_coexists_with_http_client(
              legacy_timeout=None,
              http_client=HttpClientConfig(connect_timeout=1.0),
              context="irrelevant",
          )
      mock_logger.warning.assert_not_called()


  def test_warn_if_legacy_timeout_coexists_with_http_client_silent_when_neither_set():
      from litellm.litellm_core_utils.http_client_config import (
          warn_if_legacy_timeout_coexists_with_http_client,
      )

      with patch("litellm.litellm_core_utils.http_client_config.verbose_logger") as mock_logger:
          warn_if_legacy_timeout_coexists_with_http_client(
              legacy_timeout=None, http_client=None, context="irrelevant"
          )
      mock_logger.warning.assert_not_called()
  ```
  Run: confirm `ImportError` (function doesn't exist yet).

- [ ] **Step 2: implement the pure warning function**
  ```python
  # litellm/litellm_core_utils/http_client_config.py
  from litellm._logging import verbose_logger


  def warn_if_legacy_timeout_coexists_with_http_client(
      *,
      legacy_timeout: Optional[float],
      http_client: Optional[HttpClientConfig],
      context: str,
  ) -> None:
      """Log a load-time warning when both the legacy `timeout` field and the new
      `http_client` config are set on the same scope (global litellm_settings, or a single
      deployment's litellm_params). `http_client` always wins in practice (see
      `resolve_http_client_timeout`/`merge_http_client_config`) -- this function only
      surfaces that precedence so operators are not silently confused about which
      setting is actually in effect."""
      if legacy_timeout is None or http_client is None:
          return
      verbose_logger.warning(
          "%s: both the legacy `timeout=%s` and `http_client` are configured; "
          "`http_client` takes priority and `timeout` will be ignored for the axes it covers.",
          context,
          legacy_timeout,
      )
  ```
  Run: unit tests pass.

- [ ] **Step 3: failing integration test — global boundary (`proxy_server.py`)**
  ```python
  # append to tests/test_litellm/proxy/test_proxy_server.py
  @pytest.mark.asyncio
  async def test_load_config_warns_on_global_timeout_http_client_coexistence(tmp_path):
      from litellm.proxy.proxy_server import ProxyConfig

      test_config = {
          "model_list": [],
          "litellm_settings": {
              "request_timeout": 600,
              "http_client": {"connect_timeout": 3.0},
          },
      }
      config_file = tmp_path / "config.yaml"
      config_file.write_text(yaml.dump(test_config))

      proxy_config = ProxyConfig()
      with patch("litellm.litellm_core_utils.http_client_config.verbose_logger") as mock_logger:
          await proxy_config.load_config(router=MagicMock(), config_file_path=str(config_file))
      mock_logger.warning.assert_called_once()


  @pytest.mark.asyncio
  async def test_load_config_silent_when_only_global_http_client_set(tmp_path):
      from litellm.proxy.proxy_server import ProxyConfig

      test_config = {
          "model_list": [],
          "litellm_settings": {"http_client": {"connect_timeout": 3.0}},
      }
      config_file = tmp_path / "config.yaml"
      config_file.write_text(yaml.dump(test_config))

      proxy_config = ProxyConfig()
      with patch("litellm.litellm_core_utils.http_client_config.verbose_logger") as mock_logger:
          await proxy_config.load_config(router=MagicMock(), config_file_path=str(config_file))
      mock_logger.warning.assert_not_called()
  ```
  Run: confirm failure — no warning is logged yet (both tests currently pass the `assert_not_called()` branch trivially, but `test_load_config_warns_on_global_timeout_http_client_coexistence`'s `assert_called_once()` fails).

- [ ] **Step 4: wire the global boundary — extend Task 7's `elif key == "http_client":` branch**
  ```python
  # litellm/proxy/proxy_server.py — extend the branch Task 7 added:
  elif key == "http_client":
      from litellm.litellm_core_utils.http_client_config import (
          parse_http_client_config,
          warn_if_legacy_timeout_coexists_with_http_client,
      )

      try:
          parsed_http_client = parse_http_client_config(value)
      except Exception as e:
          raise ValueError(f"Invalid `http_client` setting in litellm_settings: {e}") from e
      warn_if_legacy_timeout_coexists_with_http_client(
          legacy_timeout=litellm_settings.get("request_timeout"),
          http_client=parsed_http_client,
          context="global litellm_settings",
      )
      verbose_proxy_logger.debug("setting litellm.http_client=%s", parsed_http_client)
      setattr(litellm, key, parsed_http_client)
  ```
  Run: tests pass.

- [ ] **Step 5: failing integration test — per-deployment boundary (`router.py`)**
  ```python
  # append to tests/router_unit_tests/test_router_helper_utils.py
  def test_create_deployment_warns_on_timeout_http_client_coexistence(model_list):
      router = Router(model_list=model_list)
      with patch(
          "litellm.litellm_core_utils.http_client_config.verbose_logger"
      ) as mock_logger:
          router._create_deployment(
              deployment_info={},
              _model_name="gpt-5-mini",
              _litellm_params={
                  "model": "gpt-5-mini",
                  "api_key": "test",
                  "custom_llm_provider": "openai",
                  "timeout": 600,
                  "http_client": {"connect_timeout": 3.0},
              },
              _model_info={"id": "coexistence-test-id"},
          )
      mock_logger.warning.assert_called_once()


  def test_create_deployment_silent_when_only_http_client_set(model_list):
      router = Router(model_list=model_list)
      with patch(
          "litellm.litellm_core_utils.http_client_config.verbose_logger"
      ) as mock_logger:
          router._create_deployment(
              deployment_info={},
              _model_name="gpt-5-mini",
              _litellm_params={
                  "model": "gpt-5-mini",
                  "api_key": "test",
                  "custom_llm_provider": "openai",
                  "http_client": {"connect_timeout": 3.0},
              },
              _model_info={"id": "coexistence-test-id-2"},
          )
      mock_logger.warning.assert_not_called()
  ```
  Run: confirm failure — no warning is logged yet.

- [ ] **Step 6: wire the per-deployment boundary — `Router._create_deployment`**
  ```python
  # litellm/router.py, immediately after:
  #     litellm_params: LiteLLM_Params = LiteLLM_Params(**_litellm_params)
  # inside the existing try: block of _create_deployment:
              from litellm.litellm_core_utils.http_client_config import (
                  warn_if_legacy_timeout_coexists_with_http_client,
              )

              warn_if_legacy_timeout_coexists_with_http_client(
                  legacy_timeout=litellm_params.timeout
                  if isinstance(litellm_params.timeout, (int, float))
                  else None,
                  http_client=litellm_params.http_client,
                  context=f"deployment '{_model_name}'",
              )
  ```
  *(Note: `litellm_params.timeout` is typed `Optional[Union[float, str, httpx.Timeout]]` — the coexistence warning only makes sense for the plain-float legacy case, since an `os.environ/`-prefixed string is resolved elsewhere and an already-`httpx.Timeout` value is not what this specific spec line is warning about; a non-float, non-None `timeout` is silently treated as "not the legacy scalar case" here, not as "unset" — implementer should re-confirm this narrowing against how `LiteLLM_Params.timeout` values actually arrive by the time `_create_deployment` runs, since env-var resolution may happen earlier in the call chain.)*
  Run: all tests in this task pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/http_client_config.py litellm/proxy/proxy_server.py litellm/router.py \
      tests/test_litellm/litellm_core_utils/test_http_client_config.py \
      tests/test_litellm/proxy/test_proxy_server.py \
      tests/router_unit_tests/test_router_helper_utils.py
  git commit -m "feat: warn at load time when legacy timeout and http_client coexist on the same scope"
  ```

  **Implementer grep-confirmation needed:** re-verify `proxy_server.py:799-808` (Task 7's branch) and `router.py:7318` (`_create_deployment`'s `LiteLLM_Params(**_litellm_params)` line) against the actual working tree at the time this task is implemented, since earlier tasks in this plan (2 through 7) will have already landed and may have shifted these line numbers; also re-confirm that `LiteLLM_Params.timeout`'s value has not already been coerced away from a plain float by the time `_create_deployment` runs (e.g. by an earlier env-var-substitution pass over `_litellm_params` before it reaches this function) — if it has, the `isinstance(..., (int, float))` narrowing above may need to move earlier or be adjusted.

---

## Phase 2: Unified Deadline Infrastructure

Mirrors spec rollout step 2: build the deadline primitive and its carrier before any face wires into it.

### Task 8: `DeadlineExceeded` + `with_deadline`

**Files:**
- Create: `litellm/litellm_core_utils/asyncio_deadline.py`
- Test: `tests/test_litellm/litellm_core_utils/test_asyncio_deadline.py` (new)

- [ ] **Step 1: failing test — no deadline awaits normally; deadline in the future awaits normally; deadline already passed raises immediately without ever entering `wait_for`; deadline exceeded mid-await raises `DeadlineExceeded`**
  ```python
  # tests/test_litellm/litellm_core_utils/test_asyncio_deadline.py
  import asyncio

  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded, with_deadline


  @pytest.mark.asyncio
  async def test_with_deadline_none_awaits_normally():
      async def _coro():
          return "ok"

      assert await with_deadline(None, _coro()) == "ok"


  @pytest.mark.asyncio
  async def test_with_deadline_future_deadline_awaits_normally():
      async def _coro():
          return "ok"

      assert await with_deadline(asyncio.get_event_loop().time() + 10, _coro()) == "ok"


  @pytest.mark.asyncio
  async def test_with_deadline_past_deadline_raises_immediately_without_entering_wait_for():
      """remaining<=0 must raise directly; it must never call asyncio.wait_for at all,
      since wait_for(coro, timeout<=0) has version-dependent edge behavior we do not want
      to depend on."""
      calls = []

      async def _coro():
          calls.append("awaited")
          return "should not run"

      with pytest.raises(DeadlineExceeded):
          await with_deadline(asyncio.get_event_loop().time() - 1, _coro())
      assert calls == []


  @pytest.mark.asyncio
  async def test_with_deadline_exceeded_mid_await_raises_deadline_exceeded():
      async def _slow():
          await asyncio.sleep(10)
          return "too slow"

      with pytest.raises(DeadlineExceeded):
          await with_deadline(asyncio.get_event_loop().time() + 0.01, _slow())


  @pytest.mark.asyncio
  async def test_with_deadline_uses_injected_clock():
      """Dependency-injected clock must be used instead of the real loop clock, so tests
      don't need real sleeps to prove deadline math."""
      fake_now = [100.0]

      async def _coro():
          return "ok"

      # deadline is 100.0 (i.e. "now"): remaining == 0 -> must raise, not await
      with pytest.raises(DeadlineExceeded):
          await with_deadline(100.0, _coro(), now=lambda: fake_now[0])
  ```
  Run: confirm `ModuleNotFoundError`.

- [ ] **Step 2: implement**
  ```python
  # litellm/litellm_core_utils/asyncio_deadline.py
  """Unified asyncio-level absolute deadline enforcement, built on asyncio.wait_for so the
  same code path is correct on Python 3.10 through 3.13 (no version branching on
  asyncio.timeout/timeout_at). See docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.
  """

  import asyncio
  from typing import Awaitable, Callable, Optional, TypeVar

  T = TypeVar("T")


  class DeadlineExceeded(TimeoutError):
      """Raised when a request's absolute http_client.total_timeout deadline is exceeded,
      whether during the initial non-streaming await or during streaming byte iteration."""


  def _default_now() -> float:
      return asyncio.get_event_loop().time()


  async def with_deadline(
      deadline: Optional[float],
      awaitable: Awaitable[T],
      *,
      now: Optional[Callable[[], float]] = None,
  ) -> T:
      """Await `awaitable`, raising DeadlineExceeded if `deadline` (an absolute
      loop-time value, as produced by `establish_request_deadline`) has already passed or
      is exceeded before the awaitable completes. `deadline=None` means "no deadline":
      await normally. `now` is injectable for deterministic tests; defaults to the running
      loop's own clock."""
      if deadline is None:
          return await awaitable
      current = now() if now is not None else _default_now()
      remaining = deadline - current
      if remaining <= 0:
          if asyncio.iscoroutine(awaitable):
              awaitable.close()
          raise DeadlineExceeded(f"http_client total_timeout deadline already exceeded ({remaining=})")
      try:
          return await asyncio.wait_for(awaitable, timeout=remaining)
      except asyncio.TimeoutError as exc:
          raise DeadlineExceeded(
              f"http_client total_timeout deadline exceeded (remaining was {remaining}s)"
          ) from exc
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/asyncio_deadline.py tests/test_litellm/litellm_core_utils/test_asyncio_deadline.py
  git commit -m "feat: add with_deadline unified asyncio deadline helper"
  ```

### Task 8a: `DeadlineExceeded` -> `litellm.Timeout` exhaustive mapping across chat/responses/messages, plus Router retry-classification regression (review finding #5)

**Why this task exists and how it was designed:** review finding #5 says `DeadlineExceeded` is not mapped to the public `litellm.Timeout`/`APITimeoutError` contract, so all three face handlers fold it into a generic 500/provider error and Router's timeout-retry classification (which is purely `isinstance(exception, litellm.Timeout)`-based, see below) silently breaks. Investigated this session by reading the real exception-handling chain end to end for each face rather than assuming a single choke point exists — it does not; there are five distinct conversion points across three files, and they differ per face:

1. **`litellm/litellm_core_utils/exception_mapping_utils.py::exception_type()`** — the shared, final mapping function all three faces eventually call (chat via `main.py:690-698`'s outer `except Exception as e: raise exception_type(...)`; responses via `responses/main.py:555-556`'s equivalent inside `aresponses()`). Today it has no `DeadlineExceeded`-aware branch, so `DeadlineExceeded` falls through to the string-matching heuristics further down and typically ends up as a generic mapped error, not `Timeout`.
2. **`litellm/llms/openai/openai.py::OpenAIChatCompletion.acompletion()`** (chat non-streaming, the GHC path) — its own `except Exception as e:` (line 937) unconditionally rebuilds a bare `OpenAIError(status_code=getattr(e, "status_code", 500), ...)`, discarding `DeadlineExceeded`'s type *before* the exception ever reaches `main.py:690`'s `exception_type()` call. Fix (1) alone cannot help if this choke point isn't also fixed, since `exception_type()` never sees the original `DeadlineExceeded` instance.
3. **`litellm/llms/openai/openai.py::OpenAIChatCompletion.async_streaming()`** (chat streaming phase① — the initial stream-establishing await Task 15 wraps) — a structurally identical, but separately written, `except (Exception) as e:` (line 1087) with the same type-discarding effect.
4. **`litellm/llms/custom_httpx/llm_http_handler.py::BaseLLMHTTPHandler._handle_error()`** (line 5496) — the single shared choke point behind ~30 `except ...: raise self._handle_error(e=e, ...)` call sites across `_make_common_async_call`/`_make_common_sync_call` (chat's generic non-OpenAI-SDK path, e.g. any provider not using the OpenAI SDK client), the responses native handler (Task 19/20's `async_response_api_handler`), and the messages native handler (`async_anthropic_messages_handler`). It unconditionally builds `provider_config.get_error_class(status_code=getattr(e, "status_code", 500), ...)`, again discarding the original exception's type. Because this one method is reused by all three faces, fixing it once here is far more maintainable than patching ~30 individual call sites.
5. **`litellm/llms/anthropic/experimental_pass_through/messages/handler.py::anthropic_messages()`** (line 211, the messages face's only async public entry point) — verified there is **no** `exception_type()` call anywhere in this file's call chain (unlike chat and responses). The function dispatches the real work via `loop.run_in_executor(...)` to the sync `anthropic_messages_handler()`, gets back either a plain value or (when `_is_async=True`, which is always the case here since `kwargs["is_async"] = True` is set at line 341) an un-awaited coroutine, and does `if asyncio.iscoroutine(init_response): response = await init_response` (line 374-375) — **this `await` is the one place in the messages face where an internal exception first becomes observable**, and it is currently unguarded. Fixes (1) and (4) alone are not sufficient for messages: even with `_handle_error()` re-raising `DeadlineExceeded` transparently, nothing downstream of it in the messages face ever calls `exception_type()`, so the raw internal `DeadlineExceeded` would leak to callers of `litellm.anthropic_messages()` as-is. This face therefore needs a small, targeted, messages-specific mapping at this exact `await` site, using the `model`/`custom_llm_provider` locals already in scope there (a materially better source of debug context than reaching into `_handle_error()`'s `provider_config` argument, which does not uniformly expose a `custom_llm_provider` attribute across every config type in its `Union` — confirmed `BaseAnthropicMessagesConfig` has none at all).

None of fixes (2)-(5) require inventing a new mapping rule; they only need to *not destroy* the exception's identity before it reaches either the existing `exception_type()` call (chat, responses) or the new targeted mapping this task adds (messages).

**Files:**
- Modify: `litellm/litellm_core_utils/exception_mapping_utils.py` (add `DeadlineExceeded` import + isinstance branch, between the "End of Common Extra information" comment and the "Start of Provider Exception mapping" comment, i.e. between the current lines 2228 and 2230)
- Modify: `litellm/llms/openai/openai.py` (add `DeadlineExceeded` import; add one `except DeadlineExceeded: raise` clause each in `acompletion()` before its line-937 `except Exception as e:`, and in `async_streaming()` before its line-1087 `except (Exception) as e:`)
- Modify: `litellm/llms/custom_httpx/llm_http_handler.py` (add `DeadlineExceeded` import; add a two-line transparency guard at the top of `_handle_error()`, line 5496)
- Modify: `litellm/llms/anthropic/experimental_pass_through/messages/handler.py` (add a `try/except DeadlineExceeded` around the `response = await init_response` statement at line 375, mapping to `litellm.Timeout`)
- New test: `tests/test_litellm/litellm_core_utils/test_exception_mapping_utils.py` (extend — Fix A)
- New test: `tests/test_litellm/llms/openai/test_openai_http_client_deadline.py` (extend — the file Task 14/15 already created; Fix B/C)
- New test: `tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py` (extend — Fix D)
- New test: `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py` (extend — Fix E)
- New test file: `tests/test_litellm/router_utils/test_get_retry_from_policy.py` (new — no test file previously mapped `litellm/router_utils/get_retry_from_policy.py`; Router retry-classification regression)

- [ ] **Step 1: failing test — `exception_type()` maps a bare `DeadlineExceeded` to `litellm.Timeout`, preserving `model`/`llm_provider`**
  ```python
  # append to tests/test_litellm/litellm_core_utils/test_exception_mapping_utils.py
  import litellm


  def test_exception_type_maps_deadline_exceeded_to_litellm_timeout():
      from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
      from litellm.litellm_core_utils.exception_mapping_utils import exception_type

      original = DeadlineExceeded("http_client total_timeout deadline exceeded (remaining was -0.01s)")

      with pytest.raises(litellm.Timeout) as exc_info:
          exception_type(
              model="github_copilot/claude-opus-4.8",
              original_exception=original,
              custom_llm_provider="github_copilot",
          )

      # asserts the SPECIFIC new branch fired, not the pre-existing generic
      # string-matching Timeout path further down in exception_type() (which
      # produces a differently worded "APITimeoutError - Request timed out" message)
      assert "AsyncioDeadlineExceeded" in str(exc_info.value)
      assert exc_info.value.model == "github_copilot/claude-opus-4.8"
      assert exc_info.value.llm_provider == "github_copilot"
  ```
  Run: confirm failure (today this either falls through to the generic string-matching branch, producing a `Timeout` with the wrong message and possibly not even matching since `DeadlineExceeded`'s message text doesn't contain any of the matched substrings, or ends up as some other mapped/unmapped exception type entirely — either way, the `"AsyncioDeadlineExceeded" in str(exc_info.value)` assertion fails).

- [ ] **Step 2: add the mapping branch (Fix A)**
  ```python
  # litellm/litellm_core_utils/exception_mapping_utils.py
  # add near the top, alongside the other litellm_core_utils imports:
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded

  # inside def exception_type(...), insert between the current lines 2228 and 2230
  # (i.e. right after the "End of Common Extra information Needed for all providers"
  # comment block closes, and before the "Start of Provider Exception mapping" comment):
              if isinstance(original_exception, DeadlineExceeded):
                  exception_mapping_worked = True
                  raise Timeout(
                      message=f"AsyncioDeadlineExceeded - {error_str}",
                      model=model,
                      llm_provider=custom_llm_provider,
                      litellm_debug_info=extra_information,
                  )
  ```
  Note for the implementer: this branch sits inside the enclosing `if model:` block (same nesting as the existing string-matched Timeout branch a few lines below it), so it is skipped when `model` is falsy. Every call site in this codebase that calls `exception_type()` passes a real model string (it is a required parameter used for logging elsewhere), so this is not believed to be a practical gap — but it is a known, narrow corner case worth a one-line comment rather than silent reliance, since "exhaustive" is the explicit bar review finding #5 sets.
  Run: Step 1 test passes.
  Commit:
  ```
  git add litellm/litellm_core_utils/exception_mapping_utils.py tests/test_litellm/litellm_core_utils/test_exception_mapping_utils.py
  git commit -m "feat: map DeadlineExceeded to litellm.Timeout in exception_type()"
  ```

- [ ] **Step 3: failing tests — chat face's two OpenAI-SDK choke points (`acompletion()`, `async_streaming()`) propagate `DeadlineExceeded` unmodified instead of folding it into `OpenAIError`**
  ```python
  # append to tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  # (this file, and its existing imports of asyncio/AsyncMock/MagicMock/pytest/
  # DeadlineExceeded/OpenAIChatCompletion, were created by Task 14; add to it, don't recreate it)

  @pytest.mark.asyncio
  async def test_acompletion_propagates_deadline_exceeded_without_converting_to_openai_error():
      """Guards against acompletion()'s own except-Exception block flattening DeadlineExceeded
      into a generic OpenAIError -- if that happened, exception_type()'s new DeadlineExceeded
      branch (Step 2 above) would never see the original exception type and could never fire,
      silently breaking the mapping this task exists to guarantee."""
      handler = OpenAIChatCompletion()
      logging_obj = MagicMock()
      logging_obj.http_client_deadline = None  # deadline-already-passed precondition is Task 14's concern, not this one
      logging_obj.pre_call = MagicMock()

      handler.make_openai_chat_completion_request = AsyncMock(
          side_effect=DeadlineExceeded("simulated mid-await timeout")
      )

      with pytest.raises(DeadlineExceeded):
          await handler.acompletion(
              messages=[{"role": "user", "content": "hi"}],
              optional_params={},
              litellm_params={},
              provider_config=MagicMock(async_transform_request=AsyncMock(return_value={})),
              model="github_copilot/gpt-4",
              model_response=MagicMock(),
              logging_obj=logging_obj,
              timeout=600.0,
          )


  @pytest.mark.asyncio
  async def test_async_streaming_propagates_deadline_exceeded_without_converting_to_openai_error():
      """Same guard as above, for async_streaming()'s separately-written except-Exception block."""
      handler = OpenAIChatCompletion()
      logging_obj = MagicMock()
      logging_obj.http_client_deadline = None
      logging_obj.pre_call = MagicMock()

      handler.make_openai_chat_completion_request = AsyncMock(
          side_effect=DeadlineExceeded("simulated mid-await timeout")
      )

      with pytest.raises(DeadlineExceeded):
          await handler.async_streaming(
              timeout=600.0,
              messages=[{"role": "user", "content": "hi"}],
              optional_params={},
              litellm_params={},
              provider_config=MagicMock(transform_request=MagicMock(return_value={})),
              model="github_copilot/gpt-4",
              logging_obj=logging_obj,
          )
  ```
  Run: confirm failure — today both would raise `OpenAIError` instead of `DeadlineExceeded` (`pytest.raises(DeadlineExceeded)` fails with an unexpected-exception-type error naming `OpenAIError`).

- [ ] **Step 4: add the transparency guards (Fix B, Fix C)**
  ```python
  # litellm/llms/openai/openai.py
  # add near the top, alongside the existing with_deadline import (added by Task 14):
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded, with_deadline

  # inside async def acompletion(...), insert a new except clause immediately before
  # the existing `except Exception as e:` at line 937:
          except DeadlineExceeded:
              raise
          except Exception as e:
              ...  # unchanged

  # inside async def async_streaming(...), insert a new except clause immediately before
  # the existing `except (Exception) as e:` at line 1087:
          except DeadlineExceeded:
              raise
          except (
              Exception
          ) as e:  # need to exception handle here. async exceptions don't get caught in sync functions.
              ...  # unchanged
  ```
  Run: Step 3 tests pass.
  Commit:
  ```
  git add litellm/llms/openai/openai.py tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  git commit -m "fix: stop openai chat SDK error handling from flattening DeadlineExceeded into OpenAIError"
  ```

- [ ] **Step 5: failing test — `BaseLLMHTTPHandler._handle_error()` re-raises `DeadlineExceeded` unmodified instead of building a generic provider error class**
  ```python
  # append to tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py
  from unittest.mock import MagicMock

  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
  from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler


  def test_handle_error_reraises_deadline_exceeded_without_wrapping():
      """_handle_error() is the single shared choke point behind ~30 call sites across chat's
      generic (non-OpenAI-SDK) path, the responses native handler, and the messages native
      handler. It must not fold DeadlineExceeded into a generic provider error class -- it needs
      to survive unmodified so chat's/responses' outer exception_type() call (main.py:690,
      responses/main.py:555-556) gets a chance to map it to litellm.Timeout, and so the
      messages-face targeted mapping (Step 7 below) receives the real exception type too."""
      handler = BaseLLMHTTPHandler()
      original = DeadlineExceeded("simulated")

      with pytest.raises(DeadlineExceeded) as exc_info:
          handler._handle_error(e=original, provider_config=MagicMock())

      assert exc_info.value is original
  ```
  Run: confirm failure (today this raises a `BaseLLMException`/`provider_config.get_error_class(...)` result instead of the original `DeadlineExceeded`).

- [ ] **Step 6: add the transparency guard (Fix D)**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py
  # add near the top, alongside the other litellm_core_utils imports:
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded

  # inside def _handle_error(self, e: Exception, provider_config: Union[...]), as the very
  # first statement in the method body, before the existing `status_code = getattr(e, ...)` line:
          if isinstance(e, DeadlineExceeded):
              raise e
  ```
  Run: Step 5 test passes.
  Commit:
  ```
  git add litellm/llms/custom_httpx/llm_http_handler.py tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py
  git commit -m "fix: stop BaseLLMHTTPHandler._handle_error from flattening DeadlineExceeded into a generic provider error"
  ```

- [ ] **Step 7: failing test — the messages face's only async entry point maps `DeadlineExceeded` to `litellm.Timeout` at its one unguarded await point**
  ```python
  # append to tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  # (add `import litellm` near the top of this file if not already present)

  @pytest.mark.asyncio
  async def test_anthropic_messages_maps_deadline_exceeded_to_litellm_timeout():
      """Fix E regression: unlike chat's acompletion() (main.py:690) and responses' aresponses()
      (responses/main.py:555-556), the messages face's anthropic_messages() has NO
      exception_type()-style wrapper anywhere in its call chain. A raw DeadlineExceeded
      surfacing from the native handler's internal await (reached via
      `response = await init_response`) must be converted to litellm.Timeout right here, or it
      leaks to direct-SDK callers of litellm.anthropic_messages() as an internal exception type
      instead of the public litellm.Timeout/APITimeoutError contract."""
      from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
      from litellm.llms.anthropic.experimental_pass_through.messages import handler

      async def _raise_deadline_exceeded():
          raise DeadlineExceeded("simulated total_timeout deadline exceeded")

      def fake_handler(*args, **kwargs):
          return _raise_deadline_exceeded()

      fake_loop = MagicMock()
      fake_loop.run_in_executor = lambda _e, func: _async_return(func())

      with (
          patch.object(handler, "anthropic_messages_handler", side_effect=fake_handler),
          patch("asyncio.get_event_loop", return_value=fake_loop),
      ):
          with pytest.raises(litellm.Timeout):
              await handler.anthropic_messages(
                  max_tokens=100,
                  messages=[{"role": "user", "content": "hi"}],
                  model="anthropic/claude-sonnet-4-5-20250929",
                  custom_llm_provider="anthropic",
                  api_key="k",
              )
  ```
  Run: confirm failure — today `DeadlineExceeded` propagates out of `await init_response` completely unconverted (`pytest.raises(litellm.Timeout)` fails with the unexpected type `DeadlineExceeded`).

- [ ] **Step 8: add the targeted mapping (Fix E)**
  ```python
  # litellm/llms/anthropic/experimental_pass_through/messages/handler.py
  # add near the top, alongside the existing `import asyncio`:
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded

  # inside async def anthropic_messages(...), replacing the current lines 374-377:
  #     if asyncio.iscoroutine(init_response):
  #         response = await init_response
  #     else:
  #         response = init_response
  #     return response
  # with:
      if asyncio.iscoroutine(init_response):
          try:
              response = await init_response
          except DeadlineExceeded as e:
              raise litellm.Timeout(
                  message=str(e),
                  model=model,
                  llm_provider=custom_llm_provider or "anthropic",
              ) from e
      else:
          response = init_response
      return response
  ```
  Run: Step 7 test passes.
  Commit:
  ```
  git add litellm/llms/anthropic/experimental_pass_through/messages/handler.py tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  git commit -m "fix: map DeadlineExceeded to litellm.Timeout at the messages face's one unguarded await point"
  ```

- [ ] **Step 9: Router timeout-retry classification regression (no production code expected)**
  ```python
  # new file: tests/test_litellm/router_utils/test_get_retry_from_policy.py
  # no test file previously existed for litellm/router_utils/get_retry_from_policy.py
  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
  from litellm.litellm_core_utils.exception_mapping_utils import exception_type
  from litellm.router_utils.get_retry_from_policy import get_num_retries_from_retry_policy
  from litellm.types.router import RetryPolicy


  def test_get_num_retries_from_retry_policy_classifies_mapped_deadline_exceeded_as_timeout():
      """End-to-end pin: a DeadlineExceeded, once run through exception_type() (Step 2), must
      still be classified by Router's retry-policy lookup as a Timeout -- proving the two are
      wired together correctly, not just independently correct in isolation. This function and
      Router.get_allowed_fails_from_policy (litellm/router.py:11014) share the byte-identical
      `isinstance(exception, litellm.Timeout)` primitive with no divergent logic, so this single
      test pins the classification contract for both call sites; a second near-duplicate test at
      the router.py:11014 call site would add no independent protection."""
      try:
          exception_type(
              model="gpt-4",
              original_exception=DeadlineExceeded("simulated"),
              custom_llm_provider="openai",
          )
          raise AssertionError("exception_type() should have raised")
      except AssertionError:
          raise
      except Exception as mapped:
          retries = get_num_retries_from_retry_policy(
              exception=mapped,
              retry_policy=RetryPolicy(TimeoutErrorRetries=7),
          )
          assert retries == 7
  ```
  Run: this passes once Step 2 is in place; it exists to lock the Router-facing contract down as its own regression rather than an incidental side effect of Step 2's implementation.
  Commit:
  ```
  git add tests/test_litellm/router_utils/test_get_retry_from_policy.py
  git commit -m "test: pin Router timeout-retry classification for DeadlineExceeded-mapped exceptions"
  ```

**Open items for the implementer to re-verify with grep before trusting the line numbers above** (this session read the real files but `main.py`/`openai.py`/`llm_http_handler.py` are large and under active change):
- `litellm/llms/openai/openai.py`: confirm `acompletion()`'s `except Exception as e:` is still at line 937 and `async_streaming()`'s `except (Exception) as e:` is still at line 1087 (`grep -n "except Exception as e:\|except (\s*$" litellm/llms/openai/openai.py`).
- `litellm/llms/custom_httpx/llm_http_handler.py`: confirm `_handle_error`'s signature and first executable line are still at/near line 5496-5520.
- `litellm/llms/anthropic/experimental_pass_through/messages/handler.py`: confirm lines 374-377 are still the `asyncio.iscoroutine(init_response)` block, and that `model`/`custom_llm_provider` are still valid local variables in scope at that point in `anthropic_messages()`.
- `litellm/router.py`: confirm `Router.get_allowed_fails_from_policy` (line ~10994) still contains the `isinstance(exception, litellm.Timeout)` check at line 11014, matching `get_num_retries_from_retry_policy`'s equivalent check.

### Task 9: `DeadlineBoundAsyncIterator`

**Files:**
- Modify: `litellm/litellm_core_utils/asyncio_deadline.py`
- Test: `tests/test_litellm/litellm_core_utils/test_asyncio_deadline.py`

- [ ] **Step 1: failing tests — iterates normally with no deadline; raises `DeadlineExceeded` and invokes the shielded close callback when the deadline is exceeded mid-iteration; does not call the close callback on normal `StopAsyncIteration`**
  ```python
  @pytest.mark.asyncio
  async def test_deadline_bound_async_iterator_passes_through_with_no_deadline():
      from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

      async def _gen():
          yield b"a"
          yield b"b"

      closed = []
      wrapped = DeadlineBoundAsyncIterator(_gen(), None, on_timeout_close=lambda: closed.append(True))
      received = [chunk async for chunk in wrapped]
      assert received == [b"a", b"b"]
      assert closed == []


  @pytest.mark.asyncio
  async def test_deadline_bound_async_iterator_raises_and_closes_on_timeout():
      import asyncio

      from litellm.litellm_core_utils.asyncio_deadline import (
          DeadlineBoundAsyncIterator,
          DeadlineExceeded,
      )

      async def _gen():
          yield b"first"
          await asyncio.sleep(10)
          yield b"never"  # pragma: no cover

      closed = []

      async def _on_timeout_close():
          closed.append(True)

      wrapped = DeadlineBoundAsyncIterator(
          _gen(), asyncio.get_event_loop().time() + 0.05, on_timeout_close=_on_timeout_close
      )
      received = []
      with pytest.raises(DeadlineExceeded):
          async for chunk in wrapped:
              received.append(chunk)
      assert received == [b"first"]
      assert closed == [True]


  @pytest.mark.asyncio
  async def test_deadline_bound_async_iterator_does_not_close_on_normal_completion():
      import asyncio

      from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

      async def _gen():
          yield b"only"

      closed = []
      wrapped = DeadlineBoundAsyncIterator(
          _gen(), asyncio.get_event_loop().time() + 10, on_timeout_close=lambda: closed.append(True)
      )
      received = [chunk async for chunk in wrapped]
      assert received == [b"only"]
      assert closed == []


  @pytest.mark.asyncio
  async def test_deadline_bound_async_iterator_aclose_delegates_to_inner_aclose():
      """Review finding #6: DeadlineBoundAsyncIterator previously had no aclose() at all, so
      anything that tried to close the wrapper itself (e.g. CustomStreamWrapper.aclose(), once
      self.completion_stream has been reassigned to this wrapper -- see Task 16) silently
      no-op'd instead of releasing the underlying connection."""
      from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

      closed_inner = []

      class _Inner:
          def __aiter__(self):
              return self

          async def __anext__(self):
              raise StopAsyncIteration

          async def aclose(self):
              closed_inner.append(True)

      wrapped = DeadlineBoundAsyncIterator(_Inner(), None, on_timeout_close=lambda: None)
      await wrapped.aclose()
      assert closed_inner == [True]


  @pytest.mark.asyncio
  async def test_deadline_bound_async_iterator_aclose_delegates_to_inner_close_when_no_aclose():
      from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

      closed_inner = []

      class _Inner:
          def __aiter__(self):
              return self

          async def __anext__(self):
              raise StopAsyncIteration

          def close(self):
              closed_inner.append(True)

      wrapped = DeadlineBoundAsyncIterator(_Inner(), None, on_timeout_close=lambda: None)
      await wrapped.aclose()
      assert closed_inner == [True]


  @pytest.mark.asyncio
  async def test_deadline_bound_async_iterator_aclose_shields_itself_from_cancellation():
      """3rd-round review, minor #3: aclose() must shield its OWN inner close call, not
      merely rely on whatever cancel scope its caller (e.g. CustomStreamWrapper.aclose(),
      itself already shielded) happens to run inside. A client-disconnect can reach this
      object's aclose() through call paths other than CustomStreamWrapper's own shielded
      aclose() (e.g. a bare `await wrapper.aclose()` from a test, or a future caller that
      forgets to shield) -- if THIS aclose() doesn't shield itself, a pending cancellation
      can cut the inner close short before the underlying connection is actually released."""
      import anyio

      from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

      closed_inner = []

      class _Inner:
          def __aiter__(self):
              return self

          async def __anext__(self):
              raise StopAsyncIteration

          async def aclose(self):
              await asyncio.sleep(0)  # a checkpoint -- an unshielded scope raises Cancelled here
              closed_inner.append(True)

      wrapped = DeadlineBoundAsyncIterator(_Inner(), None, on_timeout_close=lambda: None)

      with anyio.CancelScope() as scope:
          scope.cancel()
          await wrapped.aclose()

      assert closed_inner == [True]
  ```
  Run: confirm `ImportError` (for the pre-existing tests) and `AttributeError: 'DeadlineBoundAsyncIterator' object has no attribute 'aclose'` (for the pre-existing new ones); confirm the new shielding test fails with an unshielded `aclose()` implementation (`anyio.get_cancelled_exc_class()` propagates out of `_Inner.aclose()`'s `await asyncio.sleep(0)` checkpoint, so `closed_inner` stays `[]`).

- [ ] **Step 2: implement, reusing the `anyio.CancelScope(shield=True)` precedent from `CustomStreamWrapper.aclose()` (`streaming_handler.py:225-244`) for the close callback so a timeout-triggered close is never itself cancelled; also add `aclose()` itself (review finding #6), and make `aclose()` shield its own inner close (3rd-round review, minor #3) so callers that reach this object's `aclose()` through any path -- not just through `CustomStreamWrapper`'s own already-shielded `aclose()` -- still get a guaranteed-to-complete close**
  ```python
  # litellm/litellm_core_utils/asyncio_deadline.py, appended
  import inspect
  from typing import AsyncIterator, Generic, Union

  import anyio


  class DeadlineBoundAsyncIterator(Generic[T]):
      """Wrap an async byte/chunk iterator so each `__anext__` is subject to the same
      absolute deadline as the request's initial non-streaming await. On timeout, invokes
      `on_timeout_close` (sync or async) inside a shielded cancel scope -- mirroring the
      existing precedent in CustomStreamWrapper.aclose() -- so cleanup itself is never
      cut short by the same cancellation that produced the timeout."""

      def __init__(
          self,
          inner: AsyncIterator[T],
          deadline: Optional[float],
          *,
          on_timeout_close: Callable[[], Union[None, Awaitable[None]]],
          now: Optional[Callable[[], float]] = None,
      ) -> None:
          self._inner = inner
          self._deadline = deadline
          self._on_timeout_close = on_timeout_close
          self._now = now

      def __aiter__(self) -> "DeadlineBoundAsyncIterator[T]":
          return self

      async def __anext__(self) -> T:
          try:
              return await with_deadline(self._deadline, self._inner.__anext__(), now=self._now)
          except DeadlineExceeded:
              await self._shielded_close()
              raise

      async def _shielded_close(self) -> None:
          with anyio.CancelScope(shield=True):
              result = self._on_timeout_close()
              if inspect.isawaitable(result):
                  await result

      async def aclose(self) -> None:
          """Delegate to the wrapped inner iterator's own aclose/close. Needed because, once
          CustomStreamWrapper wraps its raw completion_stream in one of these (Task 16),
          `self.completion_stream` on the wrapper IS this object -- so anything that closes
          `self.completion_stream` (client disconnect, CustomStreamWrapper.aclose() itself) must
          reach the real underlying connection through here, not stop at this object having
          nothing to close (review finding #6). Shielded independently of any caller's own
          cancel scope (3rd-round review, minor #3): CustomStreamWrapper.aclose() already
          shields its own body, but this object must not depend on that -- any other caller
          reaching this aclose() directly (tests, future call sites) still gets a close that
          cannot be cut short by a cancellation in flight at the time it's invoked."""
          with anyio.CancelScope(shield=True):
              if hasattr(self._inner, "aclose"):
                  await self._inner.aclose()
              elif hasattr(self._inner, "close"):
                  result = self._inner.close()
                  if inspect.isawaitable(result):
                      await result
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/asyncio_deadline.py tests/test_litellm/litellm_core_utils/test_asyncio_deadline.py
  git commit -m "feat: add DeadlineBoundAsyncIterator for streaming deadline enforcement, with a self-shielding aclose()"
  ```

### Task 10: `establish_request_deadline`

**Files:**
- Modify: `litellm/litellm_core_utils/http_client_config.py`
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py`

- [ ] **Step 1: failing tests — no config anywhere returns `None`; global-only, deployment-only, and merged configs compute `now() + total_timeout`; `total_timeout` unset returns `None` even if other fields are set**
  ```python
  def test_establish_request_deadline_returns_none_with_no_config(monkeypatch):
      import litellm
      from litellm.litellm_core_utils.http_client_config import establish_request_deadline

      monkeypatch.setattr(litellm, "http_client", None)
      assert establish_request_deadline({"model": "gpt-4"}, now=lambda: 1000.0) is None


  def test_establish_request_deadline_returns_none_without_total_timeout(monkeypatch):
      import litellm
      from litellm.litellm_core_utils.http_client_config import establish_request_deadline

      monkeypatch.setattr(litellm, "http_client", None)
      kwargs = {"model": "gpt-4", "http_client": {"connect_timeout": 1.0}}
      assert establish_request_deadline(kwargs, now=lambda: 1000.0) is None


  def test_establish_request_deadline_uses_deployment_total_timeout(monkeypatch):
      import litellm
      from litellm.litellm_core_utils.http_client_config import establish_request_deadline

      monkeypatch.setattr(litellm, "http_client", None)
      kwargs = {"model": "gpt-4", "http_client": {"total_timeout": 30.0}}
      assert establish_request_deadline(kwargs, now=lambda: 1000.0) == 1030.0


  def test_establish_request_deadline_merges_global_and_deployment(monkeypatch):
      import litellm
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          establish_request_deadline,
      )

      monkeypatch.setattr(litellm, "http_client", HttpClientConfig(total_timeout=60.0, connect_timeout=1.0))
      kwargs = {"model": "gpt-4", "http_client": {"total_timeout": 30.0}}
      # deployment's total_timeout (30.0) wins over global's (60.0)
      assert establish_request_deadline(kwargs, now=lambda: 1000.0) == 1030.0
  ```
  Run: confirm `ImportError`.

- [ ] **Step 2: implement**
  ```python
  import litellm


  def establish_request_deadline(kwargs: dict, *, now: Callable[[], float]) -> Optional[float]:
      """Compute the absolute asyncio-loop-time deadline for one request, given the raw
      call kwargs (which may contain a per-deployment `http_client` value, already
      typed/untyped from a direct SDK call or from Router's litellm_params spread) and the
      global `litellm.http_client` setting. Reads kwargs["http_client"] without popping it
      -- popping is the sync completion()/equivalent call's job at its own timeout-resolution
      choke point, so the value is still available there for resolve_http_client_timeout."""
      deployment_cfg = parse_http_client_config(kwargs.get("http_client"))
      global_cfg = parse_http_client_config(getattr(litellm, "http_client", None))
      merged = merge_http_client_config(global_cfg, deployment_cfg)
      if merged is None or merged.total_timeout is None:
          return None
      return now() + merged.total_timeout
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/http_client_config.py tests/test_litellm/litellm_core_utils/test_http_client_config.py
  git commit -m "feat: add establish_request_deadline combining global and deployment http_client config"
  ```

### Task 11: `Logging.http_client_deadline` carrier

**Files:**
- Modify: `litellm/litellm_core_utils/litellm_logging.py:305-374` (`Logging.__init__`)
- Test: `tests/test_litellm/litellm_core_utils/test_litellm_logging.py` (extend if it exists; create if not — check at implementation time via `ls tests/test_litellm/litellm_core_utils/test_litellm_logging.py`)

- [ ] **Step 1: failing test — `Logging` instances default to `http_client_deadline=None`; `set_http_client_deadline` sets it**
  ```python
  def test_logging_defaults_http_client_deadline_to_none():
      from litellm.litellm_core_utils.litellm_logging import Logging

      logging_obj = Logging(
          model="gpt-4",
          messages=[],
          stream=False,
          call_type="completion",
          start_time=__import__("datetime").datetime.now(),
          litellm_call_id="test-call-id",
          function_id="test-function-id",
      )
      assert logging_obj.http_client_deadline is None


  def test_logging_set_http_client_deadline():
      from litellm.litellm_core_utils.litellm_logging import Logging

      logging_obj = Logging(
          model="gpt-4",
          messages=[],
          stream=False,
          call_type="completion",
          start_time=__import__("datetime").datetime.now(),
          litellm_call_id="test-call-id",
          function_id="test-function-id",
      )
      logging_obj.set_http_client_deadline(123.45)
      assert logging_obj.http_client_deadline == 123.45
  ```
  Run: confirm `AttributeError`.

- [ ] **Step 2: add the field and setter**
  ```python
  # litellm/litellm_core_utils/litellm_logging.py, inside Logging.__init__, alongside the
  # other self.x = y field assignments (near the end of __init__, after existing fields):
      self.http_client_deadline: Optional[float] = None
  ```
  ```python
  # litellm/litellm_core_utils/litellm_logging.py, new method on class Logging, placed near
  # other simple setters:
      def set_http_client_deadline(self, deadline: Optional[float]) -> None:
          """Attach this request's absolute http_client.total_timeout deadline (an asyncio
          loop-time value from establish_request_deadline), so downstream non-streaming
          awaits and streaming iterators can enforce it via with_deadline /
          DeadlineBoundAsyncIterator without new parameters threaded through every call
          layer."""
          self.http_client_deadline = deadline
  ```
  Run: tests pass. `basedpyright litellm/litellm_core_utils/litellm_logging.py` clean (confirm `Optional` is imported — it already is, used elsewhere in the file).
  Commit:
  ```
  git add litellm/litellm_core_utils/litellm_logging.py tests/test_litellm/litellm_core_utils/test_litellm_logging.py
  git commit -m "feat: add http_client_deadline carrier field to Logging"
  ```

### Task 11a: removed in the 3rd review round (review finding G) — see Revision Record

**Task 11a previously existed here** (`set_or_warn_http_client_deadline`, a safety net for the case where `litellm_logging_obj` is `None` at `acompletion()`/`aresponses()`/`anthropic_messages()`'s entry). It has been **deleted entirely** — its own premise was factually wrong: `litellm/utils.py`'s `client` decorator's `wrapper_async` (confirmed at `litellm/utils.py:1580-1618`) asserts `logging_obj is not None` immediately after `function_setup()` and unconditionally injects it into `kwargs["litellm_logging_obj"]` (line 1618) *before* the wrapped function body ever runs. `acompletion()` (`litellm/main.py:402-403`), `aresponses()` (`litellm/responses/main.py:399-400`), and `anthropic_messages()` (`litellm/llms/anthropic/experimental_pass_through/messages/handler.py:210-211`) are all three decorated with `@client` — confirmed by direct grep — so `litellm_logging_obj` is a **guaranteed non-`None` invariant** inside all three entry points, not a "usually true, warn if not" case. There is no reachable direct/internal caller of these three specific public functions that bypasses `@client`, since `@client` wraps the function itself, not an inner call — you cannot call `acompletion()` at all without going through its decorator.

Formalizing "warn and continue" for an unreachable branch would have normalized a state ("configured total_timeout silently ineffective") that should never be treated as expected or observable behavior; per `never-swallow-errors`, the correct contract for an invariant a caller cannot actually violate is an `assert`, not a runtime warning path with its own tests pretending the `None` branch is a legitimate, supported outcome. Tasks 12/18/22 below have been reverted to call `litellm_logging_obj.set_http_client_deadline(...)` directly, guarded by an `assert litellm_logging_obj is not None` documenting the invariant and its source, rather than through the now-deleted helper. See the Revision Record at the end of this document for the full accounting of this removal.

---

## Phase 3: Chat Face Wiring (`/chat/completions`)

Mirrors spec rollout step 3. Establishes the deadline at `acompletion()`'s entry, resolves the httpx.Timeout in sync `completion()`'s existing `### TIMEOUT LOGIC ###` block, and enforces the deadline at both the non-streaming and the two-phase streaming seams in `openai.py` / `streaming_handler.py`.

### Task 12: establish deadline in `acompletion()`

**Files:**
- Modify: `litellm/main.py` (inside `async def acompletion(...)`, right after the existing `litellm_logging_obj = kwargs.get("litellm_logging_obj", None)` line, near line 499 — confirm exact line at implementation time via `grep -n 'litellm_logging_obj = kwargs.get' litellm/main.py`)
- Test: `tests/test_litellm/test_main_acompletion_http_client_deadline.py` (new — no existing file maps to `acompletion()`'s deadline-establishment behavior specifically; `tests/test_litellm/test_completion_timeout_resolution.py` covers `CompletionTimeout.resolve()` only, a different unit)

- [ ] **Step 1: failing integration test — calling `litellm.acompletion` with a `http_client={"total_timeout": ...}` kwarg results in `logging_obj.http_client_deadline` being set on the `Logging` instance used for the call**
  ```python
  # tests/test_litellm/test_main_acompletion_http_client_deadline.py
  from unittest.mock import AsyncMock, patch

  import pytest

  import litellm


  @pytest.mark.asyncio
  async def test_acompletion_establishes_http_client_deadline_on_logging_obj():
      captured_logging_objs = []

      original_get_logging_id = litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline

      def _capture(self, deadline):
          captured_logging_objs.append((self, deadline))
          return original_get_logging_id(self, deadline)

      with (
          patch(
              "litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline",
              new=_capture,
              autospec=False,
          ),
          patch("litellm.main.completion", new=AsyncMock(return_value={"choices": []})),
      ):
          await litellm.acompletion(
              model="gpt-4",
              messages=[{"role": "user", "content": "hi"}],
              http_client={"total_timeout": 30.0},
          )

      assert len(captured_logging_objs) == 1
      _, deadline = captured_logging_objs[0]
      assert deadline is not None


  @pytest.mark.asyncio
  async def test_acompletion_leaves_deadline_none_without_http_client_config():
      captured = []

      original = litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline

      def _capture(self, deadline):
          captured.append(deadline)
          return original(self, deadline)

      with (
          patch(
              "litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline",
              new=_capture,
              autospec=False,
          ),
          patch("litellm.main.completion", new=AsyncMock(return_value={"choices": []})),
      ):
          await litellm.acompletion(model="gpt-4", messages=[{"role": "user", "content": "hi"}])

      assert captured == [None]
  ```
  Run: confirm both tests fail (no call to `set_http_client_deadline` happens today).

- [ ] **Step 2: wire the call into `acompletion()`** *(reverted in the 3rd review round — review finding G: Task 11a's `set_or_warn_http_client_deadline` safety net has been deleted, since `acompletion()` is always wrapped by `litellm.utils.client`'s `wrapper_async`, which asserts `logging_obj is not None` and injects it into kwargs before this line ever runs — there is no reachable caller for which `litellm_logging_obj` could be `None` here. The `assert` below documents that invariant at its point of use instead of silently guarding against an impossible case.)*
  ```python
  # litellm/main.py, immediately after the existing line:
  #     litellm_logging_obj = kwargs.get("litellm_logging_obj", None)
  # inside async def acompletion(...):
      assert litellm_logging_obj is not None, (
          "acompletion() is always wrapped by litellm.utils.client's wrapper_async, which "
          "asserts logging_obj is not None and injects it into kwargs before this point runs"
      )
      litellm_logging_obj.set_http_client_deadline(establish_request_deadline(kwargs, now=loop.time))
  ```
  Add the import near the top of `litellm/main.py`:
  ```python
  from litellm.litellm_core_utils.http_client_config import establish_request_deadline
  ```
  *(Implementer note: `loop` must already be bound to `asyncio.get_event_loop()` earlier in `acompletion()`'s body before this line — confirm the exact existing variable name/position via `grep -n 'loop = asyncio.get_event_loop' litellm/main.py` and place this block after that assignment, not before.)*
  Run: tests pass.
  Commit:
  ```
  git add litellm/main.py tests/test_litellm/test_main_acompletion_http_client_deadline.py
  git commit -m "feat: establish http_client deadline on logging_obj in acompletion"
  ```

### Task 13: pop `http_client` in sync `completion()`'s timeout-resolution block; resolve httpx.Timeout

**Files:**
- Modify: `litellm/main.py:5097-5104` (`### TIMEOUT LOGIC ###` block)
- Test: `tests/test_litellm/test_completion_timeout_resolution.py` (extend existing file)

- [ ] **Step 1: failing test — `http_client` in kwargs is popped before reaching provider optional-params, and influences the resolved `httpx.Timeout` via `resolve_http_client_timeout`**
  ```python
  # append to tests/test_litellm/test_completion_timeout_resolution.py
  from unittest.mock import MagicMock, patch

  import httpx

  import litellm


  def test_completion_pops_http_client_and_resolves_httpx_timeout():
      captured_timeout = []

      def _fake_provider_call(*args, **kwargs):
          captured_timeout.append(kwargs.get("timeout"))
          return {"choices": [{"message": {"content": "ok"}}]}

      with patch("litellm.main.openai_chat_completions.completion", side_effect=_fake_provider_call):
          litellm.completion(
              model="github_copilot/gpt-4",
              messages=[{"role": "user", "content": "hi"}],
              http_client={"connect_timeout": 2.0, "read_timeout": 9.0},
          )

      assert len(captured_timeout) == 1
      resolved = captured_timeout[0]
      assert isinstance(resolved, httpx.Timeout)
      assert resolved.connect == 2.0
      assert resolved.read == 9.0


  def test_completion_does_not_leak_http_client_key_into_optional_params():
      from litellm.utils import get_optional_params

      # regression: http_client must never reach get_optional_params' kwargs, matching the
      # existing no-leak pattern in test_use_chat_completions_api_no_leak.py
      captured_optional_params = []
      original_get_optional_params = litellm.utils.get_optional_params

      def _capture(*args, **kwargs):
          result = original_get_optional_params(*args, **kwargs)
          captured_optional_params.append(result)
          return result

      with (
          patch("litellm.main.get_optional_params", side_effect=_capture),
          patch(
              "litellm.main.openai_chat_completions.completion",
              return_value={"choices": [{"message": {"content": "ok"}}]},
          ),
      ):
          litellm.completion(
              model="github_copilot/gpt-4",
              messages=[{"role": "user", "content": "hi"}],
              http_client={"connect_timeout": 2.0},
          )

      assert len(captured_optional_params) == 1
      assert "http_client" not in captured_optional_params[0]
  ```
  Run: confirm failure — today `http_client` is not a recognized kwarg by `completion()`'s explicit signature at all, so passing it either raises `TypeError` (unexpected kwarg) or silently flows to `**kwargs` -> `get_optional_params` unfiltered depending on the current signature; either failure mode confirms the gap.

- [ ] **Step 2: pop `http_client` and resolve the httpx.Timeout in the `### TIMEOUT LOGIC ###` block**
  ```python
  # litellm/main.py, inside sync def completion(...), replacing the existing block at
  # lines 5097-5104:
  #     timeout = CompletionTimeout.resolve(
  #         timeout, kwargs, custom_llm_provider, global_timeout=get_configured_request_timeout(),
  #         supports_httpx_timeout=supports_httpx_timeout,
  #     )
  # with:
      raw_http_client = kwargs.pop("http_client", None)
      timeout = CompletionTimeout.resolve(
          timeout,
          kwargs,
          custom_llm_provider,
          global_timeout=get_configured_request_timeout(),
          supports_httpx_timeout=supports_httpx_timeout,
      )
      if raw_http_client is not None or getattr(litellm, "http_client", None) is not None:
          deployment_cfg = parse_http_client_config(raw_http_client)
          global_cfg = parse_http_client_config(getattr(litellm, "http_client", None))
          merged_cfg = merge_http_client_config(global_cfg, deployment_cfg)
          timeout = resolve_http_client_timeout(merged_cfg, legacy_effective_timeout=timeout).httpx_timeout
  ```
  Add the import near the top of `litellm/main.py` (alongside the Task 12 import):
  ```python
  from litellm.litellm_core_utils.http_client_config import (
      establish_request_deadline,
      merge_http_client_config,
      parse_http_client_config,
      resolve_http_client_timeout,
  )
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/main.py tests/test_litellm/test_completion_timeout_resolution.py
  git commit -m "feat: resolve http_client config into httpx.Timeout in completion() and pop it before optional params"
  ```

### Task 14: non-streaming deadline wrap in `OpenAIChatCompletion.acompletion()`

**Files:**
- Modify: `litellm/llms/openai/openai.py:886-891`
- Test: `tests/test_litellm/llms/openai/test_openai_http_client_deadline.py` (new)

- [ ] **Step 1: failing test — `make_openai_chat_completion_request` is wrapped with `with_deadline` using `logging_obj.http_client_deadline`; an already-exceeded deadline raises `DeadlineExceeded` before the request is even sent**
  ```python
  # tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  import asyncio
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
  from litellm.llms.openai.openai import OpenAIChatCompletion


  @pytest.mark.asyncio
  async def test_acompletion_raises_deadline_exceeded_when_deadline_already_passed():
      handler = OpenAIChatCompletion()
      logging_obj = MagicMock()
      logging_obj.http_client_deadline = asyncio.get_event_loop().time() - 1
      logging_obj.pre_call = MagicMock()

      make_request_mock = AsyncMock()
      handler.make_openai_chat_completion_request = make_request_mock

      with pytest.raises(DeadlineExceeded):
          await handler.acompletion(
              messages=[{"role": "user", "content": "hi"}],
              optional_params={},
              litellm_params={},
              provider_config=MagicMock(async_transform_request=AsyncMock(return_value={})),
              model="github_copilot/gpt-4",
              model_response=MagicMock(),
              logging_obj=logging_obj,
              timeout=600.0,
          )
      make_request_mock.assert_not_called()
  ```
  Run: confirm failure — today no deadline check exists, so `make_request_mock` would be called (test's `assert_not_called()` fails), or an unrelated error occurs first depending on mock completeness; adjust mocks minimally until the failure is specifically about the missing deadline check (per TDD, the RED failure must be diagnostic of the gap, not of an unrelated mock-setup issue).

- [ ] **Step 2: wrap the call site**
  ```python
  # litellm/llms/openai/openai.py, inside async def acompletion(...), replacing the
  # existing call at lines 886-891:
  #     headers, response = await self.make_openai_chat_completion_request(
  #         openai_aclient=openai_aclient, data=data, timeout=timeout, logging_obj=logging_obj,
  #     )
  # with:
      headers, response = await with_deadline(
          logging_obj.http_client_deadline,
          self.make_openai_chat_completion_request(
              openai_aclient=openai_aclient, data=data, timeout=timeout, logging_obj=logging_obj
          ),
      )
  ```
  Add the import near the top of `litellm/llms/openai/openai.py`:
  ```python
  from litellm.litellm_core_utils.asyncio_deadline import with_deadline
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/llms/openai/openai.py tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  git commit -m "feat: enforce http_client total_timeout deadline on non-streaming openai acompletion"
  ```

### Task 15: streaming phase① deadline wrap in `OpenAIChatCompletion.async_streaming()`

**Files:**
- Modify: `litellm/llms/openai/openai.py:1065-1070`
- Test: `tests/test_litellm/llms/openai/test_openai_http_client_deadline.py`

- [ ] **Step 1: failing test — the initial stream-establishing await in `async_streaming()` is deadline-bound**
  ```python
  @pytest.mark.asyncio
  async def test_async_streaming_raises_deadline_exceeded_when_deadline_already_passed():
      handler = OpenAIChatCompletion()
      logging_obj = MagicMock()
      logging_obj.http_client_deadline = asyncio.get_event_loop().time() - 1
      logging_obj.pre_call = MagicMock()

      make_request_mock = AsyncMock()
      handler.make_openai_chat_completion_request = make_request_mock

      with pytest.raises(DeadlineExceeded):
          await handler.async_streaming(
              timeout=600.0,
              messages=[{"role": "user", "content": "hi"}],
              optional_params={},
              litellm_params={},
              provider_config=MagicMock(transform_request=MagicMock(return_value={})),
              model="github_copilot/gpt-4",
              logging_obj=logging_obj,
          )
      make_request_mock.assert_not_called()
  ```
  Run: confirm failure.

- [ ] **Step 2: wrap the call site**
  ```python
  # litellm/llms/openai/openai.py, inside async def async_streaming(...), replacing the
  # existing call at lines 1065-1070:
  #     headers, response = await self.make_openai_chat_completion_request(
  #         openai_aclient=openai_aclient, data=data, timeout=timeout, logging_obj=logging_obj,
  #     )
  # with:
      headers, response = await with_deadline(
          logging_obj.http_client_deadline,
          self.make_openai_chat_completion_request(
              openai_aclient=openai_aclient, data=data, timeout=timeout, logging_obj=logging_obj
          ),
      )
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/llms/openai/openai.py tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  git commit -m "feat: enforce http_client total_timeout deadline on streaming establishment in openai async_streaming"
  ```

### Task 16: streaming phase② deadline wrap in `CustomStreamWrapper.__init__`

**Files:**
- Modify: `litellm/litellm_core_utils/streaming_handler.py:114-129`
- Test: `tests/test_litellm/litellm_core_utils/test_streaming_handler_deadline.py` (new — the existing streaming_handler tests do not cover this construction path; confirm no clash via `ls tests/test_litellm/litellm_core_utils/test_streaming_handler*.py` at implementation time)

- [ ] **Step 1: failing test — chunk iteration past the deadline raises `DeadlineExceeded` and triggers `aclose()`; iteration with no deadline is unaffected**
  ```python
  # tests/test_litellm/litellm_core_utils/test_streaming_handler_deadline.py
  import asyncio
  from unittest.mock import MagicMock

  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
  from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper


  @pytest.mark.asyncio
  async def test_custom_stream_wrapper_enforces_deadline_on_async_iteration():
      async def _chunks():
          yield MagicMock(choices=[MagicMock(delta=MagicMock(content="a"), finish_reason=None)])
          await asyncio.sleep(10)
          yield MagicMock()  # pragma: no cover

      logging_obj = MagicMock()
      logging_obj.http_client_deadline = asyncio.get_event_loop().time() + 0.05

      wrapper = CustomStreamWrapper(
          completion_stream=_chunks(),
          model="github_copilot/gpt-4",
          logging_obj=logging_obj,
          custom_llm_provider="openai",
      )

      with pytest.raises(DeadlineExceeded):
          async for _ in wrapper:
              pass


  @pytest.mark.asyncio
  async def test_custom_stream_wrapper_no_deadline_iterates_normally():
      async def _chunks():
          yield MagicMock(choices=[MagicMock(delta=MagicMock(content="a"), finish_reason="stop")])

      logging_obj = MagicMock()
      logging_obj.http_client_deadline = None

      wrapper = CustomStreamWrapper(
          completion_stream=_chunks(),
          model="github_copilot/gpt-4",
          logging_obj=logging_obj,
          custom_llm_provider="openai",
      )

      received = [chunk async for chunk in wrapper]
      assert len(received) >= 1


  @pytest.mark.asyncio
  async def test_custom_stream_wrapper_deadline_timeout_closes_the_raw_completion_stream():
      """Review finding #6 regression: the timeout-close callback must reach the RAW
      completion_stream (the connection-owning object) directly, not merely
      CustomStreamWrapper.aclose() -- which, after __init__ wraps the raw stream, finds
      self.completion_stream pointing at this very DeadlineBoundAsyncIterator, not the original
      stream. Before this fix, closing the wrapper by itself was a silent no-op (it had no
      aclose()/close() of its own), so the underlying HTTP connection was never released on
      timeout. Asserts on the raw stream being closed, not merely on DeadlineExceeded
      propagating -- the latter passed even under the bug this test targets."""

      class _RawStream:
          def __aiter__(self):
              return self

          async def __anext__(self):
              await asyncio.sleep(10)  # pragma: no cover

          async def aclose(self):
              closed.append(True)

      closed: list = []
      logging_obj = MagicMock()
      logging_obj.http_client_deadline = asyncio.get_event_loop().time() + 0.05

      wrapper = CustomStreamWrapper(
          completion_stream=_RawStream(),
          model="github_copilot/gpt-4",
          logging_obj=logging_obj,
          custom_llm_provider="openai",
      )

      with pytest.raises(DeadlineExceeded):
          async for _ in wrapper:
              pass

      assert closed == [True]


  @pytest.mark.asyncio
  async def test_custom_stream_wrapper_aclose_closes_raw_stream_exactly_once_on_client_disconnect():
      """3rd-round review, minor #1: a client disconnect (unlike a deadline timeout) reaches
      CustomStreamWrapper.aclose() directly -- e.g. via the ASGI/proxy layer's own disconnect
      handling -- never through DeadlineBoundAsyncIterator.__anext__'s except-clause at all.
      This must still close the RAW completion_stream exactly once, routed through the
      now-wrapping DeadlineBoundAsyncIterator (Task 9's aclose() delegation), not merely the
      wrapper's own bookkeeping, and not double-closed by any other path."""
      from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator

      class _RawStream:
          def __init__(self):
              self.close_count = 0

          def __aiter__(self):
              return self

          async def __anext__(self):
              raise StopAsyncIteration

          async def aclose(self):
              self.close_count += 1

      raw_stream = _RawStream()
      logging_obj = MagicMock()
      # far-future deadline: this test is about the disconnect path, not the timeout path.
      logging_obj.http_client_deadline = asyncio.get_event_loop().time() + 30

      wrapper = CustomStreamWrapper(
          completion_stream=raw_stream,
          model="github_copilot/gpt-4",
          logging_obj=logging_obj,
          custom_llm_provider="openai",
      )

      # Sanity check that Step 2 actually wrapped the raw stream, not left it bare --
      # otherwise the close-count assertion below would trivially pass even without
      # DeadlineBoundAsyncIterator.aclose()'s delegation ever being exercised.
      assert isinstance(wrapper.completion_stream, DeadlineBoundAsyncIterator)

      await wrapper.aclose()

      assert raw_stream.close_count == 1
  ```
  Run: confirm the first two tests behave as previously described; confirm the third test fails with `closed == []` — today the callback closes the wrapper itself, which (pre-Task-9-fix) has no `aclose`/`close` attribute at all, so `CustomStreamWrapper.aclose()`'s `hasattr` guards both fail and nothing is ever closed on timeout; confirm the fourth (new, minor #1) test fails on its `isinstance(wrapper.completion_stream, DeadlineBoundAsyncIterator)` assertion — before Step 2 below runs, `completion_stream` is still the bare raw stream, unwrapped.

- [ ] **Step 2: wrap `completion_stream` at assignment time, gated on the stream actually being async-iterable (leaves sync/boto3-style providers, which pass non-async iterables, completely untouched); wire the timeout-close callback directly at the wrapped iterator itself rather than through `CustomStreamWrapper.aclose()` (review finding #6 — `self.aclose` reads `self.completion_stream`, which by construction time already IS the wrapper being built here, so routing the callback through it is an indirect, fragile hop instead of a direct reference to the thing that actually needs closing)**
  ```python
  # litellm/litellm_core_utils/streaming_handler.py, inside CustomStreamWrapper.__init__,
  # replacing the existing line (line 129):
  #     self.completion_stream = completion_stream
  # with (placed AFTER the existing `self.logging_obj = logging_obj` assignment, since the
  # wrap needs to read the deadline off logging_obj):
      self.completion_stream = self._maybe_wrap_completion_stream_with_deadline(completion_stream)
  ```
  ```python
  # litellm/litellm_core_utils/streaming_handler.py, new private method on CustomStreamWrapper:
      def _maybe_wrap_completion_stream_with_deadline(self, completion_stream):
          deadline = getattr(self.logging_obj, "http_client_deadline", None)
          if deadline is None or not hasattr(completion_stream, "__anext__"):
              return completion_stream
          wrapped: "DeadlineBoundAsyncIterator" = DeadlineBoundAsyncIterator(
              completion_stream, deadline, on_timeout_close=lambda: wrapped.aclose()
          )
          return wrapped
  ```
  Note for the implementer: `on_timeout_close=lambda: wrapped.aclose()` closes over `wrapped` by reference, not by value — safe here because the callback is only ever invoked later, during iteration (from inside `DeadlineBoundAsyncIterator.__anext__`'s except-clause), by which point `wrapped` is fully bound to the constructed instance; it is never invoked synchronously during `__init__` itself. `DeadlineBoundAsyncIterator.aclose()` (added in Task 9) delegates straight to the raw inner stream's own `aclose`/`close`, so this closes the actual connection-owning object, not the `CustomStreamWrapper` instance's own bookkeeping.
  Add the import near the top of `litellm/litellm_core_utils/streaming_handler.py`:
  ```python
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/streaming_handler.py tests/test_litellm/litellm_core_utils/test_streaming_handler_deadline.py
  git commit -m "feat: enforce http_client total_timeout deadline on CustomStreamWrapper async iteration, closing the raw stream (not the wrapper) on timeout"
  ```

### Task 16a: chat streaming phase② — a mid-stream `DeadlineExceeded` must trigger Router's cross-deployment fallback, not raise bare (new — review finding C, chat half, discovered during 3rd-round planning, not previously identified)

**Why this task exists — a bug discovered while planning the coordinator's requested regression test, not merely a mechanical ask:** the 3rd review round asked for a test asserting that a mid-stream `DeadlineExceeded` is wrapped into `MidStreamFallbackError` with `.original_exception` being `litellm.Timeout`, mirroring the existing 429/read-timeout precedent in `CustomStreamWrapper._handle_stream_fallback_error` (`streaming_handler.py:2123-2188`). Tracing this end to end (not assumed) surfaced a genuine, previously-unplanned production bug: Task 8a's `exception_type()` mapping of `DeadlineExceeded` to `litellm.Timeout` (Step 2 of that task) does not pass `exception_status_code`, so `Timeout.__init__`'s `self.status_code = exception_status_code or 408` (`litellm/exceptions.py:347`, confirmed empirically this session) defaults it to **408**. `_handle_stream_fallback_error`'s existing skip-fallback condition (`400 <= mapped_status_code < 500 and mapped_status_code != 429`, lines 2176/2178) only carves 429 out of the "permanent 4xx client error, skip Router fallback and re-raise bare" range — 408 was never carved out, because before this feature existed, nothing reaching this function ever produced a mapped or original status code of exactly 408 (the pre-existing `httpx.ReadTimeout` case maps to `APIConnectionError`/500 via `exception_type()`'s string-matching heuristics, confirmed empirically, which is why the existing `test_async_streaming_read_timeout_triggers_midstream_fallback` regression already passes today without ever touching this range). The result: without this task's fix, a mid-stream `http_client.total_timeout` expiry is misclassified as a permanent client error and raised bare instead of being wrapped in `MidStreamFallbackError` — silently defeating Router's cross-deployment fallback for exactly the new timeout mechanism this feature adds, and making the coordinator's literally-requested test fail forever if written naively against the unfixed function. Per `long-term-wins`/`never-swallow-errors`, this is fixed at its root (extending the transient-status-code carve-out) rather than worked around in the test.

Grepped (`grep -rn "status_code=408\|status_code == 408\|== 408"` across `litellm/` and `tests/`) to confirm no other code depends on this function's specific treatment of 408 before widening the carve-out: `exception_mapping_utils.py`'s numerous per-provider `elif original_exception.status_code == 408:` branches build *different* mapped exceptions entirely and are never invoked by `_handle_stream_fallback_error` itself; `litellm/router_utils/cooldown_handlers.py:77`'s `elif exception_status == 408:` governs an unrelated mechanism (Router deployment cooldown classification, not mid-stream fallback routing); and `tests/test_litellm/llms/vertex_ai/context_caching/test_vertex_ai_context_caching.py:628` asserts a status code on a *constructed* exception, unrelated to this routing decision. None of these call `CustomStreamWrapper._handle_stream_fallback_error` — that function is scoped exclusively to `CustomStreamWrapper`'s own sync/async iteration exception handling. *(Honesty flag for the implementer: this was a grep-based survey, not an exhaustive per-call-site trace of every hit — re-run the same grep at implementation time in case new code has landed since this plan was written, per this document's "Open Items for Implementer" policy.)*

**Files:**
- Modify: `litellm/litellm_core_utils/streaming_handler.py:2123-2188` (`_handle_stream_fallback_error`'s two skip-fallback status-code checks)
- Test: `tests/test_litellm/litellm_core_utils/test_streaming_handler.py` (extend — this file already covers the 429/400/read-timeout precedent for this exact function at lines ~775-1016)

- [ ] **Step 1: failing test — a mid-stream `DeadlineExceeded` is wrapped into `MidStreamFallbackError` (not raised bare), with `.original_exception` being `litellm.Timeout`**
  ```python
  # append to tests/test_litellm/litellm_core_utils/test_streaming_handler.py
  from unittest.mock import MagicMock

  import litellm
  import pytest

  from litellm.exceptions import MidStreamFallbackError
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
  from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper


  @pytest.mark.asyncio
  async def test_custom_stream_wrapper_deadline_exceeded_wraps_into_mid_stream_fallback_error():
      """Regression for 3rd-round review finding C (chat half): a mid-stream DeadlineExceeded
      (Task 8a maps this to litellm.Timeout via exception_type(), which defaults to
      status_code=408 per Timeout.__init__'s `exception_status_code or 408`) must be wrapped
      into MidStreamFallbackError so Router's cross-deployment fallback gets a chance to try a
      different deployment -- exactly like the existing 429/read-timeout precedent elsewhere in
      this file -- not raised bare as though it were a permanent 4xx client error. Before this
      task's fix, 408 was not carved out of _handle_stream_fallback_error's skip-fallback range
      (only 429 was), so this assertion would fail with a bare litellm.Timeout escaping instead
      of a MidStreamFallbackError wrapping it."""

      async def _chunks():
          yield MagicMock(choices=[MagicMock(delta=MagicMock(content="a"), finish_reason=None)])
          raise DeadlineExceeded("http_client total_timeout deadline exceeded (remaining was -0.01s)")

      logging_obj = MagicMock()
      # the deadline-enforcement mechanism itself (raising DeadlineExceeded once the deadline
      # elapses) is Task 16's concern; here the raw stream raises it directly so this test
      # isolates _handle_stream_fallback_error's own routing decision.
      logging_obj.http_client_deadline = None

      wrapper = CustomStreamWrapper(
          completion_stream=_chunks(),
          model="github_copilot/gpt-4",
          logging_obj=logging_obj,
          custom_llm_provider="github_copilot",
      )

      with pytest.raises(MidStreamFallbackError) as exc_info:
          async for _ in wrapper:
              pass

      assert isinstance(exc_info.value.original_exception, litellm.Timeout)
  ```
  Run: confirm failure — with only Task 8a landed, `_handle_stream_fallback_error` computes `mapped_status_code == 408`, which today satisfies `400 <= 408 < 500 and 408 != 429`, so it takes the `raise mapped_exception` branch and a bare `litellm.Timeout` escapes instead of `MidStreamFallbackError` (`pytest.raises(MidStreamFallbackError)` fails with the wrong exception type).

- [ ] **Step 2: extend the transient-status-code carve-out from `{429}` to `{408, 429}`**
  ```python
  # litellm/litellm_core_utils/streaming_handler.py, as a module-level constant near the top
  # of the file (alongside its other module-level constants — confirm exact placement
  # convention via a quick read at implementation time):
  # 408 (Request Timeout) is, like 429 (rate limit), a transient condition a different
  # deployment might resolve -- unlike permanent 400/401/403/404 client errors. Carved out per
  # 3rd-round review finding C: Task 8a's DeadlineExceeded -> litellm.Timeout mapping defaults
  # status_code to 408 (Timeout.__init__'s `exception_status_code or 408`), which would
  # otherwise collide with this range and misclassify a mid-stream total_timeout expiry as a
  # permanent client error.
  _STATUS_CODES_ELIGIBLE_FOR_MIDSTREAM_FALLBACK = frozenset({408, 429})
  ```
  ```python
  # inside _handle_stream_fallback_error, replacing the existing two conditions:
  #     if mapped_status_code is not None and 400 <= mapped_status_code < 500 and mapped_status_code != 429:
  #         raise mapped_exception
  #     if original_status_code is not None and 400 <= original_status_code < 500 and original_status_code != 429:
  #         raise mapped_exception
  # with:
          if (
              mapped_status_code is not None
              and 400 <= mapped_status_code < 500
              and mapped_status_code not in _STATUS_CODES_ELIGIBLE_FOR_MIDSTREAM_FALLBACK
          ):
              raise mapped_exception
          if (
              original_status_code is not None
              and 400 <= original_status_code < 500
              and original_status_code not in _STATUS_CODES_ELIGIBLE_FOR_MIDSTREAM_FALLBACK
          ):
              raise mapped_exception
  ```
  Run: Step 1's new test passes; also re-run the existing 429/400/read-timeout regression tests in this same file to confirm no change in their behavior (429 remains carved out, 400 remains NOT carved out, read-timeout's 500 mapping is untouched by this change).
  Commit:
  ```
  git add litellm/litellm_core_utils/streaming_handler.py tests/test_litellm/litellm_core_utils/test_streaming_handler.py
  git commit -m "fix: carve 408 out of CustomStreamWrapper's skip-fallback range so a mid-stream deadline expiry still triggers Router fallback"
  ```

### Task 17: wire-body no-leak regression test for chat (new file, modeled on the existing gold-standard pattern)

**Files:**
- Create: `tests/test_litellm/llms/openai/test_http_client_config_no_leak.py` (modeled directly on `tests/test_litellm/llms/openai/test_use_chat_completions_api_no_leak.py`'s existing pattern)

- [ ] **Step 1: write the wire-body capture test (this should already pass given Task 6 + Task 13's pop — it is a regression lock-in, not a new-behavior test; if it fails, Task 6/13 missed a leak path and must be fixed here before moving on)**
  ```python
  # tests/test_litellm/llms/openai/test_http_client_config_no_leak.py
  """Regression: http_client config must never reach the outbound wire body sent to the
  upstream chat/completions endpoint. Modeled on test_use_chat_completions_api_no_leak.py's
  existing wire-body capture pattern."""

  import json
  from unittest.mock import AsyncMock, MagicMock, patch

  import pytest

  import litellm


  @pytest.mark.asyncio
  async def test_http_client_config_does_not_leak_into_chat_completions_wire_body():
      captured_bodies = []

      async def _fake_post(*args, **kwargs):
          body = kwargs.get("data") or kwargs.get("json")
          captured_bodies.append(json.loads(body) if isinstance(body, (str, bytes)) else body)
          response = MagicMock()
          response.json = MagicMock(return_value={"choices": [{"message": {"content": "ok"}}]})
          response.headers = {}
          response.status_code = 200
          return response

      with patch(
          "litellm.llms.custom_httpx.http_handler.AsyncHTTPHandler.post", side_effect=_fake_post
      ):
          await litellm.acompletion(
              model="github_copilot/gpt-4",
              messages=[{"role": "user", "content": "hi"}],
              http_client={"connect_timeout": 2.0, "total_timeout": 30.0},
          )

      assert len(captured_bodies) == 1
      assert "http_client" not in captured_bodies[0]
  ```
  Run: `pytest tests/test_litellm/llms/openai/test_http_client_config_no_leak.py -x` — confirm it passes given the Phase 1/3 work already landed. If it fails, treat the failure as diagnostic of a missed leak path in Task 6 or Task 13 and add the necessary additional `.pop()`/filter at the discovered site before committing this task.

- [ ] **Step 2: commit**
  ```
  git add tests/test_litellm/llms/openai/test_http_client_config_no_leak.py
  git commit -m "test: lock in no wire-body leak for http_client config on chat completions"
  ```

---

## Phase 4: Responses and Messages Face Wiring

Mirrors spec rollout steps 4 (responses) and 5 (messages) combined into one phase since both faces share the exact same core-module wiring pattern established in Phases 1–3, and messages additionally requires fixing two known pre-existing bugs (missing per-request timeout, wrong httpx-client cache key) discovered during spec authoring.

### Task 18: establish deadline in `aresponses()`

**Files:**
- Modify: `litellm/responses/main.py` (inside `async def aresponses(...)`, right after the existing `litellm_logging_obj = kwargs.get("litellm_logging_obj", None)` line near line 464)
- Test: `tests/test_litellm/responses/test_main.py` (new — confirmed no existing file maps to `responses/main.py`)

- [ ] **Step 1: failing test**
  ```python
  # tests/test_litellm/responses/test_main.py
  from unittest.mock import AsyncMock, patch

  import pytest

  import litellm


  @pytest.mark.asyncio
  async def test_aresponses_establishes_http_client_deadline_on_logging_obj():
      captured = []
      original = litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline

      def _capture(self, deadline):
          captured.append(deadline)
          return original(self, deadline)

      with (
          patch(
              "litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline",
              new=_capture,
              autospec=False,
          ),
          patch("litellm.responses.main.responses", new=AsyncMock(return_value=MagicMock())),
      ):
          await litellm.aresponses(model="github_copilot/gpt-4", input="hi", http_client={"total_timeout": 45.0})

      assert len(captured) == 1
      assert captured[0] is not None
  ```
  Run: confirm failure.

- [ ] **Step 2: wire the call** *(reverted in the 3rd review round — review finding G, mirroring Task 12: `aresponses()` is always wrapped by `@client`, so `litellm_logging_obj` is a guaranteed non-`None` invariant here too; Task 11a's safety net has been deleted)*
  ```python
  # litellm/responses/main.py, immediately after the existing line:
  #     litellm_logging_obj = kwargs.get("litellm_logging_obj", None)
  # inside async def aresponses(...):
      assert litellm_logging_obj is not None, (
          "aresponses() is always wrapped by litellm.utils.client's wrapper_async, which "
          "asserts logging_obj is not None and injects it into kwargs before this point runs"
      )
      litellm_logging_obj.set_http_client_deadline(establish_request_deadline(kwargs, now=loop.time))
  ```
  Add the import near the top of `litellm/responses/main.py`:
  ```python
  from litellm.litellm_core_utils.http_client_config import establish_request_deadline
  ```
  *(As in Task 12, confirm `loop`'s existing binding via `grep -n 'loop = asyncio.get_event_loop' litellm/responses/main.py` and place this after it.)*
  Run: tests pass.
  Commit:
  ```
  git add litellm/responses/main.py tests/test_litellm/responses/test_main.py
  git commit -m "feat: establish http_client deadline on logging_obj in aresponses"
  ```

### Task 18a: responses face — merge global/deployment `http_client` and resolve into the `httpx.Timeout` used by both `post()` call sites in `async_response_api_handler`, and strip `http_client` from every provider-facing call (review finding #4, core gap; 3rd review round major finding B, wire-safety half)

**Why this task exists:** unlike chat (Task 13) and messages (Task 22 below), the Responses face's `async_response_api_handler` (`litellm/llms/custom_httpx/llm_http_handler.py:2477-2650`) never once calls `parse_http_client_config`/`merge_http_client_config`/`resolve_http_client_timeout` — both its `post()` call sites (lines 2585-2591 streaming, 2617-2622 non-streaming) pass a raw `timeout or float(response_api_optional_request_params.get("timeout", 0))` expression untouched by any of the Phase 1 infrastructure. Without this task, `http_client` config is a complete no-op for the Responses face: Task 18/19/20 only wire the *deadline* (`total_timeout`'s absolute-time enforcement), not the per-axis `httpx.Timeout` (`connect_timeout`/`read_timeout`/`pool_timeout`) that is this feature's other half.

**3rd-round addendum (finding B):** the initial draft above only read `litellm_params.get("http_client")` to build the merged config — it never removed `http_client` from the `litellm_params` structure handed to the provider layer. `validate_environment` (line 2510-2514), `get_complete_url` (line 2522-2525), `transform_responses_api_request` (line 2527-2533), and `sign_request` (line 2560-2569) all receive either the raw `litellm_params` object or `dict(litellm_params)`, both of which carry a live `http_client` value once this feature's earlier tasks (Task 2) add the field. `http_client` is purely a litellm transport-layer construct (an internal per-axis timeout config, occasionally holding an actual `httpx.Client`/`HttpClientConfig` shape) — it has no meaning to a provider's URL-building, payload-transformation, or request-signing code, and nothing in any `BaseResponsesAPIConfig` subclass is contracted to expect it. Passing it through anyway is a wire-safety leak: at best it's dead weight the provider code silently ignores, at worst a provider's `transform_responses_api_request` (many of which forward unrecognized `litellm_params` entries into the request body as passthrough/extra params) sends litellm's own internal transport config to the remote API, or a provider's `sign_request` includes it in whatever it signs over. Fix: build one filtered, provider-facing copy of `litellm_params` early (`http_client` cleared to `None` via `model_copy`, never mutated in place) and pass *that* copy to all four calls; keep reading the *original*, unfiltered `litellm_params` only for this task's own `merge_http_client_config`/`resolve_http_client_timeout` step. (This deliberately covers `validate_environment` too, even though review finding B's original wording named only `get_complete_url`/`transform_responses_api_request`/`sign_request` — same leak, same root cause, same call shape; leaving one of the four provider-facing calls unfiltered would just relocate the bug rather than fix it.)

**Files:**
- Modify: `litellm/llms/custom_httpx/llm_http_handler.py:2498-2622` (`async_response_api_handler`; the client-construction block and every provider-facing call between it and the existing `try:` block, plus both `post()` call sites)
- Test: `tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_http_client.py` (new) — rewritten this round to inject `responses_api_provider_config`/`client` directly via `BaseLLMHTTPHandler().async_response_api_handler(...)` (the same direct-injection pattern already established at `tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py:226-259`'s `test_async_response_api_handler_streams_when_provider_transform_adds_stream`), instead of the original draft's `litellm.aresponses(model="github_copilot/gpt-4", ...)` end-to-end call. The original draft never mocked GitHub Copilot's `Authenticator` (`litellm/llms/github_copilot/authenticator.py`), whose `validate_environment` path performs a real OAuth device-code flow (`_get_device_code`/`_poll_for_access_token`, both real network calls) whenever no cached token is present — flaky and environment-dependent, and irrelevant to what this task actually verifies (timeout merging and provider-facing filtering, not Copilot auth).

- [ ] **Step 1: failing tests**

  **1a. per-axis timeout resolution reaches the wire `post()` call** (rewritten from the original draft to use direct injection):
  ```python
  # tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_http_client.py
  from unittest.mock import AsyncMock, Mock

  import httpx
  import pytest

  import litellm
  from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
  from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
  from litellm.types.llms.openai import ResponsesAPIResponse
  from litellm.types.router import GenericLiteLLMParams


  def _make_provider_config() -> Mock:
      config = Mock()
      config.validate_environment.return_value = {}
      config.get_complete_url.return_value = "https://api.openai.com/v1/responses"
      config.transform_responses_api_request.return_value = {"model": "gpt-4o-mini", "input": "hi"}
      config.sign_request.return_value = ({}, None)
      config.transform_response_api_response.return_value = ResponsesAPIResponse(
          id="resp_1", created_at=0, output=[], status="completed", model="gpt-4o-mini"
      )
      return config


  def _make_client() -> AsyncHTTPHandler:
      client = AsyncHTTPHandler()
      response = httpx.Response(
          200,
          json={"id": "resp_1", "output": [], "status": "completed"},
          request=httpx.Request("POST", "https://api.openai.com/v1/responses"),
      )
      client.post = AsyncMock(return_value=response)
      return client


  @pytest.mark.asyncio
  async def test_aresponses_resolves_deployment_http_client_into_wire_timeout():
      handler = BaseLLMHTTPHandler()
      config = _make_provider_config()
      client = _make_client()

      await handler.async_response_api_handler(
          model="gpt-4o-mini",
          input="hi",
          responses_api_provider_config=config,
          response_api_optional_request_params={},
          custom_llm_provider="openai",
          litellm_params=GenericLiteLLMParams(
              api_key="sk-test", http_client={"connect_timeout": 2.0, "read_timeout": 9.0}
          ),
          logging_obj=Mock(),
          client=client,
      )

      resolved = client.post.call_args.kwargs["timeout"]
      assert isinstance(resolved, httpx.Timeout)
      assert resolved.connect == 2.0
      assert resolved.read == 9.0


  @pytest.mark.asyncio
  async def test_aresponses_merges_global_http_client_with_deployment_override():
      handler = BaseLLMHTTPHandler()
      config = _make_provider_config()
      client = _make_client()

      original_global_http_client = getattr(litellm, "http_client", None)
      litellm.http_client = {"connect_timeout": 4.0, "pool_timeout": 12.0}
      try:
          await handler.async_response_api_handler(
              model="gpt-4o-mini",
              input="hi",
              responses_api_provider_config=config,
              response_api_optional_request_params={},
              custom_llm_provider="openai",
              litellm_params=GenericLiteLLMParams(
                  api_key="sk-test", http_client={"connect_timeout": 2.0}
              ),  # deployment overrides global's connect
              logging_obj=Mock(),
              client=client,
          )
      finally:
          litellm.http_client = original_global_http_client

      resolved = client.post.call_args.kwargs["timeout"]
      assert resolved.connect == 2.0  # deployment wins
      assert resolved.pool == 12.0  # falls back to global
  ```
  *(Implementer note: confirm `litellm.http_client` is the correct global-settings attribute name by grepping how Task 13/7 read it — `getattr(litellm, "http_client", None)` — and that assigning it directly as shown here is a faithful simulation of a `litellm_settings.http_client` YAML value at proxy config load time (Task 7); if the global attribute is read through a different accessor at request time, adjust the test setup accordingly, not the production code.)*
  Run: confirm failure — `client.post.call_args.kwargs["timeout"]` is a bare `float` (or `0.0`, see Step 2's note on the pre-existing zero-timeout bug), not an `httpx.Timeout`, since no merge/resolve wiring exists yet.

  **1b. `http_client` never reaches provider-facing calls** (new — review finding B):
  ```python
  @pytest.mark.asyncio
  async def test_aresponses_strips_http_client_before_provider_calls():
      handler = BaseLLMHTTPHandler()
      config = _make_provider_config()
      client = _make_client()
      litellm_params = GenericLiteLLMParams(
          api_key="sk-test", http_client={"connect_timeout": 2.0}
      )

      await handler.async_response_api_handler(
          model="gpt-4o-mini",
          input="hi",
          responses_api_provider_config=config,
          response_api_optional_request_params={},
          custom_llm_provider="openai",
          litellm_params=litellm_params,
          logging_obj=Mock(),
          client=client,
      )

      # None of the four provider-facing calls ever see an http_client key at all.
      # Object-shaped params carry the filtered copy (http_client is None); the two
      # dict-shaped params must not even contain the key (model_dump(exclude=...)).
      assert config.validate_environment.call_args.kwargs["litellm_params"].http_client is None
      assert "http_client" not in config.get_complete_url.call_args.kwargs["litellm_params"]
      assert config.transform_responses_api_request.call_args.kwargs["litellm_params"].http_client is None
      assert "http_client" not in config.sign_request.call_args.kwargs["optional_params"]

      # The original object handed in by the caller is untouched -- filtering must
      # build a copy, never mutate litellm_params in place (other code later in the
      # same request, e.g. this task's own merge_http_client_config step, still
      # needs the real value).
      assert litellm_params.http_client is not None
      assert litellm_params.http_client.connect_timeout == 2.0
  ```
  Run: confirm failure — today all four assertions on the provider-facing captured values fail (`AttributeError`/mismatch, since the un-filtered `litellm_params`/`dict(litellm_params)` is passed through as-is and its `http_client` is populated).

- [ ] **Step 2: wire the merge + resolve, replacing the two raw `timeout=` expressions, and filter `http_client` out of every provider-facing call**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, inside async def async_response_api_handler(...),
  # immediately after the existing if/else that sets async_httpx_client (ends at line 2508),
  # and before the existing `headers = responses_api_provider_config.validate_environment(...)`
  # call at line 2510:
      # http_client is litellm's own transport-layer config -- never a provider-facing
      # field. Build one filtered copy (never mutate litellm_params in place) and pass
      # it to every call that reaches provider transform/sign code; the merge/resolve
      # step below deliberately keeps reading the *original*, unfiltered litellm_params,
      # since that is the one place that legitimately needs the real http_client value.
      provider_facing_litellm_params = litellm_params.model_copy(update={"http_client": None})
  ```
  ```python
  # same file -- replace the four provider-facing calls' litellm_params/optional_params
  # arguments (lines 2510-2569) to read from provider_facing_litellm_params instead of
  # litellm_params. Every other argument on each call is unchanged from today:
      headers = responses_api_provider_config.validate_environment(
          headers=response_api_optional_request_params.get("extra_headers", {}) or {},
          model=model,
          litellm_params=provider_facing_litellm_params,
      )
      ...
      api_base = responses_api_provider_config.get_complete_url(
          api_base=litellm_params.api_base,
          litellm_params=provider_facing_litellm_params.model_dump(exclude={"http_client"}),
      )

      data = responses_api_provider_config.transform_responses_api_request(
          model=model,
          input=input,
          response_api_optional_request_params=response_api_optional_request_params,
          litellm_params=provider_facing_litellm_params,
          headers=headers,
      )
      ...
      headers, signed_body = responses_api_provider_config.sign_request(
          headers=headers,
          optional_params=provider_facing_litellm_params.model_dump(exclude={"http_client"}),
          request_data=data,
          api_base=api_base,
          api_key=litellm_params.api_key,
          model=model,
          stream=stream,
          fake_stream=fake_stream,
      )
  ```
  Note on the two `dict`-shaped params (`get_complete_url`'s `litellm_params` and `sign_request`'s `optional_params`): a bare `dict(provider_facing_litellm_params)` still emits `"http_client": None` as a key, which `sign_request` can observe and serialize. `model_dump(exclude={"http_client"})` drops the key entirely. The two object-shaped params (`validate_environment`, `transform_responses_api_request`) receive the filtered model copy whose `http_client` is `None` (identical to any deployment that never set it), which is fine.
  ```python
  # same file, immediately before the existing `try:` block at line 2583 -- unchanged
  # in shape from the original draft, but explicitly noting it reads the ORIGINAL
  # litellm_params, not provider_facing_litellm_params:
      merged_http_client_cfg = merge_http_client_config(
          parse_http_client_config(getattr(litellm, "http_client", None)),
          parse_http_client_config(litellm_params.get("http_client")),
      )
      # Pre-existing bug fixed in passing: the old expression's final fallback was a bare
      # `float(... , 0)`, i.e. an unconditional 0-second timeout whenever both `timeout` and
      # the per-request "timeout" optional param were unset -- silently nonsensical even before
      # this feature. DEFAULT_REQUEST_TIMEOUT_SECONDS (6000s, litellm/constants.py:376) is the
      # correct final fallback and is what this task's merge must feed resolve_http_client_timeout.
      legacy_effective_timeout = (
          timeout
          or float(response_api_optional_request_params.get("timeout", 0))
          or DEFAULT_REQUEST_TIMEOUT_SECONDS
      )
      resolved_timeout = resolve_http_client_timeout(
          merged_http_client_cfg, legacy_effective_timeout=legacy_effective_timeout
      ).httpx_timeout
  ```
  ```python
  # same file, streaming branch, replacing the existing call at lines 2585-2591:
  #     response = await async_httpx_client.post(
  #         url=api_base, headers=headers,
  #         timeout=timeout or float(response_api_optional_request_params.get("timeout", 0)),
  #         stream=stream, **body_kwargs,
  #     )
  # with:
      response = await async_httpx_client.post(
          url=api_base,
          headers=headers,
          timeout=resolved_timeout,
          stream=stream,
          **body_kwargs,
      )
  ```
  ```python
  # same file, non-streaming branch, replacing the existing call at lines 2617-2622:
  #     response = await async_httpx_client.post(
  #         url=api_base, headers=headers,
  #         timeout=timeout or float(response_api_optional_request_params.get("timeout", 0)),
  #         **body_kwargs,
  #     )
  # with:
      response = await async_httpx_client.post(
          url=api_base, headers=headers, timeout=resolved_timeout, **body_kwargs
      )
  ```
  Add the import near the top of `litellm/llms/custom_httpx/llm_http_handler.py`:
  ```python
  from litellm.constants import DEFAULT_REQUEST_TIMEOUT_SECONDS
  from litellm.litellm_core_utils.http_client_config import (
      merge_http_client_config,
      parse_http_client_config,
      resolve_http_client_timeout,
  )
  ```
  Run: all three tests (1a x2, 1b) pass. Also re-run Task 19's tests (below) to confirm this change composes correctly with the deadline wrap that Task 19 adds around the same `post()` call sites — the two tasks touch adjacent lines, so implement them in order and re-run both test files after each.
  Commit:
  ```
  git add litellm/llms/custom_httpx/llm_http_handler.py tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_http_client.py
  git commit -m "feat: resolve merged http_client config into the httpx.Timeout used by the responses API handler, and stop leaking it to provider-facing calls"
  ```

**Implementer grep-confirmation needed (honestly flagged):** the exact line numbers for `validate_environment`/`get_complete_url`/`transform_responses_api_request`/`sign_request` (2510-2569) were read once this round against the live file and are believed accurate at plan-writing time, but re-confirm them (and the `try:` block's line number, 2583) before editing, since any task implemented ahead of this one in the same file could shift them.

### Task 19: non-streaming + streaming phase① deadline wrap in `async_response_api_handler`

**Files:**
- Modify: `litellm/llms/custom_httpx/llm_http_handler.py:2585-2591` (streaming branch) and `:2617-2622` (non-streaming branch)
- Test: `tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_deadline.py` (new)

- [ ] **Step 1: failing tests for both branches**
  ```python
  # tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_deadline.py
  import asyncio
  from unittest.mock import AsyncMock, MagicMock

  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
  from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
  from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
  from litellm.types.router import GenericLiteLLMParams


  def _mock_responses_api_provider_config():
      """A responses_api_provider_config mock whose collaborators return real, correctly-
      shaped values (not bare auto-generated MagicMocks) for every method
      async_response_api_handler actually calls before it ever reaches the post() call this
      task wraps -- validate_environment (-> headers dict), get_complete_url (-> url str),
      transform_responses_api_request (-> a single request-body dict, NOT a tuple),
      sign_request (-> a (headers, signed_body) 2-tuple, matching its real unpacking at the
      call site). Under-mocking any of these raises an unrelated TypeError/AttributeError
      before the deadline check runs, masking the behavior under test."""
      return MagicMock(
          validate_environment=MagicMock(return_value={}),
          get_complete_url=MagicMock(return_value="https://example.com/responses"),
          transform_responses_api_request=MagicMock(return_value={}),
          sign_request=MagicMock(return_value=({}, None)),
      )


  @pytest.mark.asyncio
  async def test_async_response_api_handler_non_streaming_raises_when_deadline_passed():
      handler = BaseLLMHTTPHandler()
      logging_obj = MagicMock()
      logging_obj.http_client_deadline = asyncio.get_event_loop().time() - 1
      logging_obj.pre_call = MagicMock()

      mock_client = MagicMock(spec=AsyncHTTPHandler)
      mock_client.post = AsyncMock()

      with pytest.raises(DeadlineExceeded):
          await handler.async_response_api_handler(
              model="github_copilot/gpt-4",
              input="hi",
              responses_api_provider_config=_mock_responses_api_provider_config(),
              response_api_optional_request_params={},
              custom_llm_provider="github_copilot",
              litellm_params=GenericLiteLLMParams(),
              logging_obj=logging_obj,
              extra_headers=None,
              extra_body=None,
              timeout=600.0,
              client=mock_client,
              fake_stream=False,
              litellm_metadata=None,
          )
      mock_client.post.assert_not_called()


  @pytest.mark.asyncio
  async def test_async_response_api_handler_streaming_raises_when_deadline_passed():
      handler = BaseLLMHTTPHandler()
      logging_obj = MagicMock()
      logging_obj.http_client_deadline = asyncio.get_event_loop().time() - 1
      logging_obj.pre_call = MagicMock()

      mock_client = MagicMock(spec=AsyncHTTPHandler)
      mock_client.post = AsyncMock()

      with pytest.raises(DeadlineExceeded):
          await handler.async_response_api_handler(
              model="github_copilot/gpt-4",
              input="hi",
              responses_api_provider_config=_mock_responses_api_provider_config(),
              response_api_optional_request_params={"stream": True},
              custom_llm_provider="github_copilot",
              litellm_params=GenericLiteLLMParams(),
              logging_obj=logging_obj,
              extra_headers=None,
              extra_body=None,
              timeout=600.0,
              client=mock_client,
              fake_stream=False,
              litellm_metadata=None,
          )
      mock_client.post.assert_not_called()
  ```
  *(Implementer note: `async_response_api_handler`'s signature was fully re-verified during plan revision (`grep -n "async def async_response_api_handler" -A 17 litellm/llms/custom_httpx/llm_http_handler.py`) -- it takes `model, input, responses_api_provider_config, response_api_optional_request_params, custom_llm_provider, litellm_params, logging_obj, extra_headers=None, extra_body=None, timeout=None, client=None, fake_stream=False, litellm_metadata=None, shared_session=None`. There is no `_is_async` parameter (an earlier plan draft included one in error; do not add it back). `client` must be `spec=AsyncHTTPHandler` (or a real instance) -- a bare `MagicMock()` fails the handler's own `isinstance(client, AsyncHTTPHandler)` check and silently falls through to constructing a real client via `get_async_httpx_client`, making `mock_client.post.assert_not_called()` vacuously true regardless of whether the deadline logic works. Re-confirm this signature and the `isinstance` check are still accurate before writing the real test, since `llm_http_handler.py` is large and under active change.)*
  Run: confirm failure — today no deadline check exists, so either the `pytest.raises(DeadlineExceeded)` block fails to raise (if mocks are wired correctly) or an earlier unrelated mocking gap raises first; iterate on the mock setup until the RED failure is specifically "no DeadlineExceeded was raised", not a mocking artifact.

- [ ] **Step 2: wrap both branches (this task runs after Task 18a, so both replacements below act on Task 18a's `resolved_timeout` variable, not the original raw `timeout or float(...)` expression)**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, streaming branch, replacing the existing
  # call (as already modified by Task 18a):
  #     response = await async_httpx_client.post(
  #         url=api_base, headers=headers, timeout=resolved_timeout, stream=stream, **body_kwargs,
  #     )
  # with:
      response = await with_deadline(
          logging_obj.http_client_deadline,
          async_httpx_client.post(
              url=api_base,
              headers=headers,
              timeout=resolved_timeout,
              stream=stream,
              **body_kwargs,
          ),
      )
  ```
  ```python
  # same file, non-streaming branch, replacing the existing call (as already modified by
  # Task 18a):
  #     response = await async_httpx_client.post(
  #         url=api_base, headers=headers, timeout=resolved_timeout, **body_kwargs,
  #     )
  # with:
      response = await with_deadline(
          logging_obj.http_client_deadline,
          async_httpx_client.post(
              url=api_base,
              headers=headers,
              timeout=resolved_timeout,
              **body_kwargs,
          ),
      )
  ```
  Add the import near the top of `litellm/llms/custom_httpx/llm_http_handler.py`:
  ```python
  from litellm.litellm_core_utils.asyncio_deadline import with_deadline
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/llms/custom_httpx/llm_http_handler.py tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_deadline.py
  git commit -m "feat: enforce http_client total_timeout deadline on responses api non-streaming and stream-establishment"
  ```

### Task 20: streaming phase② deadline wrap in `ResponsesAPIStreamingIterator`

**Files:**
- Modify: `litellm/responses/streaming_iterator.py:563-589` (`__init__`), `litellm/llms/custom_httpx/llm_http_handler.py:2606-2615` (construction call site)
- Test: `tests/test_litellm/responses/test_streaming_iterator.py` (new)

- [ ] **Step 1: failing test — `_http_client_deadline` param, when set and exceeded, raises `DeadlineExceeded` from `__anext__` and closes the underlying response**
  ```python
  # tests/test_litellm/responses/test_streaming_iterator.py
  import asyncio
  from unittest.mock import AsyncMock, MagicMock

  import httpx
  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded
  from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator


  @pytest.mark.asyncio
  async def test_streaming_iterator_raises_deadline_exceeded_and_closes_response():
      async def _aiter_bytes():
          yield b'data: {"type": "response.output_text.delta"}\n\n'
          await asyncio.sleep(10)
          yield b"never"  # pragma: no cover

      response = MagicMock(spec=httpx.Response)
      response.aiter_bytes = _aiter_bytes
      response.aclose = AsyncMock()

      iterator = ResponsesAPIStreamingIterator(
          response=response,
          model="github_copilot/gpt-4",
          responses_api_provider_config=MagicMock(),
          logging_obj=MagicMock(),
          _http_client_deadline=asyncio.get_event_loop().time() + 0.05,
      )

      with pytest.raises(DeadlineExceeded):
          async for _ in iterator:
              pass
      response.aclose.assert_awaited_once()


  @pytest.mark.asyncio
  async def test_streaming_iterator_no_deadline_unaffected():
      async def _aiter_bytes():
          yield b'data: {"type": "response.completed"}\n\n'

      response = MagicMock(spec=httpx.Response)
      response.aiter_bytes = _aiter_bytes

      iterator = ResponsesAPIStreamingIterator(
          response=response,
          model="github_copilot/gpt-4",
          responses_api_provider_config=MagicMock(),
          logging_obj=MagicMock(),
      )
      # should not raise DeadlineExceeded; whatever downstream parsing does is out of scope here
      with pytest.raises(StopAsyncIteration):
          while True:
              await iterator.__anext__()
  ```
  Run: confirm `TypeError: unexpected keyword argument '_http_client_deadline'`.

- [ ] **Step 2: add the param and wrap**
  ```python
  # litellm/responses/streaming_iterator.py, class ResponsesAPIStreamingIterator.__init__,
  # add a new keyword-only parameter (default None) to the existing signature at lines 568-578:
      def __init__(
          self,
          response: httpx.Response,
          model,
          responses_api_provider_config,
          logging_obj,
          litellm_metadata=None,
          custom_llm_provider=None,
          request_data=None,
          call_type=None,
          _http_client_deadline: Optional[float] = None,
      ):
          super().__init__(...)  # unchanged existing super().__init__ call and other assignments
          self.stream_iterator = (
              SSEDecoder().aiter_bytes(
                  DeadlineBoundAsyncIterator(
                      response.aiter_bytes(), _http_client_deadline, on_timeout_close=response.aclose
                  )
              )
              if _http_client_deadline is not None
              else SSEDecoder().aiter_bytes(response.aiter_bytes())
          )
  ```
  Add the import near the top of `litellm/responses/streaming_iterator.py`:
  ```python
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator
  ```
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, at the ResponsesAPIStreamingIterator
  # construction site (lines 2606-2615), add the new kwarg:
      return ResponsesAPIStreamingIterator(
          response=response,
          model=model,
          logging_obj=logging_obj,
          responses_api_provider_config=responses_api_provider_config,
          litellm_metadata=litellm_metadata,
          custom_llm_provider=custom_llm_provider,
          request_data=request_context,
          call_type=CallTypes.responses.value,
          _http_client_deadline=logging_obj.http_client_deadline,
      )
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/responses/streaming_iterator.py litellm/llms/custom_httpx/llm_http_handler.py tests/test_litellm/responses/test_streaming_iterator.py
  git commit -m "feat: enforce http_client total_timeout deadline on ResponsesAPIStreamingIterator"
  ```

### Task 20a: native Responses streaming phase② — a mid-stream `DeadlineExceeded` must trigger Router's cross-deployment fallback, not leak raw (new — review finding C, responses half, 3rd review round)

**Why this task exists:** Task 20 gives `ResponsesAPIStreamingIterator` the ability to *raise* `DeadlineExceeded` once `total_timeout` expires mid-stream, but its `__anext__` (lines 594-630) catches that exception with nothing more specific than the generic `except Exception as e: ... raise e` at the bottom (there is only one more specific clause above it, `except httpx.HTTPError`, which `DeadlineExceeded` — a bare `TimeoutError` subclass, not an `httpx` type — never matches). The raw `DeadlineExceeded` therefore reaches every caller unmodified. Two things follow from that, both wrong per the frozen spec's Responses-face contract:
1. `Router._aresponses_streaming_iterator` (`litellm/router.py:2241-2459`) only re-enters its fallback chain on `except MidStreamFallbackError as e:` (line 2374). A bare `DeadlineExceeded` is not that type, so it propagates straight through `FallbackResponsesStreamWrapper.__anext__` uncaught — Router's configured cross-deployment fallback silently never fires for a Responses-API deadline expiry, defeating the entire point of `total_timeout` on this face.
2. Even ignoring Router, any direct caller of `litellm.aresponses(..., stream=True)` (no Router in the path) would see an internal `litellm_core_utils.asyncio_deadline.DeadlineExceeded` instead of the public `litellm.Timeout` contract Task 8a already established for every other choke point on this exact exception (`exception_type()` already has the `isinstance(original_exception, DeadlineExceeded)` branch from Task 8a — this task is the one remaining call site on the Responses streaming face that never routes through it).

Unlike the chat-completions face (Task 16a), the Responses face's `FallbackResponsesStreamWrapper.stream_with_fallbacks()` has **no** status-code-based skip-fallback filter at all (confirmed by reading `litellm/router.py:2369-2439` in full this round — it unconditionally attempts a fallback for any `MidStreamFallbackError`, no `_handle_stream_fallback_error`-style 4xx carve-out exists on this face). So this task only needs to *produce* a `MidStreamFallbackError`; there is no parallel 408-collision bug to fix here (that bug is specific to `litellm_core_utils/streaming_handler.py`'s shared `_handle_stream_fallback_error`, which the native Responses face does not use).

**Files:**
- Modify: `litellm/responses/streaming_iterator.py:568-589` (`ResponsesAPIStreamingIterator.__init__`, add `self._any_chunk_yielded`), `:594-630` (`__anext__`, add the new `except DeadlineExceeded` clause and set the flag before each `return result`), top-of-file imports (extend the existing `asyncio_deadline` import with `DeadlineExceeded`; add `exception_type`)
- Modify (test): `tests/test_litellm/responses/test_streaming_iterator.py` — **rewrite** Task 20's own `test_streaming_iterator_raises_deadline_exceeded_and_closes_response` (its current `pytest.raises(DeadlineExceeded)` assertion is exactly the bug this task fixes, so leaving it as-is would pin the wrong behavior)
- Modify (test): `tests/router_unit_tests/test_router_aresponses_streaming_fallback.py` — new Router-wrapped test using a real `ResponsesAPIStreamingIterator` as the source (not a `_FakeSource`/`MagicMock`), per the coordinator's explicit requirement for both a direct-iterator test and a Router-wrapped test

- [ ] **Step 1: failing tests**

  **1a. Rewrite the existing direct-iterator test** (replaces the version written in Task 20 — same file, same test name, new body):
  ```python
  # tests/test_litellm/responses/test_streaming_iterator.py
  import asyncio
  from unittest.mock import AsyncMock, MagicMock

  import httpx
  import pytest

  import litellm
  from litellm.exceptions import MidStreamFallbackError
  from litellm.responses.streaming_iterator import ResponsesAPIStreamingIterator


  @pytest.mark.asyncio
  async def test_streaming_iterator_raises_deadline_exceeded_and_closes_response():
      """Regression for 3rd-round review finding C (responses half): a mid-stream
      total_timeout expiry must surface as MidStreamFallbackError wrapping
      litellm.Timeout, not the internal DeadlineExceeded — otherwise
      Router._aresponses_streaming_iterator's `except MidStreamFallbackError`
      (router.py:2374) never engages and cross-deployment fallback silently
      never fires for this face."""

      async def _aiter_bytes():
          yield b'data: {"type": "response.output_text.delta"}\n\n'
          await asyncio.sleep(10)
          yield b"never"  # pragma: no cover

      response = MagicMock(spec=httpx.Response)
      response.aiter_bytes = _aiter_bytes
      response.aclose = AsyncMock()

      iterator = ResponsesAPIStreamingIterator(
          response=response,
          model="github_copilot/gpt-4",
          responses_api_provider_config=MagicMock(),
          logging_obj=MagicMock(),
          _http_client_deadline=asyncio.get_event_loop().time() + 0.05,
      )

      with pytest.raises(MidStreamFallbackError) as exc_info:
          async for _ in iterator:
              pass
      assert isinstance(exc_info.value.original_exception, litellm.Timeout)
      # One delta chunk was already yielded before the deadline hit, so this
      # is NOT a pre-first-chunk failure — the fallback continuation-input
      # path (router.py:2391-2400) depends on this being False.
      assert exc_info.value.is_pre_first_chunk is False
      response.aclose.assert_awaited_once()
  ```
  Run: confirm today's actual failure is `Failed: DID NOT RAISE <class 'litellm.exceptions.MidStreamFallbackError'>` (the exception raised is the raw `DeadlineExceeded`, which does not match).

  **1b. New Router-wrapped test** (proves the fix integrates with Router's actual fallback machinery, not just the iterator in isolation):
  ```python
  # tests/router_unit_tests/test_router_aresponses_streaming_fallback.py
  # add near the other _aresponses_streaming_iterator tests

  @pytest.mark.asyncio
  async def test_aresponses_streaming_iterator_wraps_real_iterator_deadline_into_fallback():
      """Regression for 3rd-round review finding C (responses half): drives a
      *real* ResponsesAPIStreamingIterator (not a fake/mock source) through
      Router._aresponses_streaming_iterator so a mid-stream DeadlineExceeded
      exercises the actual `except MidStreamFallbackError` branch
      (router.py:2374), not just the iterator's own exception mapping."""
      import asyncio
      from unittest.mock import AsyncMock, MagicMock, patch

      import httpx

      from litellm.responses.streaming_iterator import (
          BaseResponsesAPIStreamingIterator,
          ResponsesAPIStreamingIterator,
      )
      from litellm.types.llms.openai import (
          ResponseAPIUsage,
          ResponseCompletedEvent,
          ResponsesAPIResponse,
          ResponsesAPIStreamEvents,
      )

      async def _aiter_bytes():
          yield b'data: {"type": "response.output_text.delta"}\n\n'
          await asyncio.sleep(10)
          yield b"never"  # pragma: no cover

      response = MagicMock(spec=httpx.Response)
      response.aiter_bytes = _aiter_bytes
      response.aclose = AsyncMock()

      source = ResponsesAPIStreamingIterator(
          response=response,
          model="openai/gpt-4o-mini",
          responses_api_provider_config=MagicMock(),
          logging_obj=MagicMock(),
          custom_llm_provider="openai",
          _http_client_deadline=asyncio.get_event_loop().time() + 0.05,
      )

      fallback_completed = ResponseCompletedEvent.model_construct(
          type=ResponsesAPIStreamEvents.RESPONSE_COMPLETED,
          response=ResponsesAPIResponse.model_construct(
              usage=ResponseAPIUsage(input_tokens=1, output_tokens=1, total_tokens=2)
          ),
      )

      async def _fallback_stream():
          yield fallback_completed

      router = _make_router()
      with patch.object(
          router,
          "async_function_with_fallbacks_common_utils",
          new=AsyncMock(return_value=_fallback_stream()),
      ) as mock_fallback:
          wrapper = await router._aresponses_streaming_iterator(
              source, initial_kwargs={"model": "primary"}
          )
          assert isinstance(wrapper, BaseResponsesAPIStreamingIterator)
          collected = [ev async for ev in wrapper]

      # The raw DeadlineExceeded (and the intermediate MidStreamFallbackError)
      # never reached the caller — Router's own fallback branch absorbed it
      # and yielded the fallback stream's event instead.
      assert len(collected) == 1
      assert collected[0].type == ResponsesAPIStreamEvents.RESPONSE_COMPLETED
      mock_fallback.assert_awaited_once()
  ```
  Run: confirm today's actual failure — without the fix, the raw `DeadlineExceeded` escapes `stream_with_fallbacks()`'s `try/except MidStreamFallbackError` unchanged (it's the wrong exception type), so the test fails with an uncaught `DeadlineExceeded` propagating out of the `async for ev in wrapper` loop instead of reaching the `collected == [...]` assertions.

- [ ] **Step 2: implementation**
  ```python
  # litellm/responses/streaming_iterator.py — extend the existing asyncio_deadline
  # import added in Task 20, and add exception_type:
  from litellm.litellm_core_utils.asyncio_deadline import (
      DeadlineBoundAsyncIterator,
      DeadlineExceeded,
  )
  from litellm.litellm_core_utils.exception_mapping_utils import exception_type
  ```
  ```python
  # ResponsesAPIStreamingIterator.__init__ — add one new attribute
  # (placed after self.stream_iterator = ... from Task 20):
      self._any_chunk_yielded = False
  ```
  ```python
  # ResponsesAPIStreamingIterator.__anext__ — set the flag right before the
  # existing `return result`, and add a new except clause between the
  # existing `except httpx.HTTPError as e:` block and the generic
  # `except Exception as e:` block:
              elif result is not None:
                  result = await self._call_post_streaming_deployment_hook(
                      chunk=result,
                  )
                  self._any_chunk_yielded = True
                  return result
              # If result is None, continue the loop to get the next chunk

      except StopAsyncIteration:
          raise
      except httpx.HTTPError as e:
          self.finished = True
          self._handle_failure(e)
          raise e
      except DeadlineExceeded as e:
          # Map to the public litellm.Timeout contract (Task 8a's exception_type()
          # branch), then wrap for Router's Responses-face fallback contract
          # (Router._aresponses_streaming_iterator's `except MidStreamFallbackError`,
          # router.py:2374) — parity with the chat-completions face (Task 16a),
          # except this face has no status-code skip-fallback filter to carve
          # a collision out of, so no frozenset/constant is needed here.
          from litellm.exceptions import MidStreamFallbackError

          self.finished = True
          # exception_type() RAISES its mapped exception (litellm.Timeout) rather than
          # returning it (exception_mapping_utils.py:2150-2160/2234-2247), so it must be
          # called inside try/except and the mapped exception caught -- a bare
          # `mapped = exception_type(...)` assignment would never complete, leaving the
          # _handle_failure()/MidStreamFallbackError below unreachable.
          try:
              exception_type(
                  model=self.model,
                  custom_llm_provider=self.custom_llm_provider,
                  original_exception=e,
                  completion_kwargs={},
                  extra_kwargs={},
              )
              raise AssertionError("exception_type() must raise")  # defensive; never reached
          except Exception as mapping_error:
              mapped_exception = mapping_error
          self._handle_failure(mapped_exception)
          raise MidStreamFallbackError(
              message=str(mapped_exception),
              model=self.model,
              llm_provider=self.custom_llm_provider or "responses",
              original_exception=mapped_exception,
              is_pre_first_chunk=not self._any_chunk_yielded,
          ) from e
      except Exception as e:
          self.finished = True
          self._handle_failure(e)
          raise e
  ```
  Run: all three tests (Step 1a rewritten, Step 1b new, plus Task 20's untouched `test_streaming_iterator_no_deadline_unaffected`) pass.
  Commit:
  ```
  git add litellm/responses/streaming_iterator.py tests/test_litellm/responses/test_streaming_iterator.py tests/router_unit_tests/test_router_aresponses_streaming_fallback.py
  git commit -m "fix: map a mid-stream DeadlineExceeded to MidStreamFallbackError in ResponsesAPIStreamingIterator so Router's cross-deployment fallback fires"
  ```

**Implementer grep-confirmation needed (honestly flagged, not verified against a live diff since Task 20 itself hasn't been implemented yet at plan-writing time):** re-confirm the exact line numbers for `ResponsesAPIStreamingIterator.__init__`/`__anext__` once Task 20's edits actually land (Task 20 itself shifts the file's line numbers from the pre-Task-20 baseline this task's line references were taken from), and re-confirm `Router._aresponses_streaming_iterator`'s `async_function_with_fallbacks_common_utils` is still the correct patch target (not renamed) at implementation time.

### Task 21: responses-completion-bridge regression test (no new production code)

**Files:**
- Test: `tests/test_litellm/responses/litellm_completion_transformation/test_handler.py` (extend existing file)

- [ ] **Step 1: write a passing regression test proving the bridge, which internally calls `litellm.acompletion`/streams via `CustomStreamWrapper`, transitively inherits the Task 12/14/15/16 deadline enforcement with zero bridge-specific code**
  ```python
  # append to tests/test_litellm/responses/litellm_completion_transformation/test_handler.py
  from unittest.mock import AsyncMock, patch

  import pytest

  from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded


  @pytest.mark.asyncio
  async def test_completion_bridge_inherits_chat_deadline_enforcement():
      """The responses-api completion-bridge path (used when a provider config has no
      native responses handler) delegates to litellm.acompletion under the hood via
      LiteLLMCompletionTransformationHandler.async_response_api_handler; it must
      raise DeadlineExceeded via the exact same seam Task 14 added, with no bridge-specific
      deadline code."""
      from litellm.responses.litellm_completion_transformation.handler import (
          LiteLLMCompletionTransformationHandler,
      )

      with patch(
          "litellm.main.OpenAIChatCompletion.acompletion",
          new=AsyncMock(side_effect=DeadlineExceeded("simulated")),
      ):
          with pytest.raises(DeadlineExceeded):
              await LiteLLMCompletionTransformationHandler().async_response_api_handler(
                  litellm_completion_request={
                      "model": "github_copilot/gpt-4",
                      "messages": [{"role": "user", "content": "hi"}],
                      "http_client": {"total_timeout": 1.0},
                  },
                  request_input="hi",
                  responses_api_request={},
              )
  ```
  *(Corrected against the real class during plan revision: the bridge's entry point is the instance method `LiteLLMCompletionTransformationHandler.async_response_api_handler` (`litellm/responses/litellm_completion_transformation/handler.py:90-96`), taking `litellm_completion_request: dict, request_input, responses_api_request: ResponsesAPIOptionalRequestParams (a plain dict-shaped TypedDict, not a MagicMock), **kwargs` — NOT `LiteLLMCompletionResponsesConfig` (a sibling, unrelated request/response-transformation class with no such method) with `model`/`input`/`http_client` as flat kwargs, which the previous draft of this test incorrectly assumed and would have failed with `AttributeError`/`TypeError` before ever reaching the deadline check. `async_response_api_handler` merges `kwargs` then `litellm_completion_request` into `acompletion_args` and calls `litellm.acompletion(**acompletion_args)`, so `model`/`messages`/`http_client` must live inside `litellm_completion_request` as shown, not as top-level call arguments. Re-verify this signature at implementation time, since it may have drifted further.)*
  Run: confirm it passes given Phase 3 already lands the seam; if it fails, that is a genuine gap in Phase 3 (the bridge calls a different code path than assumed) and must be fixed by re-checking which chat-face function the bridge actually calls, not by adding bridge-specific deadline code (per the "no Router/bridge production changes" design decision).

- [ ] **Step 2: commit**
  ```
  git add tests/test_litellm/responses/litellm_completion_transformation/test_handler.py
  git commit -m "test: lock in that responses completion-bridge inherits chat deadline enforcement"
  ```

### Task 21a: responses face wire-body no-leak regression test (review finding #4, wire-safety half)

**Why this task exists:** the Responses face threads `litellm_params` (which now carries `http_client`, per Task 5) through several `dict(litellm_params)` call sites that chat's face does not have an equivalent of — `get_complete_url(litellm_params=dict(litellm_params))`, `sign_request(optional_params=dict(litellm_params), ...)`, and the agentic-hooks `kwargs=dict(litellm_params)` (`litellm/llms/custom_httpx/llm_http_handler.py:2524, 2562, 2645`). None of these are the chat face's `all_litellm_params`-registry leak-prevention mechanism (Task 6), so this task cannot simply assume Task 6 covers it — it must independently prove, end to end, that `http_client` never reaches the actual bytes sent over the wire for a real (non-bridge) responses-api provider config, mirroring Task 17's chat-face gold-standard pattern.

**Files:**
- Create: `tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_no_leak.py` (modeled on `tests/test_litellm/llms/openai/test_http_client_config_no_leak.py` from Task 17)

- [ ] **Step 1: write the wire-body capture test (should already pass given Task 18a's merge/resolve reads `http_client` only to build `resolved_timeout`, never writing it into `data`; if it fails, that reveals a real leak path through one of the `dict(litellm_params)` call sites above and must be fixed here before moving on)**
  ```python
  # tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_no_leak.py
  """Regression: http_client config must never reach the outbound wire body sent to the
  upstream responses API endpoint, including via the dict(litellm_params) values threaded
  through get_complete_url/sign_request/agentic-hooks kwargs that the responses face (unlike
  chat) passes around internally."""

  import json
  from unittest.mock import MagicMock, patch

  import pytest

  import litellm


  @pytest.mark.asyncio
  async def test_http_client_config_does_not_leak_into_responses_wire_body():
      captured_bodies = []

      async def _fake_post(*args, **kwargs):
          body = kwargs.get("data") or kwargs.get("json")
          captured_bodies.append(json.loads(body) if isinstance(body, (str, bytes)) else body)
          response = MagicMock()
          response.json = MagicMock(return_value={"id": "resp_1", "output": [], "status": "completed"})
          response.headers = {}
          response.status_code = 200
          return response

      with patch(
          "litellm.llms.custom_httpx.http_handler.AsyncHTTPHandler.post", side_effect=_fake_post
      ):
          await litellm.aresponses(
              model="github_copilot/gpt-4",
              input="hi",
              http_client={"connect_timeout": 2.0, "total_timeout": 30.0},
          )

      assert len(captured_bodies) == 1
      assert "http_client" not in captured_bodies[0]
      assert "http_client" not in json.dumps(captured_bodies[0])
  ```
  Run: `pytest tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_no_leak.py -x`. **Open item for the implementer:** confirm `github_copilot`'s actual `responses_api_provider_config.sign_request` implementation does not serialize `optional_params`/`dict(litellm_params)` into the signed body (a behavior specific to some non-OpenAI-compatible provider configs, e.g. AWS SigV4-style signing) — if it does, `http_client` would leak here and must be excluded at the `sign_request` call site (`litellm/llms/custom_httpx/llm_http_handler.py:2560-2569`) by passing a filtered dict instead of the raw `dict(litellm_params)`, not by weakening this test.

- [ ] **Step 2: commit**
  ```
  git add tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_no_leak.py
  git commit -m "test: lock in no wire-body leak for http_client config on responses API"
  ```

### Task 22: messages face — establish deadline + fix missing per-request timeout

**Files:**
- Modify: `litellm/llms/anthropic/experimental_pass_through/messages/handler.py` (`anthropic_messages()`, near its existing `loop = asyncio.get_event_loop()` line), `litellm/llms/custom_httpx/llm_http_handler.py:1869-1998` (`_async_post_anthropic_messages_with_http_error_retry` — add `timeout` parameter and pass it to `async_httpx_client.post`; `async_anthropic_messages_handler` — resolve the httpx.Timeout and pass it through)
- Test: `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py` (extend existing file)

- [ ] **Step 1: failing tests — (a) `anthropic_messages()` establishes the deadline on `logging_obj`; (b) the wire-level `post` call now receives an explicit `timeout=` derived from `resolve_http_client_timeout`, closing the pre-existing "no per-request timeout at all" bug**
  ```python
  # append to tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  import asyncio
  from unittest.mock import AsyncMock, MagicMock, patch

  import httpx
  import pytest

  import litellm


  @pytest.mark.asyncio
  async def test_anthropic_messages_establishes_http_client_deadline():
      captured = []
      original = litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline

      def _capture(self, deadline):
          captured.append(deadline)
          return original(self, deadline)

      with (
          patch(
              "litellm.litellm_core_utils.litellm_logging.Logging.set_http_client_deadline",
              new=_capture,
              autospec=False,
          ),
          patch(
              "litellm.llms.anthropic.experimental_pass_through.messages.handler.anthropic_messages_handler",
              return_value=MagicMock(),
          ),
      ):
          await litellm.anthropic_messages(
              max_tokens=100,
              messages=[{"role": "user", "content": "hi"}],
              model="github_copilot/claude-3-haiku",
              http_client={"total_timeout": 20.0},
          )

      assert len(captured) == 1
      assert captured[0] is not None


  @pytest.mark.asyncio
  async def test_async_post_anthropic_messages_passes_explicit_timeout():
      """Regression for the pre-existing bug: the wire-level post() call previously had no
      timeout= kwarg at all, meaning per-request http_client config (and even the legacy
      per-face default) was silently ignored for anthropic-messages."""
      from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

      handler = BaseLLMHTTPHandler()
      captured_kwargs = []

      async def _fake_post(**kwargs):
          captured_kwargs.append(kwargs)
          response = MagicMock(spec=httpx.Response)
          response.status_code = 200
          return response

      mock_client = MagicMock()
      mock_client.post = AsyncMock(side_effect=_fake_post)

      await handler._async_post_anthropic_messages_with_http_error_retry(
          async_httpx_client=mock_client,
          request_url="https://api.example.com/v1/messages",
          headers={},
          signed_json_body=None,
          request_body={"model": "claude-3-haiku", "messages": []},
          stream=False,
          logging_obj=MagicMock(http_client_deadline=None),
          provider_config=MagicMock(max_retry_on_anthropic_messages_http_error=1),
          litellm_params=MagicMock(http_client=None, timeout=None),
          api_key="fake-key",
          model="claude-3-haiku",
      )

      assert len(captured_kwargs) == 1
      assert "timeout" in captured_kwargs[0]
      assert isinstance(captured_kwargs[0]["timeout"], httpx.Timeout)
  ```
  Run: confirm both fail — the second because `_async_post_anthropic_messages_with_http_error_retry` currently has no `timeout` parameter/argument to `post()` at all.

- [ ] **Step 2: establish the deadline in `anthropic_messages()`** *(reverted in the 3rd review round — review finding G, mirroring Task 12/18: `anthropic_messages()` is always wrapped by `@client`, so `litellm_logging_obj` is a guaranteed non-`None` invariant here too; Task 11a's safety net has been deleted)*
  ```python
  # litellm/llms/anthropic/experimental_pass_through/messages/handler.py, right after the
  # existing `loop = asyncio.get_event_loop()` line inside async def anthropic_messages(...):
      litellm_logging_obj = kwargs.get("litellm_logging_obj")
      assert litellm_logging_obj is not None, (
          "anthropic_messages() is always wrapped by litellm.utils.client's wrapper_async, which "
          "asserts logging_obj is not None and injects it into kwargs before this point runs"
      )
      litellm_logging_obj.set_http_client_deadline(establish_request_deadline(kwargs, now=loop.time))
  ```
  Add the import near the top of `litellm/llms/anthropic/experimental_pass_through/messages/handler.py`:
  ```python
  from litellm.litellm_core_utils.http_client_config import establish_request_deadline
  ```

- [ ] **Step 3: resolve and pass an explicit `timeout=` through `_async_post_anthropic_messages_with_http_error_retry`, wrapped with `with_deadline`**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, add a `timeout: httpx.Timeout` parameter
  # to the existing signature (lines 1869-1884):
      async def _async_post_anthropic_messages_with_http_error_retry(
          self,
          async_httpx_client: AsyncHTTPHandler,
          request_url: str,
          headers: dict,
          signed_json_body: Optional[Union[str, bytes]],
          request_body: dict,
          stream: bool,
          logging_obj: LiteLLMLoggingObj,
          provider_config: BaseAnthropicMessagesConfig,
          litellm_params: GenericLiteLLMParams,
          api_key: Optional[str],
          model: str,
          timeout: httpx.Timeout,
      ) -> httpx.Response:
  ```
  ```python
  # same method body, replacing the existing call at lines 1890-1896:
  #     response = await async_httpx_client.post(
  #         url=request_url, headers=headers, data=signed_json_body or json.dumps(request_body),
  #         stream=stream or False, logging_obj=logging_obj,
  #     )
  # with:
          response = await with_deadline(
              logging_obj.http_client_deadline,
              async_httpx_client.post(
                  url=request_url,
                  headers=headers,
                  data=signed_json_body or json.dumps(request_body),
                  stream=stream or False,
                  timeout=timeout,
                  logging_obj=logging_obj,
              ),
          )
  ```
  Add the import near the top of `litellm/llms/custom_httpx/llm_http_handler.py` (if not already added by Task 19):
  ```python
  from litellm.litellm_core_utils.asyncio_deadline import with_deadline
  ```

- [ ] **Step 4: resolve the httpx.Timeout in `async_anthropic_messages_handler`, merging global `litellm.http_client` with the deployment's `litellm_params.http_client` the same way chat (Task 13) and responses (Task 18a) do, and pass it into the retry helper (review finding #8: this step previously resolved only `litellm_params.http_client` in isolation, silently dropping any global `litellm.http_client` setting for the messages face alone)**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, inside async_anthropic_messages_handler
  # (lines 1930-1998), before the existing call to
  # self._async_post_anthropic_messages_with_http_error_retry(...):
      from litellm.llms.custom_httpx.http_handler import _default_cached_client_timeout

      merged_http_client_cfg = merge_http_client_config(
          parse_http_client_config(getattr(litellm, "http_client", None)),
          parse_http_client_config(litellm_params.http_client),
      )
      resolved_timeout = resolve_http_client_timeout(
          merged_http_client_cfg,
          legacy_effective_timeout=litellm_params.timeout or _default_cached_client_timeout(),
      ).httpx_timeout
  ```
  ```python
  # then update the existing call site to pass timeout=resolved_timeout:
  ```
  Note for the implementer: `parse_http_client_config` accepts either an already-typed `HttpClientConfig`/`None` or a raw `HttpClientConfigDict`/`dict`, so passing `litellm_params.http_client` (already typed per the `GenericLiteLLMParams.http_client: Optional[HttpClientConfig]` field) through `parse_http_client_config` again is a deliberate, harmless idempotent normalization — it keeps this call site textually identical to Task 18a's, rather than special-casing "this one's already parsed."
  ```python
      response = await self._async_post_anthropic_messages_with_http_error_retry(
          async_httpx_client=async_httpx_client,
          request_url=request_url,
          headers=headers,
          signed_json_body=signed_json_body,
          request_body=request_body,
          stream=stream,
          logging_obj=logging_obj,
          provider_config=anthropic_messages_provider_config,
          litellm_params=litellm_params,
          api_key=api_key,
          model=model,
          timeout=resolved_timeout,
      )
  ```
  Add the imports near the top of `litellm/llms/custom_httpx/llm_http_handler.py` (already added by Task 18a if implemented in order; otherwise add here):
  ```python
  from litellm.litellm_core_utils.http_client_config import (
      merge_http_client_config,
      parse_http_client_config,
      resolve_http_client_timeout,
  )
  ```
  Run: both tests pass, plus a new third test below.

- [ ] **Step 5: failing test — messages face merges global `litellm.http_client` with the per-deployment override (review finding #8 regression)**
  ```python
  # append to tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py

  @pytest.mark.asyncio
  async def test_async_anthropic_messages_handler_merges_global_http_client_with_deployment_override():
      """Regression for review finding #8: this call site previously fed
      resolve_http_client_timeout only litellm_params.http_client, silently ignoring any global
      litellm.http_client setting -- unlike chat (Task 13) and responses (Task 18a), which both
      merge global + deployment before resolving."""
      import litellm
      from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
      from litellm.types.router import GenericLiteLLMParams

      handler = BaseLLMHTTPHandler()
      captured_kwargs = []

      async def _fake_post(**kwargs):
          captured_kwargs.append(kwargs)
          response = MagicMock(spec=httpx.Response)
          response.status_code = 200
          return response

      mock_client = MagicMock()
      mock_client.post = AsyncMock(side_effect=_fake_post)

      original_global_http_client = litellm.http_client
      litellm.http_client = {"connect_timeout": 4.0, "pool_timeout": 12.0}
      try:
          with patch(
              "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
              return_value=mock_client,
          ):
              try:
                  await handler.async_anthropic_messages_handler(
                      model="claude-3-haiku",
                      messages=[{"role": "user", "content": "hi"}],
                      anthropic_messages_provider_config=MagicMock(),
                      anthropic_messages_optional_request_params={},
                      custom_llm_provider="anthropic",
                      litellm_params=GenericLiteLLMParams(http_client={"connect_timeout": 2.0}),
                      logging_obj=MagicMock(http_client_deadline=None),
                  )
              except Exception:
                  pass  # request construction beyond the post() call is out of scope here
      finally:
          litellm.http_client = original_global_http_client

      assert len(captured_kwargs) == 1
      resolved = captured_kwargs[0]["timeout"]
      assert resolved.connect == 2.0  # deployment overrides global
      assert resolved.pool == 12.0  # falls back to global
  ```
  Run: confirm failure prior to Step 4's merge fix; passes after.
  Commit:
  ```
  git add litellm/llms/anthropic/experimental_pass_through/messages/handler.py litellm/llms/custom_httpx/llm_http_handler.py tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  git commit -m "fix: pass explicit resolved timeout to anthropic messages http_error_retry post call, merging global and deployment http_client config"
  ```

### Task 23: messages face — fix wrong httpx-client cache key + streaming deadline wrap + wire the deadline through to the one real call site

**Files:**
- Modify: `litellm/llms/custom_httpx/llm_http_handler.py:1950-1951` (cache-key bug), `litellm/proxy/pass_through_endpoints/streaming_handler.py:28-88` (`chunk_processor`), `litellm/llms/anthropic/experimental_pass_through/messages/streaming_iterator.py:57-77` (`get_async_streaming_response_iterator` — review finding #7)
- Test: `tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py` (extend existing file), `tests/test_litellm/proxy/pass_through_endpoints/test_streaming_handler_interrupt.py` (extend existing file), `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py` (extend existing file)

- [ ] **Step 1: failing test — `get_async_httpx_client` is called with `custom_llm_provider`'s actual `LlmProviders` value, not a hardcoded `ANTHROPIC`, so a `github_copilot`-routed messages call gets its own cached client (and hence its own resolved `http_client` config) instead of silently sharing Anthropic's cached client/timeout**
  ```python
  # append to tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py
  from unittest.mock import MagicMock, patch

  import pytest

  import litellm


  @pytest.mark.asyncio
  async def test_async_anthropic_messages_handler_uses_actual_provider_for_client_cache_key():
      from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

      handler = BaseLLMHTTPHandler()
      captured_providers = []

      def _fake_get_async_httpx_client(llm_provider, **kwargs):
          captured_providers.append(llm_provider)
          mock_client = MagicMock()
          mock_client.post = MagicMock()
          return mock_client

      with patch(
          "litellm.llms.custom_httpx.llm_http_handler.get_async_httpx_client",
          side_effect=_fake_get_async_httpx_client,
      ):
          try:
              await handler.async_anthropic_messages_handler(
                  model="claude-3-haiku",
                  messages=[{"role": "user", "content": "hi"}],
                  anthropic_messages_provider_config=MagicMock(),
                  anthropic_messages_optional_request_params={},
                  custom_llm_provider="github_copilot",
                  litellm_params=MagicMock(http_client=None, timeout=None),
                  logging_obj=MagicMock(http_client_deadline=None),
              )
          except Exception:
              pass  # request construction downstream of client selection is out of scope here

      assert len(captured_providers) >= 1
      assert captured_providers[0] == litellm.LlmProviders.GITHUB_COPILOT
  ```
  Run: confirm failure — today `captured_providers[0]` is hardcoded `litellm.LlmProviders.ANTHROPIC` regardless of `custom_llm_provider`.

- [ ] **Step 2: fix the cache-key bug**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, replacing the existing line (1950-1951):
  #     if client is None or not isinstance(client, AsyncHTTPHandler):
  #         async_httpx_client = get_async_httpx_client(llm_provider=litellm.LlmProviders.ANTHROPIC)
  # with:
      if client is None or not isinstance(client, AsyncHTTPHandler):
          async_httpx_client = get_async_httpx_client(
              llm_provider=litellm.LlmProviders(custom_llm_provider)
          )
  ```
  Run: test passes.

- [ ] **Step 3: failing test — `chunk_processor` accepts a new `_http_client_deadline` kwarg and enforces it on both `aiter_bytes()` call sites**
  ```python
  # append to tests/test_litellm/proxy/pass_through_endpoints/test_streaming_handler_interrupt.py
  @pytest.mark.asyncio
  async def test_chunk_processor_raises_deadline_exceeded_when_deadline_passed():
      from litellm.litellm_core_utils.asyncio_deadline import DeadlineExceeded

      async def _slow_chunks():
          yield b"chunk-1"
          await asyncio.sleep(10)
          yield b"never"  # pragma: no cover

      response = MagicMock(spec=httpx.Response)
      response.status_code = 200
      response.aiter_bytes = _slow_chunks
      response.aclose = AsyncMock()

      mock_logging_obj = MagicMock()
      mock_passthrough_handler = MagicMock()

      with patch.object(
          PassThroughStreamingHandler, "_route_streaming_logging_to_handler", new=AsyncMock()
      ):
          received = []
          with pytest.raises(DeadlineExceeded):
              async for chunk in PassThroughStreamingHandler.chunk_processor(
                  response=response,
                  request_body={"model": "claude-3-haiku"},
                  litellm_logging_obj=mock_logging_obj,
                  endpoint_type=EndpointType.GENERIC,
                  start_time=datetime.now(),
                  passthrough_success_handler_obj=mock_passthrough_handler,
                  url_route="/anthropic/v1/messages",
                  _http_client_deadline=asyncio.get_event_loop().time() + 0.05,
              ):
                  received.append(chunk)
          await asyncio.sleep(0)
      assert received == [b"chunk-1"]
      response.aclose.assert_awaited_once()
  ```
  Run: confirm `TypeError: unexpected keyword argument '_http_client_deadline'`.

- [ ] **Step 4: add the parameter and wrap both `aiter_bytes()` call sites once via a shared local variable**
  ```python
  # litellm/proxy/pass_through_endpoints/streaming_handler.py, extend the existing signature
  # (lines 28-37) with a new keyword-only parameter:
      @staticmethod
      async def chunk_processor(
          response: httpx.Response,
          request_body: Optional[dict],
          litellm_logging_obj: LiteLLMLoggingObj,
          endpoint_type: EndpointType,
          start_time: datetime,
          passthrough_success_handler_obj: PassThroughEndpointLogging,
          url_route: str,
          _http_client_deadline: Optional[float] = None,
      ):
  ```
  ```python
  # immediately inside the function body, before the existing `try:` block, build ONE
  # deadline-bound iterator and reuse it at both existing `async for chunk in
  # response.aiter_bytes():` call sites (replacing `response.aiter_bytes()` with this
  # local variable at both lines 59 and 68):
      byte_iterator = (
          DeadlineBoundAsyncIterator(
              response.aiter_bytes(), _http_client_deadline, on_timeout_close=response.aclose
          )
          if _http_client_deadline is not None
          else response.aiter_bytes()
      )
  ```
  Then replace both existing occurrences of `async for chunk in response.aiter_bytes():` with `async for chunk in byte_iterator:`.
  Add the import near the top of `litellm/proxy/pass_through_endpoints/streaming_handler.py`:
  ```python
  from litellm.litellm_core_utils.asyncio_deadline import DeadlineBoundAsyncIterator
  ```
  Run: tests pass.
  Commit:
  ```
  git add litellm/llms/custom_httpx/llm_http_handler.py litellm/proxy/pass_through_endpoints/streaming_handler.py tests/test_litellm/llms/custom_httpx/test_llm_http_handler.py tests/test_litellm/proxy/pass_through_endpoints/test_streaming_handler_interrupt.py
  git commit -m "fix: use actual provider for anthropic-messages httpx client cache key; enforce deadline on passthrough streaming"
  ```

- [ ] **Step 5: failing test — the messages face's own streaming call site actually passes `_http_client_deadline` through to `chunk_processor` (review finding #7: Step 3/4 above change `chunk_processor`'s signature, but the one call site this whole face relies on, `BaseAnthropicMessagesStreamingIterator.get_async_streaming_response_iterator`, was never updated to pass the new kwarg — so messages-face streaming silently never enforced the deadline this task exists to add)**
  ```python
  # append to tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  from unittest.mock import MagicMock, patch


  def test_get_async_streaming_response_iterator_passes_http_client_deadline():
      from litellm.llms.anthropic.experimental_pass_through.messages.streaming_iterator import (
          BaseAnthropicMessagesStreamingIterator,
      )

      logging_obj = MagicMock()
      logging_obj.http_client_deadline = 12345.0
      instance = BaseAnthropicMessagesStreamingIterator(litellm_logging_obj=logging_obj, request_body={})

      with patch(
          "litellm.proxy.pass_through_endpoints.streaming_handler.PassThroughStreamingHandler.chunk_processor"
      ) as mock_chunk_processor:
          instance.get_async_streaming_response_iterator(
              httpx_response=MagicMock(),
              request_body={},
              litellm_logging_obj=logging_obj,
          )

      assert mock_chunk_processor.call_args.kwargs.get("_http_client_deadline") == 12345.0
  ```
  Run: confirm failure — today `chunk_processor` is called without a `_http_client_deadline` kwarg at all, so `mock_chunk_processor.call_args.kwargs.get("_http_client_deadline")` is `None`, not `12345.0`.

- [ ] **Step 6: pass the deadline through at the call site**
  ```python
  # litellm/llms/anthropic/experimental_pass_through/messages/streaming_iterator.py, inside
  # get_async_streaming_response_iterator, replacing the existing call (lines 69-77):
      return PassThroughStreamingHandler.chunk_processor(
          response=httpx_response,
          request_body=request_body,
          litellm_logging_obj=litellm_logging_obj,
          endpoint_type=EndpointType.ANTHROPIC,
          start_time=self.start_time,
          passthrough_success_handler_obj=GLOBAL_PASS_THROUGH_SUCCESS_HANDLER_OBJ,
          url_route="/v1/messages",
          _http_client_deadline=getattr(litellm_logging_obj, "http_client_deadline", None),
      )
  ```
  Run: test passes.
  Commit:
  ```
  git add litellm/llms/anthropic/experimental_pass_through/messages/streaming_iterator.py tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  git commit -m "fix: pass http_client_deadline through to chunk_processor at the messages streaming iterator's call site"
  ```

### Task 24: messages wire-body no-leak regression test

**Files:**
- Create: `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_http_client_config_no_leak.py`

- [ ] **Step 1: write the wire-body capture test (regression lock-in; if it fails, it surfaces a leak path Task 22/23 missed)**
  ```python
  """Regression: http_client config must never reach the outbound wire body sent to the
  upstream /v1/messages endpoint."""

  import json
  from unittest.mock import AsyncMock, MagicMock, patch

  import pytest

  import litellm


  @pytest.mark.asyncio
  async def test_http_client_config_does_not_leak_into_anthropic_messages_wire_body():
      captured_bodies = []

      async def _fake_post(**kwargs):
          body = kwargs.get("data")
          captured_bodies.append(json.loads(body) if isinstance(body, (str, bytes)) else body)
          response = MagicMock()
          response.status_code = 200
          response.json = MagicMock(return_value={"content": [], "role": "assistant"})
          return response

      with patch(
          "litellm.llms.custom_httpx.http_handler.AsyncHTTPHandler.post", side_effect=_fake_post
      ):
          await litellm.anthropic_messages(
              max_tokens=100,
              messages=[{"role": "user", "content": "hi"}],
              model="github_copilot/claude-3-haiku",
              http_client={"connect_timeout": 2.0},
          )

      assert len(captured_bodies) == 1
      assert "http_client" not in captured_bodies[0]
  ```
  Run: confirm it passes given the earlier tasks; if it fails, fix the discovered leak site (most likely `all_litellm_params` needs an additional key registered for a messages-specific catch-all, or the explicit-param extraction in `anthropic_messages()` needs `http_client` added to its named-pop list) before committing.

- [ ] **Step 2: commit**
  ```
  git add tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_http_client_config_no_leak.py
  git commit -m "test: lock in no wire-body leak for http_client config on anthropic messages"
  ```

### Task 24a: messages direct-SDK failure-hook "plan b" regression (review finding #10 — a frozen spec decision, not new production code)

**Why no production code:** the spec froze this as **plan b** (spec section on failure-hook ownership, "已冻结，方案 b"): a direct-SDK consumer of `litellm.anthropic_messages()`'s streaming iterator gets **partial-spend logging only, zero `failure_handler` triggers**, matching `chunk_processor`'s existing behavior exactly (its `except Exception as e: ... raise` at `streaming_handler.py:85-87` never calls a failure handler; its `finally:` block at lines 88-112 only ever schedules `_route_streaming_logging_to_handler`, i.e. a *success*-shaped partial-spend log, and only when `raw_bytes` is non-empty and `response.status_code < 400`). The full failure hook remains the proxy's own responsibility at `litellm/proxy/common_request_processing.py:2551-2577`, which direct-SDK callers never go through. Task 23 already wires `_http_client_deadline` all the way to `chunk_processor` for the messages face; this task is the acceptance-level regression that pins the resulting behavior down so a future change to `chunk_processor` cannot silently start double-logging or start calling a failure handler that direct-SDK callers never asked for.

**3rd-round rewrite (finding D):** the original draft below drove the assertion with `await asyncio.sleep(0)` after the exception, hoping that single scheduler yield would be enough for `GLOBAL_LOGGING_WORKER`'s background worker thread to pick up and run the enqueued coroutine before the test's assertions ran. That is a race, not a regression test: the logging worker's `ensure_initialized_and_enqueue` (`litellm/litellm_core_utils/logging_worker.py:331-336`) starts a worker (thread + its own event loop, `self.start()`) and hands the coroutine to it via `self.enqueue(...)` — there is no guarantee that thread gets scheduled, gets an event-loop turn, and completes the coroutine within one `sleep(0)` on the *test's* loop; on a loaded CI box this test would be flaky in exactly the direction that hides a real regression (a false pass when the coroutine never actually ran, silently permitting `mock_partial_spend_logger.assert_awaited_once()` — currently *absent* from the original draft entirely — to never have been checked with a real await having happened). Fixed by patching `GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue` itself (an attribute of a live singleton instance, safe to patch via `patch.object`) to capture the coroutine object synchronously instead of handing it to the background thread, then `await`ing that captured coroutine directly on the test's own event loop — deterministic, no thread hand-off, no sleep. The original draft also used a bare `model="anthropic/claude-3-haiku"` with no `api_key`, silently depending on whatever `ANTHROPIC_API_KEY` happens to be set in the ambient test environment; fixed by passing an explicit fake `api_key`.

**Files:**
- Test only: `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_http_client_config_no_leak.py` (extend — the file Task 24 just created)

- [ ] **Step 1: failing-until-earlier-tasks-land test — a `total_timeout` deadline firing mid-stream on a direct `litellm.anthropic_messages()` call logs partial spend exactly once (verified by directly awaiting the captured logging coroutine, not by sleeping) and never calls either failure-handler method**
  ```python
  # append to tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_http_client_config_no_leak.py
  import asyncio
  from typing import Coroutine, List

  from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
  from litellm.litellm_core_utils.logging_worker import GLOBAL_LOGGING_WORKER
  from litellm.proxy.pass_through_endpoints.streaming_handler import (
      PassThroughStreamingHandler,
  )


  @pytest.mark.asyncio
  async def test_anthropic_messages_direct_sdk_timeout_logs_partial_spend_not_failure_handler():
      async def _slow_aiter_bytes():
          yield b'event: message_start\ndata: {"type": "message_start"}\n\n'
          await asyncio.sleep(10)
          yield b"never"  # pragma: no cover

      async def _fake_post(**kwargs):
          response = MagicMock()
          response.status_code = 200
          response.aiter_bytes = _slow_aiter_bytes
          response.aclose = AsyncMock()
          return response

      # Capture the coroutine synchronously instead of letting
      # GLOBAL_LOGGING_WORKER hand it to its background worker thread --
      # awaiting it ourselves below is deterministic, unlike sleep(0)
      # racing an unrelated thread's event loop.
      captured_coroutines: List[Coroutine] = []

      def _capture_enqueue(async_coroutine: Coroutine) -> None:
          captured_coroutines.append(async_coroutine)

      with (
          patch(
              "litellm.llms.custom_httpx.http_handler.AsyncHTTPHandler.post",
              side_effect=_fake_post,
          ),
          patch.object(
              GLOBAL_LOGGING_WORKER,
              "ensure_initialized_and_enqueue",
              side_effect=_capture_enqueue,
          ),
          patch.object(
              PassThroughStreamingHandler,
              "_route_streaming_logging_to_handler",
              new=AsyncMock(),
          ) as mock_partial_spend_logger,
          patch.object(LiteLLMLoggingObj, "failure_handler") as mock_sync_failure_handler,
          patch.object(
              LiteLLMLoggingObj, "async_failure_handler", new=AsyncMock()
          ) as mock_async_failure_handler,
      ):
          response = await litellm.anthropic_messages(
              max_tokens=100,
              messages=[{"role": "user", "content": "hi"}],
              model="anthropic/claude-3-haiku",
              api_key="fake-key",  # explicit fake key -- never depend on a real ANTHROPIC_API_KEY
              stream=True,
              http_client={"total_timeout": 0.05},
          )
          with pytest.raises(Exception):
              async for _ in response:
                  pass

          assert len(captured_coroutines) == 1, (
              "chunk_processor's finally block should have scheduled exactly one "
              "partial-spend logging coroutine"
          )
          await captured_coroutines[0]

      mock_partial_spend_logger.assert_awaited_once()
      mock_sync_failure_handler.assert_not_called()
      mock_async_failure_handler.assert_not_awaited()
  ```
  *(Implementer note: exact response shape/streaming entry point for `litellm.anthropic_messages(..., stream=True)` must be confirmed via a quick manual run at implementation time — `python -c` reproduction or a scratch test — since this plan does not re-derive the streaming face's full public return type; if the returned object is not directly async-iterable, adapt the `async for` to whatever the confirmed public streaming contract is, without changing the assertions.)*
  Run: this test's *assertions* should already pass once Task 22/23 land (it exercises existing `chunk_processor` behavior end-to-end, not new production code); if `mock_sync_failure_handler`/`mock_async_failure_handler` turn out to be called, or `captured_coroutines` ends up empty/with more than one entry, that is a genuine, previously-unknown regression against the frozen "plan b" decision — fix the call site that introduced the unexpected behavior (most likely a wrapper between `anthropic_messages()` and `chunk_processor` added by an earlier task in this plan) rather than weakening this test.

- [ ] **Step 2: commit**
  ```
  git add tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_http_client_config_no_leak.py
  git commit -m "test: lock in direct-SDK anthropic_messages timeout logs partial spend only, never failure_handler, via deterministic logging-worker interception"
  ```

---

## Phase 5: Router Regression, Global Custom-Client Warning, HTTP/2 Schema-Only, Mutation Testing

Mirrors spec rollout step 5: prove the transitive-flow-through design decision, add the one remaining safety-net (warn when a caller supplies a custom httpx client that bypasses `http_client` config entirely), validate `http2` as schema-reserved-only, and close with the mutation-testing pass required by the project's testing bar.

### Task 25: Router transitive flow-through regression tests (no production code)

**Files:**
- Test: `tests/test_litellm/test_router.py` (extend existing file)

- [ ] **Step 1: write regression tests proving Router needs zero new code — `litellm_params.http_client` set on a `Deployment` reaches the resolved deployment's `acompletion`/`aresponses`/`anthropic_messages` call unchanged, because Router's existing `input_kwargs = {**litellm_params, ...}`-style spread already forwards it**
  ```python
  # append to tests/test_litellm/test_router.py
  from unittest.mock import AsyncMock, patch

  import pytest

  from litellm import Router


  @pytest.mark.asyncio
  async def test_router_acompletion_forwards_http_client_from_deployment_litellm_params():
      router = Router(
          model_list=[
              {
                  "model_name": "gh-copilot",
                  "litellm_params": {
                      "model": "github_copilot/gpt-4",
                      "http_client": {"connect_timeout": 3.0, "total_timeout": 30.0},
                  },
              }
          ]
      )
      captured_kwargs = []

      async def _fake_acompletion(*args, **kwargs):
          captured_kwargs.append(kwargs)
          return {"choices": [{"message": {"content": "ok"}}]}

      with patch("litellm.router.litellm.acompletion", side_effect=_fake_acompletion):
          await router.acompletion(model="gh-copilot", messages=[{"role": "user", "content": "hi"}])

      assert len(captured_kwargs) == 1
      assert captured_kwargs[0]["http_client"] == {"connect_timeout": 3.0, "total_timeout": 30.0}


  @pytest.mark.asyncio
  async def test_router_aresponses_forwards_http_client_from_deployment_litellm_params():
      router = Router(
          model_list=[
              {
                  "model_name": "gh-copilot",
                  "litellm_params": {
                      "model": "github_copilot/gpt-4",
                      "http_client": {"total_timeout": 45.0},
                  },
              }
          ]
      )
      captured_kwargs = []

      async def _fake_aresponses(*args, **kwargs):
          captured_kwargs.append(kwargs)
          return MagicMock()

      with patch("litellm.router.litellm.aresponses", side_effect=_fake_aresponses):
          await router.aresponses(model="gh-copilot", input="hi")

      assert len(captured_kwargs) == 1
      assert captured_kwargs[0]["http_client"] == {"total_timeout": 45.0}


  @pytest.mark.asyncio
  async def test_router_anthropic_messages_forwards_http_client_from_deployment_litellm_params():
      """review finding minor #2: Task 25's original draft only covered acompletion/aresponses;
      router.anthropic_messages() (bound to the same async factory_function wrapper as
      router.aanthropic_messages(), confirmed at router.py:1105-1106, both call_type="anthropic_messages")
      needs its own transitive-flow-through regression, since it is a third, independently
      reachable face this feature must cover."""
      router = Router(
          model_list=[
              {
                  "model_name": "claude-messages",
                  "litellm_params": {
                      "model": "anthropic/claude-3-haiku",
                      "http_client": {"connect_timeout": 5.0},
                  },
              }
          ]
      )
      captured_kwargs = []

      async def _fake_anthropic_messages(*args, **kwargs):
          captured_kwargs.append(kwargs)
          return MagicMock()

      with patch("litellm.router.litellm.anthropic_messages", side_effect=_fake_anthropic_messages):
          await router.anthropic_messages(
              model="claude-messages", messages=[{"role": "user", "content": "hi"}], max_tokens=100
          )

      assert len(captured_kwargs) == 1
      assert captured_kwargs[0]["http_client"] == {"connect_timeout": 5.0}
  ```
  *(Implementer note: confirm the exact patch target — `litellm.router.litellm.acompletion`/`litellm.router.litellm.anthropic_messages` vs. a locally-imported name inside `router.py` — via `grep -n "^from litellm import\|^import litellm" litellm/router.py` at implementation time, since patching the wrong reference silently no-ops the mock.)*
  Run: these should PASS already, proving the "no Router production code needed" design decision. If any fails, that is a genuine, previously-unknown gap in Router's kwarg forwarding — fix it in `router.py` at the specific point the test reveals is missing http_client, and note it explicitly in the phase-completion report as a divergence from the design assumption, rather than silently patching without flagging it.

- [ ] **Step 2: commit**
  ```
  git add tests/test_litellm/test_router.py
  git commit -m "test: lock in that Router forwards http_client config transitively with no new code"
  ```

### Task 26: warn when a caller-supplied custom client bypasses `http_client` config (review finding #9: the original draft of this task only covered one OpenAI chat code path; a shared helper plus four concrete choke points are needed for real coverage)

**Why a shared helper, and why these four choke points specifically:** `grep`-confirmed four independent places in this codebase where a caller can hand in an already-built transport object that then bypasses whatever `http_client` config this feature resolves, because the resolved config is only ever applied when *litellm itself* constructs the client:
1. Chat's OpenAI-SDK path — `OpenAIChatCompletion._get_openai_client()` (`litellm/llms/openai/openai.py:350-410`): `client: Optional[Union[OpenAI, AsyncOpenAI]] = None` — when non-`None`, the `else:` branch at line 404 only tunes `organization`/`max_retries` on the caller's own client, never touching timeouts.
2. Responses face — `BaseLLMHTTPHandler.async_response_api_handler()` (`litellm/llms/custom_httpx/llm_http_handler.py:2477-2508`): `client: Optional[Union[HTTPHandler, AsyncHTTPHandler]] = None` — the `else: async_httpx_client = client` branch at line 2507-2508 is structurally the same bypass, one layer lower (a `custom_httpx.AsyncHTTPHandler`, not an OpenAI SDK object).
3. Messages face — `BaseLLMHTTPHandler.async_anthropic_messages_handler()` (same file, lines 1930-1953): the identical `else: async_httpx_client = client` pattern at line 1952-1953.
4. The global `litellm.aclient_session` escape hatch — `BaseOpenAILLM._get_async_http_client()` (`litellm/llms/openai/common_utils.py:200-205`): when `litellm.aclient_session is not None`, it is returned unconditionally as the OpenAI SDK's `http_client=`, again bypassing any resolved `http_client` config — a *global*, not per-call, bypass mechanism, so it needs its own warning wired at its own call site rather than being folded into choke point 1.

Since all four choke points need the exact same warning semantics (a caller-owned transport's connect/read/pool timeouts win; `total_timeout`'s asyncio-level deadline still applies regardless), this task adds one shared helper in `litellm/litellm_core_utils/http_client_config.py` — the existing home for all http_client-config cross-cutting logic — rather than duplicating the warning text four times.

**Files:**
- Modify: `litellm/litellm_core_utils/http_client_config.py` (new `warn_if_custom_client_bypasses_http_client_config` function)
- Modify: `litellm/llms/openai/openai.py` (`_get_openai_client`, `acompletion`, `async_streaming`)
- Modify: `litellm/llms/custom_httpx/llm_http_handler.py` (`async_response_api_handler`, `async_anthropic_messages_handler`)
- Modify: `litellm/llms/openai/common_utils.py` (`BaseOpenAILLM._get_async_http_client`)
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py` (extend — shared helper), `tests/test_litellm/llms/openai/test_openai_http_client_deadline.py` (extend — choke points 1 and 4), `tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_http_client.py` (extend — choke point 2), `tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py` (extend — choke point 3)

- [ ] **Step 1: failing test — the shared helper warns exactly when both a custom client and http_client config are present, and stays silent for either alone**
  ```python
  # append to tests/test_litellm/litellm_core_utils/test_http_client_config.py
  import logging


  def test_warn_if_custom_client_bypasses_http_client_config_warns_when_both_present(caplog):
      from litellm.litellm_core_utils.http_client_config import (
          warn_if_custom_client_bypasses_http_client_config,
      )

      with caplog.at_level(logging.WARNING, logger="LiteLLM"):
          warn_if_custom_client_bypasses_http_client_config(
              has_custom_client=True, http_client_config_present=True, context="unit test context"
          )

      assert any(
          "http_client" in record.message and "bypass" in record.message.lower() and "unit test context" in record.message
          for record in caplog.records
      )


  def test_warn_if_custom_client_bypasses_http_client_config_silent_otherwise(caplog):
      from litellm.litellm_core_utils.http_client_config import (
          warn_if_custom_client_bypasses_http_client_config,
      )

      with caplog.at_level(logging.WARNING, logger="LiteLLM"):
          warn_if_custom_client_bypasses_http_client_config(
              has_custom_client=True, http_client_config_present=False, context="unit test context"
          )
          warn_if_custom_client_bypasses_http_client_config(
              has_custom_client=False, http_client_config_present=True, context="unit test context"
          )

      assert not any("bypass" in record.message.lower() for record in caplog.records)
  ```
  Run: confirm `ImportError`.

- [ ] **Step 2: implement the shared helper**
  ```python
  # litellm/litellm_core_utils/http_client_config.py, appended
  from litellm._logging import verbose_logger


  def warn_if_custom_client_bypasses_http_client_config(
      *, has_custom_client: bool, http_client_config_present: bool, context: str
  ) -> None:
      """Emit one consistently-worded warning whenever a caller supplies both (a) their own
      pre-built transport-level client (an OpenAI/AsyncOpenAI SDK client, a custom_httpx
      AsyncHTTPHandler, or the global litellm.aclient_session escape hatch) and (b) an
      http_client config -- the caller-supplied client's own transport already governs
      connect/read/pool timeouts, so only `total_timeout` (an asyncio-level deadline
      independent of which transport object is used) still applies. `context` names the call
      site so the log is actionable rather than generic."""
      if has_custom_client and http_client_config_present:
          verbose_logger.warning(
              f"A custom client was supplied for {context} together with `http_client` config; "
              "the caller-supplied client's own transport governs connect/read/pool timeouts, so "
              "those http_client fields will be ignored for this request. The `total_timeout` "
              "asyncio-level deadline still applies regardless of which client is used."
          )
  ```
  Run: Step 1 tests pass.
  Commit:
  ```
  git add litellm/litellm_core_utils/http_client_config.py tests/test_litellm/litellm_core_utils/test_http_client_config.py
  git commit -m "feat: add shared warn_if_custom_client_bypasses_http_client_config helper"
  ```

- [ ] **Step 3: failing test — choke point 1, chat's OpenAI-SDK client resolution**
  ```python
  # append to tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  import logging


  def test_get_openai_client_warns_when_custom_client_bypasses_http_client_config(caplog):
      from litellm.llms.openai.openai import OpenAIChatCompletion

      handler = OpenAIChatCompletion()
      fake_client = MagicMock()

      with caplog.at_level(logging.WARNING, logger="LiteLLM"):
          handler._get_openai_client(
              is_async=True,
              api_key="fake",
              api_base="https://example.com",
              timeout=600.0,
              client=fake_client,
              http_client_config_present=True,
          )

      assert any("http_client" in record.message and "bypass" in record.message.lower() for record in caplog.records)


  def test_get_openai_client_silent_when_no_http_client_config(caplog):
      from litellm.llms.openai.openai import OpenAIChatCompletion

      handler = OpenAIChatCompletion()
      fake_client = MagicMock()

      with caplog.at_level(logging.WARNING, logger="LiteLLM"):
          handler._get_openai_client(
              is_async=True,
              api_key="fake",
              api_base="https://example.com",
              timeout=600.0,
              client=fake_client,
              http_client_config_present=False,
          )

      assert not any("bypass" in record.message.lower() for record in caplog.records)
  ```
  Run: confirm `TypeError: unexpected keyword argument 'http_client_config_present'`.

- [ ] **Step 4: wire choke point 1 — add the parameter to `_get_openai_client` and thread it from `acompletion`/`async_streaming`**
  ```python
  # litellm/llms/openai/openai.py, inside def _get_openai_client(...), add the new parameter
  # (real signature confirmed at lines 350-361):
      def _get_openai_client(
          self,
          is_async: bool,
          api_key: Optional[str] = None,
          api_base: Optional[str] = None,
          api_version: Optional[str] = None,
          timeout: Union[float, httpx.Timeout] = httpx.Timeout(None),
          max_retries: Optional[int] = DEFAULT_MAX_RETRIES,
          organization: Optional[str] = None,
          client: Optional[Union[OpenAI, AsyncOpenAI]] = None,
          shared_session: Optional["ClientSession"] = None,
          http_client_config_present: bool = False,
      ) -> Optional[Union[OpenAI, AsyncOpenAI]]:
  ```
  ```python
  # same method, replacing the existing `else:` branch (confirmed real code at lines 404-409):
  #     else:
  #         self._set_dynamic_params_on_client(
  #             client=client, organization=organization, max_retries=max_retries,
  #         )
  # with:
      else:
          warn_if_custom_client_bypasses_http_client_config(
              has_custom_client=True,
              http_client_config_present=http_client_config_present,
              context="chat completions (OpenAI SDK client)",
          )
          self._set_dynamic_params_on_client(
              client=client,
              organization=organization,
              max_retries=max_retries,
          )
  ```
  ```python
  # litellm/llms/openai/openai.py, inside async def acompletion(...), immediately before the
  # existing `openai_aclient: AsyncOpenAI = self._get_openai_client(` call (confirmed real code
  # at lines 862-872), compute presence and thread it through:
      http_client_config_present = bool(litellm_params.get("http_client")) or getattr(
          litellm, "http_client", None
      ) is not None
      openai_aclient: AsyncOpenAI = self._get_openai_client(  # type: ignore
          is_async=True,
          api_key=api_key,
          api_base=api_base,
          api_version=api_version,
          timeout=timeout,
          max_retries=max_retries,
          organization=organization,
          client=client,
          shared_session=shared_session,
          http_client_config_present=http_client_config_present,
      )
  ```
  Apply the identical two-line addition (`http_client_config_present = ...` then thread the kwarg) at `async_streaming()`'s own `self._get_openai_client(` call site.
  Add the import near the top of `litellm/llms/openai/openai.py`:
  ```python
  from litellm.litellm_core_utils.http_client_config import (
      warn_if_custom_client_bypasses_http_client_config,
  )
  ```
  Run: Step 3 tests pass.
  Commit:
  ```
  git add litellm/llms/openai/openai.py tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  git commit -m "feat: warn when a caller-supplied OpenAI SDK client bypasses http_client config"
  ```

- [ ] **Step 5: failing test — choke point 2, the Responses face's `AsyncHTTPHandler` bypass**
  ```python
  # append to tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_http_client.py
  import logging
  from unittest.mock import MagicMock

  from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler


  @pytest.mark.asyncio
  async def test_async_response_api_handler_warns_when_custom_client_bypasses_http_client_config(caplog):
      from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

      handler = BaseLLMHTTPHandler()
      fake_client = MagicMock(spec=AsyncHTTPHandler)
      fake_client.post = AsyncMock(return_value=MagicMock(json=MagicMock(return_value={}), headers={}, status_code=200))

      with caplog.at_level(logging.WARNING, logger="LiteLLM"):
          try:
              await handler.async_response_api_handler(
                  model="github_copilot/gpt-4",
                  input="hi",
                  responses_api_provider_config=MagicMock(
                      validate_environment=MagicMock(return_value={}),
                  ),
                  response_api_optional_request_params={},
                  custom_llm_provider="github_copilot",
                  litellm_params=GenericLiteLLMParams(http_client={"connect_timeout": 2.0}),
                  logging_obj=MagicMock(http_client_deadline=None),
                  client=fake_client,
              )
          except Exception:
              pass  # response-shape handling beyond client selection is out of scope here

      assert any("http_client" in record.message and "bypass" in record.message.lower() for record in caplog.records)
  ```
  Run: confirm no warning is emitted (assertion fails).

- [ ] **Step 6: wire choke point 2**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, inside async_response_api_handler,
  # replacing the existing else branch (confirmed real code at lines 2507-2508):
  #     else:
  #         async_httpx_client = client
  # with:
      else:
          warn_if_custom_client_bypasses_http_client_config(
              has_custom_client=True,
              http_client_config_present=bool(litellm_params.get("http_client"))
              or getattr(litellm, "http_client", None) is not None,
              context="responses API (AsyncHTTPHandler)",
          )
          async_httpx_client = client
  ```
  Add the import near the top of `litellm/llms/custom_httpx/llm_http_handler.py` (if not already present from Task 18a):
  ```python
  from litellm.litellm_core_utils.http_client_config import (
      warn_if_custom_client_bypasses_http_client_config,
  )
  ```
  Run: Step 5 test passes.
  Commit:
  ```
  git add litellm/llms/custom_httpx/llm_http_handler.py tests/test_litellm/llms/custom_httpx/test_llm_http_handler_responses_http_client.py
  git commit -m "feat: warn when a caller-supplied AsyncHTTPHandler bypasses http_client config in responses API"
  ```

- [ ] **Step 7: failing test — choke point 3, the Messages face's `AsyncHTTPHandler` bypass**
  ```python
  # append to tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  import logging


  @pytest.mark.asyncio
  async def test_async_anthropic_messages_handler_warns_when_custom_client_bypasses_http_client_config(caplog):
      from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
      from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
      from litellm.types.router import GenericLiteLLMParams

      handler = BaseLLMHTTPHandler()
      fake_client = MagicMock(spec=AsyncHTTPHandler)

      with caplog.at_level(logging.WARNING, logger="LiteLLM"):
          try:
              await handler.async_anthropic_messages_handler(
                  model="claude-3-haiku",
                  messages=[{"role": "user", "content": "hi"}],
                  anthropic_messages_provider_config=MagicMock(),
                  anthropic_messages_optional_request_params={},
                  custom_llm_provider="anthropic",
                  litellm_params=GenericLiteLLMParams(http_client={"connect_timeout": 2.0}),
                  logging_obj=MagicMock(http_client_deadline=None),
                  client=fake_client,
              )
          except Exception:
              pass  # request construction beyond client selection is out of scope here

      assert any("http_client" in record.message and "bypass" in record.message.lower() for record in caplog.records)
  ```
  Run: confirm no warning is emitted.

- [ ] **Step 8: wire choke point 3**
  ```python
  # litellm/llms/custom_httpx/llm_http_handler.py, inside async_anthropic_messages_handler,
  # replacing the existing else branch (confirmed real code at lines 1952-1953):
  #     else:
  #         async_httpx_client = client
  # with:
      else:
          warn_if_custom_client_bypasses_http_client_config(
              has_custom_client=True,
              http_client_config_present=bool(litellm_params.http_client)
              or getattr(litellm, "http_client", None) is not None,
              context="anthropic messages (AsyncHTTPHandler)",
          )
          async_httpx_client = client
  ```
  Run: Step 7 test passes.
  Commit:
  ```
  git add litellm/llms/custom_httpx/llm_http_handler.py tests/test_litellm/llms/anthropic/experimental_pass_through/messages/test_anthropic_experimental_pass_through_messages_handler.py
  git commit -m "feat: warn when a caller-supplied AsyncHTTPHandler bypasses http_client config in anthropic messages"
  ```

- [ ] **Step 9: failing test — choke point 4, the global `litellm.aclient_session` escape hatch**
  ```python
  # append to tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  def test_get_async_http_client_warns_when_aclient_session_bypasses_http_client_config(caplog):
      import litellm
      from litellm.llms.openai.openai import OpenAIChatCompletion

      original_aclient_session = litellm.aclient_session
      litellm.aclient_session = MagicMock()
      try:
          with caplog.at_level(logging.WARNING, logger="LiteLLM"):
              OpenAIChatCompletion._get_async_http_client(http_client_config_present=True)
      finally:
          litellm.aclient_session = original_aclient_session

      assert any("http_client" in record.message and "bypass" in record.message.lower() for record in caplog.records)
  ```
  Run: confirm `TypeError: unexpected keyword argument 'http_client_config_present'`.

- [ ] **Step 10: wire choke point 4 and thread it from the one already-covered call site**
  ```python
  # litellm/llms/openai/common_utils.py, inside BaseOpenAILLM._get_async_http_client (confirmed
  # real code at lines 200-205), add the new parameter and the warning:
      @staticmethod
      def _get_async_http_client(
          shared_session: Optional["ClientSession"] = None,
          http_client_config_present: bool = False,
      ) -> Optional[httpx.AsyncClient]:
          if litellm.aclient_session is not None:
              warn_if_custom_client_bypasses_http_client_config(
                  has_custom_client=True,
                  http_client_config_present=http_client_config_present,
                  context="the global litellm.aclient_session override",
              )
              return litellm.aclient_session
  ```
  ```python
  # litellm/llms/openai/openai.py, inside def _get_openai_client(...)'s async branch (confirmed
  # real code at line 381), thread the same http_client_config_present computed in Step 4:
      _new_client: Union[OpenAI, AsyncOpenAI] = AsyncOpenAI(
          api_key=api_key,
          base_url=api_base,
          http_client=OpenAIChatCompletion._get_async_http_client(
              shared_session=shared_session,
              http_client_config_present=http_client_config_present,
          ),
          timeout=timeout,
          max_retries=max_retries,
          organization=organization,
      )
  ```
  Add the import near the top of `litellm/llms/openai/common_utils.py`:
  ```python
  from litellm.litellm_core_utils.http_client_config import (
      warn_if_custom_client_bypasses_http_client_config,
  )
  ```
  Run: Step 9 test passes.
  Commit:
  ```
  git add litellm/llms/openai/common_utils.py litellm/llms/openai/openai.py tests/test_litellm/llms/openai/test_openai_http_client_deadline.py
  git commit -m "feat: warn when the global litellm.aclient_session override bypasses http_client config"
  ```

**Deliberately not covered by this task (flagged, not silently dropped):** `BaseOpenAILLM._get_async_http_client()` and `_get_sync_http_client()` are also called from `litellm/llms/openai/completion/handler.py:183`, `litellm/llms/azure/common_utils.py:593`, and other non-chat-completions call sites this plan's Phases 1-4 never touch (legacy `/v1/completions`, Azure's own client construction path, sync completions generally). Extending the `http_client_config_present` thread to every one of those call sites would mean modifying provider integrations this spec's frozen scope (chat/responses/messages faces) does not otherwise touch. Recorded here per `no-silently-cut-but-defer` rather than silently dropped; if the user/architect judges these in-scope, they need their own follow-on task since they touch materially different code (Azure's client bootstrap, the legacy completions handler), not a mechanical repeat of Steps 9-10.

### Task 27: `http2` schema-only validation (explicitly not wired)

**Files:**
- Test: `tests/test_litellm/litellm_core_utils/test_http_client_config.py` (extend)

- [ ] **Step 1: write a test proving `http2` parses/validates but is never read by any resolution function (a documentation-as-test safeguard against silent future wiring without an explicit spec update)**
  ```python
  def test_http2_field_parses_but_is_not_consumed_by_resolve_http_client_timeout():
      from litellm.litellm_core_utils.http_client_config import (
          HttpClientConfig,
          resolve_http_client_timeout,
      )

      cfg = HttpClientConfig(http2=True, connect_timeout=1.0)
      resolved = resolve_http_client_timeout(cfg, legacy_effective_timeout=600.0)
      # httpx.Timeout has no http2 concept at all -- this assertion simply documents that
      # resolve_http_client_timeout's return type structurally cannot carry http2 forward,
      # so any future accidental wiring would have to be an explicit, visible type change
      assert not hasattr(resolved, "http2")
      assert not hasattr(resolved.httpx_timeout, "http2")
  ```
  Run: this should already pass (Task 1 already defined `http2` as schema-only); this task exists purely to make the "reserved, not wired" contract test-visible per the spec's explicit non-goal, not to add new production code.

- [ ] **Step 2: commit**
  ```
  git add tests/test_litellm/litellm_core_utils/test_http_client_config.py
  git commit -m "test: document http2 as schema-reserved-only, not wired to any transport"
  ```

### Task 28: mutation-testing pass on the two new core modules

**Files:**
- No production/test file changes expected unless mutants survive; if any survive, extend the relevant existing test file from Phases 1–2 to kill them.

- [ ] **Step 1: run mutation testing against the two new pure-logic modules**
  ```bash
  pip show mutmut >/dev/null 2>&1 || pip install mutmut
  mutmut run --paths-to-mutate litellm/litellm_core_utils/http_client_config.py,litellm/litellm_core_utils/asyncio_deadline.py --tests-dir tests/test_litellm/litellm_core_utils/
  mutmut results
  ```
- [ ] **Step 2: for any surviving mutant, add the minimal test that kills it (e.g., a boundary-flip test on `remaining <= 0` vs. `remaining < 0`, or on `model_fields_set` vs. truthiness in `merge_http_client_config`), following the same red-green-commit cycle as every prior task**
- [ ] **Step 3: confirm kill rate exceeds 90% on both modules; record the final rate in the commit message**
  ```
  git add tests/test_litellm/litellm_core_utils/
  git commit -m "test: close mutation-testing gaps in http_client_config and asyncio_deadline (kill rate >90%)"
  ```

---

## Self-Check (performed by the plan author before delivery, per `writing-plans`' Self-Review section)

1. **Spec coverage**: every clause from the frozen spec's 组件设计/测试/落地顺序 sections maps to at least one task above — schema+parse+merge+resolve (Tasks 1-4), `github_copilot` allowlist opt-in + safety net (Task 4a), `GenericLiteLLMParams`/`LiteLLMParamsTypedDict` fields (Task 5), leak prevention (Tasks 6, 17, 21a, 24), proxy validation (Task 7), legacy-timeout/`http_client` coexistence warning at both the global and per-deployment boundaries (Task 7a), unified deadline helper + iterator including `aclose()` delegation and its own self-shielding (Tasks 8, 8a, 9), deadline carrier + missing-`logging_obj` safety net (Tasks 10, 11), chat non-streaming + two-phase streaming + cross-deployment fallback on mid-stream timeout + client-disconnect close regression (Tasks 12-16, 16a), responses native + per-axis timeout merge + wire-safety + bridge + cross-deployment fallback on mid-stream timeout (Tasks 18, 18a, 19-21, 20a, 21a), messages including both known bugs, the global/deployment merge fix, and the streaming-iterator deadline-passthrough fix (Tasks 22-23), messages direct-SDK failure-hook "plan b" regression (Task 24a), Router transitive flow-through proof across `acompletion`/`aresponses`/`anthropic_messages` (Task 25), custom-client bypass warning across all 4 named choke points (Task 26), `http2` reserved-only (Task 27), mutation testing (Task 28). 36 total task units (28 numbered + 8 lettered: 4a, 7a, 8a, 16a, 18a, 20a, 21a, 24a — `11a` was deleted in the 3rd review round, review finding G, and is not counted), each added in response to a specific, named review finding or frozen spec decision — none were added speculatively.
2. **Placeholder scan**: every step above shows complete, real code; no "TBD," no "similar to Task N" (each face's wrap code is written out in full even where structurally repetitive across chat/responses/messages, since the three faces' surrounding signatures genuinely differ).
3. **Type/function-name consistency**: `HttpClientConfig`, `HttpClientConfigDict`, `parse_http_client_config`, `merge_http_client_config`, `ResolvedHttpClientTimeout`, `resolve_http_client_timeout`, `establish_request_deadline`, `warn_if_custom_client_bypasses_http_client_config`, `warn_if_legacy_timeout_coexists_with_http_client`, `DeadlineExceeded`, `with_deadline`, `DeadlineBoundAsyncIterator`, `Logging.http_client_deadline`, `Logging.set_http_client_deadline` are each defined exactly once (in the task noted in the Component Naming Reference table above) and referenced identically by name in every later task that uses them. (`set_or_warn_http_client_deadline` was removed in the 3rd review round — review finding G — see the Revision Record.)
4. **Adversarial review disposition**: all 11 major issues, all 3 minor issues, and the 1 adopted suggestion from the 2nd-round GPT reviewer, PLUS all 7 major findings (A-G) and all 3 minor findings from the 3rd-round adversarial review, are resolved in this document (see the Revision Record sections below for the exact task-by-task mapping); none were silently dropped or downgraded to "optional."

## Open Items for Implementer (flagged, not silently resolved)

- **`grep`-verify line numbers before editing**: every line number cited above was re-verified fresh during plan-writing (2026-07-14), but code drifts; if a cited line doesn't match the quoted existing code, grep for the quoted snippet first rather than assuming the plan's line number is exact.
- **`constants.py`'s second `"github_copilot"` occurrence** (confirmed at line 751, inside what appears to be a second provider-name list, likely `provider_list` or similar): confirmed benign — a duplicate entry across two separate provider-name lists, not a bug; no task above touches it, and none should.
- **Confirmed via direct file reads during this planning session** (safe to treat as accurate, not placeholders): `ProxyConfig.load_config()`'s real structure at `proxy_server.py:3993-4044` (Task 7 — there is no separately-named helper method; the `litellm_settings.items()` loop is inline in `load_config()` itself, and Task 7's test now goes through `load_config()` directly with a temp YAML file, mirroring the existing `test_load_config_max_budget_env_var_coerced_to_float` pattern); `_get_openai_client`'s real signature and `else:` branch at `openai.py:350-410` (Task 26 choke point 1); `async_response_api_handler`'s and `async_anthropic_messages_handler`'s real `else: async_httpx_client = client` branches at `llm_http_handler.py:2507-2508` and `:1952-1953` respectively (Task 26 choke points 2 and 3); `BaseOpenAILLM._get_async_http_client`'s real `litellm.aclient_session` branch at `common_utils.py:200-205` (Task 26 choke point 4); `PassThroughStreamingHandler.chunk_processor`'s real body and its `finally:`-block partial-spend-only behavior at `streaming_handler.py:29-112` (Task 24a); `Router.anthropic_messages`/`Router.aanthropic_messages`'s shared binding to one `factory_function(litellm.anthropic_messages, call_type="anthropic_messages")` wrapper at `router.py:1105-1106`, routed through `_ageneric_api_call_with_fallbacks` (Task 25's third test); `Router._create_deployment`'s real `litellm_params: LiteLLM_Params = LiteLLM_Params(**_litellm_params)` construction point at `router.py:7318`, and `Router._aresponses_streaming_iterator`'s `except MidStreamFallbackError as e:`/`async_function_with_fallbacks_common_utils(...)` calls at `router.py:2374`/`2411-2421` (both confirmed during the 3rd review round, Tasks 7a and 20a).
- **Still not independently re-verified line-by-line in this exact session** (reconstructed from earlier investigation notes carried over from prior planning turns; re-confirm via `grep` before finalizing that task's test, per each task's own implementer note): `async_response_api_handler`'s exact parameter list and both `post()` call sites at `llm_http_handler.py:2585-2591`/`2617-2622` (Task 19); the responses-completion-bridge's exact entry-point/module structure (Task 21); `proxy_server.py:799-808` (Task 7's own `elif key == "http_client":` branch, whose exact lines will have shifted once Tasks 2-6 have actually landed by the time Task 7a is implemented — re-grep before editing); `ResponsesAPIStreamingIterator`'s exact line numbers once Task 20 has landed (Task 20a depends on them, and Task 20's own edits will shift them).
- **`common_request_processing.py:2551-2577`** (proxy pass-through's own failure-hook, distinct from Task 24a's direct-SDK "plan b" regression): cited by the spec as the owner of the *full* failure hook for proxy pass-through callers. Task 24a locks in that direct-SDK callers get partial-spend-only logging and zero `failure_handler` triggers (frozen spec decision, "方案 b"); it does not exercise this proxy-side code path at all, since direct-SDK callers never reach it. If the implementer discovers during Task 22/23/24a that a proxy pass-through streaming failure path double-logs or under-logs once `DeadlineExceeded` starts firing there, treat that as a genuine gating question for the main session rather than silently choosing a resolution — it touches spend-logging correctness, not just this feature's scope.
- **Task 7a's per-deployment `timeout` narrowing** (3rd review round, item F): the production snippet only treats a plain `int`/`float` legacy `timeout` as eligible for the coexistence warning, deliberately not warning when `timeout` is an `os.environ/`-prefixed string or an already-constructed `httpx.Timeout`. This narrowing has not been checked against exactly when/where env-var substitution happens relative to `_create_deployment`'s own execution — if substitution happens earlier in the call chain (so `_litellm_params["timeout"]` already arrives as a resolved float), the narrowing is unnecessary-but-harmless; if it happens later or not at all before this point, some legitimate coexistence cases could go unwarned. Flagged for implementer verification, not silently assumed correct.

## Alternatives Considered (not adopted)

- **`async-timeout` third-party package** for the deadline helper: rejected. `asyncio.wait_for` already provides the exact same guarantee (cancel the awaitable, raise on timeout) with zero new dependencies, and the project already leans on `anyio` (present in `streaming_handler.py`) rather than adding another timeout-specific library for one function.
- **Version-branched `asyncio.timeout_at()` (3.11+) vs. `asyncio.wait_for()` (3.10)**: rejected in favor of a single `wait_for`-based implementation for all supported versions (3.10–3.13). Two tested code paths for identical externally-observed behavior would double the test surface (Task 8/9) for no behavioral gain, and would reintroduce exactly the kind of version-conditional complexity the spec's "Python 3.10 compatibility" constraint is meant to avoid.
- **Router-side deadline computation** (an earlier working assumption during investigation, before `acompletion()`/`aresponses()`/`anthropic_messages()`'s shared `run_in_executor` + `await init_response` dispatch idiom was read in full): considered and abandoned once it became clear Router's existing `litellm_params` spread already reaches each face's own entry point, where the deadline is established once, uniformly, regardless of caller. Adding Router-side computation would have meant either double-computing the deadline (Router computes one value, the face recomputes another, with no clear authority over which wins) or threading a new explicit parameter through Router's own call surface for no behavioral benefit. Task 25 exists specifically to lock in, via regression test, that this simpler design is correct rather than merely assumed.
- **Contextvar-based deadline propagation** (spec-excluded up front, reconfirmed here): rejected per explicit spec constraint ("must not touch aiohttp transport or use contextvar"); the `Logging` object carrier (Task 11) achieves the same "ambient availability at depth" property through an object that is already explicitly threaded as a parameter everywhere it's needed, which is more debuggable (visible in signatures/call sites) and testable (no implicit context-copying edge cases across `run_in_executor`'s thread boundary) than a contextvar would be.
- **New parameters threaded through `openai.py`/`llm_http_handler.py`'s existing large method signatures** (e.g., adding `http_client_deadline: Optional[float]` directly to `acompletion()`/`async_streaming()`/`_async_post_anthropic_messages_with_http_error_retry()`): rejected in favor of reading the deadline off the already-passed `logging_obj` (Task 11), since these signatures are already long and adding a parameter to each would need to be threaded through every one of their own callers in turn (Router, proxy handlers, direct SDK users) — the `logging_obj` carrier reaches the same code with zero signature growth beyond the one new field.
- **Wrapping `CustomStreamWrapper.fetch_stream()`** for chat's streaming phase① (an earlier working assumption, corrected mid-investigation): abandoned once reading `streaming_handler.py:1862-1875` confirmed `fetch_stream()`'s lazy `self.make_call` branch is exercised only by non-openai providers (bedrock/boto3-style) whose stream isn't populated until first iteration; for openai/GHC, `completion_stream` is already populated eagerly by `openai.py`'s own call before `CustomStreamWrapper.__init__` even runs, so wrapping at `__init__`'s assignment point (Task 16) is both correct and leaves `__anext__` completely untouched, whereas wrapping `fetch_stream()` would have added dead code for this provider path.

---

## Kick-off Prompt

Copy the block below verbatim to start a fresh session (or hand off to a subagent) to execute this plan.

```
Read docs/superpowers/plans/2026-07-14-upstream-http-client-config.md in full before doing anything else.

Context: this is a private litellm fork (branch `ghc`, no upstream PRs). The plan you just read implements
fine-grained upstream HTTP client config (connect/read/pool/total timeouts, http2 reserved-only) for the
github_copilot provider, uniformly across the chat/responses/messages API surfaces, per the frozen spec at
docs/superpowers/specs/2026-07-13-upstream-http-client-config-design.md.

Use the superpowers:subagent-driven-development skill (preferred) or superpowers:executing-plans to execute
this plan phase by phase, task by task, step by step. Do not skip the red-green-commit cycle for any step,
and do not batch multiple tasks into one commit.

Before editing any file the plan cites a line number for, grep for the exact quoted existing code first --
line numbers drift. Tasks 19 and 21 explicitly flag an implementer-note needing a fresh `grep`/read to
confirm an exact signature or entry-point structure before finalizing that task's test; Tasks 7a and 20a
similarly flag line numbers that will have shifted once the tasks they depend on (7, and 20 respectively)
have actually landed (everything else the plan cites a line number for, including Tasks 7 and 26, was
confirmed via direct file reads during plan-writing on 2026-07-14 -- see "Open Items for Implementer" for
the exact list of which is which); do this verification as the first step of that task, not as an
afterthought.

Follow every Global Constraint in the plan (Python 3.10 compatibility, 120-char lines, LIT001/LIT002
immutability, no bare `Any`/coarse `dict`, frozen dataclasses/Pydantic models, dependency injection with no
monkeypatching, Conventional Commits, `make pre-commit` before every commit that touches litellm/).

Run `make pre-commit` before each commit that touches litellm/ (stage exactly what you intend to commit
first). Fix every violation it reports; do not use `# mutable-ok` unless truly unavoidable, and always pair
it with a real reason if you do.

If you discover a genuine fork not covered by this plan's "Open Items for Implementer" section (a spec
clause that doesn't map to any task, a contradiction between two tasks, or a line-number drift bad enough
that the surrounding code no longer matches the plan's described shape), stop and flag it rather than
silently improvising a resolution -- this is a private fork prioritizing long-term correctness, not
ship-it-now speed.

When all 36 task units (28 numbered tasks plus 8 lettered insertions: 4a, 7a, 8a, 16a, 18a, 20a, 21a, 24a --
`11a` was deleted in the 3rd review round, review finding G, and no longer exists) across all 5 phases are
complete and committed, run the full test suite for every file touched, run mutmut per Task 28, and report:
final mutation kill rate on the two new core modules, and any divergence you found between this plan and
the actual code (in addition to what's already listed in "Open Items for Implementer").
```

---

## Revision Record — GPT Adversarial Review Round (11 major, 3 minor, 1 adopted suggestion)

This plan was adversarially reviewed once by a GPT reviewer, which surfaced 11 major issues, 3 minor issues, and 1 adopted suggestion. Every item below was addressed in this document; none were silently dropped, downgraded, or deferred without an explicit note. This section is the honest, task-by-task account of exactly what changed, required by the coordinator's original delivery instruction.

**Tasks added (6 new lettered task units, none of which existed before this review round):**

| Task | Addresses | One-line summary |
|---|---|---|
| Task 4a | Major #3 | `github_copilot` opt-in to `supports_httpx_timeout`, plus a chat-face safety net so a resolved `httpx.Timeout` is never silently degraded for a provider absent from that allowlist. |
| Task 8a | Major #5 | `DeadlineExceeded` -> `litellm.Timeout` mapping at 5 coordinated choke points across `exception_mapping_utils.py`, `openai.py` (2 sites), `llm_http_handler.py`, and messages `handler.py`; plus a Router retry-classification regression test. |
| Task 11a | Adopted suggestion | `set_or_warn_http_client_deadline` shared helper — warns (does not silently drop) when a resolved deadline has no `logging_obj` to attach to, e.g. direct/internal calls that bypass `litellm.utils.client`'s setup. Wired into Tasks 12/18/22. |
| Task 18a | Major #4 | Responses face's missing global/deployment `http_client` merge + per-axis `httpx.Timeout` resolution, wired into both `post()` call sites in `async_response_api_handler`. |
| Task 21a | Major #4 (wire-safety half) | Responses face wire-body no-leak regression test, mirroring Task 17's chat-face pattern. |
| Task 24a | Major #10 | Messages direct-SDK failure-hook "plan b" regression: pins that a `total_timeout` deadline firing mid-stream on a direct `litellm.anthropic_messages()` call logs partial spend exactly once and never calls `failure_handler`/`async_failure_handler`, matching the frozen spec decision. No new production code — `chunk_processor`'s existing `finally:`-only-logs-partial-spend behavior already satisfies this; the task exists to lock it in as a regression. |

**Existing tasks materially rewritten (production-code or test-content changes, not just prose polish):**

- **Task 9** (`DeadlineBoundAsyncIterator`) — Major #6: added an `aclose()` method that delegates to the wrapped inner iterator's own `aclose()`/`close()`, plus two new tests.
- **Task 16** (`CustomStreamWrapper.__init__` deadline wrap) — Major #6: fixed `on_timeout_close` to close the wrapped iterator itself via a forward-reference closure (`lambda: wrapped.aclose()`) instead of the wrong, indirect `self.aclose` (which by the time `__init__` returns no longer points at the raw stream). Added a regression test asserting the raw stream's `aclose()` is actually awaited on timeout.
- **Task 22** (messages face establish-deadline) — Major #8: Step 4 rewritten to merge global `litellm.http_client` + deployment `litellm_params.http_client` via `merge_http_client_config`/`parse_http_client_config` before `resolve_http_client_timeout`, mirroring Task 18a's pattern; added a new regression test for the merge. Step 2 also rewritten (independently, for the adopted suggestion) to call `set_or_warn_http_client_deadline` instead of a bare `if litellm_logging_obj is not None:` guard.
- **Task 23** (messages face cache-key + streaming deadline) — Major #7: added Steps 5-6, a failing test plus the one-line production fix passing `_http_client_deadline=getattr(litellm_logging_obj, "http_client_deadline", None)` at `streaming_iterator.py`'s `get_async_streaming_response_iterator` call site, which previously never forwarded the deadline to `chunk_processor` despite Steps 3-4 already having added the parameter to `chunk_processor` itself.
- **Task 12** (chat `acompletion()` establish-deadline) — Adopted suggestion: Step 2 rewritten to call `set_or_warn_http_client_deadline` instead of a bare `if litellm_logging_obj is not None:` guard.
- **Task 18** (responses `aresponses()` establish-deadline) — Adopted suggestion: same rewrite as Task 12, for the responses face.
- **Task 25** (Router transitive flow-through) — Minor #2: added a third regression test, `test_router_anthropic_messages_forwards_http_client_from_deployment_litellm_params`, since the original draft only covered `acompletion`/`aresponses` and omitted the third face Router also exposes.
- **Task 26** (custom-client-bypass warning) — Major #9: fully replaced. The original draft assumed a fictitious `_get_openai_client(http_client_config_present=...)` signature and covered only one OpenAI chat code path. The rewrite introduces a shared `warn_if_custom_client_bypasses_http_client_config` helper and wires it into all 4 real bypass choke points: the OpenAI chat SDK client (`openai.py::_get_openai_client`), the Responses face's `AsyncHTTPHandler` (`llm_http_handler.py::async_response_api_handler`), the Messages face's `AsyncHTTPHandler` (`llm_http_handler.py::async_anthropic_messages_handler`), and the global `litellm.aclient_session` override (`common_utils.py::BaseOpenAILLM._get_async_http_client`). Explicitly scopes out (flagged, not silently dropped) the same helper's non-chat-completions call sites (`completion/handler.py`, `azure/common_utils.py`) as a follow-on task if the user/architect judges them in-scope.
- **Task 7** (proxy global `http_client` validation) — Minor #1a: Step 1's test rewritten from a fictitious `ProxyConfig._update_general_settings_or_litellm_settings()` call to the real `ProxyConfig.load_config()` entry point (confirmed at `proxy_server.py:3993-4044`), using a temp-YAML-file pattern mirrored from the file's own existing `test_load_config_max_budget_env_var_coerced_to_float` test.

**Minor #3** ("no monkeypatch" vs. heavy test-patching contradiction) was resolved in an earlier session (not this one) by clarifying the Global Constraints section's scope: the "no monkeypatching" rule governs production code only; `unittest.mock.patch` in test files remains a standard, accepted isolation technique, used specifically at seams where adding a new constructor/call parameter purely for testability would itself violate the "no new parameters threaded through existing large signatures" design decision.

**Total task-count change:** 0 tasks removed; 6 lettered task units added (4a, 8a, 11a, 18a, 21a, 24a); 9 existing task units materially rewritten (9, 12, 16, 18, 22, 23, 25, 26, 7) as listed above. Final count: 34 task units across 5 phases (up from 28 before this review round).

**Signatures/line numbers still requiring implementer grep-confirmation** (honestly flagged as unconfirmed by this plan's author, not presented as certain — see "Open Items for Implementer" above for the full, categorized list): `async_response_api_handler`'s exact parameter list and both `post()` call sites at `llm_http_handler.py:2585-2591`/`2617-2622` (Task 19); the responses-completion-bridge's exact entry-point/module structure (Task 21). Every other line number newly introduced or touched by this review round's changes (Tasks 4a, 7, 8a, 9, 11a, 16, 18a, 21a, 22, 23, 24a, 25, 26) was confirmed via direct file reads during plan-writing across this and prior planning sessions, and is cited above with its exact confirmed location.

---

## Revision Record — 3rd Adversarial Review Round (7 major findings A-G, 3 minor items)

A third adversarial review round, on top of the already-fully-addressed 2nd round above, surfaced 7 lettered major findings (A-G) and 3 minor items. Every item below was addressed in this document across this and prior sessions within the same round; none were silently dropped, downgraded, or deferred without an explicit note.

**Findings and their disposition:**

| Finding | Addresses | Disposition |
|---|---|---|
| A | `resolve_http_client_timeout`: `cfg=None` must be pure passthrough; `cfg` non-`None` must merge per-axis using `is not None` checks, never `or` | Completed in a prior session. No task renumbering; Task 4's implementation and tests already reflect this. |
| B | Responses face (`async_response_api_handler`) must consume `http_client` before provider mapping — strip it from the provider-visible parameter set before `get_complete_url`/`transform_responses_api_request`/`sign_request`/`validate_environment`; tests must inject provider config/client directly, not depend on real Copilot OAuth | **Task 18a fully rewritten this session.** Adds `provider_facing_litellm_params = litellm_params.model_copy(update={"http_client": None})`, used for all 4 provider-facing calls (the review's literal wording named 3; `validate_environment` was added too since it shares the same root cause). Tests rewritten to use the "gold-standard direct-injection pattern" (`BaseLLMHTTPHandler()` + `Mock()` provider config + `AsyncHTTPHandler()` with mocked `.post`), eliminating the `github_copilot` real-OAuth dependency entirely in favor of `openai`/`gpt-4o-mini` with an explicit fake `api_key`. |
| C | `DeadlineExceeded` streaming phase② mapping gaps: (a) chat phase②'s `_handle_stream_fallback_error` needs a real test asserting `MidStreamFallbackError.original_exception` is `litellm.Timeout`; (b) native Responses phase② (`ResponsesAPIStreamingIterator.__anext__`) must map `DeadlineExceeded` to `MidStreamFallbackError` instead of leaking it raw | **Chat half: Task 16a (new), completed in a prior session** — discovered, while writing the requested regression test, that Task 8a's `DeadlineExceeded` -> `litellm.Timeout` mapping defaults `status_code` to 408, which `_handle_stream_fallback_error`'s existing skip-fallback carve-out (only 429) did not exempt; fixed by widening the carve-out to `_STATUS_CODES_ELIGIBLE_FOR_MIDSTREAM_FALLBACK = frozenset({408, 429})`. **Responses half: Task 20a (new), completed this session** — adds `ResponsesAPIStreamingIterator._any_chunk_yielded` tracking and a new `except DeadlineExceeded as e:` clause that maps via `exception_type()` (already produces `litellm.Timeout` for free, per Task 8a) then raises `MidStreamFallbackError`; ships both a direct-iterator test (rewrite of Task 20's own test) and a new Router-wrapped test driving a *real* `ResponsesAPIStreamingIterator` through `Router._aresponses_streaming_iterator`'s actual `except MidStreamFallbackError` branch (`router.py:2374`), confirmed this session by reading `router.py:2241-2459` in full — unlike the chat face, the Responses face has no status-code skip-fallback filter at all, so no 408-collision fix was needed on this half. |
| D | Task 24a must not use `sleep(0)` (a race against `GLOBAL_LOGGING_WORKER`'s background thread); must directly inject/intercept `GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue` so the test can explicitly `await` the captured coroutine, then assert `failure_handler` called 0 times and partial-spend logging called 1 time; must inject an explicit fake provider API key, not depend on a real Anthropic key | **Task 24a fully rewritten this session.** Patches `GLOBAL_LOGGING_WORKER.ensure_initialized_and_enqueue` with a `side_effect` closure that appends the coroutine to a list instead of letting it reach the background worker thread, then explicitly `await`s the captured coroutine on the test's own event loop — deterministic, no race. Adds `mock_partial_spend_logger.assert_awaited_once()`, `mock_sync_failure_handler.assert_not_called()`, and `mock_async_failure_handler.assert_not_awaited()` (the original draft never asserted the partial-spend-logger call count at all). Adds `api_key="fake-key"` to remove the ambient `ANTHROPIC_API_KEY` dependency. |
| E | Merge's override should only take a deployment field when it is both explicitly set AND non-`None` (explicit `null` must fall back to global, not clear it) | Completed in a prior session. `merge_http_client_config` (Task 3) already filters `deployment_cfg.model_dump(include=deployment_cfg.model_fields_set)` down to non-`None` values before overlaying on `base_values`; the regression test `test_merge_http_client_config_explicit_null_in_deployment_falls_back_to_global_not_cleared` locks this in. |
| F | When the same deployment has BOTH the old `timeout` field AND `http_client` configured, `http_client` must win (already true, see A/E), and a warning must be logged **at load time**, at both the global (`litellm_settings`) and per-deployment (`litellm_params`) validation boundaries | **New Task 7a, designed and written this session.** One new pure function, `warn_if_legacy_timeout_coexists_with_http_client(*, legacy_timeout, http_client, context)`, in the shared `http_client_config.py` module (avoids duplicating the "both non-None -> warn" logic across two boundaries). Wired into two call sites: (1) the global boundary — extends Task 7's existing `elif key == "http_client":` branch in `proxy_server.py`, reading `litellm_settings.get("request_timeout")` from the same dict already in scope in `ProxyConfig.load_config()`'s loop (confirmed at `proxy_server.py:4044`); (2) the per-deployment boundary — `Router._create_deployment`, immediately after `litellm_params: LiteLLM_Params = LiteLLM_Params(**_litellm_params)` (confirmed at `router.py:7318`, called once per `model_list` entry from `set_model_list()`), matching the spec's explicit "model-list/Deployment 构造边界" requirement (spec lines 117-122). Four new tests: 2 pure-function unit tests (warn / silent) plus one integration test per boundary (global via `ProxyConfig.load_config()` with a temp YAML; per-deployment via `Router._create_deployment` directly, extending `test_router_helper_utils.py`'s existing `test_create_deployment` neighborhood). Flags one open narrowing question in "Open Items for Implementer": the per-deployment check only treats a plain float `timeout` as eligible, deliberately not warning for an `os.environ/`-prefixed string or an already-`httpx.Timeout` value, pending implementer verification of exactly when env-var substitution happens relative to `_create_deployment`. |
| G | `@client` decorator guarantees non-`None` `logging_obj`; delete `set_or_warn_http_client_deadline` entirely, revert Tasks 12/18/22 to direct calls | Completed in a prior session. Task 11a's body replaced with a tombstone ("removed in the 3rd review round (review finding G) — see Revision Record") documenting the invariant (`litellm/utils.py`'s `client` decorator's `wrapper_async` asserts `logging_obj is not None` and injects it before the wrapped function body runs) and why a runtime warning path for an unreachable branch would have violated `never-swallow-errors`'s "assert an invariant, don't warn-and-continue past it" principle. Tasks 12/18/22 reverted to direct `litellm_logging_obj.set_http_client_deadline(...)` calls guarded by an `assert litellm_logging_obj is not None`. |

**Minor items:**

1. **Client-disconnect integration test** — added this session as a new 4th test inside Task 16's Step 1 (`test_custom_stream_wrapper_aclose_closes_raw_stream_exactly_once_on_client_disconnect`): constructs a `CustomStreamWrapper` wrapping a raw stream (which Task 16's Step 2 wraps again in a `DeadlineBoundAsyncIterator`), calls `await wrapper.aclose()` directly (simulating an ASGI/proxy-layer client-disconnect callback, not a deadline timeout), and asserts the raw stream's own `aclose()` is invoked exactly once — distinct from Task 16's pre-existing timeout-path test, which exercises the `DeadlineExceeded`-triggered `_shielded_close` callback instead of a direct `aclose()` call.
2. **Delete the dead `_http_client_deadline` registration** — Task 6 rewritten this session to drop the `_http_client_deadline` entry (and its paired test) from the `all_litellm_params` list. Root cause: `_http_client_deadline` is never a public top-level kwarg any caller passes to `completion()`/`acompletion()`/`aresponses()`/`anthropic_messages()` — it is purely an internal parameter name threaded between `chunk_processor`, `DeadlineBoundAsyncIterator`, and `ResponsesAPIStreamingIterator.__anext__`, always derived from `logging_obj.http_client_deadline`. Since `all_litellm_params`/`get_non_default_completion_params` exists specifically to protect the public-kwargs-to-wire-body boundary, and this key can never reach that boundary, its exclusion (and the exclusion's test) was dead code that could never fail even if removed. `http_client` itself was kept — it IS a legitimate public kwarg (see Task 2/18a's examples).
3. **`DeadlineBoundAsyncIterator.aclose()` must shield itself** — Task 9 rewritten this session: `aclose()` now wraps its own body in `anyio.CancelScope(shield=True)`, rather than depending solely on whatever cancel scope its caller (e.g. `CustomStreamWrapper.aclose()`, itself already shielded) happens to run inside. New regression test `test_deadline_bound_async_iterator_aclose_shields_itself_from_cancellation` constructs a cancelled `anyio.CancelScope`, calls `await wrapped.aclose()` from inside it, and asserts the inner iterator's `aclose()` still ran to completion (its own `await asyncio.sleep(0)` checkpoint would otherwise raise `Cancelled` under an unshielded implementation).

**Tasks added (3 new lettered task units in this 3rd round):** Task 7a (finding F), Task 16a (finding C, chat half), Task 20a (finding C, responses half).

**Tasks deleted:** Task 11a (finding G) — replaced with a tombstone heading; its content is fully described in the disposition table above and in the 2nd-round Revision Record's own entry for it (kept as historical record, not rewritten).

**Existing tasks materially rewritten in this 3rd round:** Task 3/4 (findings A/E, prior session), Task 6 (minor #2, this session), Task 9 (minor #3, this session), Task 16 (minor #1, this session — test-only addition, no new production code), Task 18a (finding B, this session), Task 24a (finding D, this session), Task 12/18/22 (finding G revert, prior session).

**Total task-count change (3rd round):** 1 task unit removed (11a); 3 lettered task units added (7a, 16a, 20a). Net: +2. Final count: **36 task units across 5 phases** (28 numbered + 8 lettered: 4a, 7a, 8a, 16a, 18a, 20a, 21a, 24a) — up from 34 after the 2nd review round.

**Signatures/line numbers still requiring implementer grep-confirmation from this 3rd round** (honestly flagged, not presented as certain): Task 7a's citations of `proxy_server.py:799-808` (Task 7's own branch) and `router.py:7318` (`_create_deployment`'s construction line) were both confirmed via direct reads *this session*, but Task 7a is designed to run only after Tasks 2-7 have already landed — by which point those exact line numbers will very likely have shifted (re-grep before editing, as Task 7a's own text notes). Task 20a's citations of `litellm/responses/streaming_iterator.py:568-589`/`594-630` were confirmed via direct reads this session, but likewise depend on Task 20 having already landed first, which will shift its own line numbers; Task 20a's text already carries this same caveat. Task 7a's narrowing of "legacy timeout" to plain floats only (excluding `os.environ/`-prefixed strings and pre-built `httpx.Timeout` values) has not been checked against the actual env-var-substitution timing in the real codebase — flagged as an open question in "Open Items for Implementer," not assumed resolved.

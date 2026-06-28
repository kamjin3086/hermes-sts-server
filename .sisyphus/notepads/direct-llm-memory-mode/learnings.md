# learnings.md - Direct-LLM + Memory Mode

## 初始化（2026-06-25）
- Plan: direct-llm-memory-mode
- Wave 1 (Foundation): 6 tasks, most parallel-safe except memory.py writers (Task 2,4,5,6)
  - File conflicts: Tasks 2,4,5,6 all write to memory.py → must serialize
  - Tasks 1,2,3 are file-independent: config.py / memory.py / websearch.py → parallel OK
  - Strategy: Phase 1 = Tasks 1+2+3 parallel, Phase 2 = Task 4, Phase 3 = Task 5, Phase 4 = Task 6
- Wave 2 (Integration): depends on Wave 1 completion
- Wave 3 (UI): depends on Wave 2 Task 10
- Wave 4 (Tests): after implementation stable

## T1 Complete (2026-06-25)
- Settings: 24 fields added to config.py Settings dataclass (memory + openviking + tavily + websearch)
- config_store.py: ENV_TO_ATTR mapped all 24 env→attr
- admin.py:
  - _validate_settings_patch: memory_provider whitelist, openviking_api_key required check, tavily_depth whitelist, tavily_timeout ≤ 3.0
  - _requires_rebuild: 8 new rebuild keys
  - _settings_payload: visible["memory"] group + 4 fields added to "llm" group
- NOTE: _validate_settings_patch receives **attribute names** as keys (not env names), consistent with all existing validations in admin.py

## T2 Complete (2026-06-25)
- memory.py created with MemoryHit + MemoryProvider Protocol (9 methods) + NoopMemoryProvider + build_memory placeholder

## T3 Complete (2026-06-25)
- websearch.py created with SearchHit + WebSearchProvider Protocol + NoopWebSearchProvider + TavilySearchProvider (depth clamp, timeout clamp) + build_websearch factory
- Tavily: advanced → basic forced; timeout > 3.0 clamped to 3.0

## Task 1 Complete — config.py / config_store.py / admin.py (memory+websearch Settings fields)
- **What**: Appended 24 Settings fields (memory_*, openviking_*, tavily_*) to `Settings` frozen dataclass before `models_dir`, 24 ENV_TO_ATTR entries before `HERMES_STS_CONFIG_DB`, validation rules in `_validate_settings_patch`, rebuild keys in `_requires_rebuild`, and `visible["memory"]` + llm group additions in `_settings_payload`.
- **Pattern used**: Followed existing field style exactly (`_bool_env`, `_int_env`, `_float_env`, `_path_env`, `os.getenv`).
- **ENV_TO_ATTR insertion point**: Before `HERMES_STS_CONFIG_DB` entry (not after all existing entries).
- **Validate rules**: `memory_provider ∈ {sqlite, openviking, noop}`, `openviking` requires non-empty `openviking_api_key`, `tavily_search_depth ∈ {ultra-fast, fast, basic}`, `tavily_timeout_seconds` must be ≤ 3.0 (else 422).
- **Rebuild keys** added: `memory_enabled`, `memory_provider`, `web_search_enabled`, `tavily_api_key`, `openviking_base_url`, `openviking_api_key`, `openviking_account`, `openviking_user`.
- **visible["llm"]** appended: `memory_enabled`, `memory_provider`, `memory_remember_in_hermes`, `web_search_enabled`.
- **visible["memory"]** (new group): all 24 fields (memory_*, openviking_*, tavily_*, sqlite_memory_path, web_search_enabled).
- `sqlite_memory_path` uses `_path_env` helper (resolves to absolute path like other _path_env fields).

## T6 Complete — SqliteMemoryProvider (2026-06-25)
- **What**: Appended `SqliteMemoryProvider` class to `hermes_sts/memory.py` (lines 124-412), between NoopMemoryProvider and build_memory placeholder.
- **Imports added**: `asyncio`, `json`, `sqlite3`, `threading`, `time`, `Path` + `if TYPE_CHECKING: from hermes_sts.llm import LLMProvider`
- **DB schema**: `memories` table (id TEXT PK, content, abstract, category, tags, source, turn_id, created_at, updated_at) + `memories_fts` FTS5 external content table with `content='memories', content_rowid='rowid'`
- **FTS5 triggers**: 3 triggers (AFTER INSERT/DELETE/UPDATE) sync FTS index. UPDATE trigger does DELETE+INSERT in one trigger.
- **FTS5 fallback**: If `CREATE VIRTUAL TABLE` fails with `sqlite3.OperationalError` at init, sets `_fts5_ok=False` and recall uses `LIKE '%word%'` on content/abstract. Per-query FTS errors also caught and fall back to LIKE.
- **Thread safety**: `threading.Lock` acquired inside sync `_db_fetchall`/`_db_fetchone`/`_db_execute` helpers (NOT around `await asyncio.to_thread` — that would block event loop). Single sqlite3 connection with `check_same_thread=False`.
- **All SQL parameterized**: No f-strings in SQL. Dynamic WHERE for LIKE fallback built with string concatenation of hardcoded condition templates + `?` params.
- **recall**: FTS5 `MATCH ?` with `bm25(memories_fts) ASC` ordering, score = `-bm25`. Empty query → `ORDER BY created_at DESC LIMIT ?`.
- **record_turn**: Fire-and-forget via `asyncio.create_task(self._extract_and_save(...))`. Task ref stored in `self._bg_tasks` set with `add_done_callback(discard)` to prevent GC.
- **_extract_and_save**: Calls `self.llm.chat(messages=messages, instructions=None)` — NOT `transcript=` to avoid polluting LLM history. Strips markdown code fences before JSON parse. Catches `json.JSONDecodeError` + broad `Exception` → `logger.warning`.
- **add_memory**: Extra `source` and `turn_id` params (with defaults) beyond Protocol signature — structurally compatible.
- **Smoke test passed**: add → recall (FTS5) → list → get → update → recall updated content (FTS5 trigger re-indexed) → delete → stats. All 35 existing tests still pass.
- **build_memory placeholder**: Unchanged (line 415-416), still raises NotImplementedError.

## T5 Complete — OpenVikingMemoryProvider (2026-06-25)
- **What**: Appended `_OVSessionState` dataclass + `OpenVikingMemoryProvider` class to `hermes_sts/memory.py` (lines 424-698), between SqliteMemoryProvider and build_memory placeholder.
- **Imports added**: `from urllib.parse import quote` (stdlib), `import httpx` (third-party, separated by blank line per websearch.py pattern)
- **_OVSessionState**: dataclass with `ov_session_id: str`, `turn_count: int = 0`, `last_commit_at: float = 0.0`, `commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)`. Docstring explains OV session_id vs our session_id mapping (critical architectural distinction).
- **Lazy client**: Backing field `self._http_client: httpx.AsyncClient | None = None` + `@property _client` returns/creates persistent AsyncClient. Can't use `self._client` as both attribute and property name — used `_http_client` as backing field.
- **_headers()**: X-API-Key, X-OpenViking-Account, X-OpenViking-User, Content-Type. Set on client at creation time (persistent).
- **_request()**: Generic httpx helper, catches all exceptions → logger.warning → None. Does NOT call raise_for_status() — each method checks status codes individually (needed for 409 handling in _commit).
- **recall**: POST /api/v1/search/find → parse data["result"]["memories"] → MemoryHit list. abstract falls back to content[:200].
- **record_turn**: Lazy OV session creation (POST /api/v1/sessions → extract "session_id"). POST messages. Increment turn_count. Commit trigger: `turn_count >= memory_commit_interval_turns` OR (`last_commit_at > 0` AND `time.time() - last_commit_at >= memory_commit_idle_seconds`). Guard `last_commit_at > 0` prevents idle condition from firing on first turn (time.time() - 0 = huge). Fire-and-forget via `asyncio.create_task(self._commit(session_id))` — NOT awaited.
- **_commit**: `async with state.commit_lock:` prevents concurrent commits. 409 → logger.debug (already committing). Other non-200 → logger.warning. Success → reset turn_count=0, last_commit_at=time.time().
- **list_memories**: q non-empty → recall(q). q empty → GET /api/v1/fs/ls → sort URIs → slice [offset:offset+limit] → batch fetch get_memory in groups of 5 via asyncio.gather(return_exceptions=True) → filter MemoryHit instances.
- **get_memory**: GET /api/v1/content/read?uri={quoted} → MemoryHit or None.
- **update_memory**: POST /api/v1/content/write {mode: "replace", wait: true}. category/tags accepted per Protocol but not sent to OV.
- **delete_memory**: DELETE /api/v1/fs?uri={quoted}&recursive=false → True if status 200.
- **add_memory**: UUID hex → POST /api/v1/content/write {mode: "create", wait: true, uri: f"{target_uri}{category}/{uid}"} → extract uri from response or fallback to request uri. Failure → "ov://error".
- **stats()**: SYNC method per Protocol — uses httpx.get() (sync) not AsyncClient. GET /api/v1/stats/memories → merge into {enabled, provider, **data}. Exception → {enabled, provider, error: str(e)}.
- **final_commit**: Awaits _commit (not fire-and-forget since session ending). Skips if turn_count == 0. Try/except safety wrapper.
- **URL encoding**: `quote(uri, safe='')` for all URI query params (viking://user/memories/ → viking%3A%2F%2Fuser%2Fmemories%2F).
- **All failures degrade silently**: Every method catches Exception → logger.warning → return [] / None / False / "ov://error". NEVER raises to caller.
- **build_memory placeholder**: Unchanged (line 701-702), still raises NotImplementedError.
- **Smoke test**: py_compile OK, compileall OK, all 35 existing tests pass.

## 2026-06-25: build_memory factory + server injection

- Replaced `build_memory` placeholder at memory.py:701-702 with a 5-branch factory:
  - `memory_enabled=False` → `NoopMemoryProvider`
  - `provider="noop"` → `NoopMemoryProvider`
  - `provider="sqlite"` → `SqliteMemoryProvider(settings, llm=llm)`
  - `provider="openviking"` with key → `OpenVikingMemoryProvider(settings)`
  - `provider="openviking"` without key → fallback to `SqliteMemoryProvider` with warning
  - unknown provider → `NoopMemoryProvider` with warning
- Added imports for `build_memory` and `build_websearch` in server.py
- Injected `app.state.memory` and `app.state.web_search` in `_build_components` after `app.state.llm`
- Updated `rebuild_components` logger to include `memory_provider` and `web_search_provider`
- Verified all 6 routing paths pass via test script

## README Update (2026-06-25)
- Marked Roadmap items 1 (web search) and 3 (local memory) as "✅ Done:" prefix on the title line.
- Added new "## Direct LLM + Memory Mode" section after the Roadmap section (end of file).
- Section uses `text` code block for env vars (matching README style), bullet points with **bold** labels for features.
- Preserved all existing Roadmap bullet content unchanged (only the title line of items 1 and 3 modified).
- Naming consistent with config.py: STS_LLM_PROVIDER, STS_MEMORY_ENABLED, STS_MEMORY_PROVIDER, STS_WEB_SEARCH_ENABLED, TAVILY_API_KEY, LLM_BASE_URL/LLM_MODEL/LLM_API_KEY, OPENVIKING_BASE_URL, STS_MEMORY_REMEMBER_IN_HERMES.

## T7 Complete — admin.py memory REST endpoints + server.py wiring (2026-06-25)
- **What**: Added 7 memory REST endpoints to `hermes_sts/admin.py` + `get_memory` parameter on `create_admin_router` + memory stats in admin_state + server.py call site wiring.
- **Imports**: Added `asdict` to existing `from dataclasses import replace` → `from dataclasses import asdict, replace`.
- **Pydantic models** (after PreviewRequest): `MemoryAddRequest` (content/category/tags), `MemoryUpdateRequest` (uri/content/category/tags), `MemoryRecallRequest` (query/limit/min_score). Used `Field(default_factory=list)` for list defaults.
- **Signature**: `create_admin_router(settings, rebuild_components, get_llm=None, get_memory=None)` — `get_memory: Callable[[], Any] | None = None`.
- **admin_state**: Added `"memory": get_memory().stats() if get_memory else None` to response dict (after metrics).
- **7 endpoints** (declared in this order to avoid {uri:path} catch-all swallowing fixed paths):
  1. `GET /api/memories` (list, query params limit/offset/q) — try/except on list_memories returns `{"memories": []}` on error
  2. `POST /api/memories` (add, MemoryAddRequest body) → `{"ok": True, "uri": uri}`
  3. `GET /api/memories/activity` (fixed path BEFORE {uri:path}) — filters store.metrics by kind in memory_* set
  4. `POST /api/memories/recall` (fixed path, MemoryRecallRequest body) — times recall with perf_counter → `{"hits": [...], "ms": ms}`
  5. `GET /api/memories/{uri:path}` (get one) — 404 if not found
  6. `PUT /api/memories/{uri:path}` (update, MemoryUpdateRequest body) → `{"ok": True}`
  7. `DELETE /api/memories/{uri:path}` (delete) — 404 if not deleted
- **422 guard**: All endpoints (except activity which uses store.metrics) use `if not get_memory or (prov := get_memory()) is None: raise HTTPException(422, "memory not configured")`.
- **Route ordering CRITICAL**: `/api/memories/activity` and `/api/memories/recall` MUST be declared before `/api/memories/{uri:path}` or FastAPI matches them as uri="activity"/"recall".
- **server.py**: Updated call site to `create_admin_router(settings, rebuild_components, lambda: app.state.llm, lambda: app.state.memory)`.
- **Smoke test**: All 7 routes registered, 422 when no memory, 200/404 with NoopMemoryProvider, admin_state.memory populated. All 35 existing tests pass.

## T8 Complete — realtime.py memory + web_search integration (2026-06-25)
- **What**: Integrated memory injection, record_turn, web_search tool registration, memory metrics, and disconnect final_commit into `RealtimeSession` in `hermes_sts/realtime.py`. Updated `server.py` ws handler.
- **Dataclass fields**: Added `memory: Any = None` and `web_search: Any = None` after `session_id` field (both default None, not init=False).
- **`__post_init__`**: After `self.tools = ToolRegistry()`, added local import `from hermes_sts.tools import register_default_local_tools` + call with `web_search_provider=self.web_search`. Local import avoids circular import risk at module load.
- **`run` finally block**: After `await self._cancel_processing(send_done=False)`, added `final_commit` guarded by `self.settings.memory_enabled` + `memory is not None` + try/except logger.warning.
- **`_inject_memory` helper**: Placed before `_decode_audio_append`. Guards: `memory_enabled` → `transcript.strip()` → hermes_agent+`memory_remember_in_hermes` skip → `memory is None` → recall try/except. Builds block with `memory_injection_budget` char budget, truncates block (not instructions) if combined > 2500. Writes `memory_read` metric.
- **`_record_memory_turn` helper**: Awaits `memory.record_turn`, writes `memory_record_turn` metric, catches all exceptions.
- **`_fire_record_turn` helper**: Synchronous gate — checks `openai_compatible` mode + `memory_enabled` + `memory is not None` + `answer` non-empty, then `asyncio.create_task(self._record_memory_turn(...))`. Used by both `_ask_llm_with_tools` (3 return paths) and `_process_tool_result_turn`.
- **`_ask_llm_with_tools`**: Added `if instructions is None: instructions = self._effective_instructions()` guard before `_inject_memory` (defensive, since `_inject_memory` does `instructions + block`). All 3 return paths now capture `final_text` and call `self._fire_record_turn(transcript, final_text)` before returning.
- **`_process_tool_result_turn`**: Extracts original user transcript from `context` messages (first `role=="user"` content) for record_turn. Fires `_fire_record_turn` before `_send_response`.
- **server.py**: Added `memory=websocket.app.state.memory` and `web_search=websocket.app.state.web_search` to `RealtimeSession` constructor.
- **Verification**: `compileall` clean, all 35 existing tests pass.

## T9 Complete — admin_ui/src/main.tsx Memory tab (2026-06-25)
- **What**: Added "记忆" (Memory) management tab to the single-file React SPA. 7 edits, all within main.tsx. File grew from 1508 → 1980 lines.
- **Edits**:
  1. Added `Brain` to lucide-react named imports (verified `brain.js` exists in node_modules)
  2. Added `memory?: Record<string, any> | null` to `AdminState` type (for `state.memory` from `/api/admin/state`)
  3. Added `{ id: "memory", label: "记忆", icon: Brain }` to `navItems` array
  4. Added `{tab === "memory" && <MemoryPanel .../>}` conditional render after Advanced
  5. Added `if (tab === "memory") return "记忆管理与检索"` to `headlineFor`
  6. Added 3 helper functions after `formatDuration`: `formatTime` (Unix→MM-DD HH:MM), `activityLabel` (metric kind→Chinese), `activitySnippet` (extract short text from metric value)
  7. Added `MemoryPanel` component before `createRoot` line
- **MemoryPanel features**: status strip (4 KPIs), enable toggle (SwitchControl→PATCH memory_enabled), provider select (sqlite/openviking/noop) with conditional openviking fields (base_url, api_key password, account, user), web search config (toggle + tavily_api_key password + depth dropdown + timeout), memory list table (URI/category/abstract/created_at/actions), search box (Enter or button), prev/next pagination, add/edit modal overlay, delete with confirm, activity stream (kind+snippet+timestamp), recall test (query→5 hits with score/source), error banner, loading spinners.
- **Design system**: Reused existing CSS classes (`.panel`, `.panel-head`, `.grid`, `.span-N`, `.kpi`, `.field`, `.field-row`, `.switch-line`, `.switch-control`, `.select-wrap`, `.editable-field`, `.tiny-models`, `.check-line`, `.suggestion-card`, `.icon-btn`, `.primary`, `.secondary`, `.eyebrow`, `.muted`, `.subtle`, `.spin`, `.danger`). Table and modal use inline styles referencing design tokens (`#9aa8a1`, `#9fb5aa`, `#b7c7bd`, `#e8d8b4`, `#ffd0c7`, JetBrains Mono font). No new CSS classes added — no styles.css modification needed.
- **API calls**: All use existing `api()` helper (module-level, throws on non-ok, parses JSON). CRUD: GET/POST/PUT/DELETE `/api/memories`, POST `/api/memories/recall`, GET `/api/memories/activity`. Settings: `patch()` from App (PATCH `/api/settings`). State refresh: `reload()` from App (GET `/api/admin/state`).
- **State management**: Local React state (useState) for memories list, query, offset, activity, recall, editor modal. No external state library. useEffect on `[enabled]` for initial load.
- **Props**: `{ state, patch, reload, setNotice }` — matches existing panel pattern (Setup/Advanced). No `setBusy` needed since memory CRUD uses local loading states.
- **Verification**: `tsc --noEmit` clean, `npm run build` clean (✓ built in 2.36s). Pre-existing chunk size warning unchanged.

## Test file: tests/test_realtime_memory.py (2026-06-25)
- **What**: Created `tests/test_realtime_memory.py` with 8 integration tests for realtime.py memory injection and record_turn behavior.
- **Pattern**: Uses `unittest.TestCase`, `asyncio.run()`, `bare_session()` imported from test_core.py.
- **FakeMemoryProvider**: Inline class (like FakeLlm in test_core.py) that tracks `recall_calls` and `recorded_turns`.
- **BadFake**: Minimal fake that raises if called — used to assert code paths are NOT taken.
- **Test 1** (inject_memory_appends_hits): Sets `memory_enabled=True`, creates MemoryHit, asserts `_inject_memory` returns string containing both base instructions and abstract.
- **Test 2** (inject_memory_skips_when_disabled): `memory_enabled=False` → instructions returned unchanged. BadFake proves recall not called.
- **Test 3** (inject_memory_skips_in_hermes): `llm_provider='hermes_agent'` + `memory_remember_in_hermes=False` → instructions unchanged.
- **Test 4** (inject_memory_budget_caps_block): 10 hits with 100-char abstract, `memory_injection_budget=500`, asserts appended block ≤ 700 chars.
- **Test 5** (record_turn_dispatched): `llm_provider='openai_compatible'` + `memory_enabled=True` → `_fire_record_turn` dispatches to `memory.record_turn`. Uses `asyncio.sleep(0.01)` to yield control for background task execution.
- **Test 6** (record_turn_skipped_in_hermes): `llm_provider='hermes_agent'` + `memory_enabled=True` → record_turn NOT called.
- **Test 7** (web_search_tool_not_in_hermes): `ToolRegistry` + `register_default_local_tools` with `llm_provider='hermes_agent'` → 'web_search' not in openai_tools().
- **Test 8** (web_search_tool_in_openai): `llm_provider='openai_compatible'` + `web_search_enabled=True` + `tavily_api_key='test-key'` + `TavilySearchProvider` → 'web_search' registered.
- **Key insight**: `_fire_record_turn` is synchronous and uses `asyncio.create_task` — tests must `await asyncio.sleep(0.01)` to let the background task execute before asserting.
- **Verification**: All 8 new tests + all 35 existing tests pass.

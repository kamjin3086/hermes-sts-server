# Wave 1: chat() conversation_id parameter

## Files changed
- `hermes_sts/llm.py` — 3 locations (Protocol, `chat()`, `_chat_once()`)

## Change summary
- Added `conversation_id: str | None = None` to:
  1. `LLMProvider` Protocol `chat()` signature (line 41)
  2. `BaseOpenAIChatProvider.chat()` (line 60), forwarded to `_chat_once`
  3. `BaseOpenAIChatProvider._chat_once()` (line 82), sets `body["user"] = conversation_id` when not None (line 95-96)

## Providers that DID NOT need changes (inherit from BaseOpenAIChatProvider)
- `HermesAgentProvider` — inherits `chat()` and `_chat_once()`, no override
- `OpenAICompatibleProvider` — same
- `DummyChatProvider` (test helper) — same

## Fake LLM classes in tests (already compatible via *args, **kwargs)
- `test_core.py:280` — `FakeLlm.chat(self, *args, **kwargs)`
- `test_core.py:319` — `FakeLlm.chat(self, *args, **kwargs)`
- `test_memory_websearch.py:126` — `FailingLlm.chat(self, *args, **kwargs)`

## Call sites (all keyword-based, no conversation_id passed yet — default None is safe)
- `realtime.py:608` — `self.llm.chat(messages=messages, instructions=instructions)`
- `realtime.py:756` — `self.llm.chat(transcript, instructions=instructions, tools=...)`
- `realtime.py:808` — `self.llm.chat(messages=messages, instructions=instructions)`
- `memory.py:287` — `self.llm.chat(messages=messages, instructions=None)`

## Verification
- `python -m pytest tests/ -x -q` → 76 passed, 9 skipped

---

# Wave 1: ConversationStore foundation

## Files changed
- `hermes_sts/conversation_store.py` — NEW, full `ConversationStore` class
- `hermes_sts/config.py` — added 3 conversation settings, changed idle default

## Change summary
### conversation_store.py
- Persistent-connection SQLite store following `memory.py` pattern (threading.Lock + check_same_thread=False)
- 2 tables: `conversations` (id, status, title, timestamps) and `conversation_messages` (id, conversation_id, role, content, seq, created_at)
- All 14 methods implemented: create_conversation, get_active_conversation, append_message, get_messages, archive_conversation, list_conversations, get_conversation, update_title, reload_history_into, maybe_archive_on_idle, close, plus 3 internal helpers (_ensure_tables, _db_fetchall, _db_fetchone, _db_execute)
- `create_conversation()` auto-archives existing active with 'superseded'
- `maybe_archive_on_idle()` archives active if updated_at is beyond threshold, returns bool
- `append_message()` with `set_title_if_first=True` auto-titles from first user message[:30]
- `reload_history_into()` loads messages into llm_provider.history, always sets last_llm_call_started_at

### config.py
- `hermes_history_idle_reset_seconds` default: 14400 → 21600 (4h → 6h)
- Added `sts_conversations_enabled: bool` (default True)
- Added `sts_conversations_db_path: str` (default "data/hermes_sts.sqlite3")
- Added `sts_conversations_reload_max_messages: int` (default 0)
- Conversation keys NOT added to `_requires_rebuild` in admin.py

## Key pattern decisions
- Followed `memory.py` persistent conn + lock pattern, NOT `config_store.py` per-call pattern
- All public methods accessible via `asyncio.to_thread()` (sync sqlite3, no aiosqlite dep)
- `_db_*` helpers wrap every operation with `self._lock` for thread safety
- Docstrings kept as public API documentation (new foundational module)

---

# Wave 1: Wire ConversationStore into BaseOpenAIChatProvider

## Files changed
- `hermes_sts/llm.py` — __init__, _chat_once, reset_history, archive_current_conversation (new), ensure_active_conversation (new)
- `hermes_sts/realtime.py` — _ask_llm_with_tools calls ensure_active_conversation() before chat()
- `tests/test_core.py` — added ensure_active_conversation stub to 2 FakeLlm mocks (NOT DummyChatProvider)

## Change summary
### llm.py
- Added `uuid` import + `TYPE_CHECKING` import for ConversationStore type hint
- `__init__`: added `self.conversation_id: str | None = None` and `self.conversation_store: "ConversationStore | None" = None`
- `_chat_once`: `body["user"]` now uses `self.conversation_id` (instance attr) instead of the per-call parameter
- `_chat_once`: after `self.history.append(...)` for user+assistant, write-through to store via `append_message(..., set_title_if_first=True)` — inside `_request_gate` critical section
- `archive_current_conversation(reason)`: archives via store, clears history, sets conversation_id=None; falls back to history.clear() if store/cid is None
- `reset_history(reason)`: now delegates to `archive_current_conversation(reason)` (preserves old behavior when store=None)
- `ensure_active_conversation()` async: creates conversation via `asyncio.to_thread(store.create_conversation)` if store wired, reloads history, returns cid; generates temp `conv_{uuid.hex}` if no store

### realtime.py
- `_ask_llm_with_tools`: added `await self.llm.ensure_active_conversation()` before `self.llm.chat(...)` (line 755)

### tests/test_core.py
- FakeLlm mocks (lines 279, 315) gained `async def ensure_active_conversation(self) -> str: return ""`
- DummyChatProvider unchanged (inherits from BaseOpenAIChatProvider, gets method automatically)

## Key decisions
- Used `self.conversation_id` instance attr in _chat_once (not the per-call parameter from T2). T2's parameter remains in signature for backward compat but is ignored.
- Write-through happens only for non-tool user+assistant turns (when `transcript and text and not tool_calls`). Tool followup exchanges are NOT persisted (per MUST NOT).
- `ensure_active_conversation` uses `asyncio.to_thread` for sync sqlite calls (matches ConversationStore pattern)
- reset_history preserves old behavior when store=None: archive_current_conversation falls back to history.clear()

## Verification
- `python -m unittest discover tests` → 85 passed, 9 skipped

## T6: Conversation REST endpoints in admin.py
- ConversationStore methods are SYNC (threading.Lock + sqlite3). Wrap with `asyncio.to_thread` in async handlers, matching llm.py pattern (line 232).
- `app.state.conversation_store` is wired by T5 (lifespan). Guard with `getattr(..., None)` -> HTTP 400 "conversations disabled" when absent.
- `app.state.turn_gate` is an `asyncio.Lock` already set in `_build_components` (server.py:30-31).
- `BaseOpenAIChatProvider.archive_current_conversation(reason)` archives via store, clears history, sets `conversation_id=None` (llm.py:215-221).
- `store.create_conversation()` auto-archives any pre-existing active conv with reason "superseded" — but we already archive via llm helper first, so the new active is created cleanly.
- Shared helper `_end_current_conversation(request, reason)` dedupes logic between `/api/conversations/end` and repurposed `/api/llm/context/reset`.
- Did NOT touch `_requires_rebuild` (conversation keys must not trigger service restart).
- Verification: `python -c "import hermes_sts.admin"` exits 0; pytest 78 passed, 9 skipped.

## T5: Lifespan wiring for ConversationStore (server.py)

### Pattern
- `server.py` had no lifespan; added `@asynccontextmanager async def lifespan(app)` and wired it via `FastAPI(..., lifespan=lifespan)`.
- `_build_components(app)` is sync and runs at `create_app()` time (module load). The lifespan is the correct place for **async** init that depends on built components (app.state.llm exists after `_build_components`).
- `ConversationStore` methods (`maybe_archive_on_idle`, `get_active_conversation`, `reload_history_into`) are **sync** (use `threading.Lock`). Wrap with `asyncio.to_thread` — matches the pattern in `llm.py` (`ensure_active_conversation`).
- `ConversationStore.__init__` already calls `_ensure_tables()` internally, so no separate `_ensure_tables()` call is needed in wiring (spec's `await store._ensure_tables()` is redundant + sync).
- `reload_history_into` accepts a positional `max_messages` arg (3rd positional, after `conv_id`, `llm_provider`).

### Defense-in-depth
- `llm.last_llm_call_started_at = time.monotonic()` after wiring is CRITICAL — without it, `_reset_history_if_idle` (llm.py:242) fires immediately on the first turn after restart because `last_llm_call_started_at` is `None`/stale, archiving the just-restored conversation.

### Environmental verification gotchas
- `python -c "import hermes_sts.server"` fails in dev shells due to (1) singleton lock held by running service, (2) missing `sherpa_onnx` native dep. Both are environmental and fail identically on unmodified `server.py`. Use `ast.parse` + symbol presence checks to verify code-level correctness.

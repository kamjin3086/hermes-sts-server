# 上下文连贯性：会话历史持久化 + 助手端 conversation_id

## TL;DR

> **Quick Summary**: 把纯内存的对话历史（`BaseOpenAIChatProvider.history`）落到 SQLite，让桌面语音助手符合"碎片化使用 + 偶尔重启"的真实节奏：重启/刷新透明续接上一会话（**不**自动 archive），智能判定 idle 调用助手时 body 带 `user=conversation_id`。UI 极简化为"当前会话卡片 + 一个 End & Start New 按钮 + 折叠只读近期历史"——让助手更像"一直记着刚才说什么"，而把多会话管理这件事沉到系统层自动处理。
>
> **Deliverables**:
> - `data/hermes_sts.sqlite3` 新增 `conversations` 与 `conversation_messages` 两张表（沿用既有文件，启用 WAL）
> - `ConversationStore` 模块（CRUD + 启动智能 reload + 写穿 hook）
> - `LLMProvider.chat` Protocol 扩展 `conversation_id: str | None = None` 入参；`_chat_once` 把它写入 body 的 `user` 字段
> - 在 `turn_gate` 临界区里完成"history.append → DB insert"原子写穿
> - 启动逻辑：找最近一条 active conversation；若 `last_updated` 距今 < `hermes_history_idle_reset_seconds`（默认 6h）→ 直接续接不动 status；否则 archive 旧 + 开新；同步重置 `last_llm_call_started_at = time.monotonic()` 防刚启动误触发 idle archive
> - archive 只发生在：idle 超时（6h 默认）/ 用户在 UI 点 "End & Start New" / `/api/llm/context/reset` 复用为同一路径；**服务重启不再自动 archive**
> - admin REST：`GET /api/conversations/active`、`GET /api/conversations?limit=10&offset=0`（只读）、`POST /api/conversations/end`、`POST /api/llm/context/reset`（语义改为 archive + new）；**不**暴露 `/resume`、`PATCH /title`、`/{id}/messages` 公开访问
> - admin_ui：小 ConversationCard（首句预览、消息数、起讫时间 + 一个 "End & Start New" 按钮）+ 折叠只读 "Recent conversations"（最近 N 条 archived 列表，无任何交互编辑）
> - `tests/test_conversation_store.py` 用 `unittest.TestCase + asyncio.run()`，覆盖 CRUD、写穿、重启 reload、idle 启动时边界、idle 禁用、并发
>
> **Estimated Effort**: Medium
> **Parallel Execution**: YES - 3 waves
> **Critical Path**: T1 (store) → T4 (llm 写穿) → T5 (restart reload) → T6 (REST, 收窄) → T8 (UI, 简化) → F1-F4

---

## Context

### Original Request
用户原话："每次重启或者每当刷新界面或者总之就是一些变更出现时，上下文会自动清空，就导致上下文不连贯。能否让每次上下文都连贯呢？（这是我调试期间发现的问题，因为在助手端能看到会话，我看到创建了很多个会话）"

### Interview Summary

**Key Discussions**:
- **客户端可改性**: 真·对话客户端是 Reachy Mini Conversation App，**不可修改**；admin_ui 是纯 HTTP 配置控制台（Vite+React，无 WS、无 localStorage、15s 轮询）
- **助手提供方**: OpenAI 兼容服务（用 body 的 `user` 字段传 `conversation_id`，36-char UUID 在长度上限内）
- **并发模型**: 多客户端但**共享对话历史**（保留当前 `app.state.llm` 单例共享行为，仅加落盘）
- **持久化**: 复用 `data/hermes_sts.sqlite3` 加 `conversations` + `conversation_messages` 两张表；用 `memory.py` 式的 persistent connection + `threading.Lock` + `asyncio.to_thread`，并启用 WAL
- **UI 范围（Round 2 校准）**: 极简化——去掉之前的 Sessions 大面板（list / 续聊按钮 / 调阅消息 / 改标题），改为小 ConversationCard + 折叠只读 Recent；不再有"用户主动点 resume" 这件事
- **Idle 阈值（Round 2 校准）**: 默认 6h（vs 项目原默认 4h），贴合碎片化桌面使用；保留 0=禁用自动归档
- **重启语义（Round 2 校准）**: 重启 = 瞬态中断 ≠ 会话边界，**不再**自动 archive——启动时找最近 active 直接续接；只在 last_updated 距今 ≥ idle_threshold 时才把旧 archive 并开新
- **测试**: 沿用项目既有约定 `unittest.TestCase + asyncio.run()`，新增 `tests/test_conversation_store.py`

**Research Findings**:
- 根因三连击：(1) `history: list[Message]` 永驻内存、(2) 每个 WS 连接 mint 新 `session_id`、(3) `_post_chat_completions` 不带 `user` 字段
- LLM provider 是单例（`app.state.llm`），目前跨 WS 连接已经在共享 `self.history`——本计划保留该共享语义
- `tool_followup_messages` 路径**从来不写入** `self.history`（realtime.py:776 本地构造 messages），是先前的设计取舍
- `_history_for_prompt()` 是 read-time 滑窗（max_messages=300 / max_chars=65536）—不影响落盘策略
- `last_llm_call_started_at` 用 `time.monotonic()`——重启后需主动重置，否则首次 idle 检查会立刻误归档
- 已有 `turn_gate: asyncio.Lock`（server.py:31）跨会话串行 LLM turn——用它保护写穿
- `admin.py: _schedule_service_restart` 用 `systemctl restart hermes-sts-server.service` 触发硬重启；本计划改动**不可**进入 `_requires_rebuild` set

### Metis Review
**Identified Gaps** (addressed):
- 项目用 unittest 而非 pytest → 改回 `unittest.TestCase + asyncio.run()`
- 混用 SQLite 连接模式有风险 → 在同一文件用 persistent conn + WAL，不与 config_store 的 per-call connection 冲突
- 启动 reload 后 `last_llm_call_started_at` 需重置 → reload 末尾 `llm.last_llm_call_started_at = time.monotonic()`
- 写穿应在 `turn_gate` 临界区内 → DB insert 紧贴 `self.history.append()`，同步
- `hermes_history_idle_reset_seconds=0` 时禁用 idle → 同时禁用 idle 自动归档（已知约束）
- Protocol 改签名要向后兼容 → `conversation_id: str | None = None`
- tool_followup 不写入历史，"调阅消息"只显示 user turns + 最终 assistant 文本 → 显式写为实现约束
- conversation title = 首条 user msg 前 30 字符自动生成；**不**在 admin UI 提供编辑入口（保持 UI 轻）
- v1 不做 retention 自动清理（保留全部 archived，将来再做）

**Round 2 校准理由（重开会，基于用户设计哲学反馈）**:
- 原建议"重启自动 archive + new" 与桌面助手使用直觉相违——重启只是瞬态中断，强切会让用户在每次 deploy/重启后突然忘记刚才说什么；改为启动只续接、不动 status
- 原建议"PATCH /title 编辑" / "/resume 手动续聊" 拆出多个端点与 UI 表单，与"减少口子"诉求冲突——删
- 原建议"列出 + 续聊 + 调阅消息" 三步 UI → "卡片 + 折叠只读" 一步 UI，工量降、心智降
- 原默认 4h idle → 6h，让"上午场 → 中午离开 → 下午回来" 仍属同一对话

---

## Work Objectives

### Core Objective
让桌面语音助手符合自然节奏：服务重启/客户端刷新**透明续接**同一会话；仅在长时间空闲（6h 默认）/ 用户主动 End 时切换；助手端后台同一 conversation 视为同一会话。最小化用户对面板/按钮的关心——大部分状态由系统静默维持。

### Concrete Deliverables
- 表：`conversations(id TEXT PK, status TEXT, title TEXT, created_at REAL, updated_at REAL, ended_at REAL, ended_reason TEXT)` 与 `conversation_messages(id INTEGER PK, conversation_id TEXT, role TEXT, content TEXT, seq INTEGER, created_at REAL)`，外键 + 索引 `(conversation_id, seq)`
- 模块：`hermes_sts/conversation_store.py` 实现 `ConversationStore`（含 `maybe_archive_on_idle` 启动时判定逻辑）
- Settings：新增 `STS_CONVERSATIONS_ENABLED`（默认 True）、`STS_CONVERSATIONS_RELOAD_MAX_MESSAGES`（默认 0=不限制）；保留既有 `hermes_history_idle_reset_seconds`，默认从 4h 提到 **6h**
- Protocol 扩展：`LLMProvider.chat()` 新增 `conversation_id: str | None = None`
- 写穿钩子：`BaseOpenAIChatProvider._chat_once()` 在 `self.history.append()` 后立即调 `conversation_store.append_message(...)`，**同 turn_gate 临界区内**
- 启动 reload（新语义）：找最近一条 active conv；若 `now - last_updated < idle_threshold` → 直接续接不动 status；否则调 `archive_current(reason="idle_restart")` + 新建 active；末尾 `llm.last_llm_call_started_at = time.monotonic()`
- archive 触发条件：仅 (a) idle 运行超时（`_reset_history_if_idle` 走 archive 路径），(b) 用户 UI 点 End，(c) `/api/llm/context/reset` 三条路径——统一走 `archive_current_conversation()`；**重启不 archive**
- REST 端点（admin.py，收窄）：
  - `GET  /api/conversations/active` → `{id, title?, message_count, created_at, updated_at}` 或 `{id: null}`
  - `GET  /api/conversations?status=&limit=10&offset=0` → 只读列表（含活跃与近期 archived）
  - `POST /api/conversations/end` → archive 当前 active 并开新 active，返回新 active conv
  - `POST /api/llm/context/reset` → 现有 URL 不变，内部走 `end` 同一逻辑
  - **不**实现 `/resume`、`PATCH /{id}`、`/{id}/messages`（这些是 admin_ui 续聊/调阅的支撑——本轮 UI 简化后不再需要公开）
- admin_ui：`admin_ui/src/main.tsx` 新增 **ConversationCard**（小，与 MemoryPanel 同结构范式但更小）：
  - 主位：当前对话预览（title 首句截 30 字、消息数、started、last activity）
  - 一个按钮：`End & Start New`
  - 折叠只读：`Recent (last 10)` ——archived 列表仅显示 (title, started, ended, message_count)，**不可点进、不可编辑**
- tests：`tests/test_conversation_store.py`，引入 SQLite 在 `tmp_path` 上的真测 + 内置 fake store stub

### Definition of Done
- [ ] 启动 → 发 4 轮 → `systemctl restart` → 发 1 轮 → 第 5 轮**仍属同一 active conversation**（vs 旧方案归入新 active）；DB 中 status 全程保持 'active'
- [ ] `curl /api/conversations/active` 重启前后返回**相同 conv_id X**
- [ ] 重启后 `len(app.state.llm.history) >= 8`（原 4 轮 8 条消息已 reload）
- [ ] Set `hermes_history_idle_reset_seconds=2`、发 1 轮、`sleep 3`、发 1 轮 → 第 2 轮进入新 active；旧 active 已 archived
- [ ] curl `POST /api/conversations/end` → 旧 active archived；新 active 出现
- [ ] 调用助手端时 body 含 `"user": "conv_<...>"`（QA 期间用 monkey-patched httpx 验证）
- [ ] `python -m unittest tests.test_conversation_store` 全绿（9 个测试，0 skip）

### Must Have
- 服务重启后**透明续接同一 conversation**（不自动 archive）
- 启动 reload 中按 idle 阈值**判断**是否归档（只在 last_updated 距今 ≥ idle_threshold 才归档）
- 调用助手端时 body 带 `user: <conversation_id>`
- 所有 write-through 与 archive 都在 `turn_gate` 临界区内完成
- `/api/llm/context/reset` 与 `POST /api/conversations/end` 等价（共享同一 archive + new 路径）
- admin_ui 仅展示当前会话预览 + End 按钮 + 折叠只读 Recent；**无**调阅消息 UI、**无**手动续聊 UI、**无**标题编辑 UI
- unittest 风格、零新依赖

### Must NOT Have (Guardrails)
- **不修改 Reachy Mini 客户端**——所有稳定身份在服务端解决
- **不引 pytest / aiosqlite / 新 HTTP mock 库**
- **conversation 持久化改动不得进入 `_requires_rebuild` 集**——不触发 `systemctl restart`
- **不在 v1 持久化 `tool_followup_messages` 中间交换**——只存 user + 最终 assistant 文本
- **不做 retention 自动清理**——archived 会话永久保留
- **不新建 `routers/` 目录**——所有新 REST 端点加进 `admin.py` 的 `create_admin_router()`
- **不拆分 `admin_ui/src/main.tsx`**——ConversationCard 写进同一文件，仿 `MemoryPanel` 结构
- **不引入额外 LLM 调用做 title 生成**——title = 首条 user msg[:30]，自动赋值
- **不在 admin_ui 提供任何"手动续聊某 archived 会话"入口**——这是设计取舍（Round 2 校准）：默认就是续聊最近一条，archived 只读
- **不暴露 `/api/conversations/{id}/resume`、`{id}/messages`、`PATCH {id}` 公开 API**——本轮不需要
- 不出现 `as any / @ts-ignore / # type: ignore`；不出现 console.log / print 残留；不写过度注释与空 catch

---

## Verification Strategy (MANDATORY)

> **ZERO HUMAN INTERVENTION** - 全部 QA 由 agent 执行。

### Test Decision
- **Infrastructure exists**: YES（`tests/test_core.py`、`tests/test_realtime_memory.py` 用 `unittest.TestCase + asyncio.run()`）
- **Automated tests**: tests-after（持久化模块单独覆盖，非 TDD）
- **Framework**: `unittest`（项目既有约定；Metis 显式建议）
- **Coverage target**: ConversationStore CRUD、写穿原子性、重启 reload、idle 启动边界、idle 禁用、End 端点切换、并发

### QA Policy
每个 task MUST 含 agent-executed QA scenarios。
Evidence: `.sisyphus/evidence/task-{N}-{scenario-slug}.{ext}`

- **Backend**: `Bash (curl + python -m unittest + sqlite3)`
- **UI**: `Playwright`（playwright skill）- 导航、点击、断言 DOM、截图
- **Realtime WS**: `Bash (python 脚本，直接连 ws://127.0.0.1:8765/v1/realtime 发 PCM turn)`
- **LLM body 拦截**: `Bash (python + monkeypatched httpx)`

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Foundation - 3 parallel):
├── T1: ConversationStore + Settings + 表 schema        [quick]
├── T2: LLMProvider.chat Protocol 扩展                  [quick]
└── T3: 测试桩文件 tests/test_conversation_store.py      [quick]

Wave 2 (Integration - 4 parallel after W1):
├── T4: llm.py 写穿 + archive helper + reset_history    [deep]   (depends T1,T2)
├── T5: server.py 启动 reload + last_llm_call_started_at [unspecified-high]  (depends T1,T2,T4)
├── T6: admin.py 新增 REST endpoints (复用旧 reset)      [unspecified-high]  (depends T1,T4)
└── T7: tests/test_llm_user_field.py body user 字段     [quick]  (depends T2)

Wave 3 (Consumers - 2 parallel after W2):
├── T8: admin_ui/src/main.tsx ConversationCard + 折叠只读 [quick]               (depends T6)
└── T9: tests/test_conversation_store.py 全量补完        [unspecified-high]  (depends T1,T4,T5,T6)

Wave FINAL (4 parallel reviews after W3):
├── F1: Plan Compliance Audit    (oracle)
├── F2: Code Quality Review      (unspecified-high)
├── F3: Real Manual QA           (unspecified-high + playwright)
└── F4: Scope Fidelity Check     (deep)

Critical Path: T1 → T4 → T5 → T6 → T8 → F1-F4
Parallel Speedup: ~60% vs 串行
Max Concurrent: 4 (Wave 2)
```

### Dependency Matrix (FULL)

| Task | Depends On        | Blocks            | Wave |
|------|-------------------|-------------------|------|
| T1   | -                 | T4,T5,T6,T9       | 1    |
| T2   | -                 | T4,T5,T7          | 1    |
| T3   | -                 | T9                 | 1    |
| T4   | T1,T2             | T5,T6,T9          | 2    |
| T5   | T1,T2,T4          | T9                 | 2    |
| T6   | T1,T4             | T8,T9             | 2    |
| T7   | T2                 | F1                 | 2    |
| T8   | T6                 | F1,F3             | 3    |
| T9   | T1,T4,T5,T6       | F1                 | 3    |
| F1-F4| ALL               | user okay          | FINAL|

### Agent Dispatch Summary

- **W1**: 3 × `quick` — T1/T2/T3
- **W2**: 4 × mixed — T4 `deep` / T5 `unspecified-high` / T6 `unspecified-high` / T7 `quick`
- **W3**: 2 × mixed — T8 `quick` / T9 `unspecified-high`
- **FINAL**: 4 — F1 `oracle` / F2 `unspecified-high` / F3 `unspecified-high`+playwright / F4 `deep`

---

## TODOs

> Implementation + Test = ONE Task. EVERY task MUST have: Recommended Agent Profile + Parallelization info + QA Scenarios.

- [x] 1. ConversationStore + Settings + 表 schema

  **What to do**:
  - 新建 `hermes_sts/conversation_store.py`，仿 `hermes_sts/memory.py:130-184` 的连接管理范式：persistent connection + `threading.Lock` + `check_same_thread=False` + `asyncio.to_thread()` 包裹 sync sqlite3 调用
  - 复用 `data/hermes_sts.sqlite3`，**首次连接时** `PRAGMA journal_mode=WAL`、`PRAGMA foreign_keys=ON`
  - `CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, status TEXT NOT NULL, title TEXT, created_at REAL, updated_at REAL, ended_at REAL, ended_reason TEXT)`
  - `CREATE TABLE IF NOT EXISTS conversation_messages(id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, seq INTEGER NOT NULL, created_at REAL, FOREIGN KEY(conversation_id) REFERENCES conversations(id))`
  - `CREATE INDEX IF NOT EXISTS idx_convmsg_conv_seq ON conversation_messages(conversation_id, seq)`
  - 方法（all async，全部走 `to_thread`）：
    - `create_conversation() -> str`（生成 `conv_<uuid4.hex>`，status='active'；**每次至多 1 条 active**，create 前若仍有旧 active 则自动 `archive_conversation(old, 'superseded')` 再 insert）
    - `get_active_conversation() -> dict | None`（返回当前 active 的 conv，含 id/title/created_at/updated_at/message_count）
    - `append_message(conv_id, role, content, *, set_title_if_first=False)`（seq 自动 = max(seq)+1，更新 `conversations.updated_at`；若 `set_title_if_first=True` 且本条是首条 user 消息且当前 title 仍为 NULL，则 title = content[:30] —— 让 title 自然产生，无需 LLM 处理）
    - `get_messages(conv_id, limit=0) -> list[dict]`（limit=0 表示全部；按 seq 排序）
    - `archive_conversation(conv_id, ended_reason)`（status='archived'，ended_at=now，ended_reason=reason）
    - `list_conversations(status=None, limit=10, offset=0) -> list[dict]`（含每条 message_count/created_at/updated_at/ended_at/ended_reason；status 不传则混合返回）
    - `get_conversation(conv_id) -> dict`（内部使用，不通 REST）
    - `update_title(conv_id, title)`（内部使用，title 由 append_message 自动填；保留供 debug 用，**不**经 REST 暴露）
    - `reload_history_into(conv_id, llm_provider, max_messages=0)`：从 DB 把指定 conv 的 messages 按 seq 覆盖到 `llm_provider.history`；末尾 **总是**执行 `llm_provider.last_llm_call_started_at = time.monotonic()`
    - `maybe_archive_on_idle(idle_threshold_seconds: float) -> bool`（**新设计的核心方法**）：找当前 active；若无则返回 False 不做任何操作；若有则比较 `time.time() - last_updated` (用 conversations.updated_at) 与 `idle_threshold_seconds`：
      - `idle_threshold_seconds <= 0` → 禁用自动归档，返回 False（尊重用户禁用 intent）
      - distance < threshold → 直接返回 False（重启透明续接，**不动 status**）
      - distance ≥ threshold → 调 `archive_conversation(active, f"idle_{int(distance)}s")`，返回 True
  - 在 `hermes_sts/config.py` 加（**不要**把任何 conversation 相关 key 放入 `_requires_rebuild`）：
    - `sts_conversations_enabled: bool = True`（env: `STS_CONVERSATIONS_ENABLED`）
    - `sts_conversations_db_path: str = "data/hermes_sts.sqlite3"`（env: `STS_CONVERSATIONS_DB_PATH`）
    - `sts_conversations_reload_max_messages: int = 0`（0 = reload 全部活跃会话的消息）
  - **同时**：把既有 `hermes_history_idle_reset_seconds` 默认值（若为 14400 = 4h）改成 **21600 = 6h**，注释说明"碎片化桌面使用：上午场→中午离开→下午回来仍在同一对话"。0 仍表示禁用自动归档
  - 文件不加 `__main__`，仅 import 安全

  **Must NOT do**:
  - 不引入 aiosqlite；不使用 per-call connection（这是 config_store 的范式，不适合每轮写穿）
  - 不写 retention / TTL 逻辑
  - 不持久化 tool_followup_messages（v1 范围外）
  - 不在 `admin.py` 的 `_requires_rebuild` 加入任何 conversation key

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 单文件 + 一份 Settings 改动，已有 memory.py 完整范式可直接照搬
  - **Skills**: []
  - **Skills Evaluated but Omitted**:
    - `playwright`: 不涉及 UI
    - `git-master`: 提交阶段才需要，由 orchestrator 处理

  **Parallelization**:
  - Can Run In Parallel: YES
  - Parallel Group: Wave 1（with T2, T3）
  - Blocks: T4, T5, T6, T9
  - Blocked By: None

  **References**:

  **Pattern References**:
  - `hermes_sts/memory.py:130-184` — SQLite 连接管理范式（persistent conn + Lock + check_same_thread=False + to_thread），整套照搬到 ConversationStore
  - `hermes_sts/memory.py:OpenHelper._ensure_tables()` — `CREATE TABLE IF NOT EXISTS` 的写法与 PRAGMA 设置位置
  - `hermes_sts/config_store.py` — 同一 `data/hermes_sts.sqlite3` 文件的另一种用法（per-call sync），用于了解 WAL 切换不会相互影响

  **API/Type References**:
  - `hermes_sts/llm.py:BaseOpenAIChatProvider`（行 46-50）— `self.history: list[Message]`、`Message = dict[str, str]`、`last_llm_call_started_at: float | None`
  - `hermes_sts/llm.py:Message` 类型定义（顶部某处）— 角色字段为 `role`/`content` 字符串

  **Test References**:
  - `tests/test_core.py:26-46 DummyChatProvider` — LLM provider 测试桩的结构，提示 store 测试可同理 stub
  - `tests/test_realtime_memory.py` — memory store 的测试组织（面向真实 sqlite 的写法），可参考列：用 `tmp_path` fixture 等效

  **External References**:
  - https://www.sqlite.org/wal.html — `PRAGMA journal_mode=WAL` 与并发多连接行为
  - https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection — check_same_thread=False 的语义

  **WHY Each Reference Matters**:
  - memory.py 是本仓库已验证的 SQLite 长连接范式；照搬它最大限度减少风险并保证一致风格
  - config_store.py 确认同一文件可被多个 connection 同时持有（WAL），允许 conversation 表加入既有文件
  - llm.py:46-50 是 store 的核心消费者契约
  - test_realtime_memory.py 证明用 `tmp_path` 真测 SQLite 是可行路径

  **Acceptance Criteria**:

  **If TDD**: N/A（tests-after）

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 表创建 + 基本 CRUD
    Tool: Bash (python)
    Preconditions: clean repo, data/ 目录可写
    Steps:
      1. python -c "
         import asyncio
         from pathlib import Path
         from hermes_sts.conversation_store import ConversationStore
         async def main():
             store = ConversationStore(db_path=str(Path('/tmp/test_conv_t1.sqlite3').resolve()))
             await store._ensure_tables()
             cid = await store.create_conversation()
             assert cid.startswith('conv_'), cid
             await store.append_message(cid, 'user', 'hi')
             await store.append_message(cid, 'assistant', 'hello there')
             msgs = await store.get_messages(cid)
             assert len(msgs)==2 and msgs[0]['role']=='user' and msgs[1]['seq']==2, msgs
             active = await store.get_active_conversation()
             assert active['id']==cid, active
             await store.archive_conversation(cid, 'test')
             assert (await store.get_active_conversation()) is None
             print('OK')
         asyncio.run(main())"
      2. Assert stdout contains 'OK'
    Expected Result: 退出码 0，输出 'OK'
    Failure Indicators: 退出码非 0，或 stdout 不含 'OK'（被 assert 中断）
    Evidence: .sisyphus/evidence/task-1-store-crud.txt

  Scenario: WAL 模式启用 + 同文件多连接无冲突
    Tool: Bash (python + sqlite3 CLI)
    Preconditions: data/hermes_sts.sqlite3 存在（或本次创建）
    Steps:
      1. python -c "
         from hermes_sts.conversation_store import ConversationStore
         from hermes_sts.config_store import ConfigStore
         import asyncio
         async def m():
             s = ConversationStore(db_path='data/hermes_sts.sqlite3'); await s._ensure_tables()
         asyncio.run(m())
         ConfigStore.default()"  # 验证并打开 config_store，两者共存
      2. sqlite3 data/hermes_sts.sqlite3 'PRAGMA journal_mode'
      3. Assert stdout == 'wal'
      4. sqlite3 data/hermes_sts.sqlite3 '.tables'
      5. Assert stdout contains 'conversations' and 'conversation_messages'
    Expected Result: WAL 启用；两张新表存在；config_store 既有表仍完整
    Failure Indicators: pragma 不是 wal；新表缺失；既有表消失
    Evidence: .sisyphus/evidence/task-1-wal-tables.txt
  ```

  **Commit**: YES — `feat(store): add ConversationStore with SQLite persistence`
  Files: `hermes_sts/conversation_store.py`, `hermes_sts/config.py`
  Pre-commit: `python -c "import hermes_sts.conversation_store"`

- [x] 2. LLMProvider.chat Protocol 扩展（conversation_id 入参）

  **What to do**:
  - 先用 `lsp_find_references` 全局定位 `LLMProvider.chat` 的所有调用点（至少：`realtime.py:_ask_llm_with_tools`、可能的 `admin.py` 测试通道、`tests/test_core.py:DummyChatProvider`、`hermes_agent` provider 子类如已存在）
  - 在 `hermes_sts/llm.py` 找 `LLMProvider`（Protocol）的 `chat` 签名，新增 `conversation_id: str | None = None` 参数（**默认 None 保证向后兼容**）
  - 在 `BaseOpenAIChatProvider.chat()` 与 `_chat_once()` 同步新增对应参数，并透传
  - 在 `_chat_once` 构造请求 body 时，当 `conversation_id is not None` 则置 `body["user"] = conversation_id`
  - 更新 `DummyChatProvider`（`tests/test_core.py`）与新 `HermesAgentProvider` 之类实现（如果存在）的 `chat()` 签名以保持 Protocol 一致——**这一步只是签名对齐，本任务不实现真正的 `user` 写入；写入由 T4 完成**

  **Must NOT do**:
  - 不改 `_post_chat_completions` 的 body 拼装（T4 才负责"实际拼 user"），但本任务确保 `_chat_once` 签名上**已能接收** conversation_id 且不报 unexpected kwarg
  - 不调任何 LLM API；不引入新依赖
  - 不改 `chat()` 已有返回类型

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 单文件 Protocol/签名扩展，向后兼容；lsp_find_references 已能枚举调用点
  - **Skills**: []

  **Parallelization**:
  - Can Run In Parallel: YES
  - Parallel Group: Wave 1（with T1, T3）
  - Blocks: T4, T5, T7
  - Blocked By: None

  **References**:

  **API/Type References**:
  - `hermes_sts/llm.py:LLMProvider`（Protocol 定义）—— 必读，签名权威源头
  - `hermes_sts/llm.py:BaseOpenAIChatProvider.chat()` 与 `_chat_once()`（行 40-115）—— 实现链路
  - `hermes_sts/llm.py:_post_chat_completions`（行 117-128）—— body 拼装位置（T4 真写）
  - `hermes_sts/realtime.py:_ask_llm_with_tools` (行 ~756) —— 唯一生产侧调用点

  **Test References**:
  - `tests/test_core.py:DummyChatProvider` —— 必须同步签名
  - 任何 `tests/test_*llm*.py` 或 `test_*hermes_agent*.py` 如存在，需同步

  **External References**:
  - https://platform.openai.com/docs/api-reference/chat/create — `user` 字段长度上限（OpenAI 限 128 字符，UUID 36 字符安全）

  **WHY Each Reference Matters**:
  - Protocol 改签名后所有实现都得对齐；lsp_find_references 是唯一可靠枚举手段
  - 默认 None 保证既有调用点无需立刻更新；T4 才会把 conversation_id 从 RealtimeSession 真传下来

  **Acceptance Criteria**:

  - [ ] `python -c "import hermes_sts.llm; help(hermes_sts.llm.BaseOpenAIChatProvider.chat)"` 输出含 `conversation_id`
  - [ ] 既有测试 `python -m unittest discover tests` 仍全绿（无 unexpected kwarg 报错）

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 签名向后兼容
    Tool: Bash (python)
    Preconditions: T2 已合并入 working tree
    Steps:
      1. python -c "
         import inspect
         from hermes_sts.llm import LLMProvider, BaseOpenAIChatProvider
         sig = inspect.signature(BaseOpenAIChatProvider.chat)
         assert 'conversation_id' in sig.parameters, sig
         assert sig.parameters['conversation_id'].default is None, sig
         from tests.test_core import DummyChatProvider
         async def m():
             p = DummyChatProvider()
             r = await p.chat('hello')
             r2 = await p.chat('hello', conversation_id='conv_test')
             assert r == r2, (r, r2)
         import asyncio; asyncio.run(m())
         print('OK')"
      2. Assert stdout contains 'OK'
    Expected Result: 旧调用（不传 conversation_id）与新调用（传）都不抛错且结果一致
    Failure Indicators: 任何 call 抛 TypeError / UnexpectedKeywordArgument
    Evidence: .sisyphus/evidence/task-2-protocol-backcompat.txt

  Scenario: 既有 unittest 测试不被破坏
    Tool: Bash (python)
    Preconditions: T2 不应改动 DummyChatProvider 的行为逻辑，仅签名对齐
    Steps:
      1. python -m unittest discover tests -v
      2. Assert all tests pass (FAIL=0)
    Expected Result: 不引入任何失败
    Failure Indicators: 任何 test 失败或 error
    Evidence: .sisyphus/evidence/task-2-existing-tests.txt
  ```

  **Commit**: YES — `feat(llm): extend LLMProvider.chat with conversation_id param`
  Files: `hermes_sts/llm.py`, `tests/test_core.py`（如 DummyChatProvider 需同步）
  Pre-commit: `python -m unittest discover tests`

- [x] 3. tests/test_conversation_store.py 测试桩文件

  **What to do**:
  - 新建 `tests/test_conversation_store.py`
  - 文件顶部按 `tests/test_core.py` 风格：`import unittest`、`import asyncio`、`from pathlib import Path`
  - 定义 `class TestConversationStore(unittest.TestCase)`（**空方法骨架**，本任务只写"待 T9 填充"的字面占位 test 方法，`self.skipTest("filled in T9")`；T9 会把 `skipTest` 拆成实际断言）
  - 测试方法名（占位即可）：
    - `test_create_and_get_active`
    - `test_append_message_seq_increments`
    - `test_archive_sets_ended`
    - `test_reload_history_into_overwrites`
    - `test_maybe_archive_on_idle_within_threshold_keeps_active`
    - `test_maybe_archive_on_idle_over_threshold_archives`
    - `test_maybe_archive_on_idle_disabled_never_archives`
    - `test_concurrent_append_within_lock`
    - `test_wal_journal_mode_pragma`
  - 验证 `python -m unittest tests.test_conversation_store` 能跑出 9 个 skipped（不报错）
  - 在 `DummyConversationStore` 辅助 stub（可选，若 T9 不需要就略过）

  **Must NOT do**:
  - 不填实际断言（T9 任务）
  - 不引入 pytest
  - 不引入 mock 库

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 单文件骨架占位
  - **Skills**: []

  **Parallelization**:
  - Can Run In Parallel: YES
  - Parallel Group: Wave 1（with T1, T2）
  - Blocks: T9
  - Blocked By: None

  **References**:

  **Pattern References**:
  - `tests/test_core.py` —— 文件组织风格、import 顺序、asyncio.run 用法
  - `tests/test_realtime_memory.py` —— 真测 SQLite 的范式（如使用 tmp_path）

  **API/Type References**:
  - `hermes_sts/conversation_store.py:ConversationStore`（T1 产出）—— 占位需对其公共 API

  **WHY Each Reference Matters**:
  - 范式一致性；T9 把占位填实不会因风格不符返工

  **Acceptance Criteria**:
  - [ ] `python -m unittest tests.test_conversation_store -v` 跑出 7 个 skipped（无 error）

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 骨架可跑
    Tool: Bash (python)
    Preconditions: T3 已合并
    Steps:
      1. python -m unittest tests.test_conversation_store -v 2>&1 | tail
      2. Assert 输出含 'OK' 且 'skipped=7' 或等同
    Expected Result: 0 failed, 0 errored, 7 skipped
    Failure Indicators: 任何 error（如 import error 说明路径/命名不对）
    Evidence: .sisyphus/evidence/task-3-test-skeleton.txt
  ```

  **Commit**: NO（与 T9 一起提交：`test(store): unittest suite for conversation persistence`）
  Files: `tests/test_conversation_store.py`

- [x] 4. llm.py 写穿 + archive_conversation() + reset_history 新语义

  **What to do**:
  - 在 `BaseOpenAIChatProvider.__init__` 增 `self.conversation_id: str | None = None` 与 `self.conversation_store: ConversationStore | None = None`（None 时禁用持久化，向后兼容旧行为）
  - 在 `_chat_once` 现有 `self.history.append({"role":"user",...})` 与 `self.history.append({"role":"assistant",...})`（llm.py:112-114 附近）**之后**同步调用 `self.conversation_store.append_message(...)`（包在已有的 `turn_gate` 临界区内，**不得 fire-and-forget**）
  - 在 `_chat_once` 的 body 拼装处（llm.py:84-93 附近），当 `conversation_id is not None` 则置 `body["user"] = conversation_id`
  - 新增 `archive_current_conversation(self, reason: str) -> None`：调 `self.conversation_store.archive_conversation(self.conversation_id, reason)`，清空 `self.history`，把 `self.conversation_id` 设回 None（下次发言触发 `ensure_active_conversation`）
  - 改造 `reset_history(self, reason: str)`：若 conversation_store 已 wire 且 conversation_id 不为 None，则**内部**调 `archive_current_conversation(reason)` 而非简单 `self.history.clear()`；如未 wire（None）则保留旧的 `self.history.clear()` 行为
  - 改造 `_reset_history_if_idle`：空闲到期时调 `self.reset_history(reason=f"idle_{int(idle_seconds)}s")`，它会经 archive 路径。`hermes_history_idle_reset_seconds <= 0` 时维持原禁用语义（早 return）——意味着关 idle 即关自动 archive
  - 新增 `ensure_active_conversation(self) -> str`：若 `self.conversation_id` 为 None 且 store 已 wire，则 `cid = await store.create_conversation()`，赋值 `self.conversation_id = cid`，并从 store 读历史覆盖 self.history（新会话此为空）。在 `RealtimeSession._ask_llm_with_tools` 进入 turn_gate 后、调 `self.llm.chat(...)` 之前调用——传 `conversation_id=cid` 给 chat

  **Must NOT do**:
  - 不持久化 tool_followup_messages（v1 范围外，明确不记）
  - 不在 turn_gate 临界区外写 conversation_messages
  - 不修改 admin.py（T6 负责）
  - 不修改 server.py 启动流程（T5 负责）

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: 单文件但语义敏感、并发/临界区要谨慎；signoff 要 oracle 级审阅
  - **Skills**: []

  **Parallelization**:
  - Can Run In Parallel: YES（与 T5/T6/T7 同 wave，但因 T5 依赖 T4 完成的 archive helper）
  - Parallel Group: Wave 2（与 T5/T6/T7 同 wave；T5/T6 实际需等 T4 完成 archive 与 write-through API 后才能 wire）—— orchestrator 调度时若 T5/T6 早完成属正常，它们调用点可以 stub-OK，scriptionally T5 在 _build_components 里 lint 即可，T6 在 admin.py 里只是新增 endpoint，不实际改 llm 内部
  - Blocks: T5, T6, T9
  - Blocked By: T1（store API）, T2（Protocol 签名）

  **References**:

  **Pattern References**:
  - `hermes_sts/llm.py:112-114`（self.history.append 现有双 append 点）—— 拼接写穿的精确锚点
  - `hermes_sts/llm.py:_chat_once body 构造区`（行 84-93）—— body["user"] 在此加
  - `hermes_sts/llm.py:_reset_history_if_idle 200-206` —— idle 归档应同走 reset_history 改后版本
  - `hermes_sts/realtime.py:_ask_llm_with_tools` ~756 —— 唯一 chat 调用点，需在此前 ensure_active_conversation 并传 conversation_id

  **API/Type References**:
  - `hermes_sts/conversation_store.py:ConversationStore`（T1 产出）—— `append_message`、`archive_conversation`、`create_conversation`、`get_messages` 是本任务消费的契约
  - `hermes_sts/llm.py:LLMProvider.chat` Protocol（T2 产出）—— conversation_id 已是新入参

  **Test References**:
  - `tests/test_core.py:DummyChatProvider` —— 若本任务把 ConversationStore wiring 加进 provider __init__，DummyChatProvider 也需要默认 conversation_store=None（向后兼容，T2 已处理签名）

  **External References**:
  - https://platform.openai.com/docs/api-reference/chat/create — `user` 字段行为

  **WHY Each Reference Matters**:
  - 写穿临界区错位即等于数据损坏；以 llm.py:112-114 为锚能精确安置 DB insert
  - idle reset 走 reset_history 路径才能让"归档"语义一致

  **Acceptance Criteria**:
  - [ ] `python -c "from hermes_sts.llm import BaseOpenAIChatProvider; p = BaseOpenAIChatProvider.from_settings() if hasattr(BaseOpenAIChatProvider,'from_settings') else None; assert hasattr(p,'archive_current_conversation')"`
  - [ ] 旧测 `python -m unittest discover tests` 仍全绿（store=None 时行为零变化）

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 写穿在 conversation_messages 留痕
    Tool: Bash (python + sqlite3)
    Preconditions: server 启动；T1/T2/T4 已合并
    Steps:
      1. 启动 hermes-sts-server
      2. 用 python WS 脚本或 curl 凑出一条 turn：导入 tests/test_realtime_memory.py 风格发 PCM 包到 ws://127.0.0.1:8765/v1/realtime，触发 VAD→ASR→LLM→TTS
      3. sqlite3 data/hermes_sts.sqlite3 "SELECT role,content FROM conversation_messages JOIN conversations USING(conversation_id) WHERE status='active' ORDER BY seq"
      4. Assert: 输出至少 2 行（user + assistant）
    Expected Result: 数据库已落 1 行 user + 1 行 assistant 对应当前 turn
    Failure Indicators: 表为空 / 仅 1 行 / 多余角色（assistant+tool）
    Evidence: .sisyphus/evidence/task-4-write-through.txt

  Scenario: body 含 user=conversation_id
    Tool: Bash (python with monkeypatched httpx)
    Preconditions: T4 已合并
    Steps:
      1. python -c "
         import asyncio, httpx, hermes_sts.llm as L
         from hermes_sts.config import Settings
         captured = {}
         orig = httpx.AsyncClient
         class FakeResp:
             def json(self): return {'choices':[{'message':{'content':'ok'}}]}
             def raise_for_status(self): pass
         class FakeClient:
             def __init__(self,*a,**k): pass
             async def post(self, url, json=None, **k):
                 captured['body']=json
                 return FakeResp()
             async def __aenter__(self): return self
             async def __aexit__(self,*a): pass
         httpx.AsyncClient = FakeClient
         try:
             from hermes_sts.llm import BaseOpenAIChatProvider
             p = BaseOpenAIChatProvider(Settings())
             asyncio.run(p.chat('hi', conversation_id='conv_test123'))
         finally:
             httpx.AsyncClient = orig
         assert captured.get('body',{}).get('user')=='conv_test123', captured
         print('OK')"
      2. Assert stdout contains 'OK'
    Expected Result: body['user'] == 'conv_test123'
    Failure Indicators: KeyError 'user' 或值不符
    Evidence: .sisyphus/evidence/task-4-body-user-field.txt

  Scenario: idle reset 触发 archive（而非裸 clear）
    Tool: Bash (python + sqlite3)
    Preconditions: 设 sts_history_idle_reset_seconds=2 临时启动服务
    Steps:
      1. 启动 server，发 1 turn 让 active conv 有 2 条消息
      2. sleep 3 触发 idle；再发 1 turn
      3. sqlite3 data/hermes_sts.sqlite3 "SELECT status,count(*) FROM conversations GROUP BY status"
      4. Assert: 'archived' 至少 1 条，'active' 至少 1 条
      5. SELECT count(*) FROM conversation_messages WHERE conversation_id IN (SELECT id FROM conversations WHERE status='archived') >=2
    Expected Result: 第一条 active 被 archive，新 turn 进入新 active
    Failure Indicators: 第一条 active 状态仍为 'active'（空闲未触发归档）
    Evidence: .sisyphus/evidence/task-4-idle-archive.txt

  Scenario: turn_gate 临界区保护写穿（无交错）
    Tool: Bash (python)
    Preconditions: 并发 2 个 WS 脚本同时发 turn
    Steps:
      1. 启动两个 python WS 客户端，各发一条 turn
      2. SELECT seq, role, conversation_id FROM conversation_messages ORDER BY conversation_id, seq
      3. Assert: 每条 conversation 内 seq 连续递增无重复（不会 1,1,2 / 1,2,2），user/assistant 成对出现
    Expected Result: 2 条 active 会话同一 conversation_id 下 4 条消息 (user/assistant/user/assistant)，seq 1..4 严格递增
    Failure Indicators: 出现重复 seq 或交织错位
    Evidence: .sisyphus/evidence/task-4-concurrent-write.txt
  ```

  **Commit**: YES — `feat(llm): write-through history + unified archive_conversation()`
  Files: `hermes_sts/llm.py`, `hermes_sts/realtime.py`（只 ensure_active_conversation + chat 调用传参）
  Pre-commit: `python -m unittest discover tests`

- [x] 5. server.py 启动智能 reload（idle 边界判定 + 透明续接）

  **What to do**:
  - 在 `server.py:*lifespan*` startup 钩子（若不存在则新增 `@asynccontextmanager async def lifespan(app)`），在 `_build_components(app)` 创建 LLM 之后 await 一个新的 `await _wire_conversation_store(app)` 异步函数。
  - `_wire_conversation_store(app)` 实现：
    ```python
    async def _wire_conversation_store(app: FastAPI) -> None:
        settings = app.state.settings
        if not settings.sts_conversations_enabled:
            return
        store = ConversationStore(db_path=settings.sts_conversations_db_path)
        await store._ensure_tables()
        app.state.conversation_store = store
        app.state.llm.conversation_store = store

        # 启动时 idle 边界判定（核心：重启 ≠ 会话边界，除非确实超过 idle）
        archived = await store.maybe_archive_on_idle(
            idle_threshold_seconds=settings.hermes_history_idle_reset_seconds
        )
        active = await store.get_active_conversation()
        if active is None:
            app.state.llm.conversation_id = None  # 首句时 ensure_active_conversation 会创建
        else:
            await store.reload_history_into(
                active['id'], app.state.llm,
                max_messages=settings.sts_conversations_reload_max_messages,
            )
            app.state.llm.conversation_id = active['id']
        # 防御：无论分支，都重置 idle 计时，避免刚启动误触发 idle archive
        app.state.llm.last_llm_call_started_at = time.monotonic()
        logger.info("Conversation store wired: archived_on_start=%s active=%s",
                    archived, app.state.llm.conversation_id)
    ```
    **关键点**：
      - 重启时 **不直接 archive**；由 `maybe_archive_on_idle` 决定（若 `last_updated` 距今 ≥ idle_threshold（默认 6h），才 archive；否则直接续接）
      - 若 idle_threshold=0（禁用自动归档），`maybe_archive_on_idle` 直接返回 False——history 永久续接，仅靠用户手动 End 切换（贴合"自然续接"哲学）
      - 若 active 存在但本次启动判定 idle 命中 → 旧 active 已被 `maybe_archive_on_idle` 改为 archived；此时 `get_active_conversation()` 返回 None，自动等首句重建——流程闭环
  - lifespan shutdown 钩子里关闭 store 连接（`await store.close()` 若 ConversationStore 提供）；close 应当幂等
  - **不**向 `_requires_rebuild` 加入任何 conversation key——本任务改动属于旧设置范畴内的"行为调优"，不触发 `systemctl restart`。同时 IdS 默认值改变（4h → 6h）也不进 rebuild 集——idle 阈值仅影响运行时判定，不需要重建组件

  **Must NOT do**:
  - 不持久化 tool_followup / 在线 turn 状态
  - 不向 `_requires_rebuild` 添加 conversation 相关 keys
  - 不阻塞 lifespan 超过 1s（同文件 SQLite + 至多一两条 active → 收选是毫秒级）
  - 不在启动时无条件 archive active（必须是 idle 已到期才 archive）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: lifespan 异步钩子 + 跨模块 wiring + idle 边界判定语义敏感
  - **Skills**: []

  **Parallelization**:
  - Can Run In Parallel: YES（同 Wave 2 与 T4/T6/T7），但 T5 真正 wire 需要 T4 的 `conversation_store`/`conversation_id` 字段及 `ensure_active_conversation` 已存在；orchestrator 调度时优先排 T4 → T5
  - Parallel Group: Wave 2
  - Blocks: T9
  - Blocked By: T1（store + maybe_archive_on_idle API），T2（Protocol），T4（LLM provider 属性）

  **References**:

  **Pattern References**:
  - `hermes_sts/server.py:create_app/lifespan` 现有实现 —— 不存在则新增 `@asynccontextmanager async def lifespan(app)`
  - `hermes_sts/memory.py` 模块初始化/注册到 app.state 的范式 —— memory 是如何在 _build_components 中 wire 到 app.state 的，按对应 style 完成 conversation_store 的 wire

  **API/Type References**:
  - `hermes_sts/conversation_store.py:ConversationStore` —— 主要消费 `maybe_archive_on_idle`、`get_active_conversation`、`reload_history_into`、`_ensure_tables`、`close`（后者如未在 T1 实现，则 T5 在 store 上加一个 close 方法）
  - `hermes_sts/llm.py:BaseOpenAIChatProvider.conversation_store / conversation_id / last_llm_call_started_at / history`（T4 引入）

  **WHY Each Reference Matters**:
  - 启动只调三个 store 方法就完成"透明续接 + idle 边界"判定；不符合桌面助手朴素直觉的做法（一律 archive）曾被要求避雷

  **Acceptance Criteria**:
  - [ ] 启动后 `curl http://127.0.0.1:8765/api/conversations/active` 返回 200 且 `id` 字段为 str（旧 active 续接）或 null（首次/idle 已 archive 情形）
  - [ ] 启动后 `python -c "from hermes_sts.server import app; llm=app.state.llm; assert llm.last_llm_call_started_at is not None"`
  - [ ] 启动日志中可 grep `archived_on_start=True/False` 与 `active=<...|None>`

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 重启透明续接同一 conversation（关键新约束）
    Tool: Bash (systemctl + python WS 脚本 + curl + sqlite3)
    Preconditions: 服务中已有 4 轮 (active conv X, 8 条 messages)；距离 last_updated 仅 30 秒
    Steps:
      1. old_active=$(curl -s http://127.0.0.1:8765/api/conversations/active | jq -r .id)
      2. systemctl --user restart hermes-sts-server.service ; sleep 3
      3. new_active=$(curl -s http://127.0.0.1:8765/api/conversations/active | jq -r .id)
      4. Assert: "${new_active}" == "${old_active}"
      5. sqlite3 data/hermes_sts.sqlite3 "SELECT status FROM conversations WHERE id='${old_active}'" → Assert: 'active' (NOT 'archived')
      6. python -c "from hermes_sts.server import app; print(len(app.state.llm.history))"
      7. Assert: stdout == '8'
      8. python -c "from hermes_sts.server import app; assert app.state.llm.last_llm_call_started_at>0; print('ok')"
      9. Assert: stdout contains 'ok'
    Expected Result: 重启后续接同一 conversation；history reload 到原 8 条；last_llm_call_started_at 重置；老 active status 保持 'active'（未被 archive）
    Failure Indicators: new_active 与 old_active 不同；旧 active status='archived'；history 为空；last_llm_call_started_at is None；启动后立即被 idle 误 archive
    Evidence: .sisyphus/evidence/task-5-restart-transparent.txt

  Scenario: 启动时 idle 已超阈值 → 自动 archive + 等首句新建
    Tool: Bash (sqlite3 + systemctl + curl)
    Preconditions: 临时把 idle_threshold 调到 2 秒；DB 里有 active 但 last_updated 已经 5 秒前
    Steps:
      1. 用 sqlite3 把现有 active conv 的 updated_at 改成 5 秒前的 epoch (`UPDATE conversations SET updated_at = strftime('%s','now')-5 WHERE status='active'`)
      2. 用 /api/settings 把 hermes_history_idle_reset_seconds=2 持久化（不改 _requires_rebuild）
      3. systemctl --user restart hermes-sts-server.service ; sleep 3
      4. curl -s http://127.0.0.1:8765/api/conversations/active | jq -r .id ; Assert: null
      5. sqlite3 data/hermes_sts.sqlite3 "SELECT status FROM conversations WHERE id='<原 active id>'" → Assert: 'archived'
      6. WS 发 1 turn → 再 curl /api/conversations/active → Assert: 新 conv_id 存在
    Expected Result: 启动判定 distance ≥ threshold → archive 旧 active；首句触发新 active
    Failure Indicators: 旧 active 仍 'active'；新 turn 没有创建新 conv
    Evidence: .sisyphus/evidence/task-5-startup-idle-archive.txt

  Scenario: idle disabled (idle_threshold=0) → 永不被自动 archive
    Tool: Bash (curl + sqlite3)
    Preconditions: hermes_history_idle_reset_seconds=0（已 disable）
    Steps:
      1. 设 0 → /api/settings 持久化
      2. 服务中已有 active conv X
      3. 用 sqlite3 把 X 的 updated_at 改成 7 天前（强制 stale）
      4. systemctl --user restart ; sleep 3
      5. Assert: GET /api/conversations/active 仍返回 id=X (没被 archive)
      6. Assert history reload 成功（X 的 messages 全部 reload）
    Expected Result: idle 禁用时彻底不归档，无视 stale；纯人工 End 路径才 archive
    Failure Indicators: 旧 active 自动变 archived（与"永不"约束违背）
    Evidence: .sisyphus/evidence/task-5-idle-disabled.txt

  Scenario: 首次启动无 active 也能正常运转
    Tool: Bash (rm + systemctl + curl)
    Preconditions: data/hermes_sts.sqlite3 不存在
    Steps:
      1. rm -f data/hermes_sts.sqlite3 data/hermes_sts.sqlite3-*
      2. systemctl --user restart hermes-sts-server.service; sleep 3
      3. curl -s http://127.0.0.1:8765/api/conversations/active ; 200, body = {"id": null}
      4. WS 发 1 turn → again curl /api/conversations/active ; Assert: id 形如 conv_<uuid>
    Expected Result: 首次启动空状态 → 首句触发 store.ensure_active_conversation 创建新 active
    Failure Indicators: 启动报 500；首句不发起新 conv；自动 archive 不存在的 active 报错
    Evidence: .sisyphus/evidence/task-5-first-start.txt
  ```

  **Commit**: YES — `feat(server): transparent reconnect + idle-boundary archive on startup`
  Files: `hermes_sts/server.py`，可能 `hermes_sts/conversation_store.py` 追加 `close()` 方法（若 T1 未提供）
  Pre-commit: `python -c "import hermes_sts.server"`

- [x] 6. admin.py 新增 conversation REST 端点（收窄）+ 复用旧 reset

  **What to do**:
  - 在 `hermes_sts/admin.py` 的 `create_admin_router()` 内新增**仅以下**公开路由：
    - `GET  /api/conversations/active` → `app.state.conversation_store.get_active_conversation()`；若 None 返回 `{"id": null}`；**never 404**
    - `GET  /api/conversations?status=&limit=10&offset=0` → `list_conversations(...)`，每项含 `id/status/title/message_count/created_at/updated_at/ended_at/ended_reason`；**只读**
    - `POST /api/conversations/end` → **`async with app.state.turn_gate:`** 内：
      1. `current = await store.get_active_conversation()`
      2. 若 `current` 为 None → 返回 200 `{"id": null, "archived": false}`（无操作幂等）
      3. 否则 `await app.state.llm.archive_current_conversation("admin_end")`（T4 实现，内部 archive + 清 history + 把 `conversation_id` 置 None）
      4. 调 `await store.create_conversation()`（这样新 active 立即可用）；赋值 `app.state.llm.conversation_id = new_id`；调 `store.reload_history_into(new_id, llm)`（重置 self.history 空列表 + last_llm_call_started_at）
      5. 返回新 active 信息 `{"id": new_id, "archived": true, "previous_id": current["id"]}`
  - **改造现有 `POST /api/llm/context/reset`（admin.py:259-266）**：完全等价于 `POST /api/conversations/end` 的实现（共享同一 helper），URL/method/response schema 保持向后兼容。T6 仅在 handler 内调同一 archive + new 实现路径，不增加独立 wrapper 调用——T4 已让 `llm.reset_history("admin")` 在有 store 时内部走 archive 路径，但 `/api/conversations/end` 的"开新 active + wire" 需要本任务显式完成（因为 reset_history 只清不 create）。**最终行为**：`POST /api/llm/context/reset` 与 `POST /api/conversations/end` 返回结构完全等同
  - 所有端点复用既有 admin 路由的 `Depends(get_settings)` / 错误模型模式
  - **本任务**也向 `ConversationStore` 加一个 `close()` 方法若 T1/T5 未提供（用于 lifespan shutdown）：仅 `self._conn.close()` 幂等

  **Must NOT do**:
  - **不**实现 `/api/conversations/{id}/resume`、`PATCH /api/conversations/{id}`、`GET /api/conversations/{id}`、`GET /api/conversations/{id}/messages`——这些会被"主动管理"诱惑回来；本轮 UI 简化后不再需要公开
  - 不在 `_requires_rebuild` 加入任何 conversation key
  - 不新增任何 systemctl restart 路径
  - 不在 admin_ui 里加任何 UI（T8 负责）
  - 不修改 `/api/llm/context/reset` 的请求 / 响应 schema（仅内部行为变成 archive + new）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: 单文件收窄端点 + turn_gate 互斥
  - **Skills**: []

  **Parallelization**:
  - Can Run In Parallel: YES
  - Parallel Group: Wave 2（与 T4/T5/T7 同）
  - Blocks: T8, T9
  - Blocked By: T1（store API），T4（archive_current_conversation helper 必须存在）

  **References**:

  **Pattern References**:
  - `hermes_sts/admin.py:create_admin_router` 当前实现 —— 路由风格、Depends 注入、错误模型
  - `hermes_sts/admin.py:259-266 /api/llm/context/reset` —— 复用此位置
  - `hermes_sts/server.py:app.state.turn_gate` —— End 端点必须在 `async with turn_gate:` 内执行 archive + new

  **API/Type References**:
  - `hermes_sts/conversation_store.py:ConversationStore` 完整公开 API（特别是 `create_conversation`、`archive_conversation`、`get_active_conversation`、`reload_history_into`）
  - `hermes_sts/llm.py:BaseOpenAIChatProvider.archive_current_conversation`、`reset_history`、`conversation_id`、`history`（T4 引入）

  **WHY Each Reference Matters**:
  - turn_gate 互斥是关键：End 期间若有在飞 turn，archive 历史 + 新建 active 可能与 in-flight append 冲突；临界区串行化才能保证 atomic
  - "`/reset` 与 `/end` 等价" 出于向后兼容：admin_ui 已存在的"reset context" 按钮无需改 endpoint，只是结果变成 archive + new 而非裸 clear

  **Acceptance Criteria**:
  - [ ] `curl /api/conversations` 返回 list JSON（每条含必要字段）
  - [ ] `curl /api/conversations/active` 在有 active 时返回 id；无 active 时返回 `{"id": null}` 不 404
  - [ ] `curl -X POST /api/conversations/end` → 返回新 active id；旧 active 在 DB 中变为 'archived'
  - [ ] `curl -X POST /api/llm/context/reset` → 行为与 `/end` 等价（响应 schema 相同）
  - [ ] 并发发起 End 与在飞 turn 时无 history 污染（turn_gate 保护）

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: End 端点切换 active
    Tool: Bash (curl + jq + sqlite3)
    Preconditions: 服务中已发过 2 turn → active X 有 4 条 messages
    Steps:
      1. before=$(curl -s http://127.0.0.1:8765/api/conversations/active | jq -r .id)
      2. resp=$(curl -s -X POST http://127.0.0.1:8765/api/conversations/end) ; echo "$resp"
      3. after=$(echo "$resp" | jq -r .id) ; archived=$(echo "$resp" | jq -r .archived) ; prev=$(echo "$resp" | jq -r .previous_id)
      4. Assert: archived == "true" && prev == "$before" && "$after" != "$before" && "$after" != "null"
      5. sqlite3 data/hermes_sts.sqlite3 "SELECT status FROM conversations WHERE id='${before}'" → Assert: 'archived'
      6. sqlite3 data/hermes_sts.sqlite3 "SELECT status FROM conversations WHERE id='${after}'" → Assert: 'active'
    Expected Result: End 端点同时 archive 旧 + 创建新 active；返回新信息
    Failure Indicators: 旧 active 仍 active；新 active 为 null；return 缺字段
    Evidence: .sisyphus/evidence/task-6-end-switch.txt

  Scenario: /api/llm/context/reset 与 /api/conversations/end 等价
    Tool: Bash (curl + sqlite3)
    Preconditions: active X 有 2 条 messages
    Steps:
      1. before1=$(curl -s /api/conversations/active | jq -r .id)
      2. resp1=$(curl -s -X POST /api/llm/context/reset) ; echo "$resp1"
      3. after1=$(echo "$resp1" | jq -r .id) ; Assert: "$after1" != "$before1" && "$after1" != "null"
      4. sqlite3 data/hermes_sts.sqlite3 "SELECT status FROM conversations WHERE id='${before1}'" → Assert: 'archived'
      5. curl -s -X POST /api/conversations/end | jq -r .id  # End 再调一次确保两者都用同一 helper
      6. python -c "from hermes_sts.server import app; assert len(app.state.llm.history)==0; print('ok')"
      7. Assert stdout contains 'ok'
    Expected Result: /reset 与 /end 都将旧 active archive + 新建 active；自 End 后 history 重置为空（因为新 active 还没消息）
    Failure Indicators: /reset 仍把 history 裸 clear 但旧 active 没 archive；/end 与 /reset 行为不一致
    Evidence: .sisyphus/evidence/task-6-reset-compatible.txt

  Scenario: active 为 null 时 End 端点幂等无报错
    Tool: Bash (curl)
    Preconditions: DB 中 active 已为 null（首次启动 + 还没首句）
    Steps:
      1. curl -s -X POST http://127.0.0.1:8765/api/conversations/end ; Assert: 200, body {"id": null, "archived": false, "previous_id": null} 或 schema 等同表示"无 active 被归档"
      2. 再次 curl 同一端点；Assert: 200 同上（幂等）
    Expected Result: 无 active 时调用 End 不会报错；不创建新 active（避免空对话也建 conv 之浪费）
    Failure Indicators: 200 但创建了多余新 active；或 500 错误
    Evidence: .sisyphus/evidence/task-6-end-idempotent.txt

  Scenario: 列表只读，不支持 PATCH/resume 等已删端点
    Tool: Bash (curl)
    Preconditions: 客户端能 try PATCH /api/conversations/xxx 或 POST /api/conversations/xxx/resume
    Steps:
      1. curl -s -o /dev/null -w "%{http_code}" -X PATCH http://127.0.0.1:8765/api/conversations/conv_test123 -d '{"title":"x"}'
      2. Assert: 404 (or 405; endpoint not registered)
      3. curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:8765/api/conversations/conv_test123/resume
      4. Assert: 404 (or 405)
    Expected Result: 已删端点 genuinely 不存在，回归 404/405 而非 200
    Failure Indicators: 任何 PATCH/resume 端点仍存在
    Evidence: .sisyphus/evidence/task-6-removed-endpoints.txt
  ```

  **Commit**: YES — `feat(admin): conversation REST endpoints (active/list/end + reset repurpose)`
  Files: `hermes_sts/admin.py`，可能 `hermes_sts/conversation_store.py`（追加 `close()` 方法）
  Pre-commit: `python -c "import hermes_sts.admin"`

- [x] 7. tests/test_llm_user_field.py — body 携带 user 字段

  **What to do**:
  - 新建 `tests/test_llm_user_field.py`
  - 仿 `tests/test_core.py` 风格：`class TestUserField(unittest.TestCase)`
  - 实现 `test_chat_includes_user_in_body_when_conversation_id_set`：monkeypatch `httpx.AsyncClient` 拦截 body（参考本计划 T4 QA Scenario 中的脚本结构）；断言 `body["user"] == "conv_test123"`
  - `test_chat_omits_user_when_none`：保证传 conversation_id=None 时无 user 字段（不破坏现有协议契约）
  - 至少 2 个 test method

  **Must NOT do**:
  - 不引入 mock 库——手工 monkeypatch 即可
  - 不实际访问真实 LLM base_url

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 单文件 2 个测，模式已在 T4 QA 中验证
  - **Skills**: []

  **Parallelization**:
  - Can Run In Parallel: YES
  - Parallel Group: Wave 2（与 T4-T6 同；T7 仅依赖 T2 Protocol 签名）
  - Blocks: F1
  - Blocked By: T2

  **References**:

  **Pattern References**:
  - 本 plan Task 4 的 QA Scenario: body 含 user=conversation_id （脚本可直接接入）
  - `tests/test_core.py` 文件骨架

  **API/Type References**:
  - `BaseOpenAIChatProvider.chat(conversation_id=...)`
  - `hermes_sts.config.Settings`（构造一个最小可用 Settings）

  **WHY Each Reference Matters**:
  - T4 QA 已把脚本写好；本任务是把它写成持久测

  **Acceptance Criteria**:
  - [ ] `python -m unittest tests.test_llm_user_field -v` 2 个测全绿

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 2 个 unittest 通过
    Tool: Bash (python)
    Preconditions: T2 已 merge
    Steps:
      1. python -m unittest tests.test_llm_user_field -v
      2. Assert: 'OK' 且 'ran 2 tests'
    Expected Result: 两个测全过
    Failure Indicators: 任一 failed/error，说明 body 拼装与预期不符
    Evidence: .sisyphus/evidence/task-7-user-field-unit.txt
  ```

  **Commit**: YES — `test(llm): assert user field carries conversation_id`
  Files: `tests/test_llm_user_field.py`
  Pre-commit: `python -m unittest tests.test_llm_user_field`

- [x] 8. admin_ui/src/main.tsx — ConversationCard + End 按钮 + 折叠只读 Recent

  **What to do**:
  - 在 `admin_ui/src/main.tsx` **同一文件**新增 React 组件（不拆文件，仿 `MemoryPanel` 风格）：
    - `ConversationCard`：小型卡片，~120 行内
      - 主区域渲染当前 active conversation：title（首句截断）、消息条数、created_at、last_activity（updated_at 相对时间）
      - 一个按钮：`End & Start New`（点击 POST `/api/conversations/end`；成功后刷新本地 active 状态 + 弹出 toast/notification "已结束旧对话，开启新对话"）
      - 状态为 `{"id": null}` 时显示 "尚未开始对话" 占位 + 一个禁用按钮（或 `End` 按钮灰显）
    - `RecentConversations` 折叠只读：默认折叠，点击展开后 GET `/api/conversations?limit=10`，渲染一行一条：`title | started | ended | N messages`——**无链接、无按钮、无编辑**。仅文本展示，鼠标 hover 显示 ended_reason
  - 在主 tab 栏新增一个 tab：`Conversation`（在已有 Memory tab 之前或之后），点击后渲染 `ConversationCard` + 折叠 `RecentConversations`
  - 复用既有 `fetchJSON` helper 实现 GET / POST；复用既有 `useState` 轮询模式（15s polling 同步 active 状态）——避免再写独立 polling 逻辑
  - tsx 不引入新依赖；不使用 localStorage（保持项目无 localStorage 风格）
  - 不出现 `as any` / `@ts-ignore` / 复杂泛型套路；prop types 简单内联即可
  - 完成后 `cd admin_ui && npm run build` 应成功；产物落 `admin_ui/dist/`

  **Must NOT do**:
  - 不实现"调阅消息""改标题""续聊按钮""删除会话"——这些在 Round 2 设计哲学明确剔除
  - 不拆分 `admin_ui/src/main.tsx`——一个文件搞定
  - 不引入新 npm 依赖（react-router、zustand、swr 都是雷区，禁用）
  - 不在 RecentConversations 行加任何 onClick 跳转或编辑——明确只读
  - 不暴露 conversation_id 的完整 UUID 给用户看（如需显示就用 `conv_…8 chars…` 缩写或干脆不显示 ID）

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 单文件 ~120 行新增；仿 MemoryPanel 范式；无新依赖；纯 React `useState + fetch + 简单 JSX`
  - **Skills**: [`frontend-design`]（可选）——用于给个小卡片视觉骨架
  - **Skills Evaluated but Omitted**:
    - `playwright`: 不在实现阶段；playwright 用于 QA 阶段
    - `remotion-best-practices`: 与视频无关

  **Parallelization**:
  - Can Run In Parallel: YES
  - Parallel Group: Wave 3（with T9）
  - Blocks: F1, F3
  - Blocked By: T6（依赖 `/api/conversations/active` 与 `/api/conversations/end` 端点存在）

  **References**:

  **Pattern References**:
  - `admin_ui/src/main.tsx:MemoryPanel` 组件结构 —— 整体 React 卡片结构、tab registry、fetch 范式
  - `admin_ui/src/main.tsx` `fetchJSON` helper 与 toast 组件（若有）

  **API/Type References**:
  - 后端公开契约：`GET /api/conversations/active` → `{id, title, message_count, created_at, updated_at}` 或 `{id: null}`
  - `POST /api/conversations/end` → `{id, archived, previous_id}` 或 `{id: null, archived: false, previous_id: null}`
  - `GET /api/conversations?limit=10` → `list[{id, status, title, message_count, created_at, updated_at, ended_at, ended_reason}]`

  **External References**:
  - React 19 useState 基础（无新知识）

  **WHY Each Reference Matters**:
  - MemoryPanel 是项目既有最相近的小型卡片 + 列表面板范式，整套照搬省时；同时与项目视觉一致

  **Acceptance Criteria**:
  - [ ] `cd admin_ui && npm run build` 退出码 0，无 TypeScript 报错
  - [ ] `npm run build` 输出 `admin_ui/dist/index.html` 等静态文件
  - [ ] Accessibility sanity：`<button>` 都有可读的 aria-label 或可见文字

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: UI 卡片显示当前会话 + End 按钮可用
    Tool: Playwright (playwright skill)
    Preconditions: server 运行；DB 中已有 active conversation（发过至少 1 turn）
    Steps:
      1. await page.goto('http://127.0.0.1:8765/')
      2. await page.getByRole('tab', { name: /Conversation/ }).click()
      3. 等待卡片渲染完成：await expect(page.getByText(/End & Start New/)).toBeVisible()
      4. await expect(page.getByText(/消息数|messages/i)).toBeVisible()
      5. 记录原 active_id 文本（卡片中如有显示）
      6. await page.getByRole('button', { name: /End & Start New/ }).click()
      7. await expect(page.getByText(/已结束|开启新/i)).toBeVisible({ timeout: 5000 })
      8. 校验后端已被切换：let new_active = await fetch('/api/conversations/active').then(r=>r.json()); assert new_active.id != null
    Expected Result: 卡片正确展示 active；End 按钮可调端点切换 active；UI 有反馈
    Failure Indicators: tab 不存在；按钮不可见或 disabled 异常；点击无反响或返回 500
    Evidence: .sisyphus/evidence/task-8-card-end.pnz (Playwright 截图)

  Scenario: 折叠只读 Recent 展开
    Tool: Playwright
    Preconditions: DB 中已有 2+ 条 archived
    Steps:
      1. await page.goto('http://127.0.0.1:8765/'); 进入 Conversation tab
      2. await page.getByText(/Recent|历史/i).click() —— 折叠展开
      3. await expect(page.locator('[data-testid="recent-row"]').first()).toBeVisible({ timeout: 4000 })
      4. let rows = await page.locator('[data-testid="recent-row"]').count()
      5. Assert: rows >= 2
      6. 验证每一行没有任何 button / link：待`[data-testid="recent-row"] button` count === 0
    Expected Result: Recent 折叠展开后能看到 N 行只读列表，行内没有任何交互元素
    Failure Indicators: 行内出现 button/link/可点击元素；展开后 fetch 失败；行少于 DB 实际数
    Evidence: .sisyphus/evidence/task-8-recent-readonly.png

  Scenario: 无 active 时空状态文案 + 按钮灰显
    Tool: Playwright
    Preconditions: 首次启动还没发过 turn → /api/conversations/active 返回 {id: null}
    Steps:
      1. await page.goto('http://127.0.0.1:8765/'); 进入 Conversation tab
      2. await expect(page.getByText(/尚未开始/i)).toBeVisible()
      3. await expect(page.getByRole('button', { name: /End & Start New/ })).toBeDisabled()
    Expected Result: 空状态明确，按钮禁用避免误触产生空 End
    Failure Indicators: 卡片显示 active id 为 'null' 字串；按钮没禁用导致可点击产生多余空 active
    Evidence: .sisyphus/evidence/task-8-empty-state.png
  ```

  **Commit**: YES — `feat(ui): add ConversationCard with end-and-new + read-only recent`
  Files: `admin_ui/src/main.tsx`, `admin_ui/dist/*` (build 产物)
  Pre-commit: `cd admin_ui && npm run build`

- [x] 9. tests/test_conversation_store.py 全量补完（含重启不 archive、End 切换、并发）

  **What to do**:
  - 把 T3 提交的 9 个占位 test 方法**全数填充真实断言**（unittest.TestCase + asyncio.run）：
    1. `test_create_and_get_active`：创建 → get_active 同 id；再 create → 自动 archive 第一条 → get_active 返回新 id 且旧 status='archived'
    2. `test_append_message_seq_increments`：append 3 条 → seq 严格 1,2,3；conversations.updated_at 单调递增
    3. `test_archive_sets_ended`：archive('test') → status='archived'，ended_at 非 null，ended_reason='test'
    4. `test_reload_history_into_overwrites`：模拟两种历史（A 与 B），reload A 后 `llm.history` 长 = A 长度，且 `last_llm_call_started_at` 不为 None；reload 空 conv 后 `llm.history == []`
    5. `test_maybe_archive_on_idle_within_threshold_keeps_active`：active 更新时间 5s 前；threshold=10 → within_threshold；调用后 active 仍 active；返回 False
    6. `test_maybe_archive_on_idle_over_threshold_archives`：updated_at 100s 前；threshold=10 → 触发 archive；active 变 archived；返回 True
    7. `test_maybe_archive_on_idle_disabled_never_archives`：threshold=0；updated_at 7_DAY 旧；返回 False；active 状态保持
    8. `test_concurrent_append_within_lock`：用 `asyncio.gather` 同时 append 5 条到同一 conv → seq 严格不重复、连续 1..5
    9. `test_wal_journal_mode_pragma`：连接后 `PRAGMA journal_mode` 返回 'wal'
  - 每个方法的 store 实例应在 `tmp_path / "conv_test.sqlite3"` 上构造（参考 `tests/test_realtime_memory.py` 的 `tmp_path` 用法）；每个 test 之间互不影响数据
  - 若需要 LLM stub，沿用 `tests/test_core.py:DummyChatProvider` ；只需 `self.history` 与 `self.last_llm_call_started_at` 两个属性即可——可在 TEST 内 inline 一个 fake class
  - 引入 `unittest.mock.AsyncMock` 仅用于 avoid 真实 LLM —— 不引入第三方 mock 库

  **Must NOT do**:
  - 不引入 pytest / aiosqlite / 任何外部 mock 库（仅 stdlib）
  - 不持久化任何 tool_followup 测试用例（v1 不记录）
  - 不写"刻意测一个已删 resume 端点"的测试（resume 已被剔除）
  - 不再写"重启自动 archive"的测试（与新设计冲突——重启已不再 archive）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: 单文件 9 个真测，覆盖并发/idle 边界/SQL 行为细节；测试要稳，需小心
  - **Skills**: []

  **Parallelization**:
  - Can Run In Parallel: YES（与 T8 同 Wave 3）
  - Parallel Group: Wave 3
  - Blocks: F1, F2
  - Blocked By: T1（store API）、T4（写穿行为）、T5（启动 reload 实现）、T6（端点行为）

  **References**:

  **Pattern References**:
  - `tests/test_core.py:26-46 DummyChatProvider` —— LLM stub 范式
  - `tests/test_realtime_memory.py` —— 用 `tmp_path` 在 sqlite 上做真测的范式

  **API/Type References**:
  - `hermes_sts/conversation_store.py:ConversationStore`（T1 产出的 9 个方法）
  - `ConversationStore.maybe_archive_on_idle` 的新语义是本轮 T5 启动逻辑的关键依赖，单测要充分覆盖

  **WHY Each Reference Matters**:
  - maybe_archive_on_idle 是新设计哲学的核心函数；任何误归档或漏归档都会破坏"重启透明续接"的承诺——单元覆盖三档边界
  - 并发临界区由 ConversationStore 内部 `threading.Lock` 保证；单元并发测能验证 seq 严格递增

  **Acceptance Criteria**:
  - [ ] `python -m unittest tests.test_conversation_store -v` 跑出 0 failed / 0 errored / 0 skipped（9 个测试全过）
  - [ ] 任何单独 `python -m unittest tests.test_conversation_store.TestConversationStore.test_maybe_archive_on_idle_disabled_never_archives` 也能单独跑通

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 9 个测试全绿
    Tool: Bash (python)
    Preconditions: T1/T4/T5/T6 已合入；T9 完成
    Steps:
      1. python -m unittest tests.test_conversation_store -v 2>&1 | tail -20
      2. Assert: 输出含有 'OK'，并 'ran 9 tests' 或等同
      3. Assert: 不含 'skipped'
      4. Assert: 不含 'FAIL' 或 'ERROR'
    Expected Result: 9 个测试全过；零 skip；零 failure
    Failure Indicators: 任一测试 failed/error/skipped
    Evidence: .sisyphus/evidence/task-9-all-tests.txt

  Scenario: 与既有测试一并跑也不破坏
    Tool: Bash (python)
    Preconditions: 既有 tests/test_core.py、tests/test_llm_user_field.py 等
    Steps:
      1. python -m unittest discover tests -v 2>&1 | tail
      2. Assert: 全绿（含 T7 的 user field 测）
    Expected Result: 整套 tests 都过
    Failure Indicators: 任一测失败（多半是 store 单例被污染或 import order 问题）
    Evidence: .sisyphus/evidence/task-9-full-discover.txt
  ```

  **Commit**: YES — `test(store): unittest suite for conversation persistence`
  Files: `tests/test_conversation_store.py` （把 T3 占位填充为真测）
  Pre-commit: `python -m unittest tests.test_conversation_store -v`

---

## Final Verification Wave (MANDATORY — after ALL implementation tasks)

> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> Never mark F1-F4 as checked before getting user's okay. Rejection/feedback → fix → re-run → present again → wait for okay.

- [x] F1. **Plan Compliance Audit** — `oracle`
  Read plan end-to-end. 对每条 "Must Have" 验证实现存在（read file / curl endpoint / run command）；对每条 "Must NOT Have" 在 codebase 搜禁用模式 — 命中即列入 file:line 拒绝。检查 `.sisyphus/evidence/` 文件存在。对比 deliverables vs diff。
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [x] F2. **Code Quality Review** — `unspecified-high`
  跑 `python -m unittest discover` + `mypy`（如已配置）+ `ruff`（如已配置）。Review 所有变更文件：`as any / @ts-ignore`、空 catch、`print/console.log`、注释掉的代码、未用 imports。AI slop 检查：过度注释、过度抽象、`data/result/item/temp` 这类泛名。
  Output: `Build [PASS/FAIL] | Lint [PASS/FAIL] | Tests [N pass/N fail] | Files [N clean/N issues] | VERDICT`

- [x] F3. **Real Manual QA** — `unspecified-high` (+ `playwright` skill)
  从 clean 状态按 EVERY task 的 QA scenario 执行：跑命令、抓 evidence。测试跨 task 集成（restart + resume + UI list 三者配合）。edge：空 conversation、concurrent WS、idle disabled、resume 抢占 turn。Evidence 落 `.sisyphus/evidence/final-qa/`。
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [x] F4. **Scope Fidelity Check** — `deep`
  对每条 task：读 "What to do"，比 git diff，1:1 核对——计划里的都做了（无缺失），没做计划外的（无 creep）。查 "Must NOT do" 是否遵守。检测 task 间越界（T4 改 T6 的文件）。Flag 未计入的修改。
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT`

---

## Commit Strategy

- **T1**: `feat(store): add ConversationStore with SQLite persistence`
- **T2**: `feat(llm): extend LLMProvider.chat with conversation_id param`
- **T3+T9**: `test(store): unittest suite for conversation persistence`
- **T4**: `feat(llm): write-through history + unified archive_conversation()`
- **T5**: `feat(server): reload active conversation on startup`
- **T6**: `feat(admin): conversation REST endpoints + reset repurpose`
- **T7**: `test(llm): assert user field carries conversation_id`
- **T8**: `feat(ui): add ConversationCard with end-and-new + read-only recent`
- **F1-F4**: review approvals (no commit unless fixups needed)

---

## Success Criteria

### Verification Commands
```bash
python -m unittest discover tests                                            # Expect: OK (>=7 tests)
# 重启不 archive（关键新约束）
mid=$(curl -s http://127.0.0.1:8765/api/conversations/active | jq -r .id)
systemctl --user restart hermes-sts-server.service; sleep 3
curl -s http://127.0.0.1:8765/api/conversations/active | jq -r .id          # Expect: == $mid (同一会话续接)
# IDLE 超时归档 (用极短阈值小范围测)
# Set hermes_history_idle_reset_seconds=2 via /api/settings, send 1 turn, sleep 3, send 1 turn
curl -s http://127.0.0.1:8765/api/conversations | jq '[.[]|.status]|unique' # Expect: ['active', 'archived']
# End 端点切换
curl -s -X POST http://127.0.0.1:8765/api/conversations/end | jq -r .id     # Expect: new conv_...
# 助手端 user 字段（QA 期 mock httpx 拦截 body）
python -c "import sqlite3; c=sqlite3.connect('data/hermes_sts.sqlite3'); \
  print(c.execute('PRAGMA journal_mode').fetchone())"                        # Expect: ('wal',)
```

### Final Checklist
- [ ] All "Must Have" present
- [ ] All "Must NOT Have" absent
- [ ] `python -m unittest discover tests` 全绿
- [ ] 重启后**仍**续聊同一 active conversation（vs 旧方案归入新 active）
- [ ] idle 超时归档 + End 按钮 + `/api/llm/context/reset` 三条路径归同一 archive helper
- [ ] 助手端后台再接请求时能看到 `user=conv_xxx` 一致
- [ ] admin_ui 只显示当前卡片 + 一个 End 按钮 + 折叠只读 Recent，无任何手动续聊/调阅入口
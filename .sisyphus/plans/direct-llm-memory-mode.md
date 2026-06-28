# Direct-LLM + 联网搜索 + 可选 OpenViking 记忆 + UI 记忆管理

## TL;DR

> **Quick Summary**: 在 hermes-sts-server 现有 WebSocket STS 通路基础上，新增一条"直连 LLM + Tavily 联网搜索 + 可选 OpenViking / 默认 SQLite 轻量记忆"模式，与现有 hermes_agent 模式并存（`STS_LLM_PROVIDER` 切换即用）。比 Hermes 小助手更快，又有持久记忆，适合语音场景。
>
> **Deliverables**:
> - `hermes_sts/memory.py` —— `MemoryProvider` Protocol + 3 实现（OpenViking / SQLite fallback / Noop）
> - `hermes_sts/websearch.py` —— `WebSearchProvider` Protocol + Tavily + Noop
> - `hermes_sts/tools.py` 扩展 —— 条件性 `web_search` local 工具工厂
> - `hermes_sts/realtime.py` 最小侵入 —— 两 provider 通用 system prompt 注入 + `record_turn` 后台异步触发
> - `hermes_sts/config.py` + `config_store.py` + `admin.py` —— 30+ 新 settings、CRUD REST、`_requires_rebuild` 切换
> - `admin_ui/src/main.tsx` —— 新增"记忆"面板（列表 / 搜索 / 编辑 / 删除 / 手动添加 / 启用开关 / 活动流）
> - 测试：`tests/test_memory_websearch.py` + 扩展 `tests/test_core.py`，沿用 `DummyChatProvider` / `bare_session` / 内联 fakes 风格
> - README roadmap 勾掉对应条目
>
> **Estimated Effort**: Large
> **Parallel Execution**: YES - 3 waves
> **Critical Path**: Task 1 → Task 6 → Task 8 → Task 11 → Task 12 → F1-F4

---

## Context

### Original Request

当前项目是 reachymini_conversation_app 通过 ws 接口连接的 STS 后端，目前主要走 Hermes agent LLM 路径，但 Hermes 太慢。希望新增一条路径：直接 LLM + 联网 tool + OpenViking 记忆 + 界面添加记忆功能，适合语音场景使用，比小助手快又能有记忆。前提是不干扰现有路径（后续仍可切回 Hermes API）。OpenViking 作为**可选但推荐**的记忆后端；未接入 OpenViking 时要有“还不错”的轻量替代方案。

### Interview Summary

**Key Discussions**:
- 路径并存，`STS_LLM_PROVIDER=openai_compatible | hermes_agent` 切换已存在，新能力叠在 openai_compatible 上
- 搜索 provider：Tavily（按 README roadmap 建议）
- 记忆粒度：全局单用户（不做 profile/user_id 分桶）
- 自动读 + 自动写 + UI 可干预（推荐方案，兼顾语音体验和可控性）
- Hermes 模式：**只读记忆、不写**（仅注入 system prompt，不调用 commit / LLM 抽取）
- OpenViking 形态：本机部署的 HTTP API（127.0.0.1:1933），按官方文档接入，不装 Python SDK
- OpenViking 可选但推荐，未接入时使用 SQLite + FTS5 + LLM-driven 抽取的轻量替代
- UI 范围：用户未明确指定，按使用场景定为"管理面板 + 简化活动流 + 手动添加 + 启用开关"
- 测试：实现后补单测，沿用现有 `tests/test_core.py` 的 `unittest.TestCase` + `DummyChatProvider` + `bare_session` + `asyncio.run()` 风格

**Research Findings**:
- 现有 `llm.py:364-370` 的 `build_llm(settings)` 已按 `STS_LLM_PROVIDER` 分支
- `_system_prompt()` 在 `llm.py:209-221` 会把 instructions 截断到 2500 字符 —— 注入记忆要计字符预算
- `_ask_llm_with_tools()` 在 `realtime.py:647-708` 接受 `instructions: str | None`，传给 `llm.chat()`；3 个 LLM 调用点（657、705、517），所有都接受 `instructions` 参数 —— 注入点选在构造 `instructions` 处
- 共享 `llm` 单例 + 全局 `llm.history`；`session_id` 每 ws 新建 —— 记忆注入是按 turn 的 instructions，按 turn 安全
- 现有 ToolRegistry 已分 local / client 工具，`register_local()` 是注入 web_search 的正确入口
- OpenViking HTTP API（本机实测，OpenAPI /docs 可达）：
  - `POST /api/v1/sessions` 建会话、`POST /api/v1/sessions/{id}/messages` 廉价追加上轮对话
  - `POST /api/v1/sessions/{id}/commit` **昂贵**（15-60s、2-6 VLM + embedding），返回 `task_id` 可后台异步
  - `POST /api/v1/search/find` `{query,target_uri:"viking://user/memories/",limit,score_threshold}` 返回 `{memories:[{uri,abstract,score,...}]}`
  - `GET /api/v1/stats/memories` **仅返聚合计数**，UI 列表不能用，要用 `GET /api/v1/fs/ls?uri=viking://user/memories/&recursive=true`
  - `GET /api/v1/content/read?uri=...` 读单条
  - `POST /api/v1/content/write` (`mode="replace"` 编辑、`mode="create"` `wait=true` 手动新建)
  - `DELETE /api/v1/fs?uri=...` 删除
  - Headers：`X-API-Key` 必需、`X-OpenViking-Account`、`X-OpenViking-User` 租户
- Tavily：`POST /search` Bearer auth；`search_depth=ultra-fast`（亚秒、1 credit）最快；无流式；默认 60s 超时必须按请求覆盖；作为 LLM tool 调用会增 2 轮往返（1.5-4s），需明确接受此延迟预算

### Metis Review

**Identified Gaps** (addressed):
- 每轮 commit 不可行（OV 15-60s VLM 成本）→ 改成每轮仅 post message + 周期性后台 commit（fire-and-forget asyncio.create_task）
- UI 列表不能用 `stats/memories` → 改用 `fs/ls` + 懒加载 `content/read`
- 不存在 `PUT memory` → 编辑 = `POST /api/v1/content/write` `mode="replace"`
- 不存在按 id 删除 → `DELETE /api/v1/fs?uri=...`
- `_system_prompt` 2500 字符截断 → 注入预算显式常量（500 字），优先 persona，用 `abstract` 字段而非 `content` 保持精简
- Tavily Pattern A 慢 → 限定 ultra-fast + max_results=3 + 2.0s 超时 + 失败静默降级，记录 `web_search_ms` 指标
- 无 httpx mock 库 → 测试用 Protocol 注入 Fake provider + 内联 Tavily 假对象，不动现有测试风格
- 提交与编辑竞态、空记忆搜索、commit 409、OpenViking session 过期、断线孤儿会话 → 见各任务"Must NOT do / 边界"
- 用户最后澄清 OpenViking 可选 → `SqliteMemoryProvider` 升格为生产 fallback（FTS5 + LLM-driven 抽取）

---

## Work Objectives

### Core Objective

在不动 STT / TTS / VAD / persona / 现有两个 LLMProvider 子类核心流程的前提下，为 `openai_compatible` 直连模式叠加"自动记忆（可选 OpenViking、默认 SQLite 轻量）+ Tavily 联网搜索 + UI 记忆管理"能力，让切到此模式的语音对话又快又可记忆；同时给 `hermes_agent` 模式提供只读记忆注入能力（不写）。

### Concrete Deliverables

- `hermes_sts/memory.py`：MemoryProvider Protocol + MemoryHit dataclass + NoopMemoryProvider + SqliteMemoryProvider（FTS5 + LLM 抽取）+ OpenVikingMemoryProvider（httpx + 内存会话状态 + 后台 commit）+ `build_memory(settings, llm=None)` 工厂
- `hermes_sts/websearch.py`：WebSearchProvider Protocol + SearchHit dataclass + TavilySearchProvider + NoopWebSearchProvider + `build_websearch(settings)` 工厂
- `hermes_sts/tools.py` 扩展：`register_default_local_tools(registry, settings, *, web_search_provider=None)` 工厂；条件性注册 `web_search` 工具
- `hermes_sts/realtime.py` 最小侵入：在构造 `instructions` 处叠加记忆 hits（预算 500 字）；answer 返回后按 provider 模式异步触发 `record_turn`（fire-and-forget）；新指标 `memory_read` / `memory_commit` / `memory_extract` 写入 `runtime_metrics`
- `hermes_sts/config.py`：新增 settings 字段（详 Task 1）
- `hermes_sts/config_store.py`：ENV_TO_ATTR 映射 + ensure_defaults（默认 `memory_provider="sqlite"`、`memory_enabled=false`）+ `_requires_rebuild` 加入 memory/web_search 切换键
- `hermes_sts/admin.py`：新增 `/api/memories`（GET list、POST add）、`/api/memories/{uri}`（GET read、PUT replace、DELETE）+ `/api/memories/recall`（POST 手动测试查询）+ `/api/memories/activity`（GET 最近活动）；admin_state 增加 `memory` 块（enabled / provider / stats / recent activity）
- `admin_ui/src/main.tsx`：新增"记忆"标签页（列表 + 搜索 + 编辑 + 删除 + 手动添加 + 启用开关 + provider 配置 + 活动流）
- `tests/test_memory_websearch.py`：单测覆盖 3 个 memory provider + Tavily + Noop websearch，全部用 Protocol 注入 fakes，无真实 HTTP
- `tests/test_core.py` 或新文件：扩展 `bare_session` + `FakeMemoryProvider` + `FakeWebSearchProvider` 验证 realtime 注入与 record_turn 路径
- `README.md`：勾掉 roadmap 的 1（搜索）与 3（本地记忆）条目，新增"直连 LLM + 记忆"小节

### Definition of Done

- [ ] `python -m unittest discover -s tests -p "test_*.py" -v` 全绿
- [ ] `openai_compatible` 模式 + `memory_enabled=true`：发一轮对话，`runtime_metrics` 表能看到 `memory_read` / `memory_commit` 行
- [ ] `hermes_agent` 模式：ToolRegistry 不含 `web_search`；turn 后不触发 `record_turn`
- [ ] 关掉 OpenViking 或不配 `OPENVIKING_API_KEY` → 切到 `memory_provider=sqlite` 后对话仍正常，且能从 `/api/memories` 列出 LLM 抽取的记忆
- [ ] `curl http://127.0.0.1:8765/api/memories` 返回 `{"memories": [...]}`（OV 后端走 fs/ls，Sqlite 后端走 SELECT）
- [ ] 启用 `web_search` 后发"今天杭州天气怎么样" — LLM 触发 web_search，对话继续无挂死；关掉 Tavily 端点仍能 2s 内降级回答

### Must Have

- MemoryProvider / WebSearchProvider Protocol 抽象，让 OV / SQLite / Tavily / Noop 可插拔
- OpenViking 后端走 httpx，不装 Python SDK
- SqliteMemoryProvider 用 SQLite FTS5（或 LIKE 兜底）+ LLM-driven 抽取，复用现有 LLMProvider，无新重依赖
- 记忆注入预算 500 字硬上限，优先 persona，截断记 WARNING
- OpenViking 模式：每轮 post message 廉价 + 周期性后台 `asyncio.create_task` commit；绝不阻塞 turn 关键路径
- Tavily `search_depth=ultra-fast`、`max_results=3`、`timeout=2.0`、失败静默降级
- `openai_compatible` 模式 + `memory_enabled=true` → 自动读 + 自动写
- `hermes_agent` 模式 → 默认只读记忆，**不写 / 不 commit / 不抽 LLM**
- 所有外部调用 (OV / Tavily) 失败按 `WARNING` 记录，绝不冒泡成 WebSocket 断开 / 500
- 新模块单测全部用 Protocol 注入 fakes，**不引入 pytest-httpx / responses**
- UI 记忆面板：列表 / 搜索 / 编辑 / 删除 / 手动添加 / 启用 + provider 配置 + 简化活动流

### Must NOT Have (Guardrails)

- 不改 `HermesAgentProvider` / `OpenAICompatibleProvider` 的核心 `chat()` / `_prepare_messages` 主体；只通过构造更好的 `instructions` 字符串间接影响 system prompt
- 不动 STT / TTS / VAD / persona 模块核心代码
- 不装 OpenViking Python SDK
- 不做用户 / profile 分桶的记忆（全局单用户）
- 不在 hermes 模式注册 `web_search` 工具
- 不在 Tavily `advanced` 模式或 `timeout > 3.0` 下运行
- 不把记忆搜索结果 `content` 全文注入 system prompt —— 只用 `abstract`
- 不在 `_ask_llm_with_tools` 关键路径中调用 `/commit` 或同步等待 `task_id` 完成
- 不在 v1 做 Tavily 查询缓存、commit 状态轮询 UI、记忆分类树浏览、批量操作
- 不构建独立的本地记忆 SQLite 缓存（OV 后端直接查 fs/read）
- UI 列表不使用 `stats/memories` 作数据源（只返计数）
- 不引入 `pytest-httpx` / `responses` / 任何 HTTP mock 库
- 不让记忆 / 搜索失败冒泡到 WebSocket 断开 / 500
- 记忆注入 prompt 必须把 hits 标为参考上下文（"不要逐条复述"），不污染语音短答风格

---

## Verification Strategy (MANDATORY)

> **ZERO HUMAN INTERVENTION** - ALL verification is agent-executed.

### Test Decision

- **Infrastructure exists**: YES（`scripts/run_tests.sh` 用 `unittest discover`，已用 SQLite FTS5 依赖）
- **Automated tests**: YES（Tests-after，与现有风格一致）
- **Framework**: `unittest`（标准库），不引入 pytest
- **Mocking strategy**: Protocol 注入 fakes（同 `DummyChatProvider` / `FakeLlm` / `FakeTts` 风格）；不引入 HTTP mock 库

### QA Policy

每个 task MUST 包含 agent-executed QA scenarios。Evidence 存 `.sisyphus/evidence/task-{N}-{scenario-slug}.{ext}`。

- **Backend / API**: Bash (curl) — 发请求、断言 status + JSON 字段
- **Library / Module**: Bash + `python -c` — import、调函数、比对输出；或 `python -m unittest`
- **Frontend / UI**: Playwright (playwright skill) — navigate / click / 填表 / 截图 / DOM 断言
- **WebSocket / Realtime**: Bash + `.venv-sts/bin/python scripts/smoke/ws_turn_smoke.py` + log 抓 `memory_read` / `memory_commit` 行

---

## Execution Strategy

### Parallel Execution Waves

```
Wave 1 (Foundation - 6 parallel, async-safe modules):
├── Task 1:  Settings + ConfigStore 新字段 + ENV_TO_ATTR 映射 [quick]
├── Task 2:  MemoryProvider Protocol + MemoryHit + NoopMemoryProvider 骨架 [quick]
├── Task 3:  WebSearchProvider Protocol + SearchHit + NoopWebSearchProvider + TavilySearchProvider [quick]
├── Task 4:  SqliteMemoryProvider（FTS5 + LLM-driven 抽取，生产 fallback）[unspecified-high]
├── Task 5:  OpenVikingMemoryProvider（httpx + 周期性后台 commit）[deep]
└── Task 6:  build_memory/build_websearch 工厂 + settings 路由 [quick]

Wave 2 (Integration - 5 parallel, depends on Wave 1):
├── Task 7:  tools.py register_default_local_tools 工厂 + web_search ToolSpec (depends: 3, 6) [quick]
├── Task 8:  realtime.py 集成记忆注入 + record_turn 后台触发 + web_search 工具注入 ToolRegistry (depends: 4, 5, 6, 7) [deep]
├── Task 9:  config_store.ensure_defaults 注入 memory/websearch 默认值 (depends: 1) [quick]
├── Task 10: admin.py REST /api/memories CRUD + /api/memories/recall + admin_state.memory + _requires_rebuild (depends: 4, 5, 6) [unspecified-high]
└── Task 11: README roadmap 更新 (depends: none, async) [writing]

Wave 3 (UI - depends on Wave 2 task 10):
└── Task 12: admin_ui/src/main.tsx 新增 Memory 面板 (depends: 10) [visual-engineering]

Wave 4 (Tests - after implementation stable):
├── Task 13: tests/test_memory_websearch.py 单测 (depends: 4, 5, 6, 3) [unspecified-high]
└── Task 14: tests/test_core.py 扩展 + realtime集成测 (depends: 8, 13) [unspecified-low]

Wave FINAL (after ALL tasks):
├── F1: Plan compliance audit [oracle]
├── F2: Code quality review [unspecified-high]
├── F3: Real manual QA [unspecified-high]
└── F4: Scope fidelity check [deep]
-> Present results -> Get explicit user okay

Critical Path: 1 → 6 → 8 → 10 → 12 → 14 → F1-F4
Parallel Speedup: ~65% faster than sequential
Max Concurrent: 6 (Wave 1)
```

### Dependency Matrix

- **1**: -  → 9, 10, 2
- **2**: -  → 6
- **3**: -  → 6, 7
- **4**: -  → 6, 8, 10, 13
- **5**: -  → 6, 8, 10, 13
- **6**: 1, 2, 3, 4, 5  → 7, 8, 10
- **7**: 3, 6  → 8
- **8**: 4, 5, 6, 7  → 14
- **9**: 1  → -
- **10**: 4, 5, 6  → 12
- **11**: -  → -
- **12**: 10  → -
- **13**: 4, 5, 6, 3  → 14
- **14**: 8, 13  → F1-F4
- **F1**: 14  → user
- **F2**: 14  → user
- **F3**: 14  → user
- **F4**: 14  → user

### Agent Dispatch Summary

- **Wave 1 (6)**: T1 → `quick`, T2 → `quick`, T3 → `quick`, T4 → `unspecified-high`, T5 → `deep`, T6 → `quick`
- **Wave 2 (5)**: T7 → `quick`, T8 → `deep`, T9 → `quick`, T10 → `unspecified-high`, T11 → `writing`
- **Wave 3 (1)**: T12 → `visual-engineering` (+`frontend-design`)
- **Wave 4 (2)**: T13 → `unspecified-high`, T14 → `unspecified-low`
- **FINAL (4)**: F1 → `oracle`, F2 → `unspecified-high`, F3 → `unspecified-high` (+`playwright`), F4 → `deep`

---

## TODOs

- [x] 1. Settings + ConfigStore 新字段 + ENV_TO_ATTR 映射

  **What to do**:
  - 在 `hermes_sts/config.py` 的 `Settings` frozen dataclass 末尾追加字段（按 README roadmap 命名对齐），全部带 `os.getenv` 默认值：
    - `memory_enabled: bool = _bool_env("STS_MEMORY_ENABLED", False)`
    - `memory_provider: str = os.getenv("STS_MEMORY_PROVIDER", "sqlite")`  # `sqlite` | `openviking` | `noop`
    - `memory_remember_in_hermes: bool = _bool_env("STS_MEMORY_REMEMBER_IN_HERMES", True)`  # hermes 模式只读注入开关
    - `memory_injection_budget: int = _int_env("STS_MEMORY_INJECTION_BUDGET", 500)`
    - `memory_recall_limit: int = _int_env("STS_MEMORY_RECALL_LIMIT", 5)`
    - `memory_recall_min_score: float = _float_env("STS_MEMORY_RECALL_MIN_SCORE", 0.0)`
    - `memory_commit_interval_turns: int = _int_env("STS_MEMORY_COMMIT_INTERVAL_TURNS", 10)`
    - `memory_commit_idle_seconds: float = _float_env("STS_MEMORY_COMMIT_IDLE_SECONDS", 300.0)`
    - `memory_extract_enabled: bool = _bool_env("STS_MEMORY_EXTRACT_ENABLED", True)`  # sqlite 后端的 LLM 抽取开关
    - `memory_extract_max_per_turn: int = _int_env("STS_MEMORY_EXTRACT_MAX_PER_TURN", 2)`
    - `openviking_base_url: str = os.getenv("OPENVIKING_BASE_URL", "http://127.0.0.1:1933")`
    - `openviking_api_key: str = os.getenv("OPENVIKING_API_KEY", "")`
    - `openviking_account: str = os.getenv("OPENVIKING_ACCOUNT", "default")`
    - `openviking_user: str = os.getenv("OPENVIKING_USER", "reachy")`
    - `openviking_target_uri: str = os.getenv("OPENVIKING_TARGET_URI", "viking://user/memories/")`
    - `openviking_timeout_seconds: float = _float_env("OPENVIKING_TIMEOUT_SECONDS", 6.0)`
    - `openviking_commit_timeout_seconds: float = _float_env("OPENVIKING_COMMIT_TIMEOUT_SECONDS", 30.0)`
    - `sqlite_memory_path: str = _path_env("STS_SQLITE_MEMORY_PATH", "data/memory.sqlite3")`
    - `web_search_enabled: bool = _bool_env("STS_WEB_SEARCH_ENABLED", False)`
    - `tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")`
    - `tavily_search_depth: str = os.getenv("TAVILY_SEARCH_DEPTH", "ultra-fast")`  # 强制非 advanced
    - `tavily_max_results: int = _int_env("TAVILY_MAX_RESULTS", 3)`
    - `tavily_timeout_seconds: float = _float_env("TAVILY_TIMEOUT_SECONDS", 2.0)`  # 强制 <= 3.0
    - `tavily_base_url: str = os.getenv("TAVILY_BASE_URL", "https://api.tavily.com")`
  - 在 `hermes_sts/config_store.py` 的 `ENV_TO_ATTR` 字典追加以上所有 env→attr 映射
  - 在 `admin.py` 的 `_validate_settings_patch` 加入 `memory_provider ∈ {sqlite, openviking, noop}`、`memory_provider=sqlite` 时允许空 api_key、`openviking=m` 时要求 `openviking_api_key` 非空、`tavily_search_depth ∈ {ultra-fast, fast, basic}`、断言 `tavily_timeout_seconds <= 3.0` 否则 422
  - 在 `_settings_payload` 的 `visible["llm"]` 追加 `memory_enabled, memory_provider, memory_remember_in_hermes, web_search_enabled`；新增 `visible["memory"]` 段包含所有 memory_* / openviking_* / tavily_* 字段
  - 在 `_requires_rebuild` set 加入 `memory_enabled, memory_provider, web_search_enabled, tavily_api_key, openviking_base_url, openviking_api_key, openviking_account, openviking_user`（这些切换需要 rebuild memory/websearch 实例）

  **Must NOT do**:
  - 不在 config.py 引入新的 import（除已有 `os`、`Path`）
  - 不动现有 settings 字段顺序（追加在末尾）
  - 不在本任务实现 memory.py / admin.py 的 REST 端点逻辑（只补字段、映射、validate、payload、requires_rebuild）

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 单文件追加 frozen dataclass 字段 + dict 映射 + 简单 validate，无算法逻辑
  - **Skills**: []
  - **Skills Evaluated but Omitted**:
    - `git-master`: 本任务不涉及 git 操作

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1 (with Tasks 2, 3, 4, 5, 6)
  - **Blocks**: Tasks 6, 9, 10（依赖新字段）
  - **Blocked By**: None（可立即开始）

  **References**:

  **Pattern References** (existing code to follow):
  - `hermes_sts/config.py:57-201` — 完整 Settings frozen dataclass 风格，字段都带 `os.getenv("_ENV", default)` 或 `_bool_env` / `_int_env` / `_float_env` / `_path_env` 辅助器
  - `hermes_sts/config.py:85` — `STS_LLM_PROVIDER` 字段是我们要增强的同款切换键
  - `hermes_sts/config_store.py:20-128` — `ENV_TO_ATTR` 完整映射示例，照此追加
  - `hermes_sts/admin.py:675-690` — `_validate_settings_patch` 现有 422 校验示例（如 `tts_provider` 白名单）

  **API/Type References**:
  - `hermes_sts/admin.py:647-671` — `_requires_rebuild` 当前 rebuild_keys 集合，照此追加新键

  **External References**:
  - OpenViking 官方文档：`https://docs.openviking.ai/en/api/06-retrieval` — 字段命名沿用其术语（target_uri, score_threshold, limit）
  - Tavily API：`https://docs.tavily.com/documentation/api-reference/endpoint/search` — search_depth 枚举值

  **WHY Each Reference Matters**:
  - config.py 末尾追加确保不破坏 frozen=True 的 kwargs 顺序
  - ENV_TO_ATTR 必须全量加，否则 SQLite 持久化读不回这些字段
  - _requires_rebuild 加新键保证切换 memory_provider 真正重建 provider 实例

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Settings 字段被 SQLite 正确持久化
    Tool: Bash (python + curl)
    Preconditions: 项目根目录可写，使用临时 config_db
    Steps:
      1. 设环境变量 HERMES_STS_CONFIG_DB=/tmp/test_task1.sqlite3
      2. 运行 python -c "from hermes_sts.config_store import ConfigStore; s=ConfigStore.default(); s.set_settings({'STS_MEMORY_ENABLED': True, 'STS_MEMORY_PROVIDER': 'openviking', 'OPENVIKING_API_KEY': 'k', 'TAVILY_API_KEY': 'tvly-x'}); print(s.load_settings().memory_enabled, s.load_settings().memory_provider, s.load_settings().openviking_api_key)"
      3. 断言 stdout == "True openviking k"
    Expected Result: 三个新字段都被 SQLite 正确存读
    Failure Indicators: AttributeError（字段未定义）或值未持久化（默认值不变）
    Evidence: .sisyphus/evidence/task-1-settings-persistence.txt

  Scenario: 非法 memory_provider 被 422 拒绝
    Tool: Bash (python)
    Preconditions: 已注册新字段
    Steps:
      1. python -c "from hermes_sts.admin import _validate_settings_patch; 
             try:
                 _validate_settings_patch({'STS_MEMORY_PROVIDER': 'mongo'})
                 print('FAIL: no 422')
             except Exception as e:
                 print('OK', type(e).__name__, str(e)[:80])"
      2. 断言 stdout 以 'OK' 开头且包含 'HTTPException'
    Expected Result: 校验拒绝 mongo，HTTPException 422
    Evidence: .sisyphus/evidence/task-1-validate-memory-provider.txt

  Scenario: Tavily timeout > 3.0 被 422 拒绝
    Tool: Bash (python)
    Preconditions: 已注册新字段
    Steps:
      1. python -c "from hermes_sts.admin import _validate_settings_patch;
             try:
                 _validate_settings_patch({'TAVILY_TIMEOUT_SECONDS': 10.0})
                 print('FAIL')
             except Exception:
                 print('OK rejected')"
      2. 断言 stdout == 'OK rejected'
    Expected Result: 大 timeout 被 422 拒绝
    Evidence: .sisyphus/evidence/task-1-validate-tavily-timeout.txt
  ```

  **Evidence to Capture**:
  - [ ] task-1-settings-persistence.txt（python stdout）
  - [ ] task-1-validate-memory-provider.txt
  - [ ] task-1-validate-tavily-timeout.txt

  **Commit**: NO（与 Wave 1 其他任务合并成 1 个 commit）

---

- [x] 2. MemoryProvider Protocol + MemoryHit + NoopMemoryProvider 骨架

  **What to do**:
  - 新建 `hermes_sts/memory.py`，定义：
    ```python
    @dataclass(frozen=True)
    class MemoryHit:
        uri: str          # OV: viking://user/memories/...；SQLite: mem_{hex}
        content: str      # 完整内容（UI 编辑用）
        abstract: str     # 注入 system prompt 的精简版（OV 用 abstract，SQLite 用 content 前 N 字）
        score: float = 0.0
        category: str = ""
        tags: list[str] = field(default_factory=list)
        created_at: float = 0.0
        updated_at: float = 0.0
        source: str = ""  # "openviking" / "sqlite" / "manual"

    class MemoryProvider(Protocol):
        async def recall(self, query: str, *, limit: int = 5, min_score: float = 0.0) -> list[MemoryHit]: ...
        async def record_turn(self, transcript: str, answer: str, *, session_id: str) -> None: ...
        async def list_memories(self, *, limit: int = 50, offset: int = 0, q: str = "") -> list[MemoryHit]: ...
        async def get_memory(self, uri: str) -> MemoryHit | None: ...
        async def update_memory(self, uri: str, *, content: str, category: str | None = None, tags: list[str] | None = None) -> None: ...
        async def delete_memory(self, uri: str) -> bool: ...
        async def add_memory(self, *, content: str, category: str = "manual", tags: list[str] | None = None) -> str: ...  # 返回新 uri
        def stats(self) -> dict[str, Any]: ...  # 同步，给 admin_state 用
    ```
  - 实现 `NoopMemoryProvider`：所有 recall 返 `[]`，record_turn / add / update / delete 返 `None`/`False`，list 返 `[]`，stats 返 `{"enabled": False, "provider": "noop"}`
  - 在文件末尾预留 `build_memory(settings, llm=None) -> MemoryProvider` 的占位（实际实现放 Task 6，本任务只 `raise NotImplementedError`）
  - 所有方法签名都要带 `async`（除 stats），调用方统一 `await provider.xxx()`

  **Must NOT do**:
  - 不在本任务实现 SqliteMemoryProvider / OpenVikingMemoryProvider 主体逻辑（Task 4、5）
  - 不引入外部依赖（仅 dataclasses、typing、Protocol、标准库）
  - 不在 record_turn 内做 LLM 调用或 HTTP 调用（具体后端各自实现）

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 抽象接口 + dataclass + Noop 桩实现，无外部依赖、无算法
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: Task 6（工厂依赖此文件）
  - **Blocked By**: None

  **References**:

  **Pattern References**:
  - `hermes_sts/llm.py:33-42` — `LLMProvider` Protocol + `LLMResponse` dataclass 风格模板
  - `hermes_sts/llm.py:20-30` — `ToolCall` / `LLMResponse` frozen dataclass 用法
  - `hermes_sts/stt.py`（任意段） — STT provider 抽象同款模式
  - `hermes_sts/tts.py:1-50`（TtsVoice dataclass 区段） — dataclass + from_realtime / from_settings 模式可借鉴

  **WHY Each Reference Matters**:
  - llm.py 是项目里现有的"Protocol + dataclass + Provider"三件套范本，照搬风格保证一致性

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: NoopMemoryProvider 全方法零副作用
    Tool: Bash (python)
    Preconditions: memory.py 已创建
    Steps:
      1. python -c "
         import asyncio
         from hermes_sts.memory import NoopMemoryProvider
         p = NoopMemoryProvider()
         r = asyncio.run(p.recall('hi'))
         assert r == [], r
         asyncio.run(p.record_turn('q', 'a', session_id='s'))
         assert asyncio.run(p.list_memories()) == []
         assert asyncio.run(p.get_memory('x')) is None
         assert asyncio.run(p.delete_memory('x')) is False
         st = p.stats()
         assert st == {'enabled': False, 'provider': 'noop'}, st
         print('OK') "
      2. 断言 stdout == 'OK'
    Expected Result: 所有方法都返回空/false，stats 显示 disabled
    Evidence: .sisyphus/evidence/task-2-noop-provider.txt

  Scenario: MemoryHit frozen dataclass 可哈希
    Tool: Bash (python)
    Preconditions: memory.py 已创建
    Steps:
      1. python -c "
         from hermes_sts.memory import MemoryHit
         h = MemoryHit(uri='mem_a', content='c', abstract='a')
         d = {h: 1}
         print('OK', h.uri, h.score, h.tags) "
      2. 断言 stdout 以 'OK mem_a 0.0 []' 开头
    Expected Result: frozen dataclass 可作为 dict key（验证 frozen=True）
    Evidence: .sisyphus/evidence/task-2-memory-hit-frozen.txt
  ```

  **Evidence to Capture**:
  - [ ] task-2-noop-provider.txt
  - [ ] task-2-memory-hit-frozen.txt

  **Commit**: NO

---

- [x] 3. WebSearchProvider Protocol + SearchHit + TavilySearchProvider + NoopWebSearchProvider

  **What to do**:
  - 新建 `hermes_sts/websearch.py`：
    ```python
    @dataclass(frozen=True)
    class SearchHit:
        title: str = ""
        url: str = ""
        content: str = ""  # snippet, 短，给 LLM 直接用
        score: float = 0.0

    class WebSearchProvider(Protocol):
        async def search(self, query: str, *, max_results: int = 3) -> list[SearchHit]: ...
        def description(self) -> str: ...  # 给 ToolRegistry 日志/admin_state 用,例 "tavily(ultra-fast)" / "noop"

    class NoopWebSearchProvider:
        async def search(...) -> []: return []
        def description(self) -> str: return "noop"

    class TavilySearchProvider:
        def __init__(self, settings): ...
        async def search(self, query, *, max_results=None) -> list[SearchHit]:
            # POST {settings.tavily_base_url}/search
            # body: {"api_key": settings.tavily_api_key, "query": query,
            #        "search_depth": settings.tavily_search_depth,
            #        "max_results": max_results or settings.tavily_max_results,
            #        "include_answer": False, "include_raw_content": False}
            # timeout: httpx.Timeout(settings.tavily_timeout_seconds, connect=1.0)
            # 异常 / 4xx / 5xx: logger.warning + return [] (绝不 raise)
            # 解析 data.get("results", []) -> [SearchHit(title, url, content[:400])]  # content 截断 400 字防 prompt 爆炸
    ```
  - 实现一个 `build_websearch(settings) -> WebSearchProvider`：
    - `web_search_enabled && tavily_api_key` → `TavilySearchProvider`
    - 否则 → `NoopWebSearchProvider`
  - Tavily 模式硬约束：`settings.tavily_search_depth` 不能是 `advanced`（启动时 logger.warning 自动降级为 `basic`）；`tavily_timeout_seconds > 3.0` 自动 clamp 到 3.0 + WARNING

  **Must NOT do**:
  - 不重试失败请求
  - 不缓存查询结果
  - 不解析 `include_answer`、`raw_content`、`images`（只用 results[].title/url/content）
  - 不在 `realtime.path` 让 `search()` 抛异常
  - 不引入新依赖（`httpx` 已在 pyproject.toml）

  **Recommended Agent Profile**:
  - **Category**: `quick`
    - Reason: 单文件、~150 行、无复杂状态
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: Tasks 6, 7
  - **Blocked By**: None

  **References**:

  **Pattern References**:
  - `hermes_sts/llm.py:117-128` — `_post_chat_completions` httpx 调用 + raise_for_status 模式
  - `hermes_sts/llm.py:45-70` — `BaseOpenAIChatProvider.__init__(settings)` 拿 settings 模式
  - `tests/test_core.py:26-46` — `DummyChatProvider` 测试 stub 模式，供任务参考

  **External References**:
  - Tavily Search API 官方文档：`https://docs.tavily.com/documentation/api-reference/endpoint/search`（字段 search_depth、max_results、include_answer、include_raw_content、results[].title/url/content）
  - Metis 报告：ultra-fast 亚秒、basic 2-4s、advanced 4-8s 不入

  **WHY Each Reference Matters**:
  - httpx 用法与 llm.py 一致，timeout 形态照搬保项目风格

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: NoopWebSearchProvider 返空
    Tool: Bash (python)
    Steps:
      1. python -c "
         import asyncio
         from hermes_sts.websearch import NoopWebSearchProvider
         p = NoopWebSearchProvider()
         assert asyncio.run(p.search('x')) == []
         assert p.description() == 'noop'
         print('OK') "
      2. 断言 stdout == 'OK'
    Expected Result: 完全无副作用
    Evidence: .sisyphus/evidence/task-3-noop-websearch.txt

  Scenario: TavilySearchProvider 失败静默降级
    Tool: Bash (python)
    Preconditions: tavily_api_key 设为 'fake-key'，base_url 故意指向不可达端口
    Steps:
      1. python -c "
         import asyncio, os
         from hermes_sts.config import Settings
         from hermes_sts.websearch import TavilySearchProvider
         s = Settings(tavily_api_key='fake', tavily_base_url='http://127.0.0.1:1', tavily_timeout_seconds=1.0)
         p = TavilySearchProvider(s)
         r = asyncio.run(p.search('hello'))
         assert r == [], r
         print('OK', p.description()) "
      2. 断言 stdout 以 'OK' 开头
    Expected Result: 不可达不抛，返空 list
    Evidence: .sisyphus/evidence/task-3-tavily-failure.txt

  Scenario: build_websearch 路由
    Tool: Bash (python)
    Steps:
      1. python -c "
         from hermes_sts.config import Settings
         from hermes_sts.websearch import build_websearch, NoopWebSearchProvider, TavilySearchProvider
         assert isinstance(build_websearch(Settings(web_search_enabled=False, tavily_api_key='')), NoopWebSearchProvider)
         assert isinstance(build_websearch(Settings(web_search_enabled=True, tavily_api_key='k')), TavilySearchProvider)
         print('OK') "
      2. 断言 stdout == 'OK'
    Evidence: .sisyphus/evidence/task-3-build-websearch-route.txt
  ```

  **Evidence to Capture**:
  - [ ] task-3-noop-websearch.txt
  - [ ] task-3-tavily-failure.txt
  - [ ] task-3-build-websearch-route.txt

  **Commit**: NO

---

- [x] 4. SqliteMemoryProvider（FTS5 + LLM-driven 抽取，生产 fallback）

  **What to do**:
  - 在 `hermes_sts/memory.py` 追加 `SqliteMemoryProvider`：
    - `__init__(self, settings, llm: LLMProvider | None = None)`：打开 `settings.sqlite_memory_path`，建表：
      ```sql
      CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,           -- mem_{uuid4_hex}
        content TEXT NOT NULL,
        abstract TEXT NOT NULL,        -- content 前 200 字（注入 system prompt 用）
        category TEXT NOT NULL DEFAULT 'manual',
        tags TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL DEFAULT 'manual',
        turn_id TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
      );
      CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content, abstract, tags, content='memories', content_rowid='rowid'
      );
      -- 触发器保持 fts 同步（INSERT/UPDATE/DELETE）
      ```
    - `recall(query, limit, min_score)`: FTS5 `bm25(memories_fts) DESC` 排序LIKE fallback 当 FTS5 不可用（PRAGMA check）；过滤 `abstract LIKE '%{kw}%'`；返回 `MemoryHit(uri=id, content, abstract, score=-bm25, source="sqlite")`，limit 截断
    - `record_turn(transcript, answer, session_id)`: 若 `settings.memory_extract_enabled` 且拥有 `llm`，跑一个固定抽取 prompt（"从下面这轮对话中抽出值得长期记的事实，0-N 条，返回 JSON 数组 [{content,category,tags[...]}]。聊天泛泛内容返回空数组"）；解析 JSON；逐条调 `add_memory(content, category, tags, source="llm_extract", turn_id=session_id)`
    - `list_memories(limit, offset, q)`: SQL `SELECT` + offset，q 非空时 `WHERE content LIKE ?`；返回 MemoryHit 列表
    - `get_memory(uri)` -> MemoryHit | None
    - `update_memory(uri, content, category, tags)`: UPDATE，自动维护 fts；返回 None
    - `delete_memory(uri)`: DELETE 并 poll fts trigger；return bool
    - `add_memory(content, category, tags, source, turn_id)`: INSERT + 触发 fts 索引；return new id
    - `stats()`: 同步，return `{"enabled": True, "provider": "sqlite", "count": N, "latest_created_at": ts}`
  - 抽取 prompt 用一个常量字符串：`SQLITE_MEMORY_EXTRACT_PROMPT`；调用 LLM 时构造 `messages=[{system: 抽取 prompt}, {user: f"用户：{transcript}\n助手：{answer}"}]`，max_tokens=200，`stream=False`
  - LLM 调用失败 / JSON 解析失败 → logger.warning + return（不阻断）
  - LLM 调用要走 `await self.llm.chat(messages=..., instructions=None)`；用 settings.llm_* 不可用时静默跳过
  - 写 sql 时参数化防注入；用 sqlite3.connect（同步）+ `await asyncio.to_thread(...)` 包裹避免阻塞 event loop
  - 并发：每方法内部用 `with self._lock`（threading.Lock）保护 sqlite connection（sqlite 不支持跨线程共享 connection）

  **Must NOT do**:
  - 不在 `recall` 走任何外部 HTTP
  - 不让 LLM 抽取调用 `self.llm.chat(transcript=..., instructions=...)`（会污染 LLM provider 的 history）；必须用 `messages=...` 路径
  - 不在 `record_turn` 内同步等待 LLM 完成（用 `asyncio.create_task` 让 `record_turn` 立即返回；但等等——根据 Metis 指令 sqlite 抽取可以每轮做，但 LLM 调用仍是阻塞的；改成 `asyncio.create_task(self._extract_and_save(...))` 异步抽完即写）
  - 不重复写已存在内容（用 `content hash` 做 dedup？v1 不做，允许重复）
  - 不引入 `transformers` / `sentence-transformers` 等 NLP 库

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
    - Reason: 实质性算法：FTS5 + 触发器 + LLM 抽取 + async/threading 协调，~250 行
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: Tasks 6, 8, 10, 13
  - **Blocked By**: Task 2（MemoryProvider Protocol）

  **References**:

  **Pattern References**:
  - `hermes_sts/config_store.py:193-260` — `_init_db` + `executescript` + `connect` sqlite 用法范本
  - `hermes_sts/config_store.py:616-627` — `add_metric` 同步 sqlite 写 + 上限裁剪
  - `hermes_sts/llm.py:186-198` — `_messages_for_transcript` 构造初 messages 列表
  - `hermes_sts/llm.py:140-184` — `_ask_llm_fallback` 调 LLM + JSON 解析 fail-soft 风格（参考但不直接抄）

  **API/Type References**:
  - `hermes_sts/llm.py:33-42` — `LLMProvider.chat(messages: list[Message] | None, instructions: str | None, ...)` 签名

  **External References**:
  - SQLite FTS5 官方：`https://www.sqlite.org/fts5.html`（external content table 模式）
  - Python asyncio + to_thread：`https://docs.python.org/3.12/library/asyncio-task.html#asyncio.to_thread`

  **WHY Each Reference Matters**:
  - config_store 已用 sqlite3 + row_factory，照搬可保一致性；FTS5 触发器是从无到有，需读官方 external content 章节

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: add → recall → delete 完整流程
    Tool: Bash (python)
    Preconditions: 用 tempdir memory.sqlite3
    Steps:
      1. python -c "
         import asyncio, tempfile, os
         from pathlib import Path
         from hermes_sts.config import Settings
         from hermes_sts.memory import SqliteMemoryProvider
         with tempfile.TemporaryDirectory() as t:
             os.environ['STS_SQLITE_MEMORY_PATH'] = str(Path(t)/'m.sqlite3')
             s = Settings()
             p = SqliteMemoryProvider(s, llm=None)
             uri = asyncio.run(p.add_memory(content='用户喜欢深色模式', category='preferences'))
             hits = asyncio.run(p.recall('深色', limit=5))
             assert any(h.uri == uri for h in hits), hits
             ok = asyncio.run(p.delete_memory(uri))
             assert ok
             assert asyncio.run(p.get_memory(uri)) is None
             print('OK') "
      2. 断言 stdout == 'OK'
    Expected Result: add / recall / get / delete 全链路
    Evidence: .sisyphus/evidence/task-4-sqlite-cycle.txt

  Scenario: LLM 抽取失败静默降级
    Tool: Bash (python)
    Preconditions: 用一个总是 raise 的 FakeLlm
    Steps:
      1. python -c "
         import asyncio, tempfile, os
         from pathlib import Path
         from hermes_sts.config import Settings
         from hermes_sts.memory import SqliteMemoryProvider
         class BadLlm:
             async def chat(self, *a, **k): raise RuntimeError('boom')
         with tempfile.TemporaryDirectory() as t:
             os.environ['STS_SQLITE_MEMORY_PATH'] = str(Path(t)/'m.sqlite3')
             p = SqliteMemoryProvider(Settings(), llm=BadLlm())
             asyncio.run(p.record_turn('我想喝咖啡', '好的', session_id='s1'))
             assert asyncio.run(p.list_memories()) == []
             print('OK') "
      2. 断言 stdout == 'OK'
    Expected Result: LLM 抽取 raise 不传播，记忆为空
    Evidence: .sisyphus/evidence/task-4-sqlite-llm-fail.txt

  Scenario: FTS5 不可用时 LIKE 降级
    Tool: Bash (python)
    Preconditions: 用编译时不带 FTS5 的 sqlite 可能不存在，备用方案
    Steps:
      1. python -c "
         import sqlite3
         # 检测 FTS5
         try:
             c = sqlite3.connect(':memory:')
             c.execute('CREATE VIRTUAL TABLE t USING fts5(content)')
             print('FTS5 available')
         except Exception as e:
             print('FTS5 missing, LIKE fallback will be tested by inject')
             "
      2. 记录结果，后续集成测覆盖
    Expected Result: 检测可用性；如缺失则 SqliteMemoryProvider 自动 LIKE fallback
    Evidence: .sisyphus/evidence/task-4-fts5-detect.txt
  ```

  **Evidence to Capture**:
  - [ ] task-4-sqlite-cycle.txt
  - [ ] task-4-sqlite-llm-fail.txt
  - [ ] task-4-fts5-detect.txt

  **Commit**: NO

---

- [x] 5. OpenVikingMemoryProvider（httpx + 周期性后台 commit）

  **What to do**:
  - 在 `hermes_sts/memory.py` 追加 `OpenVikingMemoryProvider`：
    - `__init__(self, settings)`：保存 settings、httpx.AsyncClient 实例（lazy 创建）；维护 `dict[session_id, OVSessionState]` 内存（含 ov_session_id、turn_count、last_commit_at、commit_lock=asyncio.Lock）
    - 私有 helper `_headers()`: `{"X-API-Key": settings.openviking_api_key, "X-OpenViking-Account": settings.openviking_account, "X-OpenViking-User": settings.openviking_user, "Content-Type": "application/json"}`
    - `recall(query, limit, min_score)`:
      - `POST {base}/api/v1/search/find` body `{query, target_uri=settings.openviking_target_uri, limit, score_threshold: min_score}`
      - timeout `settings.openviking_timeout_seconds`
      - 401/连接失败 → logger.warning + return []
      - 解析 `data["result"]["memories"]` → `[MemoryHit(uri, content=m.get("content",""), abstract=m.get("abstract","") or m.get("content","")[:200], score=m.get("score",0.0), category=m.get("category",""), source="openviking")]`
    - `record_turn(transcript, answer, session_id)`:
      1. 确保 OV session 存在：内存无则 `POST /api/v1/sessions` `{}` 返 `data["session_id"]`（401/失败 logger.warning + return）；缓存
      2. `POST /api/v1/sessions/{ov_session_id}/messages` body `{"messages":[{"role":"user","content":transcript},{"role":"assistant","content":answer}]}` 廉价消息追加（失败静默 return）
      3. 增 `state.turn_count`；判断是否到 commit 点：`turn_count >= settings.memory_commit_interval_turns` 或 `now - state.last_commit_at >= settings.memory_commit_idle_seconds` → 派发 `asyncio.create_task(self._commit(session_id))` **fire-and-forget**，绝不 await
    - `_commit(session_id)`: `async with state.commit_lock:` 防同 session 并发 commit；`POST /api/v1/sessions/{ov_id}/commit` 带 timeout `settings.openviking_commit_timeout_seconds`；返回 `task_id` 即不 poll；409 (already committing) → logger.debug 跳过；其他失败 logger.warning；最后更新 `state.last_commit_at = now`、`state.turn_count = 0`
    - `list_memories(limit, offset, q)`:
      - q 空时 `GET /api/v1/fs/ls?uri={openviking_target_uri}&recursive=true` 返文件列表，按 offset/limit 切片，对每个 uri 懒调 `get_memory`（包成 `asyncio.gather` 限并发 5）→ MemoryHit
      - q 非空走 `recall(q, limit=limit)`（语义检索），offset 用客户端分页截断
      - 失败 → []
    - `get_memory(uri)`: `GET /api/v1/content/read?uri={uri}`；返 MemoryHit 或 None
    - `update_memory(uri, content, category, tags)`: `POST /api/v1/content/write` body `{"uri": uri, "content": content, "mode": "replace", "wait": true}`；非 200 静默
    - `delete_memory(uri)`: `DELETE /api/v1/fs?uri={uri}&recursive=false`；返 bool
    - `add_memory(content, category, tags)`: `POST /api/v1/content/write` body `{"mode": "create", "wait": true, "content": content, "uri": f"{target_uri}{category}/{uuid4_hex}"}`；返回新 uri（解析 response 中 uri 字段，若无则拼回 request uri）
    - `stats()`: `GET /api/v1/stats/memories` 同步包 to_thread；返 `{"enabled": True, "provider": "openviking", "stats": <ov 原数据>, "sessions": <len(内存)>}`
    - 所有 HTTP 失败 try/except `Exception` 仅 logger.warning 不 raise
    - 关闭时 `await self._client.aclose()`（不一定有 session.close 钩子，Task 6 build_memory 持句柄即可）

  **Must NOT do**:
  - 不在 `_commit` 调用点 await 完成（必须 fire-and-forget；调用 record_turn 本身可 await message post，但 commit 是 create_task）
  - 不 poll `task_id` 完成（v1 不做）
  - 不把 `content` 全文注入 system prompt（用 abstract，注入逻辑在 Task 8）
  - 不在 recall / list / update / delete 内 raise
  - 不缓存 fs/ls 结果
  - 不开启自动重试
  - 不删除 OV session（断线由 Task 8 触发 final fire-and-forget commit，不删 session）

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: 多端点协调、状态机、并发锁、httpx 异步模式、错误处理面广，~250 行
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 1
  - **Blocks**: Tasks 6, 8, 10, 13
  - **Blocked By**: Task 2（Protocol）

  **References**:

  **Pattern References**:
  - `hermes_sts/llm.py:117-128` — httpx.AsyncClient + post + raise_for_status 同步版本，本任务改为不 raise
  - `hermes_sts/admin.py:716-795` — 演示如何在已有项目里组织一个完整 HTTP 调用 helper + JSON 解析 + 问题降级，长但可参考结构
  - `hermes_sts/llm.py:140-184` — `_ask_llm_fallback` HTTP fail-soft 范本（log + return）

  **External References**:
  - OpenViking API 文档：`https://docs.openviking.ai/en/api/06-retrieval`（find/search）、`https://docs.openviking.ai/en/api/03-filesystem`（fs/ls PUT DELETE）、commit endpoint 实测路径 `/api/v1/sessions/{id}/commit`
  - OpenViking opencode plugin 参考：`https://github.com/volcengine/OpenViking/blob/main/examples/opencode-plugin/`（fire-and-forget commit 模式、autoCommit intervalMinutes）

  **WHY Each Reference Matters**:
  - Metis / 计划明确要求 commit 必须 fire-and-forget，opencode plugin 是官方背书的同款模式

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: recall 在 OpenViking 不可达时返空
    Tool: Bash (python)
    Preconditions: base_url 指向不可达端口 1
    Steps:
      1. python -c "
         import asyncio
         from hermes_sts.config import Settings
         from hermes_sts.memory import OpenVikingMemoryProvider
         p = OpenVikingMemoryProvider(Settings(openviking_base_url='http://127.0.0.1:1', openviking_api_key='k'))
         r = asyncio.run(p.recall('hi'))
         assert r == [], r
         print('OK') "
      2. 断言 stdout == 'OK'
    Expected Result: 不可达不抛异常
    Evidence: .sisyphus/evidence/task-5-ov-unreachable.txt

  Scenario: record_turn 不阻塞 turn（fire-and-forget commit）
    Tool: Bash (python)
    Preconditions: 用 mock httpx 或不可达端口；关键是测 record_turn 不抛、不挂
    Steps:
      1. python -c "
         import asyncio, time
         from hermes_sts.config import Settings
         from hermes_sts.memory import OpenVikingMemoryProvider
         p = OpenVikingMemoryProvider(Settings(openviking_base_url='http://127.0.0.1:1', openviking_api_key='k', memory_commit_interval_turns=1))
         started = time.monotonic()
         asyncio.run(p.record_turn('q', 'a', session_id='s1'))
         elapsed = time.monotonic() - started
         assert elapsed < 2.0, elapsed
         print('OK', round(elapsed, 3)) "
      2. 断言 stdout 以 'OK' 开头
    Expected Result: record_turn 在 2s 内返回（commit 已是 fire-and-forget）
    Evidence: .sisyphus/evidence/task-5-record-eturn-fast.txt

  Scenario: stats 不可达返 disabled dict
    Tool: Bash (python)
    Steps:
      1. python -c "
         from hermes_sts.config import Settings
         from hermes_sts.memory import OpenVikingMemoryProvider
         p = OpenVikingMemoryProvider(Settings(openviking_base_url='http://127.0.0.1:1', openviking_api_key='k'))
         st = p.stats()
         assert 'enabled' in st or 'error' in st, st
         print('OK', st) "
      2. 断言 stdout 以 'OK' 开头
    Expected Result: 即使 OV down，stats 不抛
    Evidence: .sisyphus/evidence/task-5-ov-stats-degraded.txt
  ```

  **Evidence to Capture**:
  - [ ] task-5-ov-unreachable.txt
  - [ ] task-5-record-eturn-fast.txt
  - [ ] task-5-ov-stats-degraded.txt

  **Commit**: NO

---

- [x] 6. build_memory / build_websearch 工厂 + settings 路由

  **What to do**:
  - 在 `hermes_sts/memory.py` 实现 `build_memory(settings, llm: LLMProvider | None = None) -> MemoryProvider`：
    - `memory_enabled == False` → `NoopMemoryProvider`
    - `memory_provider == "noop"` → `NoopMemoryProvider`
    - `memory_provider == "sqlite"` → `SqliteMemoryProvider(settings, llm=llm)`
    - `memory_provider == "openviking"` → 若 `openviking_api_key` 空 → logger.warning + 回退 SqliteMemoryProvider；否则 `OpenVikingMemoryProvider(settings)`
    - 其他 → Noop + logger.warning
  - `build_websearch` 实现在 Task 3 已写；本任务审视并确保两个工厂签名一致便于 server.py / realtime.py 引用
  - 在 `hermes_sts/server.py`（如存在 build_components / app.state 注入处）添加 `app.state.memory = build_memory(settings, llm)` 和 `app.state.web_search = build_websearch(settings)`；调用处若 server.py 已有 `rebuild_components`，把这两个实例加进去；realtime 持引用通过 `app.state.memory` / `app.state.web_search`
  - 若 `server.py` 没显式 build，挂在 `app.state` 由 `realtime.py` 通过 `websocket.app.state` 取

  **Must NOT do**:
  - 不在 build_memory 内部 await 任何调用（同步）
  - 不在 Noop 模式下创建任何副作用
  - 不把 WebSearchProvider 注入 LLM 的 tools（那是 Task 7）

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES（与 Task 1 并行）
  - **Parallel Group**: Wave 1
  - **Blocks**: Tasks 7, 8, 10
  - **Blocked By**: Tasks 1, 2, 4, 5（要等实现齐全）

  **References**:

  **Pattern References**:
  - `hermes_sts/llm.py:364-370` — `build_llm(settings)` 完全照搬工厂模式
  - `hermes_sts/server.py`（如存在）— 看 build_components / app.state 注入位置（执行前先 read 该文件）

  **WHY Each Reference Matters**:
  - build_llm 是项目内现有的 provider 工厂范本，照搬风格

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: build_memory 路由矩阵
    Tool: Bash (python)
    Steps:
      1. python -c "
         from hermes_sts.config import Settings
         from hermes_sts.memory import build_memory, NoopMemoryProvider, SqliteMemoryProvider, OpenVikingMemoryProvider
         assert isinstance(build_memory(Settings(memory_enabled=False)), NoopMemoryProvider)
         assert isinstance(build_memory(Settings(memory_enabled=True, memory_provider='sqlite')), SqliteMemoryProvider)
         assert isinstance(build_memory(Settings(memory_enabled=True, memory_provider='openviking', openviking_api_key='k')), OpenVikingMemoryProvider)
         # openviking 缺 key 回退 sqlite
         assert isinstance(build_memory(Settings(memory_enabled=True, memory_provider='openviking', openviking_api_key='')), SqliteMemoryProvider)
         # 未知 provider 回退 noop
         assert isinstance(build_memory(Settings(memory_enabled=True, memory_provider='mongo')), NoopMemoryProvider)
         print('OK') "
      2. 断言 stdout == 'OK'
    Expected Result: 5 个分支全部正确
    Evidence: .sisyphus/evidence/task-6-build-memory-route.txt
  ```

  **Evidence to Capture**:
  - [ ] task-6-build-memory-route.txt

  **Commit**: NO

---

- [x] 7. tools.py register_default_local_tools 工厂 + web_search ToolSpec

  **What to do**:
  - 在 `hermes_sts/tools.py` 追加：
    ```python
    def register_default_local_tools(
        registry: ToolRegistry,
        settings: Settings,
        *,
        web_search_provider: WebSearchProvider | None = None,
    ) -> None:
        """Register STS-local tools gated by settings.

        Currently only web_search; noop/current_time already registered by __init__.
        Only called when settings.llm_provider == 'openai_compatible'.
        """
        if settings.llm_provider.strip().lower() != "openai_compatible":
            return  # 硬保障：hermes 模式绝不注册 web_search
        if not settings.web_search_enabled:
            return
        if web_search_provider is None or web_search_provider.description() == "noop":
            return  # 没 API key 也跳过
        registry.register_local(
            ToolSpec(
                name="web_search",
                description="Search the web for current information. Returns titles, URLs and short snippets. Use when user asks about current events, weather, news, prices or anything you don't know. Keep queries concise.",
                kind="slow",
                mode="local",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Search query in user's language"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                handler=_make_web_search_handler(web_search_provider),
            )
        )
    ```
  - 实现 `_make_web_search_handler(provider)`: 返回 `async def handler(args) -> str`：解析 `args["query"]`，`hits = await provider.search(query)`，format 成 `"1. {title}\n{url}\n{content}\n\n2. ..."` 截 2000 char；provider.search 失败已由 provider 兜底返空；空返 `"No results found."`
  - import 安全：避免 `tools.py` 在文件顶 import memory/websearch 造成循环；用 `TYPE_CHECKING` + 局部 import

  **Must NOT do**:
  - 不在 hermes 模式注册 web_search（双重保障：settings 检查 + 调用方约定不传）
  - 不在注册失败时 raise
  - 不缓存结果

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 8
  - **Blocked By**: Tasks 3, 6

  **References**:

  **Pattern References**:
  - `hermes_sts/tools.py:46-69` — `ToolRegistry.__init__` 注册 `noop` / `current_time` 风格
  - `hermes_sts/tools.py:17-25` — `ToolSpec` dataclass；`kind="fast"` / `kind="slow"` 是已有 kind 值

  **External References**:
  - OpenAI tool spec：`https://platform.openai.com/docs/guides/function-calling` parameters schema

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: openai_compatible + 启用 web_search 注册成功
    Tool: Bash (python)
    Steps:
      1. python -c "
         from hermes_sts.config import Settings
         from hermes_sts.tools import ToolRegistry, register_default_local_tools
         from hermes_sts.websearch import TavilySearchProvider
         s = Settings(llm_provider='openai_compatible', web_search_enabled=True, tavily_api_key='k')
         r = ToolRegistry()
         register_default_local_tools(r, s, web_search_provider=TavilySearchProvider(s))
         names = [t['function']['name'] for t in r.openai_tools()]
         assert 'web_search' in names, names
         print('OK', names) "
      2. 断言 stdout 以 'OK' 开头且包含 'web_search'
    Evidence: .sisyphus/evidence/task-7-register-websearch.txt

  Scenario: hermes 模式不注册 web_search
    Tool: Bash (python)
    Steps:
      1. python -c "
         from hermes_sts.config import Settings
         from hermes_sts.tools import ToolRegistry, register_default_local_tools
         from hermes_sts.websearch import TavilySearchProvider
         s = Settings(llm_provider='hermes_agent', web_search_enabled=True, tavily_api_key='k')
         r = ToolRegistry()
         register_default_local_tools(r, s, web_search_provider=TavilySearchProvider(s))
         names = [t['function']['name'] for t in r.openai_tools()]
         assert 'web_search' not in names, names
         print('OK', names) "
      2. 断言 stdout 开头 'OK' 且不含 web_search
    Evidence: .sisyphus/evidence/task-7-hermes-no-websearch.txt

  Scenario: handler 返回格式化结果
    Tool: Bash (python)
    Preconditions: 用 NoopWebSearchProvider 返空 list
    Steps:
      1. python -c "
         import asyncio
         from hermes_sts.config import Settings
         from hermes_sts.tools import ToolRegistry, register_default_local_tools
         from hermes_sts.websearch import NoopWebSearchProvider
         s = Settings(llm_provider='openai_compatible', web_search_enabled=True, tavily_api_key='k')
         r = ToolRegistry()
         register_default_local_tools(r, s, web_search_provider=NoopWebSearchProvider())  # noop 不会注册
         # 直接验证 Noop 注册失败场景：register 不执行 → 'web_search' not in
         print('OK') "
      2. 改用一个 FakeProvider 返回 [SearchHit(title='T', url='http://x', content='C')] 验证 handler
    Expected Result: handler('q') 返包含 "T" "http://x" "C" 的字符串；空结果返 "No results found."
    Evidence: .sisyphus/evidence/task-7-handler-format.txt
  ```

  **Evidence to Capture**:
  - [ ] task-7-register-websearch.txt
  - [ ] task-7-hermes-no-websearch.txt
  - [ ] task-7-handler-format.txt

  **Commit**: NO

---

- [x] 8. realtime.py 集成记忆注入 + record_turn 后台触发 + web_search 工具注入

  **What to do**:
  - 在 `RealtimeSession.__post_init__` 之外，由 server 注入 `memory: MemoryProvider` 和 `web_search: WebSearchProvider | None`（在 server.py 构建 RealtimeSession 时传入；ToolRegistry 仍 per-session）
  - 修改 `_ask_llm_with_tools(transcript, response_id, item_id, metrics, instructions)`：
    ```python
    instructions = instructions if instructions is not None else self._effective_instructions()
    # 新增：注入记忆
    instructions = await self._inject_memory(transcript, instructions)
    # 之后照旧：response = await self.llm.chat(transcript, instructions=instructions, tools=self.tools.openai_tools())
    ```
  - 新增 `async def _inject_memory(self, transcript, instructions) -> str`：
    - 若 `not self.settings.memory_enabled` 或 transcripts 空 或 `self.settings.memory_remember_in_hermes == False 且 llm_provider == hermes_agent`：return instructions 不变
    - `hits = await self.memory.recall(transcript, limit=settings.memory_recall_limit, min_score=settings.memory_recall_min_score)`
    - 若 `hits` 空返 instructions
    - 构造 block：`"\n\n参考记忆（不要逐条复述，只用于回答更准确）：\n" + "\n".join(f"- {h.abstract}" for h in hits[:settings.memory_recall_limit])`
    - budget = `settings.memory_injection_budget`；超长截断 hits 数量；若仍超预算截 block 并 logger.warning
    - 总长 instructions + block 超 2500 字（LLM 截断上限）：先保 persona，进一步截 block
    - 写指标：`ConfigStore.default().add_metric("memory_read", {"query": transcript[:80], "hits": len(hits), "ms": ...})`
  - 修改 `_ask_llm_with_tools` 返回前 answer 拼好后新增 `_maybe_record_turn`：
    - 仅当 `settings.llm_provider == "openai_compatible" and settings.memory_enabled` 执行
    - `asyncio.create_task(self.memory.record_turn(transcript, answer, session_id=self.session_id))` fire-and-forget；失败静默（provider 已吞异常）
    - 写指标 `memory_commit` 触发即写 (Victoria) / `memory_extract` (sqlite LLM 抽取) — 实际由 provider 写出，realtime 只写 `memory_record_turn` 一个 metric
  - 在 `__post_init__` 或 server.py 创建 session 后调 `register_default_local_tools(self.tools, settings, web_search_provider=web_search)`
    - 这里要小心：现有 `_apply_session_config` 会在 session.update 时 `set_client_tools` 覆盖 client_tools 但不动 local_tools → 保持兼容
  - WebSocket 断开 (`run` 的 `finally` 块) 时若 memory 是 OpenViking 后端：触发最终 fire-and-forget commit `asyncio.create_task(self.memory.final_commit(self.session_id))`（在 MemoryProvider 协议补一个默认 noop 方法 `final_commit(session_id)`，OV 覆盖实现）

  **Must NOT do**:
  - 不修改 `HermesAgentProvider` / `OpenAICompatibleProvider` 的 `chat()` 主体
  - 不在 `_inject_memory` 抛异常
  - 不在 record_turn 路径 await 阻塞 turn 返回
  - 不在 hermes 模式注册 web_search（由 Task 7 双重保障）
  - 不让 system prompt 注入超过 2500 字
  - 不让 record_turn 写入失败影响 answer 发送（answer 已经 pipeline 发完才触发 record）

  **Recommended Agent Profile**:
  - **Category**: `deep`
    - Reason: 关键路径、最小侵入原则、async 调度、字符预算、指标写入、与 server.py / tools.py 协调
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 14
  - **Blocked By**: Tasks 4, 5, 6, 7

  **References**:

  **Pattern References**:
  - `hermes_sts/realtime.py:647-708` — `_ask_llm_with_tools` 现有结构，挂钩点
  - `hermes_sts/realtime.py:546-552` — `_respond_with_agent_wait` instructions 传递链
  - `hermes_sts/realtime.py:73-75` — `__post_init__` ToolRegistry 注入点
  - `hermes_sts/realtime.py:1219-1255` — `_log_turn_metrics` 写 metric 范本，照此 add_metric

  **API/Type References**:
  - `hermes_sts/llm.py:209-221` — `_system_prompt` 在 2500 字截断，注入必须尊重此上限
  - `hermes_sts/memory.py` Task 2/4/5 输出 — MemoryProvider.recall / record_turn 签名
  - `hermes_sts/tools.py` Task 7 输出 — register_default_local_tools

  **External References**:
  - OpenViking opencode plugin 提交模式：fire-and-forget commit on session close

  **WHY Each Reference Matters**:
  - 2500 截断是 Metis 指出的关键约束，违规会导致 persona 被吃掉

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: openai_compatible + memory_enabled → 注入 + record 触发
    Tool: Bash (python) — 用 bare_session 风格 + FakeMemoryProvider
    Preconditions: 写一个 FakeMemoryProvider.recall 返 [MemoryHit(uri='m1', content='喜欢深色', abstract='喜欢深色')]
    Steps:
      1. python -c "
         import asyncio
         from hermes_sts.config import Settings
         from hermes_sts.realtime import RealtimeSession
         from hermes_sts.memory import MemoryHit, NoopMemoryProvider
         calls = []
         class FakeMemory:
             async def recall(self, q, *, limit=5, min_score=0.0):
                 calls.append(q); return [MemoryHit(uri='m1', content='喜欢深色', abstract='喜欢深色')]
             async def record_turn(self, t, a, *, session_id): calls.append(('rec', t, a))
             async def list_memories(self, **k): return []
             async def get_memory(self, u): return None
             async def update_memory(self, u, **k): pass
             async def delete_memory(self, u): return True
             async def add_memory(self, **k): return 'new'
             def stats(self): return {'enabled': True}
             async def final_commit(self, sid): pass
         session = RealtimeSession.__new__(RealtimeSession)
         # 配齐 bare_session() 关键字段 + memory
         session.settings = Settings(llm_provider='openai_compatible', memory_enabled=True)
         session.memory = FakeMemory()
         session.session_id = 'sess_test'
         instr = asyncio.run(session._inject_memory('深色主题', '你是同伴'))
         assert '喜欢深色' in instr, instr
         assert '你是同伴' in instr
         print('OK', instr[-80:])"
      2. 断言 stdout 以 'OK' 开头，且注入文字包含 abstract
    Expected Result: 记忆 abstract 拼到 instructions 后段
    Evidence: .sisyphus/evidence/task-8-inject-memory.txt

  Scenario: hermes + memory_remember_in_hermes=False → 不注入
    Tool: Bash (python)
    Steps:
      1. python -c "
         import asyncio
         from hermes_sts.config import Settings
         from hermes_sts.realtime import RealtimeSession
         session = RealtimeSession.__new__(RealtimeSession)
         session.settings = Settings(llm_provider='hermes_agent', memory_enabled=True, memory_remember_in_hermes=False)
         class P:
             async def recall(self, *a, **k): raise RuntimeError('should not be called')
         session.memory = P()
         instr = asyncio.run(session._inject_memory('hi', 'persona'))
         assert instr == 'persona'
         print('OK') "
      2. 断言 stdout == 'OK'
    Evidence: .sisyphus/evidence/task-8-hermes-no-inject.txt

  Scenario: memory 注入预算超 500 字截断
    Tool: Bash (python)
    Preconditions: FakeMemory.recall 返 10 条 abstract 各 100 字
    Steps:
      1. python -c "
         import asyncio
         from hermes_sts.config import Settings
         from hermes_sts.realtime import RealtimeSession
         from hermes_sts.memory import MemoryHit
         class P:
             async def recall(self, q, *, limit=5, min_score=0.0):
                 return [MemoryHit(uri=f'm{i}', content=f'abstract_{i}_'+'x'*100, abstract=f'abstract_{i}_'+'x'*100) for i in range(10)]
         session = RealtimeSession.__new__(RealtimeSession)
         session.settings = Settings(llm_provider='openai_compatible', memory_enabled=True, memory_injection_budget=500, memory_recall_limit=5)
         session.memory = P()
         session.session_id = 's'
         instr = asyncio.run(session._inject_memory('q', 'p'))
         # block 部分（含'参考记忆'前缀）应 <= 500 + 头部开销
         block_part = instr.split('参考记忆', 1)[-1] if '参考记忆' in instr else ''
         print('OK', len(block_part)) "
      2. 断言 stdout 以 'OK' 开头，len <= 700（含'参考记忆...\\n'前缀）
    Evidence: .sisyphus/evidence/task-8-inject-budget.txt

  Scenario: 端到端 ws turn 触发 memory_record metric
    Tool: Bash (python scripts/smoke/ws_turn_smoke.py 配合日志)
    Preconditions: 启动服务 openai_compatible + memory_enabled=true + memory_provider=sqlite
    Steps:
      1. 跑 ws_turn_smoke 完成一轮 turn
      2. curl http://127.0.0.1:8765/api/metrics | jq '.metrics[] | select(.kind=="memory_read")'
      3. 断言返回至少 1 条 memory_read
    Expected Result: metrics 表含 memory_read 条目
    Evidence: .sisyphus/evidence/task-8-ws-metric.txt
  ```

  **Evidence to Capture**:
  - [ ] task-8-inject-memory.txt
  - [ ] task-8-hermes-no-inject.txt
  - [ ] task-8-inject-budget.txt
  - [ ] task-8-ws-metric.txt

  **Commit**: NO（Wave 2 共 1 commit）

---

- [x] 9. config_store.ensure_defaults 注入 memory/websearch 默认值

  **What to do**:
  - 在 `hermes_sts/config_store.py` 的 `ensure_defaults()` 末尾的 `for key, value in {...}.items()` 块追加：
    ```python
    "memory_enabled": False,
    "memory_provider": "sqlite",
    "memory_remember_in_hermes": True,
    "web_search_enabled": False,
    ```
  - 单独开一个 `for key, value in {...}` 块（区分 always-defaults vs first-install default），用 `insert or ignore` 让首装填但不覆盖用户改过的值

  **Must NOT do**:
  - 不覆盖已存在的 memory_provider 设置（首装默认 sqlite，用户改 openviking 后不能被覆盖回 sqlite）
  - 不在 ensure_defaults 跑迁移（如 sqlite→openviking 不应自动发生）

  **Recommended Agent Profile**:
  - **Category**: `quick`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: None
  - **Blocked By**: Task 1

  **References**:

  **Pattern References**:
  - `hermes_sts/config_store.py:271-355` — `ensure_defaults` 现有结构，照搬 `insert or ignore` 模式

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 首装注入默认 + 已有设置不覆盖
    Tool: Bash (python)
    Preconditions: 用 tempdir 建 fresh db；第二次模拟用户改过 memory_provider=openviking
    Steps:
      1. python -c "
         import tempfile, os
         from pathlib import Path
         from hermes_sts.config_store import ConfigStore
         with tempfile.TemporaryDirectory() as t:
             os.environ['HERMES_STS_CONFIG_DB'] = str(Path(t)/'c.sqlite3')
             store = ConfigStore.default()
             d = store.settings_dict()
             assert d.get('memory_provider') == 'sqlite', d.get('memory_provider')
             assert d.get('memory_enabled') == False
             # 用户改 openviking
             store.set_settings({'STS_MEMORY_PROVIDER': 'openviking', 'OPENVIKING_API_KEY': 'k'})
             store.ensure_defaults()  # 二次调用不能覆盖
             d2 = store.settings_dict()
             assert d2['memory_provider'] == 'openviking', d2['memory_provider']
             print('OK') "
      2. 断言 stdout == 'OK'
    Evidence: .sisyphus/evidence/task-9-defaults-not-overwrite.txt
  ```

  **Evidence to Capture**:
  - [ ] task-9-defaults-not-overwrite.txt

  **Commit**: NO

---

- [x] 10. admin.py REST /api/memories CRUD + admin_state.memory + _requires_rebuild

  **What to do**:
  - 在 `hermes_sts/admin.py` 的 `create_admin_router` 工厂内追加端点：
    - `GET /api/memories?limit=50&offset=0&q=` → `{"memories": [MemoryHit as dict...], "total"?}`
    - `POST /api/memories` body `{content: str, category: str = "manual", tags: list[str] = []}` → `{"ok": true, "uri": "..."}`
    - `GET /api/memories/{uri:path}` → `{"memory": {...}}` 或 404
    - `PUT /api/memories/{uri:path}` body `{content: str, category: str? = null, tags: list[str]? = null}` → `{"ok": true}`
    - `DELETE /api/memories/{uri:path}?recursive=false` → `{"ok": true}` 或 404
    - `POST /api/memories/recall` body `{query: str, limit: int = 5, min_score: float = 0.0}` → `{"hits": [...], "ms": int}`  这是 UI"试查"用，复用 provider.recall
    - `GET /api/memories/activity?limit=20` → 从 runtime_metrics 拿 kind in (memory_read, memory_commit, memory_extract, memory_record_turn) 倒序前 limit 条，结构 `{activity: [{kind, value, created_at}]}`
  - 需要从 settings 取到 memory provider 实例：注入 `get_memory: Callable[[], MemoryProvider] | None = None` 参数到 `create_admin_router(settings, rebuild_components, get_llm, get_memory)`，由 server.py 提供 `lambda: app.state.memory`
  - 在 `_settings_payload` 的 visible 新增 `"memory"` 段含所有 memory_* / openviking_* / tavily_* / web_search_* 字段（多数字段已在 Task 1 加入）
  - 在 `admin_state` 响应追加 `"memory": get_memory().stats() if get_memory and get_memory() else None`
  - 在 Pydantic 模型区追加 `MemoryAddRequest`、`MemoryUpdateRequest`、`MemoryRecallRequest`
  - 在 `_requires_rebuild` 加入：`memory_enabled, memory_provider, web_search_enabled, tavily_api_key, openviking_base_url, openviking_api_key, openviking_account, openviking_user, sqlite_memory_path, memory_extract_enabled, memory_commit_interval_turns, memory_commit_idle_seconds, memory_recall_limit, memory_recall_min_score, memory_injection_budget`（这些切换需 rebuild provider）
  - 注意：所有 REST handler 调 provider 时 `await get_memory()()` → 失败 raise 自动 500，但 provider 内部已吞外部 HTTP error，所以不会因 OV down 500；只有本地 sqlite 异常或参数错才会 5xx
  - GET list 用 `provider.list_memories`；PUT / DELETE / GET single 复用 provider

  **Must NOT do**:
  - 不在 admin.py 直连 OpenViking HTTP（必须经 provider 抽象）
  - 不在 list 接口直接调 fs/ls（统一走 provider.list_memories，Provider 内部决定走 OV fs/ls 还是 sqlite SELECT）
  - 不用 `stats/memories` 实现列表

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2
  - **Blocks**: Task 12
  - **Blocked By**: Tasks 4, 5, 6

  **References**:

  **Pattern References**:
  - `hermes_sts/admin.py:238-265` — `POST /api/personas` + Pydantic + store.upsert + rebuild 完整范本
  - `hermes_sts/admin.py:140-164` — `admin_state` payload 组装模式
  - `hermes_sts/admin.py:480-483` — `metrics` endpoint 简单范本
  - `hermes_sts/admin.py:616-627` — `_llm_context_payload` 把服务内对象转 payload 模式

  **WHY Each Reference Matters**:
  - admin.py 的工厂 + Pydantic + settings patch 是项目唯一的 REST 范本

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: /api/memories GET 返 list
    Tool: Bash (curl + sqlite memory provider)
    Preconditions: 启动 server memory_provider=sqlite，先 add 一条
    Steps:
      1. curl -s -X POST http://127.0.0.1:8765/api/memories -H "Content-Type: application/json" -d '{"content":"测试条目","category":"test"}' | jq '.uri'
      2. curl -s "http://127.0.0.1:8765/api/memories?limit=50" | jq '.memories | length'
      3. 断言 ≥ 1
    Expected Result: list 返数组且包含刚 add 条
    Evidence: .sisyphus/evidence/task-10-rest-list.txt

  Scenario: PUT 编辑记忆
    Tool: Bash (curl)
    Steps:
      1. u=$(curl -s -X POST http://127.0.0.1:8765/api/memories -H "Content-Type: application/json" -d '{"content":"原"}' | jq -r '.uri')
      2. curl -s -X PUT "http://127.0.0.1:8765/api/memories/$u" -H "Content-Type: application/json" -d '{"content":"新"}' | jq -r '.ok'
      3. curl -s "http://127.0.0.1:8765/api/memories/$u" | jq -r '.memory.content'
      4. 断言 == "新"
    Expected Result: 编辑写回成功
    Evidence: .sisyphus/evidence/task-10-rest-put.txt

  Scenario: DELETE 删除
    Tool: Bash (curl)
    Steps:
      1. u=$(curl -s -X POST .../api/memories -H "Content-Type: application/json" -d '{"content":"删"}' | jq -r '.uri')
      2. curl -s -X DELETE "http://127.0.0.1:8765/api/memories/$u" | jq -r '.ok'
      3. curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:8765/api/memories/$u"
      4. 断言最后返 404 或 .memory == null
    Expected Result: 删除再查返 null 或 404
    Evidence: .sisyphus/evidence/task-10-rest-delete.txt

  Scenario: recall 手动查询
    Tool: Bash (curl)
    Steps:
      1. curl -s -X POST http://127.0.0.1:8765/api/memories/recall -H "Content-Type: application/json" -d '{"query":"天气","limit":5}' | jq '.hits | type'
      2. 断言 == "array"
    Expected Result: 返回 hits 数组（空也算 array）
    Evidence: .sisyphus/evidence/task-10-rest-recall.txt

  Scenario: admin_state.memory 块存在
    Tool: Bash (curl)
    Steps:
      1. curl -s http://127.0.0.1:8765/api/admin/state | jq '.memory'
      2. 断言返回含 enabled / provider 字段
    Expected Result: 不为 null
    Evidence: .sisyphus/evidence/task-10-admin-state-memory.txt

  Scenario: 切 memory_provider 触发 rebuild
    Tool: Bash (curl)
    Steps:
      1. curl -s -X PATCH http://127.0.0.1:8765/api/settings -H "Content-Type: application/json" -d '{"values":{"STS_MEMORY_PROVIDER":"openviking","OPENVIKING_API_KEY":"k"}}' | jq '.rebuild_required'
      2. 断言包含 'memory_provider'
    Expected Result: rebuild_required 数组含此键
    Evidence: .sisyphus/evidence/task-10-rebuild-trigger.txt
  ```

  **Evidence to Capture**:
  - [ ] task-10-rest-list.txt
  - [ ] task-10-rest-put.txt
  - [ ] task-10-rest-delete.txt
  - [ ] task-10-rest-recall.txt
  - [ ] task-10-admin-state-memory.txt
  - [ ] task-10-rebuild-trigger.txt

  **Commit**: NO（Wave 2 共 1 commit）

---

- [x] 11. README roadmap 更新

  **What to do**:
  - 在 `README.md` 的 ## Roadmap 段：
    - 把 "1. Lightweight web search tool for direct LLM mode" 标为 ✅ 已实现
    - 把 "3. Local memory for direct LLM mode" 标为 ✅ 已实现（保留另起 plan 的细节）
    - 新增 ## Direct LLM + Memory Mode 段，介绍：
      - `STS_LLM_PROVIDER=openai_compatible` 切到直连模式
      - `STS_MEMORY_ENABLED=true` + `STS_MEMORY_PROVIDER=sqlite|openviking|noop`
      - `STS_WEB_SEARCH_ENABLED=true` + `TAVILY_API_KEY`
      - Hermes 模式只读注入记忆、不写
      - UI 控制台新增"记忆"标签页
      - 后端抽象：MemoryProvider / WebSearchProvider Protocol
      - pageSize 限制 + commit 周期后台 + 失败静默降级说明
  - 不删原 Roadmap 文本，仅更新状态

  **Must NOT do**:
  - 不写使用教程，参考现有 README 风格只列关键 settings
  - 不写 API 文档

  **Recommended Agent Profile**:
  - **Category**: `writing`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 2（与重要任务并行）
  - **Blocks**: None
  - **Blocked By**: None

  **References**:

  **Pattern References**:
  - `README.md` 的 ## Roadmap 段，照其行文风格更新
  - `README.md` 的 ## Provider Configuration 段，照其描述风格介绍直连模式

  **Acceptance Criteria**:
  - [ ] README.md diff 命中 `Direct LLM + Memory Mode` 字串
  - [ ] Roadmap 1 和 3 不再 "Near-term work"（变为已完成或移到下方"Done"）

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: README 包含 Direct LLM + Memory Mode 段
    Tool: Bash (grep)
    Steps:
      1. grep -c "Direct LLM + Memory Mode" README.md
      2. 断言 ≥ 1
    Expected Result: 段标题写入
    Evidence: .sisyphus/evidence/task-11-readme-section.txt

  Scenario: Roadmap 1 3 标记完成
    Tool: Bash (grep)
    Steps:
      1. grep -E "Implemented|Done" README.md | grep -iE "web search|memory"
    Expected Result: 出现"已实现"标记
    Evidence: .sisyphus/evidence/task-11-readme-roadmap-done.txt
  ```

  **Evidence to Capture**:
  - [ ] task-11-readme-section.txt
  - [ ] task-11-readme-roadmap-done.txt

  **Commit**: NO（与 Wave 2 共 1 commit）

---

- [x] 12. admin_ui/src/main.tsx 新增 Memory 面板

  **What to do**:
  - 在 `admin_ui/src/main.tsx` 顶部 nav 注册新标签 "记忆"（`navItems` 数组，按 Task 5 Reference 提到的位置 `main.tsx:65-70`、`185-188`）
  - 实现 `MemoryPanel` 组件（参照现有 Panel 组件结构，react + tailwind 同款风格）：
    - 顶部状态块：当前 memory.provider / enabled / stats.count（可恢复 admin_state.memory）
    - 启用开关（PATCH `/api/settings` `STS_MEMORY_ENABLED`）
    - provider 选择器：sqlite / openviking / noop（PATCH `STS_MEMORY_PROVIDER`，OV 选中显示 openviking_base_url / api_key / account / user 一组输入）
    - web_search 启用 + tavily_api_key 输入 + tavily_search_depth 下拉（only ultra-fast/fast/basic）+ timeout number 最大 3.0
    - 列表区：搜索框 `q` + 表格（uri 截断 / category / abstract 按钮 (点开查 content) / 操作按钮 编辑/删除）
    - 分页：limit 50、offset pagination 简单实现
    - 添加按钮：弹模态填 content / category / tags → POST
    - 编辑模态：textarea content、category、tags → PUT
    - 删除：confirm → DELETE 后刷新 list
    - 活动流：从 `/api/memories/activity` 拉最近 20 条，时间倒序，每条显示 kind（memory_read/commit/extract）+ 1 行 value 摘要
    - "试查"按钮：复用 /api/memories/recall 显示返回 hits 顶部 5 条
  - 优雅失败：网络 5xx 显示红条提示但不挂死面板
  - 沿用现有 components（不要新引 npm 包）
  - 全部 react function component + hooks
  - 用 css class 与现有面板视觉一致（深色 dark / scanner 风格，参考 DashboardPanel 等已有组件）

  **Must NOT do**:
  - 不引新 npm 依赖
  - 不做记忆分桶 UI / 记忆分类树浏览
  - 不做 commit 状态轮询
  - 不做 Tavily 查询缓存
  - 不在 UI 硬编码 OV URL（用 settings.openviking_base_url）

  **Recommended Agent Profile**:
  - **Category**: `visual-engineering`
    - Reason: React + tailwind 单文件，新增完整面板，无外部依赖
  - **Skills**: [`frontend-design`]
    - `frontend-design`: 风格一致性、不堆默认模板
  - **Skills Evaluated but Omitted**:
    - `playwright`: 仅最终验证用，不用于开发本任务

  **Parallelization**:
  - **Can Run In Parallel**: NO
  - **Parallel Group**: Wave 3 (独自)
  - **Blocks**: Task 14 (UI 部分)
  - **Blocked By**: Task 10（API 必须先就位）

  **References**:

  **Pattern References**:
  - `admin_ui/src/main.tsx:65-70` — `navItems` 标签数组
  - `admin_ui/src/main.tsx:185-188` — 标签条件渲染
  - 现有 DashboardPanel / RoleStudio / VoiceWorkshop 组件 — 视觉和状态管理参考
  - `admin_ui/src/main.tsx` 内已有 fetch + state 模式（搜索 `fetch('/api/admin/state')` 找范本）
  - `admin_ui/vite.config.ts` — `/api` proxy 配置，本任务不动

  **API/Type References**:
  - `hermes_sts/admin.py` Task 10 输出 — `/api/memories` 全套 REST、`/api/admin/state.memory` payload shape、`/api/settings` PATCH 字段

  **WHY Each Reference Matters**:
  - 单文件 1508 行的主结构必须照搬，否则陷入风格冲突

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: Memory 标签可见
    Tool: Playwright
    Preconditions: server 在 127.0.0.1:8765 跑
    Steps:
      1. browser.goto http://127.0.0.1:8765/
      2. click text="记忆"
      3. expect page to contain "启用" / "Provider" 字段
    Expected Result: 切到记忆面板
    Evidence: .sisyphus/evidence/task-12-ui-tab.png

  Scenario: 启用开关切换且触发 settings PATCH
    Tool: Playwright
    Steps:
      1. goto 记忆面板
      2. click "启用" checkbox
      3. expect network call PATCH /api/settings with body containing STS_MEMORY_ENABLED
      4. expect status text "已启用"
    Expected Result: toggle 触发后端写入 + UI 反馈
    Evidence: .sisyphus/evidence/task-12-ui-toggle.png

  Scenario: 添加新记忆
    Tool: Playwright
    Steps:
      1. 点"添加"
      2. 填 content="测试记忆条目" category="manual"
      3. 提交
      4. 列表刷新后应出现新条目 / category=manual
    Expected Result: 列表含新条
    Evidence: .sisyphus/evidence/task-12-ui-add.png

  Scenario: 编辑记忆内容
    Tool: Playwright
    Steps:
      1. 点某条记忆的"编辑"按钮
      2. 修改 content
      3. 保存
      4. 重新点开应见新 content
    Expected Result: 修改持久
    Evidence: .sisyphus/evidence/task-12-ui-edit.png

  Scenario: 删除记忆
    Tool: Playwright
    Steps:
      1. 点某条记忆的"删除"按钮
      2. confirm dialog 接受
      3. 列表刷新不再包含此条
    Expected Result: 删除生效
    Evidence: .sisyphus/evidence/task-12-ui-delete.png

  Scenario: 试查 recall
    Tool: Playwright
    Steps:
      1. 在试查框输入"咖啡"
      2. 点查询
      3. 返回 hits 至少不报错(空也算)
    Expected Result: 返回 hits 区显示
    Evidence: .sisyphus/evidence/task-12-ui-recall.png
  ```

  **Evidence to Capture**:
  - [ ] task-12-ui-tab.png
  - [ ] task-12-ui-toggle.png
  - [ ] task-12-ui-add.png
  - [ ] task-12-ui-edit.png
  - [ ] task-12-ui-delete.png
  - [ ] task-12-ui-recall.png

  **Commit**: Wave 3 共 1 commit

---

- [x] 13. tests/test_memory_websearch.py 单测

  **What to do**:
  - 新建 `tests/test_memory_websearch.py`，沿用 `unittest.TestCase` + `asyncio.run()` 风格（不引 pytest、不引 httpx mock）
  - 文件组织 4 个 TestCase：
    - `NoopMemoryProviderTests`: 全方法返空 / 不抛
    - `SqliteMemoryProviderTests`: tempdir sqlite memory.sqlite3；add→recall→list→get→update→delete 全流程；LLM 抽取用一个 inline `FakeLlm` 注入，覆盖 (1) 抽出 1 条 → 写入 (2) 抽出空 → 不写 (3) LLM raise → 静默不写
    - `OpenVikingMemoryProviderTests`: 用 fake httpx 客户端（直接 monkeypatch 类的 `_client` 替成内存模拟）；覆盖 (1) recall 不可达返空 (2) record_turn 不阻塞 + 派发 commit fire-and-forget (3) stats down 不抛
    - `TavilySearchProviderTests`: monkeypatch `httpx.AsyncClient.post` 或者把 base_url 指向不可达 → 返空；verify `description()` 含 'tavily'
  - `FakeLlm`、`FakeMemoryProvider`、`FakeWebSearchProvider` 等内联类放在文件顶部
  - 全部测试遵循"先 fail 再 green"思路由任务 13 后置运行做

  **Must NOT do**:
  - 不引 `pytest-httpx` / `responses`
  - 不发真实 HTTP 测试 OV / Tavily
  - 不污染 `data/hermes_sts.sqlite3`（用 tempdir）
  - 不用 pytest fixture（保持 unittest 风格）

  **Recommended Agent Profile**:
  - **Category**: `unspecified-high`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4
  - **Blocks**: Task 14
  - **Blocked By**: Tasks 3, 4, 5, 6

  **References**:

  **Pattern References**:
  - `tests/test_core.py:26-46` — `DummyChatProvider(BaseOpenAIChatProvider)` 测试 stub 模式
  - `tests/test_core.py:61-74` — `bare_session()` helper 模式
  - `tests/test_core.py:234-269` — inline `FakeLlm` + monkey-patch `_send` 模式
  - `tests/test_core.py:545-615` — ConfigStore tempdir 测试模式

  **WHY Each Reference Matters**:
  - 这是项目内当今唯一的测试范式，必须严格沿用

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 新测试全 PASS
    Tool: Bash
    Preconditions: 提交测试文件
    Steps:
      1. python -m unittest tests.test_memory_websearch -v 2>&1 | tee /tmp/t13.log
      2. tail -n 5 /tmp/t13.log
      3. 断言末行含 "OK" 或 "OK" 前一行以 "Ran N tests" 开头且下一行 "OK"
    Expected Result: 全部测试用例通过
    Evidence: .sisyphus/evidence/task-13-unittest-pass.txt
  ```

  **Evidence to Capture**:
  - [ ] task-13-unittest-pass.txt

  **Commit**: NO（Wave 4 共 1 commit）

---

- [x] 14. tests/test_core.py 扩展 + realtime 集成测

  **What to do**:
  - 在 `tests/test_core.py` 追加（或新建 `tests/test_realtime_memory.py`）：
    - `test_inject_memory_appends_hits_to_instructions`: bare_session + FakeMemoryProvider.recall 返 [MemoryHit]，断言 instructions 含 abstract
    - `test_inject_memory_skips_when_disabled`: Settings(memory_enabled=False) → recall 不被调用（FakeMemory.recall raise 即证）
    - `test_inject_memory_skips_in_hermes_when_disabled`: Settings(llm_provider='hermes_agent', memory_remember_in_hermes=False) → 不注入
    - `test_inject_memory_budget_caps_block_at_500`: recall 返 10 条 100 字 abstract，断言 block ≤ 700 字符（含前缀）
    - `test_record_turn_dispatched_after_answer`: 覆盖 _respond_with_agent_wait 链路，FakeLlm 返一文本，事后 FakeMemory.record_turn 被调用一次（伪 async，task 用 `asyncio.run` 同步跑完验证）
    - `test_record_turn_only_in_openai_compatible`: hermes 模式 FakeMemory.record_turn 不被调用
    - `test_register_default_local_tools_in_openai_mode`: ToolRegistry + register_default_local_tools + Settings(openai_compatible + web_search_enabled + tavily_api_key) → openai_tools 含 web_search
    - `test_register_default_local_tools_skips_in_hermes`: hermes 模式 register → openai_tools 不含 web_search
  - 不破坏现有 35 个测试

  **Must NOT do**:
  - 不删现有测试
  - 不引真实网络调用
  - 不引入 pytest fixtures

  **Recommended Agent Profile**:
  - **Category**: `unspecified-low`
  - **Skills**: []

  **Parallelization**:
  - **Can Run In Parallel**: YES
  - **Parallel Group**: Wave 4
  - **Blocks**: F1-F4
  - **Blocked By**: Tasks 8, 13

  **References**:

  **Pattern References**:
  - `tests/test_core.py:179-213` — `_run_serialized_turn` async test 模式
  - `tests/test_core.py:234-269` — `_ask_llm_with_tools` integration test with FakeLlm
  - `tests/test_core.py:270-312` — `_process_tool_result_turn` assertion 模式

  **Acceptance Criteria**:

  **QA Scenarios (MANDATORY)**:

  ```
  Scenario: 完整测试套件全 PASS
    Tool: Bash
    Preconditions: 实现完成
    Steps:
      1. python -m unittest discover -s tests -p "test_*.py" -v 2>&1 | tee /tmp/t14.log
      2. 断言最后两行 "Ran N tests" + "OK"
      3. grep "FAILED|ERROR" /tmp/t14.log → 0 命中
    Expected Result: 全 PASS
    Evidence: .sisyphus/evidence/task-14-full-suite-pass.txt
  ```

  **Evidence to Capture**:
  - [ ] task-14-full-suite-pass.txt

  **Commit**: Wave 4 共 1 commit

---

> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
>
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**

- [ ] F1. **Plan Compliance Audit** — `oracle`
  Read the plan end-to-end. For each "Must Have": verify implementation exists (read file, curl endpoint, run command). For each "Must NOT Have": search codebase for forbidden patterns — reject with file:line if found. Check evidence files exist in `.sisyphus/evidence/`. Compare deliverables against plan.
  Output: `Must Have [N/N] | Must NOT Have [N/N] | Tasks [N/N] | VERDICT: APPROVE/REJECT`

- [ ] F2. **Code Quality Review** — `unspecified-high`
  Run `python -m compileall -q hermes_sts tests` + `python -m unittest discover -s tests -p "test_*.py" -v` + `ruff check hermes_sts tests`（若 ruff 可用）. Review all changed files for: `# type: ignore` 滥用、empty catches、print in prod、commented-out code、unused imports. Check AI slop: 过度注释、过度抽象、generic names (data/result/item/temp). 验证没引入 `pytest-httpx` / `responses`. 验证没在 `_ask_llm_with_tools` 关键路径同步调 commit。
  Output: `Build [PASS/FAIL] | Lint [PASS/FAIL] | Tests [N pass/N fail] | Files [N clean/N issues] | VERDICT`

- [ ] F3. **Real Manual QA** — `unspecified-high` (+ `playwright` skill)
  Start from clean state. Execute EVERY QA scenario from EVERY task — follow exact steps, capture evidence. Test cross-task integration: 切到 `openai_compatible`+memory+websearch 完整一轮对话；切回 `hermes_agent` 验证不写；切到 `sqlite` 回退验证；kill OpenViking 验证降级。Save screenshots / curl outputs / unittest outputs to `.sisyphus/evidence/final-qa/`.
  Output: `Scenarios [N/N pass] | Integration [N/N] | Edge Cases [N tested] | VERDICT`

- [ ] F4. **Scope Fidelity Check** — `deep`
  For each task: read "What to do", read actual diff (git log/diff). Verify 1:1 — everything in spec was built, nothing beyond spec was built. Check "Must NOT do" compliance: 没改 LLMProvider 子类 chat 主体？没装 SDK？没在 hermes 模式注册 web_search？TT/STT/VAD 文件无 diff？Flag unaccounted changes.
  Output: `Tasks [N/N compliant] | Contamination [CLEAN/N issues] | Unaccounted [CLEAN/N files] | VERDICT`

---

## Commit Strategy

- **Wave 1 共 1 commit**: `feat(memory): add memory/websearch providers abstractions and SQLite+OpenViking backends` — `hermes_sts/memory.py`, `hermes_sts/websearch.py`
- **Wave 2 共 1 commit**: `feat(sts): integrate memory injection and web_search tool into realtime` — `hermes_sts/config.py`, `hermes_sts/config_store.py`, `hermes_sts/tools.py`, `hermes_sts/realtime.py`, `hermes_sts/admin.py`, `README.md`
- **Wave 3 共 1 commit**: `feat(admin-ui): add memory management panel` — `admin_ui/src/main.tsx`
- **Wave 4 共 1 commit**: `test(memory): add unit tests for memory/websearch providers and realtime integration` — `tests/test_memory_websearch.py`, `tests/test_core.py`
- 每个 commit 前 pre-commit 跑 `python -m compileall -q hermes_sts tests`（与 `run_tests.sh` 第一阶段一致）

---

## Success Criteria

### Verification Commands

```bash
# 全单测
python -m unittest discover -s tests -p "test_*.py" -v
# Expected: 全 PASS

# 直连模式 + 记忆 + 搜索端到端
curl -s -X PATCH http://127.0.0.1:8765/api/settings \
  -H "Content-Type: application/json" \
  -d '{"values":{"STS_LLM_PROVIDER":"openai_compatible","memory_enabled":true,"memory_provider":"sqlite","web_search_enabled":true,"TAVILY_API_KEY":"tvly-test"}}' \
  | jq '.changed | keys'
# Expected: 包含 memory_enabled, memory_provider, web_search_enabled, llm_provider

# UI 记忆面板可达
curl -s http://127.0.0.1:8765/api/memories | jq '.memories | type'
# Expected: "array"

# 记忆读指标写入
curl -s http://127.0.0.1:8765/api/metrics | jq '.metrics[] | select(.kind=="memory_read") | .kind'
# Expected: "memory_read" (≥ 1 after a turn with memory enabled)
```

### Final Checklist

- [ ] All "Must Have" present
- [ ] All "Must NOT Have" absent
- [ ] `python -m unittest discover` 全 PASS
- [ ] `openai_compatible` + memory + websearch 切换后跑通一轮 ws turn
- [ ] `hermes_agent` 切回后记忆只读、web_search 不注册、record_turn 不触发
- [ ] 切到 `memory_provider=sqlite`、移除 OpenViking，`/api/memories` 仍可用
- [ ] UI 记忆面板：list / search / add / edit / delete / enable toggle 全部操作可执行
- [ ] 失败场景：OpenViking down / Tavily 超时 / API key 未配 → 对话不挂死、logs 有 WARNING
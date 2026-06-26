from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from hermes_sts.llm import LLMProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemoryHit:
    uri: str
    content: str
    abstract: str
    score: float = 0.0
    category: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    source: str = ""


class MemoryProvider(Protocol):
    async def recall(self, query: str, *, limit: int = 5, min_score: float = 0.0) -> list[MemoryHit]:
        ...

    async def record_turn(self, transcript: str, answer: str, *, session_id: str) -> None:
        ...

    async def list_memories(self, *, limit: int = 50, offset: int = 0, q: str = "") -> list[MemoryHit]:
        ...

    async def get_memory(self, uri: str) -> MemoryHit | None:
        ...

    async def update_memory(
        self,
        uri: str,
        *,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        ...

    async def delete_memory(self, uri: str) -> bool:
        ...

    async def add_memory(
        self,
        *,
        content: str,
        category: str = "manual",
        tags: list[str] | None = None,
    ) -> str:
        ...

    async def final_commit(self, session_id: str) -> None:
        ...

    def stats(self) -> dict[str, Any]:
        ...


class NoopMemoryProvider:
    async def recall(self, query: str, *, limit: int = 5, min_score: float = 0.0) -> list[MemoryHit]:
        return []

    async def record_turn(self, transcript: str, answer: str, *, session_id: str) -> None:
        pass

    async def list_memories(self, *, limit: int = 50, offset: int = 0, q: str = "") -> list[MemoryHit]:
        return []

    async def get_memory(self, uri: str) -> MemoryHit | None:
        return None

    async def update_memory(
        self,
        uri: str,
        *,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        pass

    async def delete_memory(self, uri: str) -> bool:
        return False

    async def add_memory(
        self,
        *,
        content: str,
        category: str = "manual",
        tags: list[str] | None = None,
    ) -> str:
        return f"noop://{uuid.uuid4().hex}"

    async def final_commit(self, session_id: str) -> None:
        pass

    def stats(self) -> dict[str, Any]:
        return {"enabled": False, "provider": "noop", "count": 0}


EXTRACT_PROMPT = (
    "从以下对话中提取值得长期记忆的事实信息，返回JSON数组(0-2项)。"
    "每项: {\"content\": \"事实\", \"category\": \"preferences|facts|events|personal\", \"tags\": [\"标签\"]}。"
    "闲聊或无关内容返回空数组。只返回JSON，不要其他文字。"
)


class SqliteMemoryProvider:
    """Local SQLite-backed memory provider with FTS5 full-text search."""

    def __init__(self, settings, llm: LLMProvider | None = None):
        self.settings = settings
        self.llm = llm
        self._lock = threading.Lock()
        self._bg_tasks: set[asyncio.Task] = set()
        path = Path(settings.sqlite_memory_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._fts5_ok = True
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS memories (
                  id TEXT PRIMARY KEY,
                  content TEXT NOT NULL,
                  abstract TEXT NOT NULL,
                  category TEXT NOT NULL DEFAULT 'manual',
                  tags TEXT NOT NULL DEFAULT '',
                  source TEXT NOT NULL DEFAULT 'manual',
                  turn_id TEXT NOT NULL DEFAULT '',
                  created_at REAL NOT NULL,
                  updated_at REAL NOT NULL
                );
                """
            )
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5("
                    "content, abstract, tags, content='memories', content_rowid='rowid'"
                    ");"
                )
                self._conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN "
                    "INSERT INTO memories_fts(rowid, content, abstract, tags) "
                    "VALUES (new.rowid, new.content, new.abstract, new.tags); END;"
                )
                self._conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN "
                    "INSERT INTO memories_fts(memories_fts, rowid, content, abstract, tags) "
                    "VALUES('delete', old.rowid, old.content, old.abstract, old.tags); END;"
                )
                self._conn.execute(
                    "CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN "
                    "DELETE FROM memories_fts WHERE rowid=old.rowid; "
                    "INSERT INTO memories_fts(rowid, content, abstract, tags) "
                    "VALUES (new.rowid, new.content, new.abstract, new.tags); END;"
                )
                self._conn.commit()
            except sqlite3.OperationalError as exc:
                logger.warning("FTS5 unavailable, falling back to LIKE search: %s", exc)
                self._fts5_ok = False

    def _db_fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.fetchall()

    def _db_fetchone(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.fetchone()

    def _db_execute(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    @staticmethod
    def _row_to_hit(row: sqlite3.Row, *, score: float = 0.0) -> MemoryHit:
        tags_str = row["tags"] or ""
        tags = [t for t in tags_str.split(",") if t] if tags_str else []
        return MemoryHit(
            uri=row["id"],
            content=row["content"],
            abstract=row["abstract"],
            score=score,
            category=row["category"],
            tags=tags,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source=row["source"],
        )

    @staticmethod
    def _has_cjk(text: str) -> bool:
        return any("\u4e00" <= c <= "\u9fff" for c in text)

    async def recall(self, query: str, *, limit: int = 5, min_score: float = 0.0) -> list[MemoryHit]:
        query = query.strip()
        if not query:
            rows = await asyncio.to_thread(
                self._db_fetchall,
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [self._row_to_hit(r) for r in rows]

        # FTS5 unicode61 tokenizer does not handle CJK characters.
        # Skip FTS5 for CJK queries and go directly to LIKE.
        fts5_applicable = self._fts5_ok and not self._has_cjk(query)

        if fts5_applicable:
            words = query.split()
            fts_query = " AND ".join(words)
            try:
                rows = await asyncio.to_thread(
                    self._db_fetchall,
                    "SELECT m.id, m.content, m.abstract, m.category, m.tags, m.source, "
                    "m.created_at, m.updated_at, bm25(memories_fts) AS bm25_score "
                    "FROM memories_fts JOIN memories m ON m.rowid = memories_fts.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) ASC LIMIT ?",
                    (fts_query, limit),
                )
                hits: list[MemoryHit] = []
                for r in rows:
                    score = -float(r["bm25_score"])
                    hits.append(self._row_to_hit(r, score=score))
                return hits
            except Exception as exc:
                logger.warning("FTS5 recall failed, falling back to LIKE: %s", exc)

        words = query.split()
        conditions: list[str] = []
        params: list[Any] = []
        for w in words:
            conditions.append("(content LIKE ? OR abstract LIKE ?)")
            params.append("%" + w + "%")
            params.append("%" + w + "%")
        where = " OR ".join(conditions) if conditions else "1=0"
        params.append(limit)
        rows = await asyncio.to_thread(
            self._db_fetchall,
            "SELECT * FROM memories WHERE " + where + " ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        return [self._row_to_hit(r) for r in rows]

    async def record_turn(self, transcript: str, answer: str, *, session_id: str) -> None:
        if self.settings.memory_extract_enabled and self.llm is not None:
            task = asyncio.create_task(self._extract_and_save(transcript, answer, session_id))
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _extract_and_save(self, transcript: str, answer: str, session_id: str) -> None:
        text = ""
        try:
            messages = [
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": "用户：" + transcript + "\n助手：" + answer},
            ]
            response = await self.llm.chat(messages=messages, instructions=None)
            text = (response.text or "").strip()
            if not text:
                return
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [ln for ln in lines if not ln.strip().startswith("```")]
                text = "\n".join(lines).strip()
            items = json.loads(text)
            if not isinstance(items, list):
                return
            for item in items[:2]:
                content = str(item.get("content", "")).strip()
                if not content:
                    continue
                category = str(item.get("category") or "facts").strip()
                raw_tags = item.get("tags", [])
                if not isinstance(raw_tags, list):
                    raw_tags = [raw_tags] if raw_tags else []
                tags = [str(t) for t in raw_tags if t]
                await self.add_memory(
                    content=content,
                    category=category,
                    tags=tags,
                    source="llm_extract",
                    turn_id=session_id,
                )
        except json.JSONDecodeError:
            logger.warning("Memory extract JSON parse failed: %s", text[:200])
        except Exception as exc:
            logger.warning("Memory extract failed: %s", exc)

    async def list_memories(self, *, limit: int = 50, offset: int = 0, q: str = "") -> list[MemoryHit]:
        if q:
            like = "%" + q + "%"
            rows = await asyncio.to_thread(
                self._db_fetchall,
                "SELECT * FROM memories WHERE content LIKE ? OR abstract LIKE ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (like, like, limit, offset),
            )
        else:
            rows = await asyncio.to_thread(
                self._db_fetchall,
                "SELECT * FROM memories ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return [self._row_to_hit(r) for r in rows]

    async def get_memory(self, uri: str) -> MemoryHit | None:
        row = await asyncio.to_thread(
            self._db_fetchone,
            "SELECT * FROM memories WHERE id=?",
            (uri,),
        )
        if row is None:
            return None
        return self._row_to_hit(row)

    async def update_memory(
        self,
        uri: str,
        *,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        now = time.time()
        abstract = content[:200]
        if category is not None and tags is not None:
            tags_str = ",".join(tags)
            await asyncio.to_thread(
                self._db_execute,
                "UPDATE memories SET content=?, abstract=?, category=?, tags=?, updated_at=? WHERE id=?",
                (content, abstract, category, tags_str, now, uri),
            )
        elif category is not None:
            await asyncio.to_thread(
                self._db_execute,
                "UPDATE memories SET content=?, abstract=?, category=?, updated_at=? WHERE id=?",
                (content, abstract, category, now, uri),
            )
        elif tags is not None:
            tags_str = ",".join(tags)
            await asyncio.to_thread(
                self._db_execute,
                "UPDATE memories SET content=?, abstract=?, tags=?, updated_at=? WHERE id=?",
                (content, abstract, tags_str, now, uri),
            )
        else:
            await asyncio.to_thread(
                self._db_execute,
                "UPDATE memories SET content=?, abstract=?, updated_at=? WHERE id=?",
                (content, abstract, now, uri),
            )

    async def delete_memory(self, uri: str) -> bool:
        rowcount = await asyncio.to_thread(
            self._db_execute,
            "DELETE FROM memories WHERE id=?",
            (uri,),
        )
        return rowcount > 0

    async def add_memory(
        self,
        *,
        content: str,
        category: str = "manual",
        tags: list[str] | None = None,
        source: str = "manual",
        turn_id: str = "",
    ) -> str:
        mem_id = "mem_" + uuid.uuid4().hex
        now = time.time()
        abstract = content[:200]
        tags_str = ",".join(tags or [])
        await asyncio.to_thread(
            self._db_execute,
            "INSERT INTO memories (id, content, abstract, category, tags, source, turn_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mem_id, content, abstract, category, tags_str, source, turn_id, now, now),
        )
        return mem_id

    async def final_commit(self, session_id: str) -> None:
        pass

    def stats(self) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*), MAX(updated_at) FROM memories")
            row = cur.fetchone()
        count = row[0] if row else 0
        latest = row[1] if row else 0.0
        return {"enabled": True, "provider": "sqlite", "count": count, "latest_updated_at": latest}


@dataclass
class _OVSessionState:
    """Tracks an OpenViking session mapped from our RealtimeSession.session_id.

    ``ov_session_id`` is the ID returned by OpenViking's POST /api/v1/sessions,
    which is distinct from our internal ``session_id``.
    """

    ov_session_id: str
    turn_count: int = 0
    last_commit_at: float = 0.0
    commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class OpenVikingMemoryProvider:
    """Memory provider backed by an OpenViking HTTP API server.

    All HTTP failures degrade silently: a warning is logged and an empty/None
    result is returned.  Commits are fire-and-forget during active conversation
    (``record_turn``) and only awaited during ``final_commit`` (disconnect).
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._http_client: httpx.AsyncClient | None = None
        self._sessions: dict[str, _OVSessionState] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.settings.openviking_api_key,
            "X-OpenViking-Account": self.settings.openviking_account,
            "X-OpenViking-User": self.settings.openviking_user,
            "Content-Type": "application/json",
        }

    @property
    def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.openviking_timeout_seconds),
                headers=self._headers(),
            )
        return self._http_client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response | None:
        url = f"{self.settings.openviking_base_url.rstrip('/')}{path}"
        try:
            return await self._client.request(method, url, json=json, timeout=timeout)
        except Exception as exc:
            logger.warning("OpenViking request failed: %s %s: %s", method, path, exc)
            return None

    async def recall(self, query: str, *, limit: int = 5, min_score: float = 0.0) -> list[MemoryHit]:
        body = {
            "query": query,
            "target_uri": self.settings.openviking_target_uri,
            "limit": limit,
            "score_threshold": min_score,
        }
        resp = await self._request("POST", "/api/v1/search/find", json=body)
        if resp is None or resp.status_code != 200:
            return []
        try:
            data = resp.json()
            memories = data.get("result", {}).get("memories", [])
        except Exception as exc:
            logger.warning("OpenViking recall parse failed: %s", exc)
            return []
        hits: list[MemoryHit] = []
        for m in memories:
            if not isinstance(m, dict):
                continue
            content = m.get("content", "")
            hits.append(
                MemoryHit(
                    uri=m.get("uri", ""),
                    content=content,
                    abstract=m.get("abstract") or content[:200],
                    score=float(m.get("score", 0.0)),
                    category=m.get("category", ""),
                    source="openviking",
                )
            )
        return hits

    async def record_turn(self, transcript: str, answer: str, *, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is None:
            resp = await self._request("POST", "/api/v1/sessions", json={})
            if resp is None or resp.status_code != 200:
                logger.warning("OpenViking session creation failed for session_id=%s", session_id)
                return
            try:
                ov_id = resp.json().get("session_id", "")
            except Exception as exc:
                logger.warning("OpenViking session creation parse failed: %s", exc)
                return
            if not ov_id:
                logger.warning("OpenViking session creation returned empty session_id")
                return
            state = _OVSessionState(ov_session_id=ov_id)
            self._sessions[session_id] = state

        resp = await self._request(
            "POST",
            f"/api/v1/sessions/{state.ov_session_id}/messages",
            json={"messages": [
                {"role": "user", "content": transcript},
                {"role": "assistant", "content": answer},
            ]},
        )
        if resp is None or resp.status_code != 200:
            return

        state.turn_count += 1
        should_commit = (
            state.turn_count >= self.settings.memory_commit_interval_turns
            or (
                state.last_commit_at > 0
                and time.time() - state.last_commit_at >= self.settings.memory_commit_idle_seconds
            )
        )
        if should_commit:
            asyncio.create_task(self._commit(session_id))

    async def _commit(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is None:
            return
        async with state.commit_lock:
            resp = await self._request(
                "POST",
                f"/api/v1/sessions/{state.ov_session_id}/commit",
                timeout=self.settings.openviking_commit_timeout_seconds,
            )
            if resp is None:
                logger.warning("OpenViking commit transport-failed for session_id=%s", session_id)
                return
            if resp.status_code == 409:
                logger.debug("OpenViking commit already in progress for session_id=%s", session_id)
                return
            if resp.status_code != 200:
                logger.warning(
                    "OpenViking commit failed status=%s for session_id=%s",
                    resp.status_code,
                    session_id,
                )
                return
            state.last_commit_at = time.time()
            state.turn_count = 0

    async def list_memories(self, *, limit: int = 50, offset: int = 0, q: str = "") -> list[MemoryHit]:
        if q:
            return await self.recall(q, limit=limit)
        resp = await self._request(
            "GET",
            f"/api/v1/fs/ls?uri={quote(self.settings.openviking_target_uri, safe='')}&recursive=true",
        )
        if resp is None or resp.status_code != 200:
            return []
        try:
            entries = resp.json()
            if not isinstance(entries, list):
                return []
        except Exception as exc:
            logger.warning("OpenViking fs/ls parse failed: %s", exc)
            return []
        uris = sorted(
            e.get("uri", "")
            for e in entries
            if isinstance(e, dict) and e.get("uri")
        )
        uris = uris[offset:offset + limit]
        hits: list[MemoryHit] = []
        for i in range(0, len(uris), 5):
            batch = uris[i:i + 5]
            results = await asyncio.gather(
                *(self.get_memory(u) for u in batch),
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, MemoryHit):
                    hits.append(r)
        return hits

    async def get_memory(self, uri: str) -> MemoryHit | None:
        resp = await self._request("GET", f"/api/v1/content/read?uri={quote(uri, safe='')}")
        if resp is None or resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("OpenViking content/read parse failed: %s", exc)
            return None
        content = data.get("content", "")
        return MemoryHit(
            uri=uri,
            content=content,
            abstract=data.get("abstract") or content[:200],
            score=0.0,
            category=data.get("category", ""),
            source="openviking",
        )

    async def update_memory(
        self,
        uri: str,
        *,
        content: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        body = {"uri": uri, "content": content, "mode": "replace", "wait": True}
        resp = await self._request("POST", "/api/v1/content/write", json=body)
        if resp is None or resp.status_code != 200:
            logger.warning("OpenViking update_memory failed for uri=%s", uri)

    async def delete_memory(self, uri: str) -> bool:
        resp = await self._request(
            "DELETE",
            f"/api/v1/fs?uri={quote(uri, safe='')}&recursive=false",
        )
        if resp is None:
            return False
        return resp.status_code == 200

    async def add_memory(
        self,
        *,
        content: str,
        category: str = "manual",
        tags: list[str] | None = None,
    ) -> str:
        uid = uuid.uuid4().hex
        uri = f"{self.settings.openviking_target_uri}{category}/{uid}"
        body = {"mode": "create", "wait": True, "content": content, "uri": uri}
        resp = await self._request("POST", "/api/v1/content/write", json=body)
        if resp is None or resp.status_code != 200:
            logger.warning("OpenViking add_memory failed")
            return "ov://error"
        try:
            data = resp.json()
            return str(data.get("uri") or uri)
        except Exception:
            return uri

    async def final_commit(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is None or state.turn_count == 0:
            return
        try:
            await self._commit(session_id)
        except Exception as exc:
            logger.warning("OpenViking final_commit failed for session_id=%s: %s", session_id, exc)

    def stats(self) -> dict[str, Any]:
        try:
            base = self.settings.openviking_base_url.rstrip("/")
            resp = httpx.get(
                f"{base}/api/v1/stats/memories",
                headers=self._headers(),
                timeout=self.settings.openviking_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            return {"enabled": True, "provider": "openviking", "error": str(exc)}
        return {"enabled": True, "provider": "openviking", **data}


def build_memory(settings, llm=None) -> MemoryProvider:
    if not settings.memory_enabled:
        return NoopMemoryProvider()
    provider = settings.memory_provider.strip().lower()
    if provider == "noop":
        return NoopMemoryProvider()
    if provider == "sqlite":
        return SqliteMemoryProvider(settings, llm=llm)
    if provider == "openviking":
        if not settings.openviking_api_key:
            logger.warning("openviking_api_key not configured, falling back to SqliteMemoryProvider")
            return SqliteMemoryProvider(settings, llm=llm)
        # Silently downgrade to sqlite if OpenViking is unreachable; user can re-trigger rebuild to retry.
        if not _probe_openviking(settings):
            logger.warning(
                "OpenViking server unreachable at %s, falling back to SqliteMemoryProvider",
                settings.openviking_base_url,
            )
            return SqliteMemoryProvider(settings, llm=llm)
        logger.info("OpenViking probe ok, using OpenVikingMemoryProvider at %s", settings.openviking_base_url)
        return OpenVikingMemoryProvider(settings)
    logger.warning("Unknown memory_provider=%r, falling back to NoopMemoryProvider", provider)
    return NoopMemoryProvider()


def _probe_openviking(settings, *, timeout: float = 1.5) -> bool:
    """Quick one-shot synchronous HTTP probe to decide if OpenViking is up.

    Returns True if we got any HTTP response (200/401/404/500 all qualify:
    server is alive, even if auth is wrong or endpoint changed).  Returns
    False only on transport errors (connection refused, DNS failure, timeout).
    """
    base = settings.openviking_base_url.rstrip("/")
    headers = {
        "X-API-Key": settings.openviking_api_key,
        "X-OpenViking-Account": settings.openviking_account,
        "X-OpenViking-User": settings.openviking_user,
    }
    try:
        resp = httpx.get(f"{base}/api/v1/stats/memories", headers=headers, timeout=timeout)
        logger.debug("OpenViking probe reached %s status=%s", base, resp.status_code)
        return True
    except Exception as exc:
        logger.debug("OpenViking probe failed for %s: %s", base, exc)
        return False

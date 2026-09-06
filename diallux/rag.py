"""Self-hosted RAG knowledge bases — Postgres + pgvector + OpenAI embeddings.

WHY (the hard reasoning, see DECISIONS.md §RAG):
  Your deployed Retell agent attaches its 9 knowledge bases with
  kb_config = {filter_score: 0.6, top_k: 3} — i.e. Retell was ALREADY doing
  server-side RAG: ~3 relevant chunks per turn, not the whole corpus. The
  lab engine tried to replicate KB access with `##slug-kb##` file expansion
  and (a) would inline the ENTIRE KB when it worked, and (b) actually never
  worked — its lookup searched `<slug>.md` while the shipped files are
  `<slug>-kb.md`, so every marker resolved to `[KB x MISSING]` (fixed in
  graph/subst.py). This module gives you the deployed behavior, self-hosted:

    user utterance -> ONE embedding call -> cosine top-k in pgvector (local)
                   -> ~1.6k chars of the most relevant KB excerpts
                   -> appended to a lean system prompt

  Defaults mirror the deployed kb_config: top_k=3 (RAG_TOP_K), score
  filtering approximated by cosine ranking. Chunking follows the KBs'
  heading structure so an excerpt is one complete tactic/objection.

  Cost model per turn:
    + 1 embedding call   ~60-120ms  (text-embedding-3-small)
    + pgvector query     ~5-15ms    (local, HNSW index)
    - a ~4k-token system prompt (state prompt + top-3 excerpts) instead of
      the ~16k-token everything-inline prompt the lab semantics imply
    Net: faster per round and better grounded (no attention dilution).

  Fallback: if Postgres/pgvector or the OpenAI key is unavailable, or the
  index is empty (ingest not run yet), retrieval returns nothing and the
  builder falls back to full inline KB expansion (now actually working).
  Nothing breaks; the system degrades to the lab-engine semantics.

Schema (created automatically on connect):
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE TABLE kb_chunks (
      id BIGSERIAL PRIMARY KEY, kb TEXT NOT NULL, content TEXT NOT NULL,
      embedding VECTOR(<dims>) NOT NULL);
    CREATE INDEX ... USING hnsw (embedding vector_cosine_ops);

Chunking: markdown KBs are split on headings, then packed to ~target_chars
with a small overlap so a retrieved chunk is self-contained.

Hermetic tests: `embed_fn` and the pool are injectable — see tests/test_rag.py.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("diallux.rag")

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}

_CHUNK_TARGET = 700       # chars per chunk (≈170 tokens)
_CHUNK_OVERLAP = 90       # context carry-over
_HEADING_RE = re.compile(r"^#{1,3}\s+.+$", re.MULTILINE)


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def chunk_markdown(text: str, target: int = _CHUNK_TARGET, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Heading-aware chunker: split on ##/### headings, then pack to `target`.

    Sales KBs are heading-structured; splitting there keeps each chunk about
    ONE tactic/objection, which is exactly the granularity the model needs.
    """
    sections: list[str] = []
    parts = re.split(r"(?m)^(#{1,3} .+)$", text)
    # re.split with a capturing group alternates [pre, heading, body, heading, body...]
    if len(parts) == 1:
        sections = [text]
    else:
        sections = [parts[0]]
        for i in range(1, len(parts) - 1, 2):
            sections.append(parts[i] + "\n" + parts[i + 1])
    chunks: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if len(sec) <= target:
            chunks.append(sec)
            continue
        # pack paragraphs
        paras, buf = re.split(r"\n\s*\n", sec), ""
        for p in paras:
            p = p.strip()
            if not p:
                continue
            if len(buf) + len(p) + 2 <= target:
                buf = (buf + "\n\n" + p) if buf else p
            else:
                if buf:
                    chunks.append(buf)
                # single paragraph longer than target: hard split with overlap
                while len(p) > target:
                    chunks.append(p[:target])
                    p = p[target - overlap:]
                buf = p
        if buf:
            chunks.append(buf)
    return [c for c in (c.strip() for c in chunks) if c]


# --------------------------------------------------------------------------- #
# OpenAI embeddings (injectable for hermetic tests)
# --------------------------------------------------------------------------- #
async def openai_embed_fn(model: str, api_key: str | None) -> EmbedFn:
    async def _embed(texts: list[str]) -> list[list[float]]:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key)
        resp = await client.embeddings.create(model=model, input=texts)
        return [d.embedding for d in resp.data]
    return _embed


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
class KBStore:
    """pgvector-backed retrieval over the 9 Retell knowledge bases.

    One store is shared process-wide ( pools are event-loop bound, uvicorn
    runs one loop). Created lazily by `get_kb_store()`.
    """

    def __init__(
        self,
        database_url: str,
        kb_dir: str | Path,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        api_key: str | None = None,
        embed_fn: EmbedFn | None = None,
        top_k: int = 4,
        filter_score: float = 0.40,
        char_budget: int = 1600,
        connect_timeout_s: float = 2.0,
    ):
        self.database_url = database_url
        self.kb_dir = Path(kb_dir)
        self.embedding_model = embedding_model
        self.dims = EMBEDDING_DIMS.get(embedding_model, 1536)
        self.api_key = api_key
        self.embed_fn = embed_fn
        self.top_k = top_k
        self.filter_score = filter_score
        self.char_budget = char_budget
        self.connect_timeout_s = connect_timeout_s
        self._pool = None
        self._checked_indexed = False
        self._indexed = False
        self._failed_at = 0.0          # retry a dead DB at most once a minute
        self.stats = {"queries": 0, "chunks_returned": 0, "total_ms": 0.0}

    # ------------------------------------------------------------------ #
    async def connect(self) -> bool:
        """Lazy pool creation. Returns True when usable. Never raises."""
        if self._pool is not None:
            return True
        if time.monotonic() - self._failed_at < 60:
            return False
        try:
            import asyncpg
            self._pool = await asyncpg.create_pool(
                self.database_url, min_size=1, max_size=4,
                timeout=self.connect_timeout_s, command_timeout=5.0,
            )
            await self._ensure_schema()
            return True
        except Exception as exc:
            self._pool = None
            self._failed_at = time.monotonic()
            log.warning("rag: pgvector unavailable (%s); inline KB fallback", exc)
            return False

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def ready(self) -> bool:
        return self._pool is not None

    async def _ensure_schema(self):
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS kb_chunks (
                    id BIGSERIAL PRIMARY KEY,
                    kb TEXT NOT NULL,
                    content TEXT NOT NULL,
                    embedding VECTOR({self.dims}) NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT now()
                )""")
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS kb_chunks_kb_idx ON kb_chunks (kb)")
            try:
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS kb_chunks_emb_idx "
                    "ON kb_chunks USING hnsw (embedding vector_cosine_ops)")
            except Exception:
                pass    # pgvector < 0.5: ivfflat only; sequential scan still fine at 9 KBs
            n = await conn.fetchval("SELECT count(*) FROM kb_chunks")
            self._indexed = bool(n)
            self._checked_indexed = True

    async def indexed(self) -> bool:
        if not self._checked_indexed:
            if not await self.connect():
                return False
        return self._indexed

    # ------------------------------------------------------------------ #
    async def retrieve(self, query_text: str, kb_slugs: list[str]) -> list[dict[str, Any]]:
        """Top-k chunks across `kb_slugs`, cosine distance, char-budgeted.

        Returns [{"kb": slug, "content": str, "score": float}] best-first.
        Empty list on any failure (caller falls back to inline KBs).
        """
        if not kb_slugs or not query_text.strip():
            return []
        t0 = time.perf_counter()
        try:
            if not await self.connect():
                return []
            embed = self.embed_fn or await openai_embed_fn(self.embedding_model, self.api_key)
            vectors = await embed([query_text.strip()[:1000]])
            vec = "[" + ",".join(f"{x:.6f}" for x in vectors[0]) + "]"
            assert self._pool is not None
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT kb, content, 1 - (embedding <=> $1::vector) AS score
                    FROM kb_chunks
                    WHERE kb = ANY($2::text[]) AND 1 - (embedding <=> $1::vector) >= $4
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                    """,
                    vec, kb_slugs, self.top_k, self.filter_score,
                )
            out, seen, budget = [], set(), self.char_budget
            for r in rows:
                key = (r["kb"], r["content"][:80])
                if key in seen:
                    continue
                seen.add(key)
                if budget - len(r["content"]) < 0 and out:
                    break
                out.append({"kb": r["kb"], "content": r["content"], "score": round(float(r["score"]), 4)})
                budget -= len(r["content"])
            self.stats["queries"] += 1
            self.stats["chunks_returned"] += len(out)
            self.stats["total_ms"] += (time.perf_counter() - t0) * 1000
            return out
        except Exception as exc:
            self._failed_at = time.monotonic()
            log.warning("rag retrieve failed (%s); inline fallback this turn", exc)
            return []

    # ------------------------------------------------------------------ #
    async def ingest(self, rebuild: bool = True) -> dict:
        """(Re)build the index from the KB markdown files. Run via
        scripts/rag_ingest.py at deploy time — NOT on the hot path."""
        if not await self.connect():
            raise RuntimeError("rag: postgres/pgvector unavailable; cannot ingest")
        embed = self.embed_fn or await openai_embed_fn(self.embedding_model, self.api_key)
        files = sorted(self.kb_dir.glob("*.md"))
        if not files:
            raise RuntimeError(f"rag: no .md files under {self.kb_dir}")
        rows: list[tuple[str, str]] = []
        for f in files:
            slug = f.stem.removesuffix("-kb")
            for chunk in chunk_markdown(f.read_text(encoding="utf-8")):
                rows.append((slug, chunk))
        # embed in batches (API limit safety)
        vectors: list[list[float]] = []
        B = 64
        for i in range(0, len(rows), B):
            vectors.extend(await embed([c for _, c in rows[i:i + B]]))
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            if rebuild:
                await conn.execute("TRUNCATE kb_chunks")
            await conn.executemany(
                "INSERT INTO kb_chunks (kb, content, embedding) VALUES ($1, $2, $3::vector)",
                [(kb, c, "[" + ",".join(f"{x:.6f}" for x in v) + "]")
                 for (kb, c), v in zip(rows, vectors)],
            )
        self._indexed = True
        self._checked_indexed = True
        return {"kbs": len(files), "chunks": len(rows), "embedding_model": self.embedding_model}


# --------------------------------------------------------------------------- #
# Process-wide singleton (one pool, one event loop)
# --------------------------------------------------------------------------- #
_SINGLETON: dict[str, KBStore] = {}


async def get_kb_store(settings) -> KBStore | None:
    """Returns the shared KBStore, or None when RAG is disabled/unavailable.

    RAG_MODE: "auto" (default; use pgvector when reachable+indexed, else inline)
              "rag"    (require pgvector; still falls back per-turn on errors)
              "inline" (never touch Postgres; exact V1 KB expansion)
    """
    mode = getattr(settings, "rag_mode", "auto")
    if mode == "inline":
        return None
    db = getattr(settings, "database_url", "") or ""
    if not db:
        return None
    key = db
    store = _SINGLETON.get(key)
    if store is None:
        store = KBStore(
            database_url=db,
            kb_dir=settings.knowledge_base_dir,
            embedding_model=settings.rag_embedding_model,
            api_key=settings.openai_api_key or None,
            top_k=settings.rag_top_k,
            filter_score=settings.rag_filter_score,
            char_budget=settings.rag_char_budget,
        )
        _SINGLETON[key] = store
    if not await store.connect():
        return None
    if not await store.indexed():
        log.warning("rag: index empty (run scripts/rag_ingest.py); inline fallback")
        return None
    return store


def reset_singleton_for_tests():
    _SINGLETON.clear()


# --------------------------------------------------------------------------- #
# System-prompt assembly helpers (used by graph/builder.py)
# --------------------------------------------------------------------------- #
_KB_MARKER_RE = re.compile(r"##([\w-]+)-kb##")


def kb_slugs_in(text: str) -> list[str]:
    """Ordered unique KB slugs referenced by ##slug-kb## markers."""
    out: list[str] = []
    for m in _KB_MARKER_RE.finditer(text):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def strip_kb_markers(text: str) -> str:
    """Remove ##slug-kb## markers (their content is replaced by retrieval)."""
    return _KB_MARKER_RE.sub("", text)


def render_knowledge_section(chunks: list[dict[str, Any]]) -> str:
    """The retrieved-excerpt block appended to the (now lean) system prompt."""
    if not chunks:
        return ("\n\n## KNOWLEDGE\n(No knowledge-base excerpt matched this turn; "
                "answer from the conversation.)")
    parts = ["\n\n## KNOWLEDGE (most relevant excerpts, retrieved for THIS turn)"]
    for c in chunks:
        parts.append(f"### from {c['kb']}-kb\n{c['content']}")
    return "\n\n".join(parts)

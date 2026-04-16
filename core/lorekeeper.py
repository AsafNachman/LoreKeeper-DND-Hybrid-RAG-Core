"""LoreKeeper orchestrator and public RAG API.

This module contains the primary `LoreKeeper` class. `main.py` re-exports
`LoreKeeper` so UI code can continue using `from main import LoreKeeper`.
"""

from __future__ import annotations

# NOTE: This file is intentionally a near-verbatim move of the former `main.py`
# implementation, with imports already routed through `core.*` and `services.*`.

import asyncio
import json
import logging
import multiprocessing
import os
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

from dotenv import load_dotenv

from core.constants import (
    CONDITION_LITERAL_KEEP as _CONDITION_LITERAL_KEEP,
    CONDITION_RETRIEVAL_K as _CONDITION_RETRIEVAL_K,
    CONDITION_RERANK_TOP_N as _CONDITION_RERANK_TOP_N,
    CONDITION_WHERE_DOC_LIMIT as _CONDITION_WHERE_DOC_LIMIT,
    MAX_CONTEXT_DOCS as _MAX_CONTEXT_DOCS,
    MIN_CONTEXT_CHARS_FOR_DETAILS as _MIN_CONTEXT_CHARS_FOR_DETAILS,
    MIN_TOP_RELEVANCE_SCORE_FOR_DETAILS as _MIN_TOP_RELEVANCE_SCORE_FOR_DETAILS,
    MIN_TOP_RERANK_SCORE_HARD_REFUSAL as _MIN_TOP_RERANK_SCORE_HARD_REFUSAL,
    MIN_TOP_RERANK_SCORE_SOFT_WARNING as _MIN_TOP_RERANK_SCORE_SOFT_WARNING,
    PAGE_RETRIEVAL_WINDOW_EXPANDED as _PAGE_RETRIEVAL_WINDOW_EXPANDED,
    RRF_RANK_CONSTANT as _RRF_RANK_CONSTANT,
    SINGLE_QUERY_DEEP_K as _SINGLE_QUERY_DEEP_K,
    Constants,
)
from core.dnd_logic import (
    condition_canonical_terms_from_query as _condition_canonical_terms_from_query,
    expand_condition_windowed_chunks as _expand_condition_windowed_chunks_core,
    fetch_condition_literal_hits as _fetch_condition_literal_hits_core,
    preferred_basenames_from_query as _preferred_basenames_from_query,
    query_wants_condition_deep_context as _query_wants_condition_deep_context,
    score_condition_literal_chunk as _score_condition_literal_chunk,
    where_document_clause_for_term as _where_document_clause_for_term,
)
from core.inference import (
    generate_with_self_correction as _generate_with_self_correction_core,
    _inject_verified_source_citation as _inject_verified_source_citation_core,
    stream_answer_with_integrity_timing as _stream_answer_with_integrity_timing_core,
)
from core.utils import (
    clean_query as _clean_query,
    meta_page_in_range as _meta_page_in_range,
    normalize_brain_id as _normalize_brain_id,
    normalize_stored_citation as _normalize_stored_citation,
    source_filename as _source_filename,
    viewer_page_number as _viewer_page_number,
)
from core.retrieval import (
    afinal_docs_for_query as _afinal_docs_for_query_core,
    build_retrieval_stack as _build_retrieval_stack,
    invoke_rrf_rerank as _invoke_rrf_rerank_core,
    invoke_single_query_rerank as _invoke_single_query_rerank_core,
    retrieve_by_page_window as _retrieve_by_page_window_core,
    source_relevance_score as _source_relevance_score,
)

_SOURCE_TAG_RE = re.compile(r"\[Source:\s*[^\]]+\]")
_WORD_RE = re.compile(r"[a-z0-9']+")

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "can",
    "could",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "if",
    "in",
    "is",
    "it",
    "its",
    "monster",
    "of",
    "on",
    "or",
    "that",
    "the",
    "them",
    "then",
    "they",
    "this",
    "to",
    "use",
    "when",
    "what",
    "with",
    "you",
    "your",
}

_INDEXY_HINT_RE = re.compile(r"\b(index|appendix|table of contents|glossary)\b", re.IGNORECASE)
_PAGE_DENSE_RE = re.compile(r"\b(?:p\.|pp\.|page)\s*\d{1,4}\b", re.IGNORECASE)
_DEFINITIONISH_RE = re.compile(
    r"^\s*(?:to\s+[a-z][a-z'\- ]+,\s+|[A-Z][a-zA-Z'\- ]{2,}\s+is\s+|[A-Z][a-zA-Z'\- ]{2,}\s+are\s+)",
    re.IGNORECASE,
)
_INDEX_KILLER_KEYWORDS_RE = re.compile(r"\b(see under|index|page\s+\d{1,4})\b", re.IGNORECASE)
_NUMBER_TOKEN_RE = re.compile(r"\b\d+\b")

_QUERY_TYPES = ("how", "explain", "what", "when", "can", "is", "are", "other")
_GLOBAL_NOISE_TYPES_BY_DOC: dict[tuple[str, object, str], set[str]] = defaultdict(set)
_GLOBAL_NOISE_HITS_BY_DOC: dict[tuple[str, object, str], int] = defaultdict(int)
_GLOBAL_NOISE_MIN_HITS = 20
_GLOBAL_NOISE_RATIO = 0.8


def _is_index_chunk(text: str) -> bool:
    """Return True for chunks that look like an index/reference list.

    Heuristic:
        - index-specific keywords (e.g., "See under", "Index", "Page 123")
        - high numbers-to-words ratio (> 25%)
    """
    body = str(text or "")
    if not body.strip():
        return False
    if _INDEX_KILLER_KEYWORDS_RE.search(body):
        return True
    words = _WORD_RE.findall(body.lower())
    if not words:
        return False
    num_tokens = len(_NUMBER_TOKEN_RE.findall(body))
    ratio = num_tokens / max(1, len(words))
    return ratio > 0.25


def _doc_key_for_noise(doc: Any) -> tuple[str, object, str]:
    meta = getattr(doc, "metadata", None) or {}
    src = _source_filename(meta.get("source"))
    page = meta.get("page")
    head = (getattr(doc, "page_content", "") or "")[:200]
    return src, page, head


def _query_type(query: str) -> str:
    q = (query or "").strip().lower()
    first = (q.split()[:1] or ["other"])[0]
    return first if first in _QUERY_TYPES else "other"

# Heavy LangChain / OpenAI stacks load on first `LoreKeeper()` (not on `import main`).
_LC: SimpleNamespace | None = None
_LLM_ENGINE_CACHE: dict[str, Any] = {}
_LLM_ENGINE_LOCK = threading.Lock()

if TYPE_CHECKING:
    from langchain_core.documents import Document

# Configuration
_MAIN_MODULE_T0 = time.perf_counter()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logger.info(
    "core.lorekeeper: logging ready (%.3fs since module start)",
    time.perf_counter() - _MAIN_MODULE_T0,
)


def register_runtime_singletons(runtime_bundle: Optional[dict[str, Any]]) -> None:
    """Adopt preloaded runtime singletons pushed by `app.py`."""
    global _LC
    if not runtime_bundle:
        return
    preloaded_kit = runtime_bundle.get("langchain_kit")
    if preloaded_kit is not None:
        _LC = preloaded_kit


def get_llm_engine(mode: str):
    """Return the chat model for the given tier (efficiency vs intelligence)."""
    key = (mode or "efficiency").strip().lower()
    if key in ("efficiency", "auto_efficiency", "local"):
        from langchain_ollama import ChatOllama

        base = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        ollama_model = (
            (os.getenv("OLLAMA_CHAT_MODEL") or "llama3:8b-instruct-q4_K_M").strip()
            or "llama3:8b-instruct-q4_K_M"
        )
        n_thread = int(os.getenv("OLLAMA_NUM_THREAD") or str(multiprocessing.cpu_count() or 4))
        ng_raw = (os.getenv("OLLAMA_NUM_GPU") or "").strip()
        num_gpu: int | None = int(ng_raw) if ng_raw else None
        ollama_kwargs: dict[str, Any] = {
            "model": ollama_model,
            "base_url": base,
            "temperature": 0.2,
            "num_thread": max(1, n_thread),
        }
        if num_gpu is not None:
            ollama_kwargs["num_gpu"] = num_gpu
        cache_key = (
            f"{key}|{ollama_model}|{base}|"
            f"{ollama_kwargs['num_thread']}|{ollama_kwargs.get('num_gpu', 'auto')}"
        )
        with _LLM_ENGINE_LOCK:
            cached = _LLM_ENGINE_CACHE.get(cache_key)
            if cached is not None:
                return cached
            model_obj = ChatOllama(**ollama_kwargs)
            _LLM_ENGINE_CACHE[cache_key] = model_obj
            return model_obj

    if key in ("intelligence", "premium_intelligence", "cloud", "premium"):
        langchain_kit = _langchain_bundle()
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError("OPENAI_API_KEY is required for Premium Intelligence (cloud) mode.")
        cache_key = "intelligence|gpt-5.4|0.2"
        with _LLM_ENGINE_LOCK:
            cached = _LLM_ENGINE_CACHE.get(cache_key)
            if cached is not None:
                return cached
            model_obj = langchain_kit.ChatOpenAI(model="gpt-5.4", temperature=0.2)
            _LLM_ENGINE_CACHE[cache_key] = model_obj
            return model_obj

    raise ValueError(f"Unknown LLM mode {mode!r}; use 'efficiency' or 'intelligence'.")


def _tier_is_efficiency(mode: str) -> bool:
    key = (mode or "efficiency").strip().lower()
    return key in ("efficiency", "auto_efficiency", "local")


def _langchain_bundle() -> SimpleNamespace:
    """Load LangChain-related packages once; keeps imports cheap."""
    global _LC
    if _LC is not None:
        return _LC

    _t0 = time.perf_counter()

    def _p(label: str) -> None:
        print(f"⏱ [PROFILE _langchain_bundle] {label} — {time.perf_counter() - _t0:.3f}s", flush=True)

    from langchain_classic.retrievers import ContextualCompressionRetriever

    _p("langchain_classic.retrievers")
    from langchain_chroma import Chroma

    _p("langchain_chroma")
    from langchain_community.document_compressors import FlashrankRerank

    _p("FlashrankRerank")
    from langchain_community.retrievers import BM25Retriever

    _p("BM25Retriever")
    from langchain_core.documents import Document

    _p("langchain_core.documents")
    from langchain_core.output_parsers import StrOutputParser

    _p("StrOutputParser")
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    _p("ChatPromptTemplate + MessagesPlaceholder")
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    _p("langchain_openai (ChatOpenAI + OpenAIEmbeddings)")

    _LC = SimpleNamespace(
        BM25Retriever=BM25Retriever,
        ChatOpenAI=ChatOpenAI,
        ChatPromptTemplate=ChatPromptTemplate,
        Chroma=Chroma,
        ContextualCompressionRetriever=ContextualCompressionRetriever,
        Document=Document,
        FlashrankRerank=FlashrankRerank,
        MessagesPlaceholder=MessagesPlaceholder,
        OpenAIEmbeddings=OpenAIEmbeddings,
        StrOutputParser=StrOutputParser,
    )
    _p("bundle assembled")
    return _LC


def _setup_phoenix_tracing_lazy() -> None:
    from services.observability import setup_phoenix_tracing

    setup_phoenix_tracing()


def _parse_requested_page(query: str) -> Optional[int]:
    m = re.search(r"\b(?:page|p\.|pg\.?)\s*(\d+)\b", query, re.IGNORECASE)
    if not m:
        return None
    n = int(m.group(1))
    return n - 1 if n >= 1 else None


def _dedupe_documents(docs: list["Document"]) -> list["Document"]:
    seen: set[tuple[str, object, str]] = set()
    out: list["Document"] = []
    for d in docs:
        src = _source_filename(d.metadata.get("source") if d.metadata else None)
        page = d.metadata.get("page") if d.metadata else None
        head = (d.page_content or "")[:280]
        key = (src, page, head)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _normalize_edition_filter(edition_filter: Optional[str]) -> Optional[str]:
    if edition_filter is None:
        return None
    normalized = str(edition_filter).strip()
    if not normalized or normalized.lower() == "all":
        return None
    return normalized


def _edition_where_filter(edition_filter: Optional[str]) -> Optional[dict[str, str]]:
    normalized = _normalize_edition_filter(edition_filter)
    if normalized is None:
        return None
    return {"edition": normalized}


def _filter_docs_by_edition(
    docs: list["Document"], edition_filter: Optional[str]
) -> list["Document"]:
    normalized = _normalize_edition_filter(edition_filter)
    if normalized is None:
        return docs
    kept: list["Document"] = []
    for doc in docs:
        meta = doc.metadata or {}
        if str(meta.get("edition", "")).strip() == normalized:
            kept.append(doc)
    return kept


class BaseSystemPrompt:
    """Compose compact system prompts with immutable grounding rules."""

    @classmethod
    def build(cls, *, efficiency_mode: bool) -> str:
        tuning = (
            Constants.SYSTEM_PROMPT_EFFICIENCY_TUNING
            if efficiency_mode
            else Constants.SYSTEM_PROMPT_INTELLIGENCE_TUNING
        )
        return (
            "You are the Lore Keeper for the user's licensed D&D archives.\n\n"
            f"{Constants.SYSTEM_PROMPT_IMMUTABLE_RULES}\n"
            f"{Constants.ARCHIVIST_LOCK_PROMPT}\n\n"
            f"{Constants.SYSTEM_PROMPT_UNIFIED_PERSONA}\n"
            f"{tuning}\n"
            "{directives}"
            "Context:\n{context}"
        )


class LoreKeeper:
    """Primary RAG engine entrypoint (used by Streamlit UI and CLI)."""

    def __init__(
        self,
        db_path: str,
        llm_mode: str = "efficiency",
        brain_id: str = "dnd_core",
        *,
        on_phase: Optional[Callable[[str], None]] = None,
    ) -> None:
        _init_t0 = time.perf_counter()

        def _phase(label: str) -> None:
            elapsed = time.perf_counter() - _init_t0
            print(f"⏱ [PROFILE LoreKeeper.__init__] {label} — {elapsed:.3f}s", flush=True)
            if on_phase:
                on_phase(label)
            logger.info("LoreKeeper phase: %s (%.3fs)", label, elapsed)

        _phase("Loading configuration…")
        load_dotenv()
        _phase("Optional: Phoenix tracing…")
        _setup_phoenix_tracing_lazy()
        _phase("Loading LangChain libraries…")
        langchain_kit = _langchain_bundle()
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is missing from .env file.")

        self._brain_id = _normalize_brain_id(brain_id)
        self._db_path = str(Path(db_path) / self._brain_id)
        Path(self._db_path).mkdir(parents=True, exist_ok=True)
        self._llm_mode = (llm_mode or "efficiency").strip().lower()
        self._efficiency = _tier_is_efficiency(self._llm_mode)
        # Runtime toggle controlled by the UI; defaults to enabled.
        self.multi_query_enabled = True
        # Optional post-retrieval neighbor-page fetch (UI default OFF for latency).
        self.context_expansion_enabled = False
        if self._efficiency:
            # RRF pool depth (vector + BM25) + reranker cap.
            self._retrieval_k = 30
            self._rerank_top_n = 5
            self._condition_retrieval_k = 10
            self._condition_rerank_top_n = 12
            self._merge_context_cap = 16
            self._source_prune_rerank_top_n = 16
            self._page_window_pick_limit = 16
        else:
            self._retrieval_k = 30
            self._rerank_top_n = 10
            self._condition_retrieval_k = _CONDITION_RETRIEVAL_K
            self._condition_rerank_top_n = _CONDITION_RERANK_TOP_N
            self._merge_context_cap = _MAX_CONTEXT_DOCS
            self._source_prune_rerank_top_n = 32
            self._page_window_pick_limit = 32

        _phase("Loading embeddings & vector index…")
        self.embeddings = langchain_kit.OpenAIEmbeddings(model="text-embedding-3-small")
        self.vectordb = langchain_kit.Chroma(
            persist_directory=self._db_path, embedding_function=self.embeddings
        )
        _phase("Connecting chat model (Ollama / OpenAI)…")
        self.llm = get_llm_engine(self._llm_mode)
        self._str_parser = langchain_kit.StrOutputParser()
        self._guardrail_similarity_floor = 0.32

        rk = self._retrieval_k
        all_docs = self.vectordb.get()
        doc_objects = [
            langchain_kit.Document(page_content=txt, metadata=meta)
            for txt, meta in zip(all_docs.get("documents", []), all_docs.get("metadatas", []))
            if txt
        ]

        self._docs_by_source_page: dict[tuple[str, int], list[Any]] = {}
        if doc_objects:
            by_page: defaultdict[tuple[str, int], list[Any]] = defaultdict(list)
            for d in doc_objects:
                meta = d.metadata or {}
                src = meta.get("source")
                if src is None:
                    continue
                try:
                    p = int(meta.get("page"))
                except (TypeError, ValueError):
                    continue
                by_page[(str(src), p)].append(d)
            for key, chunks in by_page.items():
                chunks.sort(key=lambda x: (x.page_content or "")[:600])
            self._docs_by_source_page = dict(by_page)

        _phase("Building retrieval stack (dense + BM25 + RRF-ready rerank)…")
        self._retrieval_stack = _build_retrieval_stack(
            langchain_kit=langchain_kit,
            vectordb=self.vectordb,
            retrieval_k=rk,
            rerank_top_n=self._rerank_top_n,
            source_prune_rerank_top_n=self._source_prune_rerank_top_n,
        )
        self._vector_retriever = self._retrieval_stack.vector_retriever
        self._keyword_retriever = self._retrieval_stack.keyword_retriever
        self.base_retriever = self._retrieval_stack.base_retriever
        self._answer_reranker = self._retrieval_stack.answer_reranker
        self.retriever = self._retrieval_stack.compressed_retriever
        self._source_pruning_reranker = self._retrieval_stack.source_pruning_reranker

        _phase("Building prompts…")
        system_prompt = BaseSystemPrompt.build(efficiency_mode=self._efficiency)
        self.prompt = langchain_kit.ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            langchain_kit.MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{question}"),
        ])

    def _get_documents_page_range(self, low: int, high: int, *, edition_filter: Optional[str] = None) -> list["Document"]:
        low, high = max(0, low), max(low, high)
        batch = None
        edition_where = _edition_where_filter(edition_filter)
        base_where = {"$and": [{"page": {"$gte": low}}, {"page": {"$lte": high}}]}
        if edition_where:
            base_where = {"$and": [base_where, edition_where]}
        for where in (base_where, None):
            if where is None:
                break
            try:
                batch = self.vectordb.get(where=where, include=["documents", "metadatas"])
                if batch.get("documents"):
                    break
                batch = None
            except Exception as exc:
                logger.debug("Chroma page-range get failed (%s); trying fallback.", exc)
                batch = None
        langchain_kit = _langchain_bundle()
        docs: list[Any] = []
        if batch and batch.get("documents"):
            texts = batch.get("documents") or []
            metas = batch.get("metadatas") or []
            for txt, meta in zip(texts, metas):
                if not txt:
                    continue
                meta = meta or {}
                if _meta_page_in_range(meta.get("page"), low, high):
                    docs.append(langchain_kit.Document(page_content=txt, metadata=meta))
            if docs:
                return docs
        for p in range(low, high + 1):
            page_clauses: list[dict[str, Any]] = [{"page": p}, {"page": str(p)}]
            if edition_where:
                page_clauses = [{"$and": [clause, edition_where]} for clause in page_clauses]
            for where in page_clauses:
                try:
                    batch_pg = self.vectordb.get(where=where, include=["documents", "metadatas"])
                except Exception:
                    continue
                texts = batch_pg.get("documents") or []
                metas = batch_pg.get("metadatas") or []
                for txt, meta in zip(texts, metas):
                    if txt:
                        docs.append(langchain_kit.Document(page_content=txt, metadata=meta or {}))
                if texts:
                    break
        return docs

    def _retrieve_by_page_window(self, query: str, center_0based: int, *, edition_filter: Optional[str] = None) -> list["Document"]:
        return _retrieve_by_page_window_core(
            get_documents_page_range_fn=self._get_documents_page_range,
            query=query,
            center_0based=center_0based,
            page_window_pick_limit=self._page_window_pick_limit,
            edition_filter=edition_filter,
            source_relevance_score_fn=_source_relevance_score,
        )

    @staticmethod
    def _meta_page_as_int(meta: Optional[dict[str, Any]]) -> Optional[int]:
        """Return 0-based stored page index from chunk metadata, or None if missing."""
        if not meta:
            return None
        p = meta.get("page")
        if isinstance(p, int):
            return p
        try:
            return int(p)
        except (TypeError, ValueError):
            return None

    def _document_keys_covering_pages(self, docs: list["Document"]) -> set[tuple[str, int]]:
        """Set of (basename, raw_page) pairs already present in the merged doc list."""
        cov: set[tuple[str, int]] = set()
        for d in docs:
            meta = getattr(d, "metadata", None) or {}
            src = _source_filename(meta.get("source"))
            p = self._meta_page_as_int(meta)
            if src and p is not None:
                cov.add((src, p))
        return cov

    def _expand_with_neighbor_pages(
        self,
        docs: list["Document"],
        *,
        edition_filter: Optional[str] = None,
    ) -> list["Document"]:
        """Append same-source chunks from pages N±1 when any hit reaches the neighbor threshold.

        Args:
            docs: Reranked/pruned retrieval output (0-based ``page`` in metadata).
            edition_filter: Optional ruleset filter passed to Chroma lookups.

        Returns:
            Original documents plus neighbor pages not already present (deduped).

        Intent:
            High-confidence hits (rerank ≥ ``NEIGHBOR_PAGE_EXPAND_RERANK_THRESHOLD``) often land on
            one PDF page while tables continue on adjacent pages; this is a **context assembly**
            step only (no change to retrieval scoring in ``retrieval.py``).

        Complexity:
            ``O(A * P)`` where ``A`` is the number of anchor pages and ``P`` is chunks per page lookup.

        Gating:
            Skips all lookups unless ``context_expansion_enabled`` is True **and** the top rerank
            across ``docs`` is at least ``NEIGHBOR_PAGE_EXPAND_RERANK_THRESHOLD`` (retrieval chunks
            are unchanged; only this optional enrichment is skipped).
        """
        thr = float(Constants.NEIGHBOR_PAGE_EXPAND_RERANK_THRESHOLD)
        if not bool(getattr(self, "context_expansion_enabled", False)):
            return list(docs)
        if self._top_rerank_score(docs) < thr:
            return list(docs)
        langchain_kit = _langchain_bundle()
        anchors: set[tuple[str, int]] = set()
        for d in docs:
            meta = getattr(d, "metadata", None) or {}
            try:
                rr = float(meta.get("rerank_score", meta.get("relevance_score", 0.0)) or 0.0)
            except (TypeError, ValueError):
                rr = 0.0
            if rr < thr:
                continue
            src = _source_filename(meta.get("source"))
            p = self._meta_page_as_int(meta)
            if not src or p is None:
                continue
            anchors.add((src, p))

        if not anchors:
            return list(docs)

        coverage = self._document_keys_covering_pages(docs)
        seen_chunk_keys = {_doc_key_for_noise(d) for d in docs}
        extra: list["Document"] = []

        needed_pages: set[tuple[str, int]] = set()
        for src, center in anchors:
            if center - 1 >= 0:
                needed_pages.add((src, center - 1))
            needed_pages.add((src, center + 1))

        for src, raw_page in sorted(needed_pages):
            if (src, raw_page) in coverage:
                continue
            batch = self._get_documents_page_range(raw_page, raw_page, edition_filter=edition_filter)
            for d in batch:
                meta = dict(d.metadata or {})
                if _source_filename(meta.get("source")) != src:
                    continue
                k = _doc_key_for_noise(d)
                if k in seen_chunk_keys:
                    continue
                seen_chunk_keys.add(k)
                meta["lk_neighbor_expansion"] = True
                try:
                    meta["rerank_score"] = max(
                        float(meta.get("rerank_score", 0.0) or 0.0),
                        thr,
                    )
                except (TypeError, ValueError):
                    meta["rerank_score"] = thr
                nd = langchain_kit.Document(page_content=d.page_content or "", metadata=meta)
                extra.append(nd)
            # One attempt per (source, page); avoids re-querying empty or duplicate-only pages every turn.
            coverage.add((src, raw_page))

        return list(docs) + extra

    @staticmethod
    def _doc_sort_key_tuple(doc: Any) -> tuple[float, float]:
        """Return (rerank_score, similarity_score) for stable LLM context ordering."""
        meta = getattr(doc, "metadata", None) or {}
        try:
            rr = float(meta.get("rerank_score", meta.get("relevance_score", 0.0)) or 0.0)
        except (TypeError, ValueError):
            rr = 0.0
        try:
            sim = float(meta.get("similarity_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            sim = 0.0
        return (rr, sim)

    @classmethod
    def _docs_ordered_for_llm(cls, docs: list[Any]) -> list[Any]:
        """Sort by main retrieval rerank (desc), then dense similarity (desc)."""
        return sorted(docs, key=cls._doc_sort_key_tuple, reverse=True)

    def _retrieval_perfect_match_prelude(self, docs: list[Any]) -> str:
        """Prefix context when the top chunk hit the maximum calibrated rerank score."""
        top = self._top_rerank_score(docs)
        if top >= 0.999 or round(top, 2) >= 1.0:
            return (
                "[Retrieval priority] A source below has Rerank 1.00 (perfect match). "
                "Prioritize its information above all other provided chunks.\n\n"
            )
        return ""

    def _citation_for_top_reranked_doc(self, docs: list[Any]) -> str:
        """Citation label for the highest-rerank chunk (matches first context block)."""
        if not docs:
            return ""
        ordered = self._docs_ordered_for_llm(docs)
        top_doc = ordered[0]
        meta = top_doc.metadata or {}
        source_file = _source_filename(meta.get("source"))
        raw_page = meta.get("page", 0)
        viewer_page = _viewer_page_number(raw_page)
        label = f"Page {viewer_page}" if viewer_page is not None else "Page ?"
        return f"{source_file} ({label})"

    def _format_context_block(self, doc: "Document", *, position: int = 0) -> str:
        meta = doc.metadata or {}
        try:
            rr = float(meta.get("rerank_score", meta.get("relevance_score", 0.0)) or 0.0)
        except (TypeError, ValueError):
            rr = 0.0
        try:
            sim = float(meta.get("similarity_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            sim = 0.0
        if meta.get("lk_neighbor_expansion"):
            tag = "NEIGHBORING PAGE (same scroll, ±1 page — context expansion)"
        elif position == 0:
            tag = "PRIMARY SOURCE (highest rerank)"
        else:
            tag = f"Supporting source (#{position + 1})"
        source_file = _source_filename(meta.get("source"))
        raw_page = meta.get("page", 0)
        page_display = raw_page + 1 if isinstance(raw_page, int) else raw_page
        body = (doc.page_content or "").strip()
        return (
            f"[{tag} | Rerank {rr:.2f} | Similarity {sim:.2f}]\n"
            f"[Source: {source_file} | Page {page_display}]\n{body}"
        )

    def _prune_irrelevant_docs(self, query: str, docs: list["Document"]) -> list["Document"]:
        if len(docs) <= 1:
            return docs
        preserve: dict[tuple[str, object, str], dict[str, float]] = {}
        for d in docs:
            k = _doc_key_for_noise(d)
            m = d.metadata or {}
            try:
                rr = float(m.get("rerank_score", m.get("relevance_score", 0.0)) or 0.0)
            except (TypeError, ValueError):
                rr = 0.0
            try:
                sim = float(m.get("similarity_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                sim = 0.0
            prev = preserve.get(k)
            if prev is None or rr > prev["rerank_score"]:
                preserve[k] = {"rerank_score": rr, "similarity_score": sim}
        reranked = list(self._source_pruning_reranker.compress_documents(docs, query))
        if not reranked:
            return docs[:1]
        qlow = (query or "").strip().lower()
        is_howto = qlow.startswith(("how", "explain", "what is", "what are", "to "))
        qtype = _query_type(query)

        scored: list[tuple[float, Any]] = []
        for d in reranked:
            meta = d.metadata or {}
            raw = meta.get("relevance_score", 0)
            try:
                base = float(raw)
            except (TypeError, ValueError):
                base = 0.0

            # Global noise filtering: chunks that show up across most query types
            # are likely index/boilerplate artifacts and should be suppressed.
            key = _doc_key_for_noise(d)
            _GLOBAL_NOISE_HITS_BY_DOC[key] += 1
            _GLOBAL_NOISE_TYPES_BY_DOC[key].add(qtype)
            if _GLOBAL_NOISE_HITS_BY_DOC[key] >= _GLOBAL_NOISE_MIN_HITS:
                ratio = len(_GLOBAL_NOISE_TYPES_BY_DOC[key]) / max(1, len(_QUERY_TYPES))
                if ratio >= _GLOBAL_NOISE_RATIO:
                    base *= 0.1

            body = (d.page_content or "").strip()
            head = (body.splitlines()[0] if body else "").strip()

            bonus = 0.0
            if is_howto and head and _DEFINITIONISH_RE.search(head):
                bonus += 0.06

            penalty = 0.0
            if is_howto:
                src = str(meta.get("source", "") or "").lower()
                if _INDEXY_HINT_RE.search(src) or _INDEXY_HINT_RE.search(body):
                    penalty += 0.08
                # Penalize "page dense" chunks that look like references or indexes.
                page_mentions = len(_PAGE_DENSE_RE.findall(body))
                if page_mentions >= 6:
                    penalty += min(0.10, 0.02 * (page_mentions - 5))
                # Index killer: hard down-weight chunks that resemble an index.
                if _is_index_chunk(body):
                    base *= 0.3

            scored.append((base + bonus - penalty, d))

        scored.sort(key=lambda row: row[0], reverse=True)
        top_score = scored[0][0] if scored else 0.0
        relevance_floor = max(top_score * 0.52, 0.04)
        kept = [d for s, d in scored if s >= relevance_floor]
        out = kept if kept else [scored[0][1]]
        for d in out:
            k = _doc_key_for_noise(d)
            if k not in preserve:
                continue
            meta = dict(d.metadata or {})
            pr = preserve[k]
            meta["rerank_score"] = max(float(meta.get("rerank_score", 0.0) or 0.0), pr["rerank_score"])
            meta["similarity_score"] = max(float(meta.get("similarity_score", 0.0) or 0.0), pr["similarity_score"])
            d.metadata = meta
        return out

    def _prioritize_gold_sources_for_llm(self, docs: list["Document"]) -> list["Document"]:
        """Place rerank ``> 0.80`` chunks before the rest while preserving score order within each band.

        Args:
            docs: Candidate documents (typically rerank-sorted).

        Returns:
            Reordered list (gold band first). Used in single-query mode to reserve
            prompt space for high-confidence rule text (e.g. core PHB pages).

        Complexity:
            Time ``O(n log n)`` from the two sort passes; space ``O(n)``.
        """
        if not docs:
            return docs
        ordered = self._docs_ordered_for_llm(docs)
        gold: list["Document"] = []
        rest: list["Document"] = []
        for d in ordered:
            meta = getattr(d, "metadata", None) or {}
            try:
                rr = float(meta.get("rerank_score", meta.get("relevance_score", 0.0)) or 0.0)
            except (TypeError, ValueError):
                rr = 0.0
            (gold if rr > 0.80 else rest).append(d)
        return self._docs_ordered_for_llm(gold) + self._docs_ordered_for_llm(rest)

    @staticmethod
    def _merge_protected_top_into_pruned(
        protected: list["Document"], pruned: list["Document"]
    ) -> list["Document"]:
        """Re-insert cross-encoder-pruned top chunks so single-query mode keeps the top-5 reranked bodies.

        Args:
            protected: First reranked documents before pruning (up to five).
            pruned: Documents kept by ``_prune_irrelevant_docs``.

        Returns:
            Deduplicated list with any missing protected docs prepended.

        Intent:
            Pruning can drop index-looking passages that are still mechanically required;
            single-query mode guarantees the top reranked slices survive for LLM grounding.

        Complexity:
            Time ``O(n + m)`` over protected and pruned lengths; space ``O(n + m)`` for key sets.
        """
        if not protected:
            return pruned
        keys_pruned = {_doc_key_for_noise(d) for d in pruned}
        front = [d for d in protected if _doc_key_for_noise(d) not in keys_pruned]
        seen: set[tuple[str, object, str]] = set()
        out: list["Document"] = []
        for d in front + pruned:
            k = _doc_key_for_noise(d)
            if k in seen:
                continue
            seen.add(k)
            out.append(d)
        return out

    def _merge_and_prune_docs(self, query: str, sem_docs: list["Document"], page_docs: list["Document"], page_filtered: bool) -> list["Document"]:
        """Merge semantic and page hits, cap list width, then prune for LLM context.

        Args:
            query: Raw user query (used by pruning heuristics).
            sem_docs: Primary semantic retrieval results.
            page_docs: Optional page-window documents merged before dedupe.
            page_filtered: True when retrieval was scoped to explicit page intent.

        Returns:
            Final document list ordered for prompt assembly.

        Intent:
            Sort by rerank **before** slicing so the cap keeps the strongest passages.
            When the top rerank is at least ``0.80``, widen the cap slightly so the
            prompt carries enough source text for detailed answers (no retrieval change).
            In **single-query** mode (multi-query expansion OFF), never drop the top five
            reranked chunks to pruning, prioritize **gold** rerank (``> 0.80``) to the
            front of the prompt, and keep at least five merge slots.

        Complexity:
            Time ``O(n log n)`` for the sort step; space ``O(n)`` for the document list.
        """
        single_sq = not bool(getattr(self, "multi_query_enabled", True))
        docs = _dedupe_documents(sem_docs + page_docs) if page_docs else sem_docs
        docs = self._docs_ordered_for_llm(docs)
        if single_sq and not page_filtered:
            docs = self._prioritize_gold_sources_for_llm(docs)
        cap = self._merge_context_cap
        if page_filtered or _query_wants_condition_deep_context(query):
            cap = min(self._merge_context_cap + 12, 40)
        top_rr = self._top_rerank_score(docs)
        if top_rr >= 0.80:
            cap = min(cap + 8, 40)
        if single_sq:
            cap = max(cap, 5)
            if top_rr > 0.80:
                cap = max(cap, min(self._merge_context_cap + 8, 40))
        if len(docs) > cap:
            docs = docs[:cap]
        protected_top = docs[: min(5, len(docs))] if single_sq else []
        docs = self._prune_irrelevant_docs(query, docs)
        if single_sq and protected_top:
            docs = self._merge_protected_top_into_pruned(protected_top, docs)
        if page_filtered:
            qlow = query.lower().replace("manul", "manual")
            docs = sorted(
                docs,
                key=lambda d: _source_relevance_score(d.metadata.get("source", "") if d.metadata else "", qlow),
                reverse=True,
            )
        return self._docs_ordered_for_llm(docs)

    def _invoke_compressed_retriever(self, query: str, *, k: int, rerank_top_n: int, edition_filter: Optional[str] = None) -> list["Document"]:
        edition_where = _edition_where_filter(edition_filter)
        variants = self._expand_multi_queries(query) if bool(getattr(self, "multi_query_enabled", True)) else []
        if variants:
            return _invoke_rrf_rerank_core(
                stack=self._retrieval_stack,
                original_query=query,
                queries=[query] + variants,
                k=k,
                rerank_top_n=rerank_top_n,
                edition_where=edition_where,
            )
        return _invoke_single_query_rerank_core(
            stack=self._retrieval_stack,
            query=query,
            k=max(int(k), int(_SINGLE_QUERY_DEEP_K)),
            rerank_top_n=rerank_top_n,
            edition_where=edition_where,
        )

    def _fetch_condition_literal_hits(self, terms: list[str], *, edition_filter: Optional[str] = None) -> list["Document"]:
        langchain_kit = _langchain_bundle()
        edition_where = _edition_where_filter(edition_filter)
        return _fetch_condition_literal_hits_core(
            vectordb=self.vectordb,
            document_cls=langchain_kit.Document,
            terms=terms,
            condition_where_doc_limit=_CONDITION_WHERE_DOC_LIMIT,
            condition_literal_keep=_CONDITION_LITERAL_KEEP,
            where_document_clause_for_term_fn=_where_document_clause_for_term,
            score_condition_literal_chunk_fn=_score_condition_literal_chunk,
            dedupe_documents_fn=_dedupe_documents,
            edition_where=edition_where,
            logger=logger,
        )

    def _expand_condition_windowed_chunks(self, docs: list["Document"], terms: list[str]) -> list["Document"]:
        return _expand_condition_windowed_chunks_core(
            docs=docs,
            terms=terms,
            docs_by_source_page=self._docs_by_source_page,
            dedupe_documents_fn=_dedupe_documents,
        )

    def _semantic_docs_for_query(self, query: str, *, edition_filter: Optional[str] = None) -> list["Document"]:
        self._log_rrf_score_breakdown(query, edition_filter=edition_filter)
        if not _query_wants_condition_deep_context(query):
            sem_docs = self._invoke_compressed_retriever(
                query, k=self._retrieval_k, rerank_top_n=self._rerank_top_n, edition_filter=edition_filter
            )
            return _filter_docs_by_edition(sem_docs, edition_filter)
        terms = _condition_canonical_terms_from_query(query)
        sem_docs = self._invoke_compressed_retriever(
            query, k=self._condition_retrieval_k, rerank_top_n=self._condition_rerank_top_n, edition_filter=edition_filter
        )
        if _parse_requested_page(query) is None:
            boost_q = f"{query} D&D 5e condition Player Handbook Appendix A rules"
            sem_docs = _dedupe_documents(
                sem_docs + self._invoke_compressed_retriever(
                    boost_q, k=self._condition_retrieval_k, rerank_top_n=self._condition_rerank_top_n, edition_filter=edition_filter
                )
            )
        literal_hits = self._fetch_condition_literal_hits(terms, edition_filter=edition_filter)
        sem_docs = _dedupe_documents(literal_hits + sem_docs)
        expanded = self._expand_condition_windowed_chunks(sem_docs, terms)
        return _filter_docs_by_edition(expanded, edition_filter)

    def _final_docs_for_query(self, query: str, *, edition_filter: Optional[str] = None) -> tuple[list["Document"], bool]:
        page_idx = _parse_requested_page(query)
        page_docs: list["Document"] = []
        page_filtered = False
        if page_idx is not None:
            page_docs = self._retrieve_by_page_window(query, page_idx, edition_filter=edition_filter)
            if page_docs:
                page_filtered = True
        sem_docs = self._semantic_docs_for_query(query, edition_filter=edition_filter)
        return self._merge_and_prune_docs(query, sem_docs, page_docs, page_filtered), page_filtered

    async def _afinal_docs_for_query(self, query: str, *, edition_filter: Optional[str] = None) -> tuple[list["Document"], bool]:
        return await _afinal_docs_for_query_core(
            parse_requested_page_fn=_parse_requested_page,
            semantic_docs_for_query_fn=self._semantic_docs_for_query,
            retrieve_by_page_window_fn=self._retrieve_by_page_window,
            merge_and_prune_docs_fn=self._merge_and_prune_docs,
            query=query,
            edition_filter=edition_filter,
            logger=logger,
        )

    def _build_sources_from_docs(self, docs: list["Document"]) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list["Document"]] = {}
        for doc in docs:
            source_file = _source_filename(doc.metadata.get("source"))
            viewer_page = _viewer_page_number((doc.metadata or {}).get("page", 0))
            label = f"Page {viewer_page}" if viewer_page is not None else "Page ?"
            key = (source_file, label)
            if key not in groups:
                groups[key] = []
            groups[key].append(doc)
        scored_keys: list[tuple[float, tuple[str, str]]] = []
        for key, gdocs in groups.items():
            rr_max = 0.0
            for d in gdocs:
                meta = d.metadata or {}
                try:
                    rr_max = max(rr_max, float(meta.get("rerank_score", meta.get("relevance_score", 0.0)) or 0.0))
                except (TypeError, ValueError):
                    continue
            scored_keys.append((rr_max, key))
        scored_keys.sort(key=lambda row: -row[0])
        sources: list[dict[str, Any]] = []
        for _rr_max, (source_file, label) in scored_keys:
            gdocs = groups[(source_file, label)]
            excerpt = (gdocs[0].page_content or "")[:300].strip()
            sim = 0.0
            rr = 0.0
            for d in gdocs:
                meta = d.metadata or {}
                try:
                    sim = max(sim, float(meta.get("similarity_score", 0.0)))
                except (TypeError, ValueError):
                    pass
                try:
                    rr = max(rr, float(meta.get("rerank_score", meta.get("relevance_score", 0.0))))
                except (TypeError, ValueError):
                    pass
            sources.append(
                {
                    "citation": f"{source_file} ({label})",
                    "excerpt": excerpt,
                    "similarity_score": round(sim, 4),
                    "rerank_score": round(rr, 4),
                }
            )
        return sources

    def _log_retrieval_debug_chunks(self, docs: list["Document"]) -> None:
        for i, doc in enumerate(docs, start=1):
            meta = doc.metadata or {}
            src = _source_filename(meta.get("source"))
            page = _viewer_page_number(meta.get("page"))
            page_lbl = f"Page {page}" if page is not None else "Page ?"
            preview = " ".join((doc.page_content or "").split())[:100]
            msg = f"🔎 [RETRIEVAL DEBUG] #{i} {src} ({page_lbl}) :: {preview}"
            print(msg, flush=True)
            logger.info(msg)
            if page == 100:
                raw = (doc.page_content or "")
                dump = raw if len(raw) <= 4000 else (raw[:4000] + "\n… <snip> …")
                print(f"📄 [PAGE 100 RAW] {src} (Page 100)\n{dump}\n", flush=True)
                logger.info("PAGE100_RAW_DUMPED=True src=%s len=%d", src, len(raw))

    def _log_rrf_score_breakdown(self, query: str, *, edition_filter: Optional[str]) -> None:
        """Log top fused RRF keys for debugging (vector rank + BM25 rank)."""
        if self._keyword_retriever is None:
            return
        vr = self._vector_retriever
        orig_vec_k = vr.search_kwargs.get("k", self._retrieval_k)
        orig_vec_filter = vr.search_kwargs.get("filter")
        orig_bm_k = getattr(self._keyword_retriever, "k", self._retrieval_k)
        edition_where = _edition_where_filter(edition_filter)
        try:
            probe_k = max(15, self._retrieval_k)
            vr.search_kwargs["k"] = probe_k
            if edition_where is None:
                vr.search_kwargs.pop("filter", None)
            else:
                vr.search_kwargs["filter"] = edition_where
            self._keyword_retriever.k = probe_k
            vec_docs = _filter_docs_by_edition(list(vr.invoke(query)), edition_filter)
            bm_docs = _filter_docs_by_edition(list(self._keyword_retriever.invoke(query)), edition_filter)
        except Exception as exc:
            logger.debug("RRF score breakdown skipped: %s", exc)
            return
        finally:
            vr.search_kwargs["k"] = orig_vec_k
            if orig_vec_filter is None:
                vr.search_kwargs.pop("filter", None)
            else:
                vr.search_kwargs["filter"] = orig_vec_filter
            self._keyword_retriever.k = orig_bm_k

        def _doc_key(d: "Document") -> tuple[str, object, str]:
            src = _source_filename(d.metadata.get("source") if d.metadata else None)
            page = d.metadata.get("page") if d.metadata else None
            head = (d.page_content or "")[:200]
            return src, page, head

        vec_ranks = {_doc_key(d): i for i, d in enumerate(vec_docs, start=1)}
        bm_ranks = {_doc_key(d): i for i, d in enumerate(bm_docs, start=1)}
        rrf: dict[tuple[str, object, str], float] = {}
        for k in set(vec_ranks) | set(bm_ranks):
            s = 0.0
            if k in vec_ranks:
                s += 1.0 / (float(_RRF_RANK_CONSTANT) + float(vec_ranks[k]))
            if k in bm_ranks:
                s += 1.0 / (float(_RRF_RANK_CONSTANT) + float(bm_ranks[k]))
            rrf[k] = s
        rows = sorted(rrf.keys(), key=lambda kk: rrf[kk], reverse=True)[:5]
        for idx, key in enumerate(rows, start=1):
            src, page, _head = key
            viewer = _viewer_page_number(page)
            page_lbl = f"Page {viewer}" if viewer is not None else "Page ?"
            b = 1.0 / (float(_RRF_RANK_CONSTANT) + float(bm_ranks[key])) if key in bm_ranks else 0.0
            v = 1.0 / (float(_RRF_RANK_CONSTANT) + float(vec_ranks[key])) if key in vec_ranks else 0.0
            msg = f"📊 [RRF WHY] #{idx} {src} ({page_lbl}) RRF={rrf[key]:.5f} vec_term={v:.5f} bm25_term={b:.5f}"
            print(msg, flush=True)
            logger.info(msg)

    @staticmethod
    def _has_source_documents(sources: list[dict[str, Any]]) -> bool:
        return bool(sources)

    @staticmethod
    def _enforce_no_verified_sources_integrity(answer_text: str) -> str:
        cleaned = _SOURCE_TAG_RE.sub("", answer_text or "").strip()
        if cleaned.startswith(Constants.COMMON_LORE_DISCLAIMER) or cleaned.startswith(Constants.ARCHIVES_SILENT_NOTE):
            return cleaned
        if cleaned:
            return f"{Constants.COMMON_LORE_DISCLAIMER}\n\n{cleaned}"
        return Constants.COMMON_LORE_DISCLAIMER

    @staticmethod
    def _strip_common_lore_disclaimer_when_sources_present(answer_text: str) -> str:
        """Remove the common-lore disclaimer prefix when verified sources exist.

        Intent:
            After pen-test hardening, the model can become overly conservative and
            prepend the common-lore disclaimer even when grounded context was
            provided. This post-process enforces the UX contract:
            - With verified sources: no "couldn't find the scroll" disclaimer.
            - Without verified sources: the disclaimer is required.

        Args:
            answer_text: Raw model answer.

        Returns:
            Answer text with any leading common-lore disclaimer removed.
        """
        raw = str(answer_text or "").lstrip()
        if not raw.startswith(Constants.COMMON_LORE_DISCLAIMER):
            return str(answer_text or "").strip()
        stripped = raw[len(Constants.COMMON_LORE_DISCLAIMER) :].lstrip()
        if stripped.startswith("\n"):
            stripped = stripped.lstrip()
        return stripped.strip()

    def _expand_multi_queries(self, query: str) -> list[str]:
        """Generate 3 semantic query variants aimed at core rules/mechanics."""
        q = str(query or "").strip()
        if not q:
            return []
        prompt = Constants.MULTI_QUERY_EXPANSION_PROMPT.format(user_query=q)
        try:
            raw = self.llm.invoke(prompt)
            content = getattr(raw, "content", raw)
            txt = str(content or "").strip()
        except Exception as exc:
            logger.debug("Multi-query expansion failed (%s) for query=%r", exc, q[:200])
            return []
        lines = [ln.strip(" \t-•") for ln in txt.splitlines() if ln.strip()]
        out: list[str] = []
        seen: set[str] = set()
        for ln in lines:
            norm = " ".join(ln.split())
            if not norm:
                continue
            if norm.lower() == q.lower():
                continue
            if norm.lower() in seen:
                continue
            seen.add(norm.lower())
            out.append(norm)
            if len(out) >= 3:
                break
        return out

    def _log_guardrail_reject(self, *, query: str, reason: str, detail: str = "") -> None:
        """Log a guardrail-triggered rejection with a stable flag.

        Args:
            query: User query text (cleaned).
            reason: Stable reason code (e.g., "classifier", "similarity_floor").
            detail: Optional detail string for diagnostics (kept short).
        """
        extra = f" detail={detail!r}" if detail else ""
        logger.info("GuardrailTriggered=True reason=%s query=%r%s", reason, query[:300], extra)

    def _guardrail_rejection_message(self) -> str:
        return Constants.OUT_OF_LORE_MESSAGE

    def _classify_in_domain(self, query: str) -> bool:
        """Cheap router to reject clearly out-of-domain requests.

        Intent:
            Prevent instruction drift and avoid spending tokens on retrieval + RAG
            prompts for obvious non-D&D / real-world advice queries.
        """
        prompt = Constants.DOMAIN_CLASSIFIER_PROMPT.format(user_query=query)
        try:
            raw = self.llm.invoke(prompt)
            content = getattr(raw, "content", raw)
            verdict = str(content or "").strip()
        except Exception as exc:
            # Fail open: if the router crashes, fall back to retrieval thresholding.
            logger.debug("Domain classifier failed open (%s) for query=%r", exc, query[:200])
            return True
        return verdict.strip().lower() == "category a"

    def _lore_intent_check(self, query: str) -> bool:
        """Return True if the query is plausibly about D&D mechanics/lore.

        Intent:
            The domain classifier can produce false negatives on short or oddly
            phrased D&D queries (e.g., "Explain Wild Magic Surge"). This check is
            a tiny follow-up that only runs on would-be rejections, and it
            converts those into an allowed "general archives" answer path.
        """
        prompt = Constants.LORE_INTENT_CHECK_PROMPT.format(user_query=query)
        try:
            raw = self.llm.invoke(prompt)
            content = getattr(raw, "content", raw)
            verdict = str(content or "").strip().lower()
        except Exception as exc:
            # Fail open: prefer allowing D&D-like queries over false refusals.
            logger.debug("Lore intent check failed open (%s) for query=%r", exc, query[:200])
            return True
        return verdict == "yes"

    def _top_retrieval_similarity(self, query: str, *, edition_filter: Optional[str]) -> Optional[float]:
        """Return the top Chroma similarity score (0..1) if available."""
        edition_where = _edition_where_filter(edition_filter)
        try:
            rows = self.vectordb.similarity_search_with_relevance_scores(
                query,
                k=1,
                filter=edition_where,
            )
        except Exception:
            rows = []
        if not rows:
            return None
        _doc, score = rows[0]
        try:
            return float(score)
        except (TypeError, ValueError):
            return None

    def _guardrail_precheck(self, query: str, history: Sequence[tuple[str, str]], *, edition_filter: Optional[str]) -> tuple[Optional[str], bool, bool]:
        """Return (hard_reject_message, in_domain, allow_general_archives) for guardrail routing."""
        _ = history  # reserved for future policy (e.g., repeated domain attacks)

        if not self._classify_in_domain(query):
            if self._lore_intent_check(query):
                logger.info("GuardrailTriggered=True reason=classifier_override query=%r", query[:300])
                return None, True, True
            self._log_guardrail_reject(query=query, reason="classifier", detail="Category B")
            return Constants.STRICT_DOMAIN_REJECTION, False, False

        # In-domain. We no longer hard-reject low similarity; we use it only as a soft signal.
        _ = self._top_retrieval_similarity(query, edition_filter=edition_filter)
        return None, True, False

    def _common_lore_answer(self, *, query: str, history: Sequence[tuple[str, str]]) -> str:
        """Answer from common D&D knowledge when retrieval found no usable scrolls."""
        answer, _corrected = self._generate_with_self_correction(
            query=query,
            history=history,
            context_text="",
        )
        cleaned = str(answer or "").strip()
        if cleaned.startswith(Constants.COMMON_LORE_DISCLAIMER):
            return cleaned
        if not cleaned:
            return Constants.ARCHIVES_SILENT_NOTE
        return f"{Constants.COMMON_LORE_DISCLAIMER}\n\n{cleaned}"

    def _general_archives_answer(self, *, query: str, history: Sequence[tuple[str, str]]) -> str:
        """Answer from internal knowledge with a "general archives" disclaimer."""
        answer, _corrected = self._generate_with_self_correction(
            query=query,
            history=history,
            context_text="",
        )
        cleaned = str(answer or "").strip()
        if cleaned.startswith(Constants.GENERAL_ARCHIVES_DISCLAIMER):
            return cleaned
        if not cleaned:
            return Constants.ARCHIVES_SILENT_NOTE
        return f"{Constants.GENERAL_ARCHIVES_DISCLAIMER}\n\n{cleaned}"

    @staticmethod
    def _context_confidence_is_low(docs: list["Document"]) -> bool:
        """Return True when retrieved context is too weak to answer details safely."""
        total_chars = 0
        top_score = 0.0
        for d in docs or []:
            total_chars += len(str(getattr(d, "page_content", "") or ""))
            meta = getattr(d, "metadata", {}) or {}
            raw = meta.get("relevance_score", 0.0)
            try:
                top_score = max(top_score, float(raw))
            except (TypeError, ValueError):
                pass
        if total_chars < _MIN_CONTEXT_CHARS_FOR_DETAILS:
            return True
        if top_score and top_score < _MIN_TOP_RELEVANCE_SCORE_FOR_DETAILS:
            return True
        return False

    @staticmethod
    def _top_rerank_score(docs: list["Document"]) -> float:
        best = 0.0
        for d in docs or []:
            meta = getattr(d, "metadata", {}) or {}
            raw = meta.get("rerank_score", meta.get("relevance_score", 0.0))
            try:
                best = max(best, float(raw))
            except (TypeError, ValueError):
                continue
        return best

    @staticmethod
    def _details_missing_message(topic: str) -> str:
        t = (topic or "").strip() or "this topic"
        return f"The archives contain the entry for {t}, but the specific details are missing from my current scrolls."

    @staticmethod
    def _with_soft_relevance_warning(answer: str) -> str:
        body = str(answer or "").strip()
        if not body:
            return Constants.LOW_RELEVANCE_SOFT_WARNING
        if body.startswith(Constants.LOW_RELEVANCE_SOFT_WARNING):
            return body
        return f"{Constants.LOW_RELEVANCE_SOFT_WARNING}\n\n{body}"

    def _answer_directives_for_context(self, context_text: str) -> str:
        """Return extra system instructions for single-query (verbose mechanical) answers.

        Args:
            context_text: Assembled retrieval context (empty for common-lore fallbacks).

        Returns:
            Level-20 directive block when expansion is OFF and evidence is present; else empty.

        Intent:
            Keeps formatting contracts in the ``directives`` slot so ``context`` stays
            evidence-only for the hidden critic and page audits.
        """
        if not (context_text or "").strip():
            return ""
        if bool(getattr(self, "multi_query_enabled", True)):
            return ""
        return Constants.SINGLE_QUERY_LEVEL20_ANSWER_DIRECTIVES.strip() + "\n"

    def _generate_with_self_correction(self, *, query: str, history: Sequence[tuple[str, str]], context_text: str) -> tuple[str, bool]:
        state = "ON" if bool(getattr(self, "multi_query_enabled", True)) else "OFF"
        q = f"{query}\n\nSystem note (do not mention): Query expansion for the RRF retrieval pool is {state}. Proceed with retrieval accordingly but maintain strict grounding."
        directives = self._answer_directives_for_context(context_text)
        return _generate_with_self_correction_core(
            prompt=self.prompt,
            llm=self.llm,
            str_parser=self._str_parser,
            query=q,
            history=history,
            context_text=context_text,
            directives=directives,
        )

    async def a_ask_impl(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[str, list[dict[str, Any]], list[str]]:
        reject, in_domain, allow_general_archives = self._guardrail_precheck(query, history, edition_filter=edition_filter)
        if reject:
            return reject, [], []
        docs, _page_filtered = await self._afinal_docs_for_query(query, edition_filter=edition_filter)
        if not docs:
            if in_domain:
                # Strict closed-book mode: do not invent mechanics without scroll context.
                return self._details_missing_message(query), [], []
            self._log_guardrail_reject(query=query, reason="no_docs", detail="retrieval_empty")
            return self._guardrail_rejection_message(), [], []
        self._log_retrieval_debug_chunks(docs)
        top_rr = self._top_rerank_score(docs)
        if top_rr < _MIN_TOP_RERANK_SCORE_HARD_REFUSAL:
            return Constants.LOW_RELEVANCE_REFUSAL, self._build_sources_from_docs(docs), []
        soft_warn = top_rr < _MIN_TOP_RERANK_SCORE_SOFT_WARNING
        if self._context_confidence_is_low(docs):
            return self._details_missing_message(query), self._build_sources_from_docs(docs), []
        docs = self._docs_ordered_for_llm(docs)
        prelude = self._retrieval_perfect_match_prelude(docs)
        docs_for_llm = self._expand_with_neighbor_pages(docs, edition_filter=edition_filter)
        context_strings = [self._format_context_block(doc, position=i) for i, doc in enumerate(docs_for_llm)]
        context_text = prelude + ("\n\n---\n\n".join(context_strings) if context_strings else "")
        answer, _corrected = await asyncio.to_thread(
            self._generate_with_self_correction,
            query=query,
            history=history,
            context_text=context_text,
        )
        sources = self._build_sources_from_docs(docs_for_llm)
        if not self._has_source_documents(sources):
            answer = self._enforce_no_verified_sources_integrity(str(answer))
        else:
            answer = self._strip_common_lore_disclaimer_when_sources_present(str(answer))
            cite = self._citation_for_top_reranked_doc(docs)
            answer = _inject_verified_source_citation_core(answer, citation=cite)
        if soft_warn:
            answer = self._with_soft_relevance_warning(answer)
        return answer, sources, context_strings

    def _run_sync_inference(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[str, list[dict[str, Any]], list[str]]:
        reject, in_domain, allow_general_archives = self._guardrail_precheck(query, history, edition_filter=edition_filter)
        if reject:
            return reject, [], []
        docs, _page_filtered = self._final_docs_for_query(query, edition_filter=edition_filter)
        if not docs:
            if in_domain:
                return self._details_missing_message(query), [], []
            self._log_guardrail_reject(query=query, reason="no_docs", detail="retrieval_empty")
            return self._guardrail_rejection_message(), [], []
        self._log_retrieval_debug_chunks(docs)
        top_rr = self._top_rerank_score(docs)
        if top_rr < _MIN_TOP_RERANK_SCORE_HARD_REFUSAL:
            return Constants.LOW_RELEVANCE_REFUSAL, self._build_sources_from_docs(docs), []
        soft_warn = top_rr < _MIN_TOP_RERANK_SCORE_SOFT_WARNING
        if self._context_confidence_is_low(docs):
            return self._details_missing_message(query), self._build_sources_from_docs(docs), []
        docs = self._docs_ordered_for_llm(docs)
        prelude = self._retrieval_perfect_match_prelude(docs)
        docs_for_llm = self._expand_with_neighbor_pages(docs, edition_filter=edition_filter)
        context_strings = [self._format_context_block(doc, position=i) for i, doc in enumerate(docs_for_llm)]
        context_text = prelude + ("\n\n---\n\n".join(context_strings) if context_strings else "")
        answer, _corrected = self._generate_with_self_correction(
            query=query,
            history=history,
            context_text=context_text,
        )
        sources = self._build_sources_from_docs(docs_for_llm)
        if not self._has_source_documents(sources):
            answer = self._enforce_no_verified_sources_integrity(str(answer))
        else:
            answer = self._strip_common_lore_disclaimer_when_sources_present(str(answer))
            cite = self._citation_for_top_reranked_doc(docs)
            answer = _inject_verified_source_citation_core(answer, citation=cite)
        if soft_warn:
            answer = self._with_soft_relevance_warning(answer)
        return answer, sources, context_strings

    def stream_query(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[Iterator[str], list[dict[str, Any]]]:
        query = _clean_query(query)
        reject, in_domain, allow_general_archives = self._guardrail_precheck(query, history, edition_filter=edition_filter)
        if reject:
            def _it() -> Iterator[str]:
                yield reject

            return _it(), []
        t_start = time.perf_counter()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            docs, _ = asyncio.run(self._afinal_docs_for_query(query, edition_filter=edition_filter))
        else:
            docs, _ = self._final_docs_for_query(query, edition_filter=edition_filter)
        t_retrieval = time.perf_counter() - t_start
        self._log_retrieval_debug_chunks(docs)
        if not docs and in_domain:
            def _it2() -> Iterator[str]:
                yield self._details_missing_message(query)

            return _it2(), []
        top_rr = self._top_rerank_score(docs) if docs else 0.0
        if docs and top_rr < _MIN_TOP_RERANK_SCORE_HARD_REFUSAL:
            def _it4() -> Iterator[str]:
                yield Constants.LOW_RELEVANCE_REFUSAL

            return _it4(), self._build_sources_from_docs(docs)
        soft_warn = bool(docs) and top_rr < _MIN_TOP_RERANK_SCORE_SOFT_WARNING
        if docs and self._context_confidence_is_low(docs):
            def _it3() -> Iterator[str]:
                yield self._details_missing_message(query)

            return _it3(), self._build_sources_from_docs(docs)
        docs = self._docs_ordered_for_llm(docs)
        prelude = self._retrieval_perfect_match_prelude(docs)
        docs_for_llm = self._expand_with_neighbor_pages(docs, edition_filter=edition_filter)
        context_strings = [self._format_context_block(doc, position=i) for i, doc in enumerate(docs_for_llm)]
        context_text = prelude + ("\n\n---\n\n".join(context_strings) if context_strings else "")
        sources = self._build_sources_from_docs(docs_for_llm)
        no_verified_context = not self._has_source_documents(sources)
        verified_citation = self._citation_for_top_reranked_doc(docs) if not no_verified_context else ""
        stream_iter = _stream_answer_with_integrity_timing_core(
            retrieval_seconds=t_retrieval,
            query=query,
            history=history,
            context_text=context_text,
            no_verified_context=no_verified_context,
            verified_source_citation=verified_citation,
            enforce_no_verified_sources_integrity=self._enforce_no_verified_sources_integrity,
            prompt=self.prompt,
            llm=self.llm,
            str_parser=self._str_parser,
            logger=logger,
            directives=self._answer_directives_for_context(context_text),
        )
        if soft_warn:
            def _soft_it() -> Iterator[str]:
                yield f"{Constants.LOW_RELEVANCE_SOFT_WARNING}\n\n"
                for chunk in stream_iter:
                    yield chunk

            return _soft_it(), sources
        return (
            stream_iter,
            sources,
        )

    def ask(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[str, list[dict[str, Any]]]:
        query = _clean_query(query)
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                answer, sources, _ = asyncio.run(self.a_ask_impl(query, history, edition_filter=edition_filter))
                return answer, sources
            answer, sources, _ = self._run_sync_inference(query, history, edition_filter=edition_filter)
            return answer, sources
        except Exception as e:
            logger.error("Reranking Inference Error: %s", e)
            return "My archives are currently under a protective ward (Error). Please retry.", []

    def ask_with_eval_contexts(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[str, list[dict[str, Any]], list[str]]:
        try:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(self.a_ask_impl(query, history, edition_filter=edition_filter))
            return self._run_sync_inference(query, history, edition_filter=edition_filter)
        except Exception as e:
            logger.error("Reranking Inference Error: %s", e)
            return "My archives are currently under a protective ward (Error). Please retry.", [], []


if __name__ == "__main__":
    raise SystemExit("This module is not a CLI entrypoint. Run `python cli.py` instead.")


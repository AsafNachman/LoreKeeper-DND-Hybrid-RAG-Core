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
    PAGE_RETRIEVAL_WINDOW_EXPANDED as _PAGE_RETRIEVAL_WINDOW_EXPANDED,
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
    invoke_compressed_retriever as _invoke_compressed_retriever_core,
    retrieve_by_page_window as _retrieve_by_page_window_core,
    source_relevance_score as _source_relevance_score,
)

_SOURCE_TAG_RE = re.compile(r"\[Source:\s*[^\]]+\]")

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

    from langchain_classic.retrievers import (
        ContextualCompressionRetriever,
        EnsembleRetriever,
    )

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
        EnsembleRetriever=EnsembleRetriever,
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
            f"{Constants.SYSTEM_PROMPT_UNIFIED_PERSONA}\n"
            f"{tuning}\n"
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
        if self._efficiency:
            self._retrieval_k = 15
            self._rerank_top_n = 5
            self._condition_retrieval_k = 10
            self._condition_rerank_top_n = 12
            self._merge_context_cap = 16
            self._source_prune_rerank_top_n = 16
            self._page_window_pick_limit = 16
        else:
            self._retrieval_k = 15
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

        _phase("Building hybrid & BM25 index…")
        self._retrieval_stack = _build_retrieval_stack(
            langchain_kit=langchain_kit,
            vectordb=self.vectordb,
            retrieval_k=rk,
            rerank_top_n=self._rerank_top_n,
            source_prune_rerank_top_n=self._source_prune_rerank_top_n,
            vector_weight=0.3,
            bm25_weight=0.7,
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
    def _format_context_block(doc: "Document") -> str:
        source_file = _source_filename(doc.metadata.get("source"))
        raw_page = doc.metadata.get("page", 0)
        page_display = raw_page + 1 if isinstance(raw_page, int) else raw_page
        body = (doc.page_content or "").strip()
        return f"[Source: {source_file} | Page {page_display}]\n{body}"

    def _prune_irrelevant_docs(self, query: str, docs: list["Document"]) -> list["Document"]:
        if len(docs) <= 1:
            return docs
        reranked = list(self._source_pruning_reranker.compress_documents(docs, query))
        if not reranked:
            return docs[:1]
        scores: list[float] = []
        for d in reranked:
            raw = d.metadata.get("relevance_score", 0)
            try:
                scores.append(float(raw))
            except (TypeError, ValueError):
                scores.append(0.0)
        top_score = max(scores)
        relevance_floor = max(top_score * 0.52, 0.04)
        kept = [d for d, s in zip(reranked, scores) if s >= relevance_floor]
        return kept if kept else [reranked[0]]

    def _merge_and_prune_docs(self, query: str, sem_docs: list["Document"], page_docs: list["Document"], page_filtered: bool) -> list["Document"]:
        docs = _dedupe_documents(sem_docs + page_docs) if page_docs else sem_docs
        cap = self._merge_context_cap
        if page_filtered or _query_wants_condition_deep_context(query):
            cap = min(self._merge_context_cap + 12, 40)
        if len(docs) > cap:
            docs = docs[:cap]
        docs = self._prune_irrelevant_docs(query, docs)
        if page_filtered:
            qlow = query.lower().replace("manul", "manual")
            docs = sorted(
                docs,
                key=lambda d: _source_relevance_score(d.metadata.get("source", "") if d.metadata else "", qlow),
                reverse=True,
            )
        return docs

    def _invoke_compressed_retriever(self, query: str, *, k: int, rerank_top_n: int, edition_filter: Optional[str] = None) -> list["Document"]:
        edition_where = _edition_where_filter(edition_filter)
        return _invoke_compressed_retriever_core(
            stack=self._retrieval_stack,
            query=query,
            k=k,
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
        self._log_hybrid_score_breakdown(query, edition_filter=edition_filter)
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
        order: list[tuple[str, str]] = []
        for doc in docs:
            source_file = _source_filename(doc.metadata.get("source"))
            viewer_page = _viewer_page_number((doc.metadata or {}).get("page", 0))
            label = f"Page {viewer_page}" if viewer_page is not None else "Page ?"
            key = (source_file, label)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(doc)
        sources: list[dict[str, Any]] = []
        for source_file, label in order:
            gdocs = groups[(source_file, label)]
            excerpt = (gdocs[0].page_content or "")[:300].strip()
            sources.append({"citation": f"{source_file} ({label})", "excerpt": excerpt})
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

    def _log_hybrid_score_breakdown(self, query: str, *, edition_filter: Optional[str]) -> None:
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
            logger.debug("Hybrid score breakdown skipped: %s", exc)
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

        vec_scores = {_doc_key(d): 1.0 / i for i, d in enumerate(vec_docs, start=1)}
        bm_scores = {_doc_key(d): 1.0 / i for i, d in enumerate(bm_docs, start=1)}
        rows = sorted(
            set(vec_scores) | set(bm_scores),
            key=lambda k: (0.7 * bm_scores.get(k, 0.0)) + (0.3 * vec_scores.get(k, 0.0)),
            reverse=True,
        )[:5]
        for idx, key in enumerate(rows, start=1):
            src, page, _head = key
            viewer = _viewer_page_number(page)
            page_lbl = f"Page {viewer}" if viewer is not None else "Page ?"
            b = bm_scores.get(key, 0.0)
            v = vec_scores.get(key, 0.0)
            msg = f"📊 [HYBRID WHY] #{idx} {src} ({page_lbl}) BM25={b:.4f} Vector={v:.4f} Hybrid={(0.7 * b + 0.3 * v):.4f}"
            print(msg, flush=True)
            logger.info(msg)

    @staticmethod
    def _has_source_documents(sources: list[dict[str, Any]]) -> bool:
        return bool(sources)

    @staticmethod
    def _enforce_no_verified_sources_integrity(answer_text: str) -> str:
        cleaned = _SOURCE_TAG_RE.sub("", answer_text or "").strip()
        if cleaned.startswith(Constants.NO_VERIFIED_SOURCES_NOTE):
            return cleaned
        if cleaned:
            return f"{Constants.NO_VERIFIED_SOURCES_NOTE}\n\n{cleaned}"
        return Constants.NO_VERIFIED_SOURCES_NOTE

    def _generate_with_self_correction(self, *, query: str, history: Sequence[tuple[str, str]], context_text: str) -> tuple[str, bool]:
        return _generate_with_self_correction_core(
            prompt=self.prompt,
            llm=self.llm,
            str_parser=self._str_parser,
            query=query,
            history=history,
            context_text=context_text,
        )

    async def a_ask_impl(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[str, list[dict[str, Any]], list[str]]:
        docs, _page_filtered = await self._afinal_docs_for_query(query, edition_filter=edition_filter)
        if not docs:
            return self._enforce_no_verified_sources_integrity(""), [], []
        self._log_retrieval_debug_chunks(docs)
        context_strings = [self._format_context_block(doc) for doc in docs]
        context_text = "\n\n---\n\n".join(context_strings)
        answer, _corrected = await asyncio.to_thread(
            self._generate_with_self_correction,
            query=query,
            history=history,
            context_text=context_text,
        )
        sources = self._build_sources_from_docs(docs)
        if not self._has_source_documents(sources):
            answer = self._enforce_no_verified_sources_integrity(str(answer))
        return answer, sources, context_strings

    def _run_sync_inference(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[str, list[dict[str, Any]], list[str]]:
        docs, _page_filtered = self._final_docs_for_query(query, edition_filter=edition_filter)
        if not docs:
            return self._enforce_no_verified_sources_integrity(""), [], []
        self._log_retrieval_debug_chunks(docs)
        context_strings = [self._format_context_block(doc) for doc in docs]
        context_text = "\n\n---\n\n".join(context_strings)
        answer, _corrected = self._generate_with_self_correction(
            query=query,
            history=history,
            context_text=context_text,
        )
        sources = self._build_sources_from_docs(docs)
        if not self._has_source_documents(sources):
            answer = self._enforce_no_verified_sources_integrity(str(answer))
        return answer, sources, context_strings

    def stream_query(self, query: str, history: Sequence[tuple[str, str]], edition_filter: Optional[str] = None) -> tuple[Iterator[str], list[dict[str, Any]]]:
        query = _clean_query(query)
        t_start = time.perf_counter()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            docs, _ = asyncio.run(self._afinal_docs_for_query(query, edition_filter=edition_filter))
        else:
            docs, _ = self._final_docs_for_query(query, edition_filter=edition_filter)
        t_retrieval = time.perf_counter() - t_start
        self._log_retrieval_debug_chunks(docs)
        context_strings = [self._format_context_block(doc) for doc in docs]
        context_text = "\n\n---\n\n".join(context_strings)
        sources = self._build_sources_from_docs(docs)
        no_verified_context = not self._has_source_documents(sources)
        return (
            _stream_answer_with_integrity_timing_core(
                retrieval_seconds=t_retrieval,
                query=query,
                history=history,
                context_text=context_text,
                no_verified_context=no_verified_context,
                enforce_no_verified_sources_integrity=self._enforce_no_verified_sources_integrity,
                prompt=self.prompt,
                llm=self.llm,
                str_parser=self._str_parser,
                logger=logger,
            ),
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


"""Retrieval plumbing extracted from `main.py`.

This module contains lightweight wrappers for building and using the hybrid
retrieval stack (vector + BM25 ensemble, Flashrank rerankers, and compressed
retriever invocation).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import asyncio

from core.constants import PAGE_RETRIEVAL_WINDOW_EXPANDED
from core.dnd_logic import preferred_basenames_from_query
from core.utils import meta_page_in_range, source_filename, viewer_page_number


@dataclass(frozen=True)
class RetrievalStack:
    """Bundle of retrieval components built during `LoreKeeper` initialization."""

    vector_retriever: Any
    keyword_retriever: Any | None
    base_retriever: Any
    answer_reranker: Any
    compressed_retriever: Any
    source_pruning_reranker: Any


def build_retrieval_stack(
    *,
    langchain_kit: Any,
    vectordb: Any,
    retrieval_k: int,
    rerank_top_n: int,
    source_prune_rerank_top_n: int,
    bm25_weight: float = 0.7,
    vector_weight: float = 0.3,
) -> RetrievalStack:
    """Build the hybrid retrieval stack (vector + BM25 + Flashrank compression).

    Args:
        langchain_kit: Constructor bundle returned from `_langchain_bundle()`.
        vectordb: LangChain `Chroma` vector store instance.
        retrieval_k: First-stage `k` for both vector and BM25 retrievers.
        rerank_top_n: Flashrank `top_n` for the answer compressor.
        source_prune_rerank_top_n: Flashrank `top_n` used for post-merge pruning.
        bm25_weight: Ensemble weight for BM25 retriever.
        vector_weight: Ensemble weight for dense vector retriever.

    Returns:
        A `RetrievalStack` holding the built components.
    """
    vector_retriever = vectordb.as_retriever(search_kwargs={"k": retrieval_k})

    keyword_retriever = None
    base_retriever = vector_retriever

    all_docs = vectordb.get()
    doc_objects = [
        langchain_kit.Document(page_content=txt, metadata=meta)
        for txt, meta in zip(all_docs.get("documents", []), all_docs.get("metadatas", []))
        if txt
    ]

    if doc_objects:
        keyword_retriever = langchain_kit.BM25Retriever.from_documents(doc_objects)
        keyword_retriever.k = retrieval_k
        base_retriever = langchain_kit.EnsembleRetriever(
            retrievers=[vector_retriever, keyword_retriever],
            weights=[vector_weight, bm25_weight],
        )

    answer_reranker = langchain_kit.FlashrankRerank(top_n=rerank_top_n)
    compressed_retriever = langchain_kit.ContextualCompressionRetriever(
        base_compressor=answer_reranker,
        base_retriever=base_retriever,
    )
    source_pruning_reranker = langchain_kit.FlashrankRerank(top_n=source_prune_rerank_top_n)

    return RetrievalStack(
        vector_retriever=vector_retriever,
        keyword_retriever=keyword_retriever,
        base_retriever=base_retriever,
        answer_reranker=answer_reranker,
        compressed_retriever=compressed_retriever,
        source_pruning_reranker=source_pruning_reranker,
    )


def invoke_compressed_retriever(
    *,
    stack: RetrievalStack,
    query: str,
    k: int,
    rerank_top_n: int,
    edition_where: Optional[dict[str, str]] = None,
) -> list[Any]:
    """Temporarily widen dense/BM25 k and Flashrank `top_n`, then restore defaults."""
    vr = stack.vector_retriever
    orig_vec_k = vr.search_kwargs.get("k", k)
    orig_vec_filter = vr.search_kwargs.get("filter")
    orig_bm_k = getattr(stack.keyword_retriever, "k", None) if stack.keyword_retriever else None
    orig_rn = stack.answer_reranker.top_n
    try:
        vr.search_kwargs["k"] = k
        if edition_where is None:
            vr.search_kwargs.pop("filter", None)
        else:
            vr.search_kwargs["filter"] = edition_where
        if stack.keyword_retriever is not None:
            stack.keyword_retriever.k = k
        stack.answer_reranker.top_n = rerank_top_n
        return list(stack.compressed_retriever.invoke(query))
    finally:
        vr.search_kwargs["k"] = orig_vec_k
        if orig_vec_filter is None:
            vr.search_kwargs.pop("filter", None)
        else:
            vr.search_kwargs["filter"] = orig_vec_filter
        if stack.keyword_retriever is not None and orig_bm_k is not None:
            stack.keyword_retriever.k = orig_bm_k
        stack.answer_reranker.top_n = orig_rn


def source_relevance_score(source_path: str, query_lower: str) -> int:
    """Heuristic score: how strongly the user's wording targets a given PDF basename."""
    query_lower = query_lower.replace("manul", "manual")
    basename_lower = (source_path or "").lower()
    score = 0
    # DMG heuristics
    if "dungeon master" in query_lower or "dmg" in query_lower or "dm " in query_lower or " dm" in query_lower:
        if any(x in basename_lower for x in ("dm", "dungeon", "dmg", "master", "guide")):
            score += 25
    if "dm manual" in query_lower or ("dm" in query_lower and "manual" in query_lower):
        if any(x in basename_lower for x in ("dm", "dungeon", "guide", "master")):
            score += 25
    # PHB heuristics
    if ("player" in query_lower and "handbook" in query_lower) or "phb" in query_lower:
        if "player" in basename_lower or "handbook" in basename_lower:
            score += 25
    # Monster Manual heuristics
    if "monster" in query_lower and "manual" in query_lower:
        if "monster" in basename_lower or "mm" in basename_lower:
            score += 25
    for word in __import__("re").findall(r"[a-z]{4,}", query_lower):
        if len(word) > 4 and word in basename_lower:
            score += 2
    return score


def retrieve_by_page_window(
    *,
    get_documents_page_range_fn: Any,
    query: str,
    center_0based: int,
    page_window_pick_limit: int,
    edition_filter: Optional[str],
    source_relevance_score_fn: Any,
) -> list[Any]:
    """Retrieve chunks within a symmetric window around a target PDF page index."""
    span = PAGE_RETRIEVAL_WINDOW_EXPANDED
    low = max(0, center_0based - span)
    high = center_0based + span
    docs = get_documents_page_range_fn(low, high, edition_filter=edition_filter)
    if not docs:
        return []
    basenames = {source_filename(d.metadata.get("source")) for d in docs}
    basenames.discard("Unknown Archive")
    preferred = preferred_basenames_from_query(query, basenames)
    if preferred:
        docs = [d for d in docs if source_filename(d.metadata.get("source")) in preferred]
    if not docs:
        return []
    query_lower = query.lower().replace("manul", "manual")
    scored_rows: list[tuple[int, Any]] = []
    for doc_row in docs:
        src = doc_row.metadata.get("source", "") if doc_row.metadata else ""
        filename_score = source_relevance_score_fn(src, query_lower)
        scored_rows.append((filename_score, doc_row))
    scored_rows.sort(key=lambda row: -row[0])
    if scored_rows and scored_rows[0][0] > 0 and (len(scored_rows) == 1 or scored_rows[0][0] > scored_rows[1][0]):
        winning_basename = source_filename(scored_rows[0][1].metadata.get("source", ""))
        picked = [doc_row for _, doc_row in scored_rows if source_filename(doc_row.metadata.get("source", "")) == winning_basename]
    else:
        picked = [doc_row for _, doc_row in scored_rows]
    return picked[:page_window_pick_limit]


async def afinal_docs_for_query(
    *,
    parse_requested_page_fn: Any,
    semantic_docs_for_query_fn: Any,
    retrieve_by_page_window_fn: Any,
    merge_and_prune_docs_fn: Any,
    query: str,
    edition_filter: Optional[str],
    logger: Any = None,
) -> tuple[list[Any], bool]:
    """Async overlap wrapper: semantic retrieval + optional page-window retrieval."""
    page_idx = parse_requested_page_fn(query)
    sem_task = asyncio.create_task(asyncio.to_thread(semantic_docs_for_query_fn, query, edition_filter=edition_filter))
    if page_idx is not None:
        page_task = asyncio.create_task(asyncio.to_thread(retrieve_by_page_window_fn, query, page_idx, edition_filter=edition_filter))
        sem_docs, page_docs = await asyncio.gather(sem_task, page_task)
        page_filtered = bool(page_docs)
        if page_filtered and logger:
            logger.info(
                "Page-window retrieval: center index %s (~viewer page %s), %s chunk(s)",
                page_idx,
                page_idx + 1,
                len(page_docs),
            )
    else:
        sem_docs = await sem_task
        page_docs = []
        page_filtered = False
    docs = merge_and_prune_docs_fn(query, sem_docs, page_docs, page_filtered)
    return docs, page_filtered


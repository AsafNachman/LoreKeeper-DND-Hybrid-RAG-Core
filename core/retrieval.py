"""Retrieval plumbing extracted from `main.py`.

This module contains lightweight wrappers for building and using the retrieval
stack: dense vector search, BM25 keyword search, Reciprocal Rank Fusion (RRF),
FlashRank reranking, and compressed retriever invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import asyncio
import logging
import math
import re

from core.constants import PAGE_RETRIEVAL_WINDOW_EXPANDED
from core.constants import (
    FINAL_CONTEXT_K,
    INITIAL_RETRIEVAL_K,
    MMR_CANDIDATE_K,
    MMR_FETCH_K,
    RRF_RANK_CONSTANT,
    SINGLE_QUERY_DEEP_K,
)
from core.dnd_logic import preferred_basenames_from_query
from core.utils import source_filename

logger = logging.getLogger("lorekeeper.retrieval")

try:
    from flashrank import Ranker as _FlashrankRanker
    from flashrank import RerankRequest as _FlashrankRerankRequest
except Exception:
    _FlashrankRanker = None  # type: ignore[assignment]
    _FlashrankRerankRequest = None  # type: ignore[assignment]

_FLASHRANK_DIRECT_RANKER: Any = None

# Gamma > 1 exaggerates min–max normalized FlashRank scores so top vs runner-up gaps widen.
_RERANK_SHARPEN_GAMMA = 2.5


@dataclass(frozen=True)
class RetrievalStack:
    """Bundle of retrieval components built during `LoreKeeper` initialization."""

    vector_retriever: Any
    keyword_retriever: Any | None
    base_retriever: Any
    answer_reranker: Any
    compressed_retriever: Any
    source_pruning_reranker: Any


def _doc_key(doc: Any) -> tuple[str, object, str]:
    meta = getattr(doc, "metadata", None) or {}
    src = source_filename(meta.get("source"))
    page = meta.get("page")
    head = (getattr(doc, "page_content", "") or "")[:200]
    return src, page, head


def _doc_source_page_key(doc: Any) -> tuple[str, object]:
    meta = getattr(doc, "metadata", None) or {}
    src = source_filename(meta.get("source"))
    page = meta.get("page")
    return src, page


def _distance_to_similarity(distance: object) -> float:
    """Convert Chroma distance to [0, 1] similarity (higher is better).

    Intent:
        Map lower-is-better distance into a stable identity score for UI/debug:
        ``similarity = 1 / (1 + distance)``, rounded to two decimals for display.
    """
    try:
        d = float(distance)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(d):
        return 0.0
    sim = 1.0 / (1.0 + max(0.0, d))
    return round(max(0.0, min(1.0, sim)), 2)


def _merge_pool_metadata_into_docs(*, pooled: list[Any], selected: list[Any]) -> None:
    """Copy RRF pool scores onto compressor output docs when identities align.

    Intent:
        FlashRank wrappers may return new ``Document`` instances; merge dense
        ``similarity_score`` / ``rrf_score`` from the pooled objects so UI and
        ordering stay consistent with vector retrieval.
    """
    pool_map: dict[tuple[str, object, str], dict[str, float]] = {}
    for d in pooled:
        ky = _doc_key(d)
        meta = getattr(d, "metadata", None) or {}
        try:
            sim = float(meta.get("similarity_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            sim = 0.0
        try:
            rrf = float(meta.get("rrf_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            rrf = 0.0
        prev = pool_map.get(ky)
        if prev is None or sim > prev.get("similarity_score", 0.0):
            pool_map[ky] = {"similarity_score": sim, "rrf_score": rrf}
    for d in selected:
        ky = _doc_key(d)
        if ky not in pool_map:
            continue
        meta = dict(getattr(d, "metadata", None) or {})
        src = pool_map[ky]
        if float(meta.get("similarity_score", 0.0) or 0.0) <= 0.0 and src["similarity_score"] > 0.0:
            meta["similarity_score"] = round(src["similarity_score"], 2)
        if float(meta.get("rrf_score", 0.0) or 0.0) <= 0.0 and src["rrf_score"] > 0.0:
            meta["rrf_score"] = src["rrf_score"]
        setattr(d, "metadata", meta)


def _apply_similarity_fallback_from_rerank(meta: dict[str, Any]) -> None:
    """When dense distance is missing, infer a high similarity from rerank strength.

    Intent:
        Lexical-only RRF hits may lack Chroma distance; the cross-encoder still
        selected them, so expose a non-zero identity score for the evidence UI.
    """
    try:
        rr = float(meta.get("rerank_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        rr = 0.0
    try:
        sim = float(meta.get("similarity_score", 0.0) or 0.0)
    except (TypeError, ValueError):
        sim = 0.0
    if sim <= 0.0 and rr > 0.0:
        meta["similarity_score"] = round(min(0.98, 0.22 + 0.76 * rr), 2)
    else:
        meta["similarity_score"] = round(max(0.0, sim), 2)


def _sort_docs_by_scores(docs: list[Any]) -> list[Any]:
    """Sort docs by rerank desc, then similarity desc."""
    return sorted(
        docs,
        key=lambda d: (
            float((getattr(d, "metadata", {}) or {}).get("rerank_score", 0.0) or 0.0),
            float((getattr(d, "metadata", {}) or {}).get("similarity_score", 0.0) or 0.0),
        ),
        reverse=True,
    )


def _multiclassing_rrf_multiplier(text: str) -> float:
    """Apply +30% to fused RRF mass when the chunk repeats the rules term.

    Intent:
        Dense multiclassing rules (e.g. PHB ~152–153) repeat the section title
        often; looser mentions (e.g. intro on page 39) rarely exceed two hits.
    """
    n = len(re.findall(r"\bMulticlassing\b", text or ""))
    return 1.3 if n > 2 else 1.0


# Single-query (Query-Off) heuristics — applied only when ``single_query_mode`` is True.
_SINGLE_QUERY_SUBJECT_STOP = frozenset(
    {
        "what",
        "how",
        "why",
        "when",
        "where",
        "which",
        "who",
        "does",
        "did",
        "do",
        "can",
        "could",
        "would",
        "should",
        "the",
        "a",
        "an",
        "for",
        "about",
        "explain",
        "tell",
        "rules",
        "rule",
        "dnd",
        "dd",
        "5e",
        "wotc",
    }
)
_PLAYER_CENTRIC_TERMS_RE = re.compile(
    r"\b(multiclassing|multiclass|leveling|level\s+up|subclass|subclasses|class\s+feature|character\s+creation|feats?|ability\s+score|proficiency)\b",
    re.I,
)
_MONSTER_MANUAL_INTENT_RE = re.compile(
    r"\b(monsters?|stat\s*blocks?|creature|creatures|challenge\s+rating|\bcr\b|action|actions|legendary|lair)\b",
    re.I,
)


def extract_single_query_subject(query: str) -> str:
    """Pick a likely lexical subject term for exact-match boosting (single-query mode).

    Args:
        query: Raw user question.

    Returns:
        A single token used for whole-word matching, or empty when none qualifies.

    Heuristic:
        Longest alphabetic token (length >= 5) not in a small stopword set.
    """
    q = str(query or "").strip()
    if not q:
        return ""
    words = re.findall(r"[A-Za-z][A-Za-z']+", q)
    candidates = [w for w in words if w.lower() not in _SINGLE_QUERY_SUBJECT_STOP and len(w) >= 5]
    if not candidates:
        shorter = [w for w in words if w.lower() not in _SINGLE_QUERY_SUBJECT_STOP and len(w) >= 4]
        if not shorter:
            return ""
        return max(shorter, key=len)
    return max(candidates, key=len)


def _subject_literal_in_chunk(subject: str, chunk_text: str) -> bool:
    """True if ``subject`` appears as a whole word in the chunk (case-insensitive)."""
    if not subject or not chunk_text:
        return False
    try:
        return bool(re.search(r"(?<!\w)" + re.escape(subject) + r"(?!\w)", chunk_text, re.I))
    except re.error:
        return subject.lower() in chunk_text.lower()


def _is_monster_manual_2024_metadata(meta: dict[str, Any]) -> bool:
    """Detect 2024 Monster Manual PDFs by basename heuristics."""
    src = source_filename(meta.get("source"))
    s = src.lower()
    return "monster" in s and "manual" in s and "2024" in s


def _single_query_rescore_pool_rrf(pooled: list[Any], original_query: str) -> list[Any]:
    """Apply exact-match and source-type multipliers to ``rrf_score`` (single-query only).

    Intent:
        When multi-query expansion is off, reduce Monster Manual dominance for
        player-rules questions by down-ranking MM chunks unless the query clearly
        targets monsters/stat blocks, and boost chunks that contain the inferred
        subject term literally.
    """
    subject = extract_single_query_subject(original_query)
    q = str(original_query or "")
    player_centric = bool(_PLAYER_CENTRIC_TERMS_RE.search(q))
    monster_intent = bool(_MONSTER_MANUAL_INTENT_RE.search(q))

    for d in pooled:
        meta = dict(getattr(d, "metadata", None) or {})
        try:
            rrf = float(meta.get("rrf_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            rrf = 0.0
        text = str(getattr(d, "page_content", "") or "")
        mult = 1.0
        if subject:
            mult *= 5.0 if _subject_literal_in_chunk(subject, text) else 0.1
        if player_centric and _is_monster_manual_2024_metadata(meta) and not monster_intent:
            mult *= 0.1
        meta["rrf_score"] = max(1e-12, rrf * mult)
        meta["single_query_mass_mult"] = mult
        setattr(d, "metadata", meta)

    return sorted(
        pooled,
        key=lambda d: (
            float((getattr(d, "metadata", {}) or {}).get("rrf_score", 0.0) or 0.0),
            float((getattr(d, "metadata", {}) or {}).get("similarity_score", 0.0) or 0.0),
        ),
        reverse=True,
    )


def _debug_single_query_top3_raw_hits(pooled: list[Any], original_query: str) -> None:
    """Terminal-only forensic line for single-query retrieval (Patch 2.3.9)."""
    top = pooled[:3]
    parts: list[str] = []
    for d in top:
        meta = getattr(d, "metadata", None) or {}
        parts.append(source_filename(meta.get("source")))
    msg = (
        f"[DEBUG 2.3.9] Top 3 Raw Hits for Query: {original_query!r} | "
        f"Sources: [{', '.join(parts)}]"
    )
    print(msg, flush=True)
    logger.info(msg)


def _flashrank_passage_augment(text: str) -> str:
    """Prefix chunk text with lightweight lexical hints for the cross-encoder.

    Intent:
        FlashRank scores raw passage–query interaction; short tags steer the
        model toward mechanics-heavy spans without replacing retrieval (Adapter-style
        hint injection on the passage side only).

    Complexity:
        O(n) over chunk length for a handful of regex scans; O(1) extra space.
    """
    raw = str(text or "")
    flags: list[str] = []
    if re.search(r"(requirements|prerequisites|level\s+up)", raw, re.I):
        flags.append("[P1_RULE_SECTION]")
    if raw.count("|") >= 6 or re.search(r"(?:^|\n)\s*(?:\d+[\.)])\s+\S", raw, re.M):
        flags.append("[P2_TABLE_OR_LIST]")
    if re.search(
        r"(table\s+of\s+contents|^chapter\s+\d|(^|\n)index\b|chapter\s+introduction)",
        raw,
        re.I | re.M,
    ):
        flags.append("[PENALTY_NAV_OR_INTRO]")
    if not flags:
        return raw
    return "".join(flags) + " " + raw


def _flashrank_passage_augment_single_query_strict(text: str) -> str:
    """Stricter cross-encoder hints for single-query (Query-Off) mode only.

    Intent:
        Reduce false positives where shared prefixes (e.g. ``multi-``) match
        unrelated subjects; combined with base rule/table tags from
        `_flashrank_passage_augment`.
    """
    base = _flashrank_passage_augment(text)
    prefix = (
        "[STRICT_RERANK] Your goal is to find the most authoritative mechanical explanation. "
        "Prioritize paragraphs with lists of requirements and level tables over general prose "
        "or introductory summaries. Only assign a high relevance score if this passage explicitly "
        "defines or explains the mechanics asked in the query. Ignore passages that only share "
        "similar word prefixes (e.g. 'multi-') but discuss a different subject. "
    )
    return prefix + base


def _query_mechanical_intent_for_priorities(query: str) -> bool:
    """True when the query reads like a mechanics / rules question (pattern-based)."""
    return bool(
        re.search(
            r"\b(how-to|how\s*to|rules?|leveling|multiclassing|multiclass|requirement|requirements|level\s+up|subclass)\b",
            str(query or ""),
            re.I,
        )
    )


def _single_query_postprocess_sharpened_rerank(
    pooled: list[Any],
    original_query: str,
    sharpened: list[float],
) -> list[float]:
    """Apply filename priors and subject caps to calibrated rerank scores (single-query only).

    Intent:
        Boost core-rulebook filenames for mechanical queries, down-rank bestiary /
        campaign sources when the query is mechanics-shaped, and cap scores when
        the inferred subject token is absent (case-insensitive literal check).
    """
    mechanical = _query_mechanical_intent_for_priorities(original_query)
    subject = extract_single_query_subject(original_query)
    out: list[float] = []
    for idx, d in enumerate(pooled):
        rr = sharpened[idx] if idx < len(sharpened) else 0.0
        text = str(getattr(d, "page_content", "") or "")
        fn = source_filename((getattr(d, "metadata", None) or {}).get("source")).lower()
        if mechanical:
            if any(tok in fn for tok in ("handbook", "core")):
                rr *= 1.4
            if any(tok in fn for tok in ("monster", "bestiary", "campaign")):
                rr *= 0.4
        if subject and subject.lower() not in text.lower():
            rr = min(rr, 0.15)
        out.append(max(0.0, min(1.0, rr)))
    return out


def _backfill_zero_similarity_on_pooled(
    pooled: list[Any],
    key_similarity: dict[tuple[str, object, str], float],
    global_rrf: dict[tuple[str, object, str], float],
) -> None:
    """Ensure UI similarity is non-degenerate when Chroma distance was missing for a key."""
    max_rrf = max(global_rrf.values(), default=0.0) or 1.0
    for d in pooled:
        ky = _doc_key(d)
        meta = dict(getattr(d, "metadata", None) or {})
        sim = float(key_similarity.get(ky, float(meta.get("similarity_score", 0.0)) or 0.0))
        if sim <= 0.0:
            rrf = float(global_rrf.get(ky, float(meta.get("rrf_score", 0.0)) or 0.0))
            sim = round(min(0.92, 0.08 + 0.84 * (rrf / max_rrf)), 2)
        meta["similarity_score"] = sim
        setattr(d, "metadata", meta)


def _gamma_sharpen_rerank_scores(raw: list[float], *, gamma: float = _RERANK_SHARPEN_GAMMA) -> list[float]:
    """Spread min–max normalized rerank scores so the leader separates from the pack.

    Intent:
        FlashRank logits on similar chunks are often nearly tied; gamma > 1
        acts like a low-temperature emphasis on the max (convex remap of [0,1]).

    Complexity:
        O(k) time and O(k) space for k candidates.
    """
    if not raw:
        return raw
    lo = min(raw)
    hi = max(raw)
    if hi <= lo + 1e-12:
        return [0.5 for _ in raw]
    out: list[float] = []
    span = hi - lo + 1e-12
    for x in raw:
        n = (x - lo) / span
        s = n**gamma
        out.append(max(0.0, min(1.0, 0.05 + 0.95 * s)))
    return out


def pool_rrf_candidates(
    *,
    stack: RetrievalStack,
    queries: list[str],
    k: int,
    edition_where: Optional[dict[str, str]] = None,
    mmr_vector_arm: bool = False,
    deep_k_floor: Optional[int] = None,
) -> list[Any]:
    """Pool vector + BM25 candidates using Reciprocal Rank Fusion (RRF).

    Intent:
        Replace legacy weighted ``EnsembleRetriever`` fusion with rank-based RRF
        so BM25 and dense retrieval combine without a single static weight.
        Literal ``Multiclassing`` repetition boosts fused mass (+30%) to favor
        dense rules pages over passing mentions.

    Args:
        stack: Built retrieval stack (vector + optional BM25).
        queries: One or more query strings (original plus optional expansions).
        k: Requested retrieval depth (caps candidate width with ``INITIAL_RETRIEVAL_K``).
        edition_where: Optional Chroma metadata filter.
        mmr_vector_arm:
            When True (single-query path only), rank the vector arm with the
            stack's MMR retriever at width ``k_cap`` after a wide distance scan
            for ``similarity_score = 1/(1+distance)``.
        deep_k_floor:
            Optional minimum ``k`` (e.g. ``SINGLE_QUERY_DEEP_K``) applied only when
            passed from the single-query caller.

    Returns:
        De-duplicated documents sorted by RRF score (descending), with
        ``similarity_score`` (dense) and ``rrf_score`` in metadata.
    """
    vectorstore = getattr(stack.vector_retriever, "vectorstore", None)
    vr = getattr(stack, "vector_retriever", None)
    kr = stack.keyword_retriever
    k_cap = max(int(k), int(INITIAL_RETRIEVAL_K), int(MMR_CANDIDATE_K))
    if deep_k_floor is not None:
        k_cap = max(k_cap, int(deep_k_floor))
    sim_scan_k = max(k_cap, 120)

    orig_bm_k = getattr(kr, "k", None) if kr is not None else None
    orig_vr: dict[str, Any] = {}
    try:
        if kr is not None:
            kr.k = k_cap

        if mmr_vector_arm and vr is not None:
            orig_vr["k"] = vr.search_kwargs.get("k", k_cap)
            orig_vr["fetch_k"] = vr.search_kwargs.get("fetch_k", None)
            orig_vr["lambda_mult"] = vr.search_kwargs.get("lambda_mult", None)
            orig_vr["filter"] = vr.search_kwargs.get("filter")
            vr.search_kwargs["k"] = k_cap
            vr.search_kwargs["fetch_k"] = max(int(MMR_FETCH_K), int(k_cap) * 2)
            vr.search_kwargs["lambda_mult"] = 0.5
            if edition_where is None:
                vr.search_kwargs.pop("filter", None)
            else:
                vr.search_kwargs["filter"] = edition_where

        global_rrf: dict[tuple[str, object, str], float] = {}
        key_similarity: dict[tuple[str, object, str], float] = {}
        key_best_doc: dict[tuple[str, object, str], Any] = {}

        for q in queries:
            q = str(q or "").strip()
            if not q:
                continue

            rows_wide: list[tuple[Any, Any]] = []
            if vectorstore is not None:
                try:
                    rows_wide = list(
                        vectorstore.similarity_search_with_score(
                            q,
                            k=sim_scan_k,
                            filter=edition_where,
                        )
                    )
                except Exception:
                    rows_wide = []
            for doc_row, distance in rows_wide:
                ky = _doc_key(doc_row)
                sim = _distance_to_similarity(distance)
                key_similarity[ky] = max(key_similarity.get(ky, 0.0), sim)
                if ky not in key_best_doc:
                    key_best_doc[ky] = doc_row

            rows_sorted_by_dist = sorted(rows_wide, key=lambda t: float(t[1]))[:k_cap]
            vec_dense_ordered = [t[0] for t in rows_sorted_by_dist]

            vec_ordered: list[Any] = []
            if mmr_vector_arm and vr is not None:
                try:
                    vec_ordered = list(vr.invoke(q))[:k_cap]
                except Exception:
                    vec_ordered = vec_dense_ordered
                if not vec_ordered:
                    vec_ordered = vec_dense_ordered
            else:
                vec_ordered = vec_dense_ordered

            bm_ordered: list[Any] = []
            if kr is not None:
                try:
                    bm_ordered = list(kr.invoke(q))[:k_cap]
                except Exception:
                    bm_ordered = []
            for d in bm_ordered:
                ky = _doc_key(d)
                if ky not in key_best_doc:
                    key_best_doc[ky] = d

            vec_ranks = {_doc_key(d): r + 1 for r, d in enumerate(vec_ordered)}
            bm_ranks = {_doc_key(d): r + 1 for r, d in enumerate(bm_ordered)}
            all_keys = set(vec_ranks) | set(bm_ranks)

            for ky in all_keys:
                rrf = 0.0
                if ky in vec_ranks:
                    rrf += 1.0 / (float(RRF_RANK_CONSTANT) + float(vec_ranks[ky]))
                if ky in bm_ranks:
                    rrf += 1.0 / (float(RRF_RANK_CONSTANT) + float(bm_ranks[ky]))
                global_rrf[ky] = max(global_rrf.get(ky, 0.0), rrf)

        for ky in list(global_rrf.keys()):
            doc = key_best_doc.get(ky)
            if doc is None:
                continue
            text = getattr(doc, "page_content", "") or ""
            mult = _multiclassing_rrf_multiplier(text)
            if mult > 1.0:
                global_rrf[ky] = global_rrf[ky] * mult

        sorted_keys = sorted(global_rrf.keys(), key=lambda key: global_rrf[key], reverse=True)
        pool_cap = max(80, int(k_cap) * 4)
        sorted_keys = sorted_keys[:pool_cap]

        pooled: list[Any] = []
        for ky in sorted_keys:
            doc = key_best_doc.get(ky)
            if doc is None:
                continue
            meta = getattr(doc, "metadata", None) or {}
            sim = float(key_similarity.get(ky, 0.0))
            meta["similarity_score"] = sim
            meta["rrf_score"] = float(global_rrf.get(ky, 0.0))
            if "rerank_score" not in meta:
                meta["rerank_score"] = 0.0
            setattr(doc, "metadata", meta)
            pooled.append(doc)

        _backfill_zero_similarity_on_pooled(pooled, key_similarity, global_rrf)

        return pooled
    finally:
        if kr is not None and orig_bm_k is not None:
            kr.k = orig_bm_k
        if mmr_vector_arm and vr is not None and orig_vr:
            vr.search_kwargs["k"] = orig_vr["k"]
            if orig_vr.get("fetch_k") is None:
                vr.search_kwargs.pop("fetch_k", None)
            else:
                vr.search_kwargs["fetch_k"] = orig_vr["fetch_k"]
            if orig_vr.get("lambda_mult") is None:
                vr.search_kwargs.pop("lambda_mult", None)
            else:
                vr.search_kwargs["lambda_mult"] = orig_vr["lambda_mult"]
            if orig_vr.get("filter") is None:
                vr.search_kwargs.pop("filter", None)
            else:
                vr.search_kwargs["filter"] = orig_vr["filter"]


def build_retrieval_stack(
    *,
    langchain_kit: Any,
    vectordb: Any,
    retrieval_k: int,
    rerank_top_n: int,
    source_prune_rerank_top_n: int,
) -> RetrievalStack:
    """Build the retrieval stack (dense MMR + BM25 + FlashRank compression).

    Intent:
        Dense and lexical retrievers stay separate so ``pool_rrf_candidates`` can
        fuse ranks with RRF. The compressed retriever uses dense-only invoke for
        legacy call sites; primary QA uses ``invoke_rrf_rerank``.

    Args:
        langchain_kit: Constructor bundle returned from `_langchain_bundle()`.
        vectordb: LangChain `Chroma` vector store instance.
        retrieval_k: First-stage `k` for dense and BM25 retrievers.
        rerank_top_n: Flashrank `top_n` for the answer compressor.
        source_prune_rerank_top_n: Flashrank `top_n` used for post-merge pruning.

    Returns:
        A `RetrievalStack` holding the built components.
    """
    vector_retriever = vectordb.as_retriever(
        search_type="mmr",
        search_kwargs={
            "k": max(int(retrieval_k), int(MMR_CANDIDATE_K)),
            "fetch_k": int(MMR_FETCH_K),
            "lambda_mult": 0.5,
        },
    )

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
    """Temporarily widen dense `k` and Flashrank `top_n`, then restore defaults."""
    vr = stack.vector_retriever
    orig_vec_k = vr.search_kwargs.get("k", k)
    orig_fetch_k = vr.search_kwargs.get("fetch_k", None)
    orig_lambda = vr.search_kwargs.get("lambda_mult", None)
    orig_vec_filter = vr.search_kwargs.get("filter")
    orig_bm_k = getattr(stack.keyword_retriever, "k", None) if stack.keyword_retriever else None
    orig_rn = stack.answer_reranker.top_n
    try:
        vr.search_kwargs["k"] = max(int(k), int(MMR_CANDIDATE_K))
        vr.search_kwargs["fetch_k"] = int(MMR_FETCH_K)
        vr.search_kwargs["lambda_mult"] = 0.5
        if edition_where is None:
            vr.search_kwargs.pop("filter", None)
        else:
            vr.search_kwargs["filter"] = edition_where
        if stack.keyword_retriever is not None:
            stack.keyword_retriever.k = max(int(k), int(MMR_CANDIDATE_K))
        stack.answer_reranker.top_n = int(rerank_top_n or FINAL_CONTEXT_K)
        return list(stack.compressed_retriever.invoke(query))
    finally:
        vr.search_kwargs["k"] = orig_vec_k
        if orig_fetch_k is None:
            vr.search_kwargs.pop("fetch_k", None)
        else:
            vr.search_kwargs["fetch_k"] = orig_fetch_k
        if orig_lambda is None:
            vr.search_kwargs.pop("lambda_mult", None)
        else:
            vr.search_kwargs["lambda_mult"] = orig_lambda
        if orig_vec_filter is None:
            vr.search_kwargs.pop("filter", None)
        else:
            vr.search_kwargs["filter"] = orig_vec_filter
        if stack.keyword_retriever is not None and orig_bm_k is not None:
            stack.keyword_retriever.k = orig_bm_k
        stack.answer_reranker.top_n = orig_rn


def invoke_rrf_rerank(
    *,
    stack: RetrievalStack,
    original_query: str,
    queries: list[str],
    k: int,
    rerank_top_n: int,
    edition_where: Optional[dict[str, str]] = None,
    single_query_mode: bool = False,
) -> list[Any]:
    """RRF candidate pool → FlashRank with semantic hints → sharpened scores.

    Args:
        single_query_mode:
            When True (single-query / Query-Off path only), apply deep-K pooling with
            MMR vector ordering, strict FlashRank passage prefixes, pool rescoring,
            rerank post-priors, debug logging, and ``SINGLE_QUERY_DEEP_K``. The
            multi-query path must pass False so behavior stays unchanged.

    Returns:
        Final reranked chunks (length <= rerank_top_n).
    """
    pooled = pool_rrf_candidates(
        stack=stack,
        queries=queries,
        k=k,
        edition_where=edition_where,
        mmr_vector_arm=single_query_mode,
        deep_k_floor=SINGLE_QUERY_DEEP_K if single_query_mode else None,
    )
    if not pooled:
        return []
    passage_aug = _flashrank_passage_augment_single_query_strict if single_query_mode else _flashrank_passage_augment
    if single_query_mode:
        pooled = _single_query_rescore_pool_rrf(pooled, original_query)
        _debug_single_query_top3_raw_hits(pooled, original_query)
    if _FlashrankRanker is not None and _FlashrankRerankRequest is not None:
        try:
            global _FLASHRANK_DIRECT_RANKER
            if _FLASHRANK_DIRECT_RANKER is None:
                _FLASHRANK_DIRECT_RANKER = _FlashrankRanker()
            passages = [
                {
                    "id": idx,
                    "text": passage_aug(str(getattr(d, "page_content", "") or "")),
                }
                for idx, d in enumerate(pooled)
            ]
            req = _FlashrankRerankRequest(query=str(original_query or ""), passages=passages)
            raw_rows = list(_FLASHRANK_DIRECT_RANKER.rerank(req))
            logger.info("RERANK_RAW query=%r rows=%s", original_query[:120], raw_rows[:8])
            by_id: dict[int, float] = {}
            for row in raw_rows:
                try:
                    rid = int(row.get("id"))
                except Exception:
                    continue
                try:
                    by_id[rid] = float(row.get("score", 0.0))
                except (TypeError, ValueError):
                    by_id[rid] = 0.0
            raw_list = [by_id.get(idx, 0.0) for idx in range(len(pooled))]
            sharpened = _gamma_sharpen_rerank_scores(raw_list)
            if single_query_mode:
                sharpened = _single_query_postprocess_sharpened_rerank(pooled, original_query, sharpened)
            scored: list[Any] = []
            for idx, d in enumerate(pooled):
                meta = dict(getattr(d, "metadata", None) or {})
                rr = sharpened[idx] if idx < len(sharpened) else 0.0
                meta["rerank_score"] = rr
                _apply_similarity_fallback_from_rerank(meta)
                setattr(d, "metadata", meta)
                scored.append(d)
            scored = _sort_docs_by_scores(scored)
            if len(scored) > 1:
                uniq = {round(float((getattr(d, "metadata", {}) or {}).get("rerank_score", 0.0) or 0.0), 6) for d in scored}
                if len(uniq) <= 1:
                    logger.error(
                        "Reranker flatline detected (direct flashrank). query=%r score=%s",
                        original_query[:120],
                        next(iter(uniq)) if uniq else None,
                    )
                    for idx, d in enumerate(scored):
                        meta = dict(getattr(d, "metadata", None) or {})
                        sim = float(meta.get("similarity_score", 0.0) or 0.0)
                        meta["rerank_score"] = max(0.0, min(1.0, 0.5 + 0.4 * sim - 0.01 * idx))
                        _apply_similarity_fallback_from_rerank(meta)
                        setattr(d, "metadata", meta)
                    scored = _sort_docs_by_scores(scored)
            return scored[: int(rerank_top_n or FINAL_CONTEXT_K)]
        except Exception as exc:
            logger.exception("Direct FlashRank rerank failed; falling back to wrapper (%s).", exc)
    orig_rn = stack.answer_reranker.top_n
    saved_texts = [str(getattr(d, "page_content", "") or "") for d in pooled]
    try:
        for d, raw in zip(pooled, saved_texts):
            setattr(d, "page_content", passage_aug(raw))
        stack.answer_reranker.top_n = int(rerank_top_n or FINAL_CONTEXT_K)
        out = list(stack.answer_reranker.compress_documents(pooled, original_query))
        _merge_pool_metadata_into_docs(pooled=pooled, selected=out)
        raw_list: list[float] = []
        for d in out:
            meta = getattr(d, "metadata", None) or {}
            raw = meta.get("relevance_score", 0.0)
            try:
                raw_list.append(float(raw))
            except (TypeError, ValueError):
                raw_list.append(0.0)
        sharpened = _gamma_sharpen_rerank_scores(raw_list) if raw_list else []
        if single_query_mode:
            sharpened = _single_query_postprocess_sharpened_rerank(out, original_query, sharpened)
        for i, d in enumerate(out):
            meta = dict(getattr(d, "metadata", None) or {})
            rr = sharpened[i] if i < len(sharpened) else 0.0
            meta["rerank_score"] = rr
            _apply_similarity_fallback_from_rerank(meta)
            setattr(d, "metadata", meta)
        out = _sort_docs_by_scores(out)
        if len(out) > 1:
            uniq = {round(float((getattr(d, "metadata", {}) or {}).get("rerank_score", 0.0) or 0.0), 6) for d in out}
            if len(uniq) <= 1:
                logger.error(
                    "Reranker flatline detected (wrapper). query=%r score=%s",
                    original_query[:120],
                    next(iter(uniq)) if uniq else None,
                )
                for idx, d in enumerate(out):
                    meta = dict(getattr(d, "metadata", None) or {})
                    sim = float(meta.get("similarity_score", 0.0) or 0.0)
                    meta["rerank_score"] = max(0.0, min(1.0, 0.5 + 0.4 * sim - 0.01 * idx))
                    _apply_similarity_fallback_from_rerank(meta)
                    setattr(d, "metadata", meta)
                out = _sort_docs_by_scores(out)
        logger.info(
            "RERANK_RAW_WRAPPER query=%r rows=%s",
            original_query[:120],
            [
                {
                    "score": float((getattr(d, "metadata", {}) or {}).get("rerank_score", 0.0) or 0.0),
                    "sim": float((getattr(d, "metadata", {}) or {}).get("similarity_score", 0.0) or 0.0),
                    "head": (getattr(d, "page_content", "") or "")[:80],
                }
                for d in out[:8]
            ],
        )
        return out
    finally:
        for d, raw in zip(pooled, saved_texts):
            setattr(d, "page_content", raw)
        stack.answer_reranker.top_n = orig_rn


def invoke_single_query_rerank(
    *,
    stack: RetrievalStack,
    query: str,
    k: int,
    rerank_top_n: int,
    edition_where: Optional[dict[str, str]] = None,
) -> list[Any]:
    """Single-query RRF pool → rerank → sort (Patch 2.3.9 single-query heuristics on)."""
    return invoke_rrf_rerank(
        stack=stack,
        original_query=query,
        queries=[query],
        k=k,
        rerank_top_n=rerank_top_n,
        edition_where=edition_where,
        single_query_mode=True,
    )


# Back-compat alias for imports expecting the old name.
invoke_multi_query_rerank = invoke_rrf_rerank


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

"""D&D-specific heuristics and query interpretation for Lore Keeper.

This module isolates game-domain logic (conditions, book/filename hints) from
general utilities and retrieval plumbing.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from core.utils import source_filename


# Named conditions: widen retrieval when no page is cited (second pass targets PHB-style appendix text).
CONDITION_DEEP_CONTEXT_RE = re.compile(
    r"\b(prone|grappled|stunned|paralyzed|restrained|unconscious|incapacitated|charmed|"
    r"frightened|poisoned|blinded|deafened|exhaustion|exhausted|petrified)\b",
    re.IGNORECASE,
)


def query_wants_condition_deep_context(query: str) -> bool:
    """True if the question likely needs full condition rules, not a single chunk fragment."""
    return bool(CONDITION_DEEP_CONTEXT_RE.search(query))


def condition_canonical_terms_from_query(query: str) -> list[str]:
    """Return distinct condition names (Title Case) mentioned in the query.

    Maps colloquial forms (e.g. exhausted) to glossary headers (Exhaustion) for
    Chroma `where_document` substring filters.
    """
    key_to_canonical = {
        "exhausted": "Exhaustion",
        "exhaustion": "Exhaustion",
    }
    seen: list[str] = []
    for m in CONDITION_DEEP_CONTEXT_RE.finditer(query):
        low = m.group(0).lower()
        canon = key_to_canonical.get(low, m.group(0).title())
        if canon not in seen:
            seen.append(canon)
    return seen


def where_document_clause_for_term(term: str) -> dict[str, Any]:
    """Build a Chroma `where_document` filter favoring glossary capitalization."""
    variants = list(dict.fromkeys([term, term.capitalize(), term.lower()]))
    if len(variants) == 1:
        return {"$contains": variants[0]}
    return {"$or": [{"$contains": t} for t in variants]}


def score_condition_literal_chunk(doc: Any, term: str) -> int:
    """Heuristic priority for appendix-style condition text vs stat-block noise."""
    text = doc.page_content or ""
    tl = text.lower()
    score = 0
    src_name = source_filename(doc.metadata.get("source") if doc.metadata else None).lower()
    if "player" in src_name and "handbook" in src_name:
        score += 120
    if "appendix" in tl and "condition" in tl:
        score += 45
    if re.search(rf"(?m)^\s*{re.escape(term)}\s*$", text, re.IGNORECASE):
        score += 70
    if re.search(r"\bAC\s+\d", text):
        score -= 40
    if "legendary" in tl and "action" in tl:
        score -= 30
    return score


# Loose match for PDF names typed in chat (e.g. "5e DM's Guide (2024).pdf")
PDF_FILENAME_IN_QUERY_RE = re.compile(
    r"([^\n\"<>|*?]+\.pdf)",
    re.IGNORECASE,
)


def preferred_basenames_from_query(
    query: str, available_basenames: set[str]
) -> Optional[set[str]]:
    """Resolve which indexed PDFs the user intends when metadata page spans many books.

    Intent:
        Page-window retrieval can return the same PDF index from multiple books.
        Matching explicit `*.pdf` substrings or book nicknames (DMG, PHB) restricts
        chunks to the relevant volume before ranking.
    """
    if not available_basenames:
        return None
    q = query.lower()
    matched: set[str] = set()

    # Explicit .pdf substring in the question
    for raw in PDF_FILENAME_IN_QUERY_RE.findall(query):
        hint = source_filename(raw.strip())
        if not hint or hint == "Unknown Archive":
            continue
        hint_low = hint.lower()
        for b in available_basenames:
            bl = b.lower()
            if hint_low == bl or hint_low in bl or bl in hint_low:
                matched.add(b)

    if matched:
        return matched

    # Book keywords → filename heuristics (only when clearly named)
    candidates: list[str] = []
    if re.search(
        r"\b(dungeon\s+master'?s?\s+guide|dm'?s?\s+guide|dmg|dm\s+guide)\b",
        q,
    ) or (re.search(r"\bdm\b", q) and "guide" in q):
        candidates = [
            b
            for b in available_basenames
            if any(
                x in b.lower()
                for x in ("dm", "dungeon", "dmg", "master", "guide")
            )
        ]
    elif ("player" in q and "handbook" in q) or re.search(r"\bphb\b", q):
        candidates = [
            b for b in available_basenames if "player" in b.lower() or "handbook" in b.lower()
        ]
    elif "monster" in q and "manual" in q:
        candidates = [
            b for b in available_basenames if "monster" in b.lower() or "mm" in b.lower()
        ]

    if not candidates:
        return None
    if len(candidates) == 1:
        return {candidates[0]}
    if "2024" in q:
        y24 = [b for b in candidates if "2024" in b]
        if len(y24) == 1:
            return {y24[0]}
        if y24:
            return set(y24)
    dmg_2024 = [b for b in candidates if "2024" in b and "dm" in b.lower()]
    if len(dmg_2024) == 1:
        return {dmg_2024[0]}
    return set(candidates)


def fetch_condition_literal_hits(
    *,
    vectordb: Any,
    document_cls: Any,
    terms: list[str],
    condition_where_doc_limit: int,
    condition_literal_keep: int,
    where_document_clause_for_term_fn: Any,
    score_condition_literal_chunk_fn: Any,
    dedupe_documents_fn: Any,
    edition_where: Optional[dict[str, str]] = None,
    logger: Any = None,
) -> list[Any]:
    """Chroma `where_document` substring pulls; score toward PHB appendix definitions."""
    if not terms:
        return []
    seen_ids: set[str] = set()
    raw_docs: list[Any] = []
    for term in terms:
        wd = where_document_clause_for_term_fn(term)
        try:
            kwargs: dict[str, Any] = {
                "where_document": wd,
                "include": ["documents", "metadatas"],
                "limit": condition_where_doc_limit,
            }
            if edition_where is not None:
                kwargs["where"] = edition_where
            batch = vectordb.get(**kwargs)
        except Exception as exc:
            if logger:
                logger.debug("Chroma where_document failed for %r: %s", term, exc)
            continue
        ids = batch.get("ids") or []
        texts = batch.get("documents") or []
        metas = batch.get("metadatas") or []
        for doc_id, txt, meta in zip(ids, texts, metas):
            if not txt or doc_id in seen_ids:
                continue
            seen_ids.add(str(doc_id))
            raw_docs.append(document_cls(page_content=txt, metadata=dict(meta or {})))

    def best_score(d: Any) -> int:
        body = d.page_content or ""
        return max(
            (
                score_condition_literal_chunk_fn(d, t)
                for t in terms
                if re.search(rf"\b{re.escape(t)}\b", body, re.IGNORECASE)
            ),
            default=-10**9,
        )

    ranked = [d for d in raw_docs if best_score(d) > -10**8]
    ranked.sort(key=best_score, reverse=True)
    ranked = dedupe_documents_fn(ranked)
    return ranked[:condition_literal_keep]


def doc_index_in_page_bucket(doc: Any, bucket: list[Any]) -> Optional[int]:
    """Index of `doc` in a same-source same-page chunk list (splitter order proxy)."""
    dc = doc.page_content or ""
    for i, x in enumerate(bucket):
        if (x.page_content or "") == dc:
            return i
    head = dc[:280]
    for i, x in enumerate(bucket):
        if (x.page_content or "")[:280] == head:
            return i
    return None


def expand_condition_windowed_chunks(
    *,
    docs: list[Any],
    terms: list[str],
    docs_by_source_page: dict[tuple[str, int], list[Any]],
    dedupe_documents_fn: Any,
) -> list[Any]:
    """Window-expansion for condition-heavy appendix text without new ingest metadata."""
    if not docs_by_source_page or not terms:
        return docs
    patterns = [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in terms]

    def mentions(text: str) -> bool:
        return any(p.search(text) for p in patterns)

    out: list[Any] = list(docs)
    for d in docs:
        text = d.page_content or ""
        if not mentions(text):
            continue
        meta = d.metadata or {}
        src = meta.get("source")
        if src is None:
            continue
        src = str(src)
        try:
            p = int(meta.get("page"))
        except (TypeError, ValueError):
            continue
        bucket = docs_by_source_page.get((src, p), [])
        out.extend(bucket)
        idx = doc_index_in_page_bucket(d, bucket)
        if idx is not None:
            for ni in (idx - 1, idx + 1):
                if 0 <= ni < len(bucket):
                    out.append(bucket[ni])
        for delta in (-1, 1):
            p2 = p + delta
            if p2 < 0:
                continue
            out.extend(docs_by_source_page.get((src, p2), []))
    return dedupe_documents_fn(out)


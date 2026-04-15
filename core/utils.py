"""General-purpose helpers for Lore Keeper core.

These functions are intentionally UI-agnostic and safe to use from Streamlit,
CLI, ingestion, or evaluation paths.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Optional


_DND_PHRASE_NORMALIZATIONS: dict[str, str] = {
    # Books (canonicalize to common short-hand tokens; stable for retrieval).
    "phb": "phb",
    "player handbook": "phb",
    "players handbook": "phb",
    "player's handbook": "phb",
    "dmg": "dmg",
    "dm guide": "dmg",
    "dungeon masters guide": "dmg",
    "dungeon master's guide": "dmg",
    "mm": "mm",
    "monster manual": "mm",
    "xgte": "xgte",
    "xanathar": "xgte",
    "xanathar's guide to everything": "xgte",
    "tcoe": "tcoe",
    "tasha": "tcoe",
    "tasha's cauldron of everything": "tcoe",

    # Common typos (high-impact on book + rules queries).
    "manul": "manual",
}

_DND_TOKEN_VOCAB: set[str] = {
    # Core terms that frequently appear in rules questions and benefit retrieval when corrected.
    "manual",
    "initiative",
    "proficiency",
    "advantage",
    "disadvantage",
    "concentration",
    "cantrip",
    "spell",
    "spells",
    "saving",
    "throw",
    "saves",
    "attack",
    "attacks",
    "damage",
    "resistance",
    "immunity",
    "vulnerability",
    "condition",
    "conditions",
    "prone",
    "grappled",
    "restrained",
    "stunned",
    "paralyzed",
    "incapacitated",
    "unconscious",
    "blinded",
    "deafened",
    "charmed",
    "frightened",
    "poisoned",
    "exhaustion",
    "invisible",
    "surprised",
    "cover",
    "flanking",
}


def clean_query(query: str) -> str:
    """Normalize common D&D shorthand and near-miss terms in user queries.

    Intent:
        Improve retrieval and filename/book disambiguation without an LLM call.
        Uses dictionary-backed fuzzy matching for a small vocabulary of high-value
        D&D terms (PHB/DMG/MM/Xanathar/Tasha, etc).

    Args:
        query: Raw user query.

    Returns:
        Cleaned query string with best-effort expansions.
    """
    raw = " ".join(str(query or "").split()).strip()
    if not raw:
        return ""

    out = raw

    # Phrase-level, word-boundary normalization (abbreviations + very common typos).
    # We process longer keys first so multi-word phrases win over single-token matches.
    for key in sorted(_DND_PHRASE_NORMALIZATIONS, key=len, reverse=True):
        val = _DND_PHRASE_NORMALIZATIONS[key]
        out = re.sub(rf"\b{re.escape(key)}\b", val, out, flags=re.IGNORECASE)

    # Token-level fuzzy correction for a small, high-value D&D vocabulary.
    # This is intentionally conservative to avoid rewriting ordinary words.
    words = out.split()
    vocab = sorted(_DND_TOKEN_VOCAB)
    changed = False
    for i, w in enumerate(words):
        lead = re.match(r"^\W+", w)
        tail = re.search(r"\W+$", w)
        prefix = lead.group(0) if lead else ""
        suffix = tail.group(0) if tail else ""
        core = w[len(prefix) : (len(w) - len(suffix) if suffix else len(w))]
        low = core.lower()

        if len(low) < 4 or len(low) > 14:
            continue
        if not low.isalpha():
            continue
        if low in _DND_TOKEN_VOCAB:
            continue

        match = difflib.get_close_matches(low, vocab, n=1, cutoff=0.88)
        if not match:
            continue
        fixed = match[0]
        words[i] = f"{prefix}{fixed}{suffix}"
        changed = True

    return " ".join(words) if changed else out


def source_filename(source_path: Optional[str]) -> str:
    """Return the PDF filename for display and LLM headers, stripping directory prefixes.

    Intent:
        Chroma metadata often stores Windows-style paths (`data\\book.pdf`). On
        POSIX hosts, `pathlib.Path` does not treat `\\` as a separator, so we
        normalize to `/` before taking the final path component (`Path.name`).

    Args:
        source_path: Raw `source` field from chunk metadata, or None.

    Returns:
        Basename such as `5e DM's Guide (2024).pdf`, or the string `Unknown Archive`.
    """
    if source_path is None:
        return "Unknown Archive"
    p = str(source_path).strip()
    if not p:
        return "Unknown Archive"
    p = p.replace("\\", "/")
    name = Path(p).name
    if not name or name in (".", ".."):
        return "Unknown Archive"
    return name


def normalize_stored_citation(citation: str) -> str:
    """Normalize persisted citation lines after UI or path format changes.

    Intent:
        Older chat history may store `data\\file.pdf (Page N)`; re-parse the file
        segment with `source_filename` so the UI matches new behavior. Supports
        both ` (Page N)` and merged ` (Pages a-b, c)` labels.

    Args:
        citation: Full citation string, e.g. `{file} (Page {n})` or `{file} (Pages {span})`.

    Returns:
        The same structure with a cleaned filename segment.
    """
    c = citation.strip()
    if not c.endswith(")"):
        return c
    pages_marker = " (Pages "
    idx = c.rfind(pages_marker)
    if idx > 0:
        file_part = c[:idx].strip()
        span_part = c[idx + len(pages_marker) : -1].strip()
        if span_part:
            return f"{source_filename(file_part)} (Pages {span_part})"
        return c
    marker = " (Page "
    idx = c.rfind(marker)
    if idx <= 0:
        return c
    file_part = c[:idx].strip()
    page_part = c[idx + len(marker) : -1].strip()
    if not page_part:
        return c
    return f"{source_filename(file_part)} (Page {page_part})"


def viewer_page_number(raw: object) -> Optional[int]:
    """Map chunk metadata `page` to a 1-based viewer page, or None if not coercible.

    PyPDFLoader stores 0-based indices; the UI has historically shown viewer page = index + 1.
    """
    if raw is None:
        return None
    try:
        if hasattr(raw, "item"):
            raw = raw.item()
        n = int(raw)
        return n + 1
    except (TypeError, ValueError):
        return None


def normalize_brain_id(brain_id: Optional[str]) -> str:
    """Normalize a brain identifier into a filesystem-safe directory name.

    Args:
        brain_id: Raw brain label from UI/CLI.

    Returns:
        Lowercase slug using `[a-z0-9_-]`. Empty values map to `dnd_core`.
    """
    raw = (brain_id or "").strip().lower()
    if not raw:
        return "dnd_core"
    safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in raw).strip("_")
    return safe or "dnd_core"


def meta_page_in_range(page_val: object, low: int, high: int) -> bool:
    """Return True if `page_val` coerces to an integer in [low, high].

    Args:
        page_val: Chroma metadata `page` (may be int, str, or numpy scalar).
        low: Inclusive lower bound (0-based PDF page index).
        high: Inclusive upper bound.

    Returns:
        Whether the page lies in the closed interval.
    """
    try:
        page_number = int(page_val)
    except (TypeError, ValueError):
        return False
    return low <= page_number <= high


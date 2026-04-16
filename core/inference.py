"""Inference machinery for Lore Keeper (self-correction, critic, and streaming).

This module isolates the "heavy machinery" of answer generation from the
`main.LoreKeeper` orchestrator wrapper.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import time
from collections.abc import Iterator, Sequence
from typing import Any, Optional

from core.constants import Constants

logger = logging.getLogger("lorekeeper.inference")

_CODE_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)

_CONTEXT_CITATION_PREFIX_PHRASE = "From"
_LEADING_ATTRIBUTION_RE = re.compile(
    r"^\s*(?:according to|from)\s+.*?(?:,\s*|\:\s*|\n\s*\n)",
    re.IGNORECASE | re.DOTALL,
)

_META_TALK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bself-?correction\b", re.IGNORECASE),
    re.compile(r"\bcritic notes?\b", re.IGNORECASE),
    re.compile(r"\bprevious candidate\b", re.IGNORECASE),
    re.compile(r"\bhallucinat", re.IGNORECASE),
    re.compile(r"\bi (?:must|will)\s+(?:correct|revise|rephrase)\b", re.IGNORECASE),
    re.compile(r"\bto preserve structure\b", re.IGNORECASE),
    re.compile(r"\bto remove\b.*\bhallucinat", re.IGNORECASE),
    re.compile(r"\binternal reasoning\b", re.IGNORECASE),
)

_PAGE_MENTION_RE = re.compile(r"\bpage\s+(\d{1,4})\b", re.IGNORECASE)
_CONTEXT_PAGE_LABEL_RE = re.compile(r"\[Source:\s*[^\]]+?\|\s*Page\s+(\d{1,4})\]", re.IGNORECASE)


def _page_number_audit(candidate_answer: str, context_text: str) -> tuple[bool, str]:
    """Return (ok, fix_instruction) for page-number integrity.

    Intent:
        The assistant must not invent page numbers. If the answer mentions any
        `Page N` that is not present in `[Source: ... | Page N]` labels in the
        retrieved context, force a DATA_MISMATCH failure.
    """
    answer_pages = {m.group(1) for m in _PAGE_MENTION_RE.finditer(str(candidate_answer or ""))}
    if not answer_pages:
        return True, ""
    allowed_pages = {m.group(1) for m in _CONTEXT_PAGE_LABEL_RE.finditer(str(context_text or ""))}
    # If context has no labels, treat any page mention as invalid.
    if not allowed_pages:
        return False, "DATA_MISMATCH: Answer mentioned page numbers but context had no page labels."
    bad = sorted(p for p in answer_pages if p not in allowed_pages)
    if bad:
        return False, f"DATA_MISMATCH: Invented page reference(s) not in context labels: {', '.join(bad)}."
    return True, ""


def _strip_common_lore_disclaimer_when_context_present(answer_text: str) -> str:
    """Remove the common-lore disclaimer prefix when context was provided.

    Intent:
        Streaming paths yield a single final answer chunk. When verified context
        exists, we should never surface the "couldn't find the exact scroll"
        disclaimer (it is reserved for true no-source answers).
    """
    raw = str(answer_text or "").lstrip()
    if not raw.startswith(Constants.COMMON_LORE_DISCLAIMER):
        return str(answer_text or "").strip()
    stripped = raw[len(Constants.COMMON_LORE_DISCLAIMER) :].lstrip()
    return stripped.strip()

def _sanitize_user_visible_answer(answer_text: str) -> str:
    """Strip meta-talk and internal repair artifacts from user-visible output.

    Intent:
        The hidden critic/repair loop should never leak "I am correcting myself"
        style narration. This sanitizer applies deterministic cleanup after the
        model generates text.
    """
    txt = str(answer_text or "").strip()
    if not txt:
        return ""
    lines = [ln.rstrip() for ln in txt.splitlines()]
    kept: list[str] = []
    for ln in lines:
        if not ln.strip():
            kept.append("")
            continue
        if any(p.search(ln) for p in _META_TALK_PATTERNS):
            continue
        kept.append(ln)
    cleaned = "\n".join(kept).strip()
    # Collapse excessive blank lines introduced by removals.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _inject_verified_source_citation(answer_text: str, *, citation: str) -> str:
    """Ensure the answer explicitly names the verified source citation.

    Intent:
        The UI already renders "View Verified Sources", but the assistant text
        sometimes says "According to the provided context from ," with a blank
        citation (model omission). This function deterministically injects the
        citation into the answer body when verified context exists.

    Args:
        answer_text: Model answer text.
        citation: Verified citation label (e.g., "Book.pdf (Page 12)").

    Returns:
        Answer text that explicitly references `citation`.
    """
    cleaned = str(answer_text or "").strip()
    cite = str(citation or "").strip()
    if not cleaned or not cite:
        return cleaned
    # If the model already included some leading attribution, replace it with ours
    # so the UI-selected "best" source wins deterministically.
    replaced = _LEADING_ATTRIBUTION_RE.sub("", cleaned, count=1).lstrip()
    cleaned = replaced if replaced and replaced != cleaned else cleaned

    lowered = cleaned.lower()
    phrase_lower = "according to the provided context from".lower()
    if lowered.startswith(phrase_lower):
        # Repair legacy phrasing: "According to the provided context from , ..."
        tail = cleaned[len("According to the provided context from") :].lstrip()
        if tail.startswith(","):
            tail = tail[1:].lstrip()
        return f"From {cite}:\n\n{tail}".strip()

    return f"From {cite}:\n\n{cleaned}".strip()


def _compact_raw_snippet(text: str, *, limit: int = 1200) -> str:
    """Return a compact snippet of potentially-long raw model output.

    Args:
        text: Raw content to summarize.
        limit: Max total characters returned.

    Returns:
        A snippet that preserves both prefix and suffix when long.
    """
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    head = raw[: int(limit * 0.7)].rstrip()
    tail = raw[-int(limit * 0.3) :].lstrip()
    return f"{head}\n… <snip {len(raw) - len(head) - len(tail)} chars> …\n{tail}"


def _extract_first_json_object(text: str) -> Optional[str]:
    """Extract the first balanced JSON object substring from arbitrary text.

    Intent:
        Some models wrap JSON in markdown fences or prepend/append prose. This
        function attempts to recover the first `{...}` region with balanced
        braces so we can still parse the verdict.

    Args:
        text: Raw model output.

    Returns:
        JSON object substring including braces, or None if not found.
    """
    s = str(text or "")
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def _parse_critic_payload(text: str) -> tuple[Optional[str], list[str], str]:
    """Parse the hidden critic payload from raw LLM output.

    Args:
        text: Raw critic output (ideally JSON).

    Returns:
        (`verdict`, `issues`, `fix_instruction`) where verdict may be None when
        extraction fails.
    """
    txt = _CODE_FENCE_RE.sub("", str(text or "")).strip()

    candidates: list[str] = []
    if txt:
        candidates.append(txt)
    extracted = _extract_first_json_object(txt)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            obj = None

        if isinstance(obj, dict):
            verdict = obj.get("verdict")
            issues = obj.get("issues", [])
            fix_instruction = obj.get("fix_instruction")
            return (
                (str(verdict).strip().lower() if verdict is not None else None),
                [str(x) for x in issues] if isinstance(issues, list) else [str(issues)],
                str(fix_instruction or "").strip(),
            )

        # Some backends stringify dicts with single quotes (Python repr). Try safely.
        try:
            py_obj = ast.literal_eval(cand)
        except Exception:
            py_obj = None
        if isinstance(py_obj, dict):
            verdict = py_obj.get("verdict")
            issues = py_obj.get("issues", [])
            fix_instruction = py_obj.get("fix_instruction")
            return (
                (str(verdict).strip().lower() if verdict is not None else None),
                [str(x) for x in issues] if isinstance(issues, list) else [str(issues)],
                str(fix_instruction or "").strip(),
            )

    return None, [], ""


def invoke_answer_chain(
    *,
    prompt: Any,
    llm: Any,
    str_parser: Any,
    context_text: str,
    query: str,
    history: Sequence[tuple[str, str]],
    directives: str = "",
) -> str:
    """Generate one candidate answer from the main prompt chain.

    Args:
        prompt: LangChain chat prompt template (expects ``context``, ``question``, ``chat_history``, ``directives``).
        llm: Chat model runnable.
        str_parser: Output parser.
        context_text: Retrieved evidence blocks for the ``Context`` slot.
        query: User question (possibly augmented with hidden system notes).
        history: Prior chat turns.
        directives: Optional extra system instructions (single-query Level-20 contract); empty for multi-query path.

    Returns:
        The model's answer string.

    Intent:
        Keeps retrieval-specific instructions out of the ``context`` blob so the hidden critic
        focuses on grounding against source excerpts, not formatting contracts.
    """
    chain = prompt | llm | str_parser
    answer = chain.invoke({
        "context": context_text,
        "question": query,
        "chat_history": history,
        "directives": directives or "",
    })
    return str(answer or "").strip()


def run_hidden_critic(
    *,
    llm: Any,
    query: str,
    context_text: str,
    candidate_answer: str,
) -> tuple[bool, str]:
    """Run a hidden critic pass that validates grounding against context.

    Returns:
        (`passed`, `notes`) where `passed=False` means one retry is required.
    """
    try:
        ok_pages, mismatch = _page_number_audit(candidate_answer, context_text)
        if not ok_pages:
            return False, mismatch
        critic_prompt = Constants.CRITIC_PROMPT_TEMPLATE.format(
            query=query,
            context_text=context_text,
            candidate_answer=candidate_answer,
        )
        raw = llm.invoke(critic_prompt)
        content = getattr(raw, "content", raw)
        txt = str(content or "").strip()

        verdict, issues, fix_instruction = _parse_critic_payload(txt)

        # Normalize verdict to a small, stable state machine: pass|fail|unknown.
        verdict_norm = (verdict or "").strip().lower()
        if verdict_norm not in {"pass", "fail"}:
            lowered = txt.lower()
            if '"verdict":"pass"' in lowered or "verdict: pass" in lowered:
                verdict_norm = "pass"
            elif '"verdict":"fail"' in lowered or "verdict: fail" in lowered:
                verdict_norm = "fail"
            elif "pass" in lowered and "fail" not in lowered:
                verdict_norm = "pass"
            elif "fail" in lowered and "pass" not in lowered:
                verdict_norm = "fail"
            else:
                verdict_norm = "unknown"

        issue_text = "; ".join(str(x) for x in issues if str(x).strip())[:800]
        notes = (fix_instruction or issue_text or "Potential grounding mismatch.").strip()

        if verdict_norm == "pass":
            return True, ""
        if verdict_norm == "fail":
            return False, notes

        # Fallback policy (v2.1.3 strict guardrail): malformed verdict defaults to REJECT.
        # This biases toward a single repair attempt instead of silently accepting an
        # unparseable critic response.
        logger.warning(
            "Hidden critic produced malformed payload; defaulting verdict=fail. "
            "raw_len=%d raw_snippet=%r",
            len(txt),
            _compact_raw_snippet(txt),
        )
        return False, "Critic verdict malformed; defaulting to reject."
    except Exception as exc:
        logger.exception(
            "Hidden critic crashed; defaulting verdict=fail (%s). raw_snippet=%r",
            exc,
            _compact_raw_snippet(str(locals().get("txt", ""))),
        )
        return False, "Critic crashed; defaulting to reject."


def generate_with_self_correction(
    *,
    prompt: Any,
    llm: Any,
    str_parser: Any,
    query: str,
    history: Sequence[tuple[str, str]],
    context_text: str,
    directives: str = "",
) -> tuple[str, bool]:
    """Generate answer, critique it, and optionally re-generate once."""
    candidate = invoke_answer_chain(
        prompt=prompt,
        llm=llm,
        str_parser=str_parser,
        context_text=context_text,
        query=query,
        history=history,
        directives=directives,
    )
    candidate = _sanitize_user_visible_answer(candidate)
    passed, critic_notes = run_hidden_critic(
        llm=llm,
        query=query,
        context_text=context_text,
        candidate_answer=candidate,
    )
    if passed:
        return candidate, False

    fix_query = (
        f"{query}\n\n"
        "Rewrite directive (do NOT mention this directive):\n"
        "- Output ONLY the final answer.\n"
        "- Do NOT mention correction, critique, hallucinations, rewriting, or internal reasoning.\n"
        "- Maintain the Lore Keeper Archivist voice: authoritative, concise, grounded.\n"
        "- Remove any uncertainty/hedging introduced only by the repair process.\n"
        "- Keep only statements grounded in the provided Context.\n"
        f"- Grounding issues to resolve: {critic_notes}\n\n"
        f"Draft answer to rewrite (do NOT mention it):\n{candidate}\n"
    )
    regenerated = invoke_answer_chain(
        prompt=prompt,
        llm=llm,
        str_parser=str_parser,
        context_text=context_text,
        query=fix_query,
        history=history,
        directives=directives,
    )
    regenerated = _sanitize_user_visible_answer(regenerated or "")
    return regenerated or candidate, True


def stream_answer_with_integrity_timing(
    *,
    retrieval_seconds: float,
    query: str,
    history: Sequence[tuple[str, str]],
    context_text: str,
    no_verified_context: bool,
    verified_source_citation: str,
    enforce_no_verified_sources_integrity: Any,
    prompt: Any,
    llm: Any,
    str_parser: Any,
    logger: Any,
    directives: str = "",
) -> Iterator[str]:
    """Yield a single answer string but record timing in logs (Streamlit-friendly iterator)."""

    def token_iterator() -> Iterator[str]:
        t_llm_start = time.perf_counter()
        token_count = 0
        if no_verified_context:
            safe = enforce_no_verified_sources_integrity("")
            token_count = 1 if safe else 0
            if safe:
                yield safe
        else:
            final_answer, _corrected = generate_with_self_correction(
                prompt=prompt,
                llm=llm,
                str_parser=str_parser,
                query=query,
                history=history,
                context_text=context_text,
                directives=directives,
            )
            if final_answer:
                final_answer = _strip_common_lore_disclaimer_when_context_present(final_answer)
                final_answer = _sanitize_user_visible_answer(final_answer)
                final_answer = _inject_verified_source_citation(
                    final_answer, citation=verified_source_citation
                )
                token_count = 1
                yield final_answer
        t_llm = time.perf_counter() - t_llm_start
        t_total = retrieval_seconds + t_llm
        msg = (
            f"⏱ [INFERENCE] retrieval={retrieval_seconds:.3f}s  "
            f"llm={t_llm:.3f}s  total={t_total:.3f}s  "
            f"chunks={token_count}  query={query[:60]!r}"
        )
        print(msg, flush=True)
        if logger:
            logger.info(msg)

    return token_iterator()


async def maybe_run_async(
    fn: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run `fn` in a thread when on an event loop; else run directly."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


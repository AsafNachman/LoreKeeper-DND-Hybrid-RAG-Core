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
) -> str:
    """Generate one candidate answer from the main prompt chain."""
    chain = prompt | llm | str_parser
    answer = chain.invoke({
        "context": context_text,
        "question": query,
        "chat_history": history,
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

        # Fallback policy: never hard-fault on malformed critic output.
        # Defaulting to PASS avoids a secondary corrective call chain when the critic
        # hallucinated its format.
        logger.warning(
            "Hidden critic produced malformed payload; defaulting verdict=pass. "
            "raw_len=%d raw_snippet=%r",
            len(txt),
            _compact_raw_snippet(txt),
        )
        return True, ""
    except Exception as exc:
        logger.exception(
            "Hidden critic crashed; defaulting verdict=pass (%s). raw_snippet=%r",
            exc,
            _compact_raw_snippet(str(locals().get("txt", ""))),
        )
        return True, ""


def generate_with_self_correction(
    *,
    prompt: Any,
    llm: Any,
    str_parser: Any,
    query: str,
    history: Sequence[tuple[str, str]],
    context_text: str,
) -> tuple[str, bool]:
    """Generate answer, critique it, and optionally re-generate once."""
    candidate = invoke_answer_chain(
        prompt=prompt,
        llm=llm,
        str_parser=str_parser,
        context_text=context_text,
        query=query,
        history=history,
    )
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
        "Self-correction directive:\n"
        "- Fix all unsupported claims.\n"
        "- Keep only statements grounded in the provided context.\n"
        "- Preserve useful structure but remove hallucinations.\n"
        f"- Critic notes: {critic_notes}\n\n"
        f"Previous candidate to fix:\n{candidate}\n"
    )
    regenerated = invoke_answer_chain(
        prompt=prompt,
        llm=llm,
        str_parser=str_parser,
        context_text=context_text,
        query=fix_query,
        history=history,
    )
    return regenerated or candidate, True


def stream_answer_with_integrity_timing(
    *,
    retrieval_seconds: float,
    query: str,
    history: Sequence[tuple[str, str]],
    context_text: str,
    no_verified_context: bool,
    enforce_no_verified_sources_integrity: Any,
    prompt: Any,
    llm: Any,
    str_parser: Any,
    logger: Any,
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
            )
            if final_answer:
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


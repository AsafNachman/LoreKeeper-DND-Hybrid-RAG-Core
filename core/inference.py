"""Inference machinery for Lore Keeper (self-correction, critic, and streaming).

This module isolates the "heavy machinery" of answer generation from the
`main.LoreKeeper` orchestrator wrapper.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator, Sequence
from typing import Any, Optional

from core.constants import Constants


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
    critic_prompt = Constants.CRITIC_PROMPT_TEMPLATE.format(
        query=query,
        context_text=context_text,
        candidate_answer=candidate_answer,
    )
    raw = llm.invoke(critic_prompt)
    content = getattr(raw, "content", raw)
    txt = str(content or "").strip()
    try:
        obj = json.loads(txt)
        verdict = str(obj.get("verdict", "")).strip().lower()
        issues = obj.get("issues", [])
        issue_text = "; ".join(str(x) for x in issues if str(x).strip())[:800]
        fix_instruction = str(obj.get("fix_instruction", "")).strip()
        notes = fix_instruction or issue_text or "Potential grounding mismatch."
        return verdict == "pass", notes
    except Exception:
        lowered = txt.lower()
        if '"verdict":"pass"' in lowered or "verdict: pass" in lowered:
            return True, ""
        return False, txt[:800] or "Potential grounding mismatch."


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


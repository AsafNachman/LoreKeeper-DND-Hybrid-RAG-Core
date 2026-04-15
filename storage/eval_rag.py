"""
Scientific RAG evaluation for Lore Keeper using RAGAS.

Computes Faithfulness (groundedness in retrieved contexts) and Answer Relevancy
(alignment of the answer with the question) for a fixed set of golden Q&A pairs.

Requires OPENAI_API_KEY (same as main.py). Chroma DB must already contain ingested PDFs.

How to read Faithfulness (common confusion):
- RAGAS checks whether **claims in the generated answer** are **entailed by the
  retrieved context strings** passed into the metric. It does **not** compare the
  answer to the `ground_truth` column (that column is only for human audit).
- Good PDFs in the UI only mean retrieval picked those books; the model can still
  add an unsupported sentence (e.g. a rule not literally supported by the chunks),
  which **lowers faithfulness** even when the reply "looks" fine.
- The metric and `gpt-4o-mini` judge are somewhat **stochastic**; use
  `--llm-temperature 0` (default) for stabler answers and more repeatable scores.

Usage:
    python eval_rag.py
    python eval_rag.py --db path/to/chroma_db
    python eval_rag.py --llm-temperature 0.2   # match production LoreKeeper if desired

Environment:
    LORE_KEEPER_LLM_MODE   # ``intelligence`` (default) or ``efficiency``; latter needs Ollama for answers.
docker-compose up --build"""

from __future__ import annotations

import argparse
import math
import os
import sys
import warnings
from typing import Any

warnings.filterwarnings(
    "ignore",
    message=".*evaluate\\(\\) is deprecated.*",
    category=DeprecationWarning,
)

from datasets import Dataset
from dotenv import load_dotenv

from main import LoreKeeper

# ---------------------------------------------------------------------------
# Golden set: replace questions / ground-truth with cases that match YOUR index.
# ground_truth is for human readers only; Faithfulness uses `contexts`, not this field.
# ---------------------------------------------------------------------------
GOLDEN_EVAL_SET: list[dict[str, str]] = [
    {
        "question": "What are the rules for flanking in combat?",
        "ground_truth": "Optional rule: flanking grants advantage when an ally is opposite the target; "
        "see DMG optional flanking rule (wording depends on edition/book).",
    },
    {
        "question": "How does the Counterspell spell work?",
        "ground_truth": "Reaction to interrupt a spell within range; ability check if spell level exceeds slot used.",
    },
    {
        "question": "What is the effect of the prone condition?",
        "ground_truth": "Creature is on the ground; melee attacks have advantage, ranged disadvantage; "
        "must spend movement to stand.",
    },
]


def _fmt_score(x: Any) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "nan"
    try:
        return f"{float(x):.4f}"
    except (TypeError, ValueError):
        return str(x)


def _print_summary_table(
    questions: list[str],
    ground_truths: list[str],
    scores_per_row: list[dict[str, Any]],
    metric_keys: list[str],
) -> None:
    """ASCII summary: one row per question with metric scores."""
    q_short = 48
    gt_short = 36
    headers = ["#", "faithfulness", "answer_relevancy", "question", "ground_truth (ref)"]
    colw = [3, 12, 16, q_short, gt_short]

    def clip(s: str, w: int) -> str:
        s = " ".join(s.split())
        return s if len(s) <= w else s[: w - 3] + "..."

    sep = (
        "+"
        + "+".join("-" * (w + 2) for w in colw)
        + "+"
    )
    head = (
        "| "
        + " | ".join(f"{h:<{colw[i]}}" for i, h in enumerate(headers))
        + " |"
    )
    print(sep)
    print(head)
    print(sep)
    for i, row_scores in enumerate(scores_per_row):
        fth = _fmt_score(row_scores.get(metric_keys[0]))
        arv = _fmt_score(row_scores.get(metric_keys[1]))
        line = (
            f"| {i + 1:<{colw[0]}} | {fth:>{colw[1]}} | {arv:>{colw[2]}} | "
            f"{clip(questions[i], q_short):<{q_short}} | "
            f"{clip(ground_truths[i], gt_short):<{gt_short}} |"
        )
        print(line)
    print(sep)


def _summarize_contexts_cell(val: Any) -> str:
    """Short label for console: full context blocks make pandas ``to_string`` unusably wide."""
    if isinstance(val, list):
        n = len(val)
        chars = sum(len(str(s)) for s in val)
        return f"<{n} block(s), {chars} chars>"
    s = str(val)
    return s if len(s) <= 80 else s[:77] + "..."


def _eval_llm_from_keeper(keeper: LoreKeeper, temperature: float):
    """RAGAS judge + answer scoring on OpenAI even when the keeper uses Ollama for chat."""
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(model="gpt-4o-mini", temperature=temperature, api_key=keeper.api_key)


def run_evaluation(db_path: str, *, llm_temperature: float = 0.0) -> None:
    load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set (check .env).")

    from ragas import evaluate
    from ragas.metrics import answer_relevancy, faithfulness

    # Default ``intelligence`` avoids requiring a local Ollama for eval; override with
    # LORE_KEEPER_LLM_MODE=efficiency when Ollama is running.
    _eval_mode = os.getenv("LORE_KEEPER_LLM_MODE", "intelligence").strip().lower()
    keeper = LoreKeeper(db_path=db_path, llm_mode=_eval_mode)
    keeper.llm = _eval_llm_from_keeper(keeper, llm_temperature)
    print(
        f"Eval LLM temperature={llm_temperature} (RAGAS faithfulness judges use this same model)."
    )

    questions: list[str] = []
    answers: list[str] = []
    contexts: list[list[str]] = []
    references: list[str] = []

    print("Running Lore Keeper pipeline on golden questions…")
    for row in GOLDEN_EVAL_SET:
        q = row["question"]
        gt = row.get("ground_truth", "")
        ans, _, ctx_blocks = keeper.ask_with_eval_contexts(q, [])
        questions.append(q)
        answers.append(ans)
        contexts.append(ctx_blocks if ctx_blocks else [""])
        references.append(gt)

    hf_ds = Dataset.from_dict({
        "question": questions,
        "answer": answers,
        "contexts": contexts,
        "ground_truth": references,
    })

    metrics = [faithfulness, answer_relevancy]
    print("Running RAGAS (LLM + embedding calls; may take a few minutes)…")
    result = evaluate(
        hf_ds,
        metrics=metrics,
        llm=keeper.llm,
        embeddings=keeper.embeddings,
        show_progress=True,
    )

    score_keys = [m.name for m in metrics]
    _print_summary_table(questions, references, result.scores, score_keys)

    print("\nAggregate (mean over rows, NaNs ignored):")
    for key in score_keys:
        col = [row.get(key) for row in result.scores]
        try:
            vals = [float(x) for x in col if x is not None and not (isinstance(x, float) and math.isnan(x))]
        except (TypeError, ValueError):
            vals = []
        mean_v = sum(vals) / len(vals) if vals else float("nan")
        print(f"  {key}: {mean_v:.4f}")

    # Optional: full merge table if pandas is installed
    try:
        import pandas as pd

        df = result.to_pandas()
        # RAGAS keeps full retrieved text in wide columns; printing them pads the layout
        # with megabytes of whitespace/newlines in the terminal.
        df_view = df.copy()
        for col in ("retrieved_contexts", "contexts"):
            if col in df_view.columns:
                df_view[col] = df_view[col].apply(_summarize_contexts_cell)
        print("\nFull evaluation frame (pandas, context columns summarized):")
        with pd.option_context(
            "display.max_columns", None,
            "display.width", 160,
            "display.max_colwidth", 64,
            "display.expand_frame_repr", False,
        ):
            frame = df_view.to_string(index=False)
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            print(frame)
        except UnicodeEncodeError:
            print(frame.encode(enc, errors="replace").decode(enc))
    except ImportError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="RAGAS evaluation for Lore Keeper")
    parser.add_argument(
        "--db",
        default="db",
        help="Chroma persist directory (default: db)",
    )
    parser.add_argument(
        "--llm-temperature",
        type=float,
        default=0.0,
        help="Temperature for answer generation and RAGAS metric LLM calls "
        "(default: 0 for reproducibility; LoreKeeper production default is 0.2).",
    )
    args = parser.parse_args()
    run_evaluation(args.db, llm_temperature=args.llm_temperature)


if __name__ == "__main__":
    main()

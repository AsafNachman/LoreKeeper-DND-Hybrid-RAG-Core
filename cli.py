"""CLI entrypoint for Lore Keeper interactive testing.

This keeps the production RAG engine (`main.py`) focused on orchestration and
imports, while the terminal loop lives in a separate module.
"""

from __future__ import annotations

import logging

from main import LoreKeeper

logger = logging.getLogger(__name__)


def main() -> None:
    """Interactive terminal loop for manual testing.

    Raises:
        None intentionally; fatal errors are logged with the standard `logging` module.
    """
    try:
        keeper = LoreKeeper(db_path="db", brain_id="dnd_core")
        chat_history = []

        print("\n" + "=" * 60)
        print("LORE KEEPER - INTERACTIVE CLI")
        print("=" * 60)
        print("Status: Online | Search: Hybrid Ensemble + Reranked | Memory: Active\n")

        while True:
            user_input = input("User: ").strip()
            if user_input.lower() in ["exit", "quit"]:
                break
            if user_input.lower() == "reset":
                chat_history = []
                print("--- Memory Reset ---")
                continue
            if not user_input:
                continue

            try:
                answer, sources = keeper.ask(user_input, chat_history)
            except Exception as query_exc:
                logger.error("CLI query failure: %s", query_exc)
                print("\nSystem Recovering: Engine issue detected. Please retry in a moment.")
                continue

            print(f"\nLore Keeper: {answer}")
            if sources:
                cite = [s["citation"] if isinstance(s, dict) else str(s) for s in sources]
                print(f"\n📚 Verified Sources: {', '.join(cite)}")

            chat_history.append(("human", user_input))
            chat_history.append(("assistant", answer))
            if len(chat_history) > 10:
                chat_history = chat_history[-10:]
            print("-" * 30)

    except Exception as e:
        logger.critical("Fatal CLI startup error: %s", e)


if __name__ == "__main__":
    main()


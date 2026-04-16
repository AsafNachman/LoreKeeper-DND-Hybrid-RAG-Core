# Changelog: Lore Keeper - AI Infrastructure

All notable changes to this project will be documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [2.4.3] - 2026-04-16

### UI/UX

- **Context expansion toggle**: Sidebar checkbox **Context Expansion (Page +/-)** (under Multi-query expansion) with tooltip; default **OFF**; persisted in ``ui_settings.json``.
- **Versioning**: Product chrome reads **Lore Keeper v2.4.3** from `VERSION`.

### RAG Engine

- **Conditional neighbor fetch**: ``LoreKeeper._expand_with_neighbor_pages`` runs only when ``context_expansion_enabled`` is True **and** top rerank ≥ ``0.95``; otherwise retrieval output is passed through unchanged (no extra Chroma reads).

## [2.4.2] - 2026-04-16

### RAG Engine

- **Neighbor page context**: After retrieval, when any chunk has rerank ≥ ``0.95``, same-source chunks for **page N−1** and **page N+1** (0-based index) are merged into the LLM context (deduped; labeled as neighboring pages). Retrieval scoring in ``retrieval.py`` is unchanged.
- **Single-query voice**: System directives no longer force rigid section headers; Archivist answers stay technically dense with mechanics woven into prose, **bold** numerics, and an optional closing **Note:**.

### UI/UX

- **Versioning**: Product chrome reads **Lore Keeper v2.4.2** from `VERSION` (``ui_settings.json`` + Brain Vault behavior unchanged from 2.4.1).

## [2.4.1] - 2026-04-16

### RAG Engine

- **Single-query context**: When multi-query expansion is OFF, merge keeps the top five reranked chunks through pruning, prioritizes **gold** sources (rerank ``> 0.80``) at the front of the prompt, and widens the merge cap appropriately—still **no** changes to retrieval/rerank scoring in ``retrieval.py``.
- **Level-20 generation**: Single-query path injects structured answer directives (Core Rule / Fine Print / Direct Archives blockquote) plus anti-summarization instructions via a dedicated ``{directives}`` prompt slot (evidence stays in ``Context`` for the critic).

### UI/UX

- **Settings hydration**: ``ui_settings.json`` is applied immediately after ``st.set_page_config`` and before other session defaults; boolean coercion avoids ``\"false\"`` strings re-enabling multi-query after refresh.
- **Versioning**: Product chrome reads **Lore Keeper v2.4.1** from `VERSION`.

## [2.4.0] - 2026-04-16

### RAG Engine

- **Answer verbosity**: Immutable rules require detailed, quote-grounded answers when any source shows Rerank **≥ 0.80**; merged LLM context widens the post-merge doc cap when the top rerank crosses that threshold (retrieval/rerank stages unchanged).

### UI/UX

- **Persistent settings**: ``storage/ui_settings.json`` hydrates ``llm_mode``, ``active_model``, ``brain_id``, and ``multi_query_enabled`` on session start (before sidebar widgets) and saves after the sidebar renders.
- **Brain Vault**: Restored **Brain Name** input and **Create Brain** inside the expander (previously unreachable after ``st.rerun()``).
- **Versioning**: Product chrome and page title use **LORE KEEPER v2.4.0** from `VERSION`.

## [2.3.10] - 2026-04-16

### RAG Engine

- **Single-query deep retrieval**: Query-Off path uses ``SINGLE_QUERY_DEEP_K = 50`` (else-branch ``k`` only), wide Chroma distance scan for ``1/(1+d)`` similarity, MMR-ordered vector arm, then BM25 + RRF + FlashRank.
- **Algorithmic priors**: Mechanical-intent queries apply filename-pattern boosts (Handbook/Core) and penalties (Monster/Bestiary/Campaign) on calibrated rerank scores; primary subject absence caps rerank at ``0.15`` (case-insensitive substring).
- **Similarity backfill**: Pooled chunks with missing dense distance get a conservative non-zero ``similarity_score`` from fused RRF mass so the evidence UI avoids spurious ``0.00``.

### UI/UX

- **Versioning**: Product chrome reads **Lore Keeper v2.3.10** from `VERSION`.

## [2.3.9] - 2026-04-16

### RAG Engine

- **Single-query (Query-Off) bias fix**: When only one query variant runs, optional ``single_query_mode`` rescales pooled ``rrf_score`` by an inferred subject term (×5 whole-word hit, ×0.1 miss) and down-ranks **Monster Manual (2024)** for player-rules wording unless the query clearly targets monsters/stat blocks.
- **Stricter FlashRank hints**: Single-query path prepends a ``[STRICT_RERANK]`` instruction so the cross-encoder favors passages that explicitly define the asked mechanics.
- **Forensics**: Single-query path prints ``[DEBUG 2.3.9] Top 3 Raw Hits…`` to the terminal for initial candidate visibility.

### UI/UX

- **Versioning**: Product chrome reads **Lore Keeper v2.3.9** from `VERSION`.

## [2.3.8] - 2026-04-16

### RAG Engine

- **Context–rerank sync**: Final LLM context is built from `_docs_ordered_for_llm` (rerank desc, then similarity); primary chunk is labeled explicitly so the model sees the best passage first.
- **Citation alignment**: Injected `From …` headers use `_citation_for_top_reranked_doc` (top rerank chunk), replacing excerpt-overlap heuristics that could prefer a weaker page.
- **Score propagation**: RRF pool metadata merges onto FlashRank wrapper outputs; `similarity_score` is rounded to two decimals with a rerank-derived fallback when Chroma distance is absent.
- **Perfect-match guidance**: Immutable rules plus an optional context prelude when top rerank ≥ 1.00 instruct the model to treat that chunk as authoritative.

### UI/UX

- **Versioning**: Product chrome reads **Lore Keeper v2.3.8** from `VERSION`.

## [2.3.7] - 2026-04-16

### RAG Engine

- **RRF retrieval**: Replaced weighted `EnsembleRetriever` fusion with Reciprocal Rank Fusion over dense ranks and BM25 ranks; literal ``Multiclassing`` repetition (>2) applies a +30% boost to fused mass so dense PHB rule sections surface ahead of passing mentions.
- **Identity scores**: Chroma distances are consistently mapped to ``similarity_score = 1/(1+distance)`` on pooled documents so the evidence UI no longer shows a blank ``0.00`` when vector scores were dropped.
- **Rerank calibration**: Initial pool depth is aligned to 30 candidates; FlashRank inputs use semantic passage tags (rules vs navigation); min–max gamma sharpening widens rerank score separation after the cross-encoder.

### UI/UX

- **Versioning**: Product chrome reads **Lore Keeper v2.3.7** from the `VERSION` file (sidebar header and page title).

## [2.3.6] - 2026-04-16

### RAG Engine

- **Toggle-safe scoring**: Multi-query ON/OFF now shares the same scoreful retrieval pipeline (Chroma distance→similarity, FlashRank rerank, and deterministic `(rerank, similarity)` sorting) to prevent score flatlines.
- **Prompt toggle state note**: Added a hidden system note carrying the multi-query toggle state to keep retrieval behavior explicit while preserving strict grounding.

### UI/UX

- **State synchronization**: Apply the multi-query toggle to the cached `LoreKeeper` instance at the start of query processing to avoid stale-session wiring.

## [2.3.5] - 2026-04-16

### UI/UX

- **Multi-query toggle**: Added a sidebar switch to enable/disable multi-query expansion without rebuilding the engine (runtime flag applied per session).

image.png## [2.3.4] - 2026-04-16

### RAG Engine

- **Knowledge trust bands**: Replaced strict rerank hard-refusal with a 3-band policy: hard refusal only when top rerank `< 0.05`, soft-warning band for `0.05..0.20`, and normal answering above `0.20`.
- **Prompt context reinforcement**: Added explicit closed-book prompt reinforcement to extract mechanics from provided official Handbook chunks (including pages `152-153`) when they contain user keywords.
- **Similarity robustness**: Strengthened Chroma distance-to-similarity mapping propagation for pooled multi-query candidates by adding source+page fallback matching.

### UI/UX

- **Soft warning rendering**: Added UI support for low-confidence soft-warning prefix handling so warning text is shown as `st.warning` while keeping the answer body clean.

## [2.3.3] - 2026-04-16

### RAG Engine

- **Flatline scoring fix**: Replaced relevance extraction with raw Chroma `similarity_search_with_score` distance capture and converted distances into normalized similarity scores (`0..1`, higher is better).
- **Reranker audit path**: Added direct FlashRank `Ranker.rerank(query, passages)` execution with raw output logging, flatline-score detection, and deterministic fallback behavior.
- **Stable ranking contract**: Enforced explicit final sorting by `rerank_score` with `similarity_score` tie-breakers before context assembly.

### UI/UX

- **Forensic score reliability**: Fixed score propagation/rendering so Verified Sources always display real `[Score | Rerank]` values sourced from document metadata.

## [2.3.2] - 2026-04-16

### RAG Engine

- **Rerank-order audit fix**: Final merged context chunks are now explicitly sorted by rerank score before prompt assembly, ensuring FlashRank order is preserved end-to-end.
- **Page 100 investigation hardening**: Kept raw `Page 100` chunk dumps and added global-noise suppression for chunks that repeatedly appear across query types.

### UI/UX

- **Verified Sources forensic view fix**: Fixed citation-page regex parsing so score metadata survives grouping, sorted source cards by rerank/similarity, and rendered scores after page labels as `[Score: 0.xx | Rerank: 0.xx]`.

## [2.3.1] - 2026-04-16

### RAG Engine

- **Forensic scores + hard refusal**: Surface Chroma similarity + FlashRank rerank scores per source and hard-refuse when top rerank `< 0.25` to avoid answering on low-relevance context.
- **Page 100 bias logging**: Temporarily dump raw retrieved chunk text for viewer `Page 100` to logs for diagnosing hidden noise inflating retrieval.

### UI/UX

- **Verified Sources score display**: Show `[Score: 0.xx | Rerank: 0.xx]` alongside each citation (while keeping consecutive-page grouping).

## [2.3.0] - 2026-04-16

### RAG Engine

- **MMR + multi-query anti index-trap**: Switched dense Chroma retrieval to `search_type="mmr"` (20-candidate pool, `lambda_mult=0.5`) and added LLM-driven 3-variant query expansion with pooled reranking.
- **Index killer + definition bias**: Added an `is_index_chunk` down-weight multiplier and a definition-like boost so explain/how-to queries prefer explanatory rule text over index/reference lists.

### UI/UX

- **Verified Sources grouping**: Consecutive pages from the same PDF now collapse into a single `Pages X–Y` citation to reduce evidence-panel clutter.

## [2.2.5] - 2026-04-16

### RAG Engine

- **Strict context grounding**: Upgraded the hidden critic to enforce quote-check verification and added a deterministic page-number audit (invented page references trigger `DATA_MISMATCH` failures).
- **Retrieval confidence gate**: Weak/short retrieved context now forces a “details missing from my current scrolls” response instead of filling gaps.
- **Closed-book prompt lockdown**: System prompt now forbids using internal training data to explain mechanics; answers must be limited to retrieved context.

### UI/UX

- **Version label**: UI header now displays `Lore Keeper v2.2.5`.

## [2.2.4] - 2026-04-16

### RAG Engine

- **No-meta output sanitization**: Prevent hidden critic/repair loop narration from leaking into the final answer by tightening the repair prompt and stripping common meta-talk phrases deterministically.
- **Attribution cleanup**: Make in-text source attribution authoritative and overwrite any mismatched leading “According to …” lines so the selected verified citation always wins.

## [2.2.3] - 2026-04-16

### RAG Engine

- **Lore Intent Check**: Added a micro yes/no intent check to override rare domain-classifier false negatives so valid D&D queries can answer from general archives when no scrolls are retrieved.

### UI/UX

- **Refresh-safe chat history**: Fixed session boot logic so `chat_history.json` is actually loaded on new browser sessions (refresh no longer wipes the conversation view).

## [2.2.2] - 2026-04-16

### RAG Engine

- **In-text source attribution**: Inject the most relevant verified source citation into the assistant answer body (repairs the “context from ,” blank and avoids always choosing the first source card).

## [2.2.1] - 2026-04-16

### RAG Engine

- **Common-lore disclaimer correctness**: Prevent the “I couldn't find the exact scroll…” prefix from appearing when verified source chunks exist (prompt rule tightened + runtime stripping in both normal and streaming paths).

## [2.2.0] - 2026-04-16

### UI/UX

- **Bottom-aligned suggestions row**: Moved the 3 suggestion “bubbles” to the bottom of the page, directly above the chat input, for consistent access during conversations.
- **Inline refresh placement**: Placed the refresh button on the far right of the same suggestion row to keep the layout compact and predictable.

## [2.1.9] - 2026-04-16

### UI/UX

- **Sticky suggestions**: Suggestion “bubbles” now remain visible throughout the session (not just empty chat) and always show 3 prompts at a time.
- **Refresh suggestions**: Added a refresh button that randomly selects 3 prompts from a 10-question pool (3 existing + 7 new).
- **Mid-gate cleanup**: The Send/Cancel draft bar no longer renders while an assistant response is pending, so it disappears immediately after Send.

## [2.1.8] - 2026-04-16

### UI/UX

- **History/response containers**: Split chat rendering into `history_container` + `response_container` so history always hydrates first and live “Retrieving…” output always renders at the bottom.
- **Sidebar flicker reduction**: Moved custom CSS injection to the top of the script (immediately after `st.set_page_config`) and eliminated late-run CSS application to stabilize sidebar hydration.
- **State hydration guard**: Initialized required `st.session_state` keys early (messages + pending queues) to prevent rerun-order glitches.

## [2.1.7] - 2026-04-16

### UI/UX

- **Chat synchronization fix**: Made `st.session_state.messages` the single source of truth; user submissions now enqueue a pending assistant run and rerender, preventing duplicate history rendering and out-of-order spinners.
- **Spinner placement**: Assistant “Retrieving…”/thinking indicators now render only after the full chat history pass, ensuring they always appear as the last element.
- **Clear conversation hard reset**: Clearing chat now also wipes pending queued queries so stale state can’t leak into the next rerun.

## [2.1.6] - 2026-04-16

### RAG Engine

- **Threshold recalibration**: Lowered the retrieval similarity rejection floor from `0.45` → `0.32` to reduce false negatives on short/proper-noun queries (e.g., magic item names).
- **Hybrid proper-noun boost**: Defaulted hybrid weights to a neutral blend and dynamically boosts BM25 to `0.7` (Vector `0.3`) when queries contain capitalized proper nouns.
- **Soft reject for missing scrolls**: Out-of-domain queries remain hard-rejected, but in-domain queries with no retrieved sources now produce a “common lore” answer with an explicit disclaimer prefix.

## [2.1.5] - 2026-04-16

### UI/UX

- **Sidebar CSS sanitization**: Removed overly-broad font overrides that broke Streamlit icon fonts and widget sizing; added targeted spacing rules to prevent overlap in expanders and buttons.
- **Status header stabilization**: Rebuilt sidebar metadata header as a compact vertical list (no wrapping) to keep alignment stable across widths.
- **Layout guardrails**: Set a wider sidebar baseline and restored Material icon rendering so expander arrows and icons don’t degrade into raw text labels.

## [2.1.4] - 2026-04-16

### UI/UX

- **Sidebar typography refresh**: Increased sidebar font size and switched to a modern sans-serif stack (Inter/Segoe UI) with improved line spacing for legibility.
- **Status density, no badges**: Replaced pill/badge status bubbles with a minimalist text status block (Status/Version/Engine/GPU/Brain/Filter/Mode) to reduce visual noise.
- **Collapsed control panels**: Moved Brain Vault and Model controls into collapsed expanders; relocated **Clear Conversation** into **Danger Zone** above **Destroy Brain** for safer, cleaner operations.

## [2.1.3] - 2026-04-16

### RAG Engine

- **Strict Domain Guardrail**: Added an in-domain router + hardened Archivist system prompt to refuse real-world advice/general chat and enforce “The archives are silent on this matter” when context does not support an answer.
- **Retrieval threshold short-circuit**: If top Chroma similarity falls below `0.45`, the pipeline bypasses the LLM and returns a predefined out-of-lore response, logging `GuardrailTriggered=True`.
- **Verdict defensive default**: Hidden-critic verdict parsing now defaults to a reject/fail path on malformed payloads instead of silently passing, ensuring one bounded repair attempt rather than unchecked acceptance.

## [2.1.2] - 2026-04-16

### RAG Engine

- **Critic verdict hardening**: Hidden-critic parsing now tolerates markdown fences and Python-dict reprs and defaults to `pass` when `verdict` is missing/malformed, preventing hard faults during the self-correction loop.
- **Critique observability**: Added warning/exception logs capturing compact raw critic-response snippets (prefix+suffix) on parse failures so verdict extraction breakages are diagnosable.

## [2.1.1] - 2026-04-15

### GPU / Runtime

- **Non-blocking GPU prewarm**: Moved Ollama VRAM prewarm into a dedicated background thread per session so engine warmup completion is never delayed by GPU activation. GPU progress is tracked via `st.session_state.gpu_status` (`warming` → `active`), and the thread triggers a rerun once `/api/ps` confirms `size_vram > 0` so the sidebar flips to 🟢 automatically.

## [2.1.0] - 2026-04-15

### GPU / Runtime

- **GPU preload reliability**: Fixed Ollama VRAM prewarm so GPU activation resolves automatically without needing a failed run + manual **Restart Engine**. Root cause was an invalid `/api/generate` payload (missing `prompt`) that could fail silently; warmup now sends a valid request and retries with backoff until `/api/ps` confirms `size_vram > 0` (or times out).

## [2.0.9] - 2026-04-15

### UI/UX

- **GPU Badge Auto-Update**: Reset the one-shot activation rerun latch and added a short post-engine-ready polling window so GPU status flips to 🟢 without requiring a manual interaction (e.g., “Restart Engine”).
- **Mid-Gate Draft UX**: Sending a blob draft now queues the query via `st.session_state.pending_query` and reruns immediately so the Send/Cancel bar disappears after submit; clicking another blob while the mid-gate is open replaces the draft text in-place.

## [2.0.8] - 2026-04-15

### UI/UX

- **Suggestion Blobs + Mid-Gate**: Restored large suggestion “blob” buttons; clicking now fills a draft bar with **Send**/**Cancel** instead of instantly submitting, preserving editability and preventing accidental queries.

## [2.0.7] - 2026-04-15

### UI/UX

- **Sidebar GPU Badge**: Moved GPU/engine badge computation behind a `st.session_state`-tracked state machine that triggers a one-time `st.rerun()` when GPU status flips to 🟢 Active, eliminating “stale until interaction” latency.
- **Empty-State Layout**: Reduced welcome hero vertical padding so quick suggestion pills visually anchor directly above the chat input instead of floating mid-screen.

## [2.0.6] - 2026-04-15

### Modularization & Cleanup

- **Root Cleanup**: Moved operational modules to `services/`, runtime JSON artifacts to `storage/`, docs to `docs/`, and the entrypoint script to `scripts/`, updating imports and paths accordingly.
- **Thin `main.py`**: Collapsed `main.py` into a <500-line re-export orchestrator; `LoreKeeper` now lives in `core.lorekeeper` while `app.py` continues to import from `main.py`.
- **Query Cleaning**: Added `core.utils.clean_query()` (dictionary + fuzzy matching for common D&D terms) and integrated it into `LoreKeeper.ask` before retrieval/inference.

## [2.0.5] - 2026-04-15

### RAG Engine

- **Inference Modularization**: Introduced `core/inference.py` and moved critic + self-correction + streaming iterator machinery out of `LoreKeeper` implementation code.
- **Retrieval Modularization**: Moved async-final retrieval overlap (`_afinal_docs_for_query`) and page-window retrieval (`_retrieve_by_page_window`) into `core/retrieval.py` as standalone functions.

### D&D Logic

- **Condition Expansion**: Extracted condition literal-hit fetching and windowed chunk expansion into `core/dnd_logic.py`, keeping D&D heuristics out of the orchestration layer.
- **Thin Delegation**: Refactored `LoreKeeper` methods to delegate to `core.*` functions (no duplicated logic), preserving behavior while reducing `main.py` responsibilities.

## [2.0.4] - 2026-04-15

### Modular Core Architecture

- **Core Package**: Introduced `core/` as a first-class package and extracted constants, utilities, D&D heuristics, and retrieval plumbing into dedicated modules.
- **Orchestrator `main.py`**: Refactored `main.py` into an orchestration layer that keeps `LoreKeeper` as the primary entry point while importing domain/retrieval helpers from `core.*`.
- **CLI Extraction**: Moved the interactive terminal loop into `cli.py` to decouple production engine code from local test tooling.

## [2.0.3] - 2026-04-15

### UI/UX

- **Suggestion Pills Placement**: Rendered quick suggestions in a grouped container immediately above `st.chat_input`, ensuring they “float” in the intended location and don’t drift into the mid-page layout.
- **Pill Styling & Click Flow**: Switched to compact labels (with full text on hover) to prevent blocky tiles; clicks enqueue `st.session_state.pending_query` and trigger an immediate `st.rerun()` through the standard query pipeline.

### Refactoring

- **RAG Prompt Constants**: Consolidated large static prompt strings into `main.Constants`, simplifying `BaseSystemPrompt`/critic construction while preserving the General Knowledge disclaimer contract.
- **Dead Code Prune**: Removed unused legacy page-window artifacts (`_PAGE_RETRIEVAL_WINDOW`, `_format_page_span_label`) after a usage audit.

## [2.0.2] - 2026-04-15

### UI/UX

- **Floating Quick Suggestions**: Replaced draft-style quick suggestion flow with `render_suggestion_pills()` so compact, secondary pills render in a horizontal row directly above `st.chat_input`.
- **Visibility Contract**: Suggestion pills are now gated to empty sessions only (`len(messages) == 0`), keeping the active conversation area focused once chat history exists.
- **Unified Interaction Path**: Pill clicks now enqueue `pending_query` and rerun through the standard query pipeline, preserving the same retrieval spinner and thinking/integrity animation used for manually typed prompts.

### Refactoring

- **Ingestion Module Rename**: Promoted `ingest.py` as the canonical ingestion module name (`g` instead of `j`) and updated code references/documentation to align with production naming.

## [2.0.1] - 2026-04-15

### Refactoring

- **Naming & API Surface**: Standardized ingestion naming by introducing canonical `ingest.py`, migrating `app.py` imports to that module, and adding action-oriented ingestion methods (`ingest_pdf_to_vector_store_async`, `ingest_pdf_to_vector_store`) with compatibility aliases.
- **Retriever / Inference Organization**: Clarified `main.LoreKeeper` structure with explicit Retriever vs Inference sections, renamed sync inference internals to `_run_sync_inference`, and improved maintainability without behavior regressions.
- **Cleanup Candidates**: Annotated legacy/duplicate logic with targeted review markers (`# TODO: Potential Delete - Verify no dependencies` and `# TODO: Candidate for Merging ...`) for safe post-refactor pruning.
- **Documentation & Governance**: Updated production-facing comments/docstrings around ingestion naming and recorded the release at `2.0.1` with aligned learning-debt tracking.

## [2.0.0] - 2026-04-14

### Changed

- **Agentic Self-Correction**: Introduced a two-step generation chain in `main.LoreKeeper` (candidate answer → hidden critic review → one deterministic repair attempt when grounding issues are detected) to reduce hallucinated mechanics/numeric claims.
- **Integrity Verification UX**: Added a Streamlit integrity status indicator (`🔍 Verifying answer integrity...`) during answer validation and explicit General Knowledge Mode cue when retrieval returns no verified chunks.
- **Insights Export**: Added session-level **Export Research (.md)** in the sidebar, generating a structured report with findings, query/answer summaries, and bibliography from verified source citations.
- **Production Branding & Reset Safety**: Updated visible product header to `LORE KEEPER v2.0 - Production Ready`, removed remaining UI readiness debug print, and hardened model-tier switching to force a clean cache/warmup reset.
- **Performance Audit Docs**: Added operator-facing `README.md` guidance for optional `autoMemoryReclaim` (`LORE_KEEPER_AUTO_MEMORY_RECLAIM=1`) on high-RAM hosts, including expected behavior during model transitions.

## [1.9.10] - 2026-04-14

### Changed

- **Runtime Singleton**: Added a strict process-wide `@st.cache_resource` runtime bundle in `app.py` that preloads heavy stacks (`langchain`, `chromadb`, `torch`) once, then injects constructors into `main.py` so engine boot avoids repeated import tax.
- **Ollama Client Lifecycle**: Switched warmup/GPU probes to a shared singleton Ollama client and added model-engine instance caching in `main.get_llm_engine`, ensuring local/chat clients are reused across sessions instead of reconstructed per rerun.
- **Background Heartbeat**: Implemented a heartbeat service that tracks warmup readiness per `(db, brain, mode)` and gates boot/status UI; already-warm engines now skip startup splash and enter ready-state immediately.
- **Resilience & Brain Metadata**: Hardened query-loop failures with a user-facing "System Recovering" message + automatic rewarm scheduling, and introduced persisted brain metadata (`creation_date`, `size_bytes`, `last_used`) via `db/brain_metadata.json` for v2.0.0 governance prep.

## [1.9.11] - 2026-04-14

### Changed

- **Error Boundaries & Recovery UX**: Centralized failure handling in `app.py` via `render_error_state(error_type, details)` with a consistent "System Recovery" panel and a one-click **Restart Engine** action.
- **LLM Pre-flight Sentinel**: Added per-query and per-ingest connectivity checks for Ollama/OpenAI; when local inference is overloaded/unreachable, the UI warns and avoids hard crashes.
- **Empty-Context Safety**: Hardened `main.LoreKeeper` query paths to **skip LLM calls entirely** when retrieval returns 0 chunks, immediately returning the General Knowledge contract response.
- **PDF Validation**: Added scanned/image-PDF detection in `injest.py`; zero-text PDFs are skipped with a clear OCR-required warning surfaced in the UI.
- **Black Box Logging**: Introduced `error_log.json` rolling log storing the last 5 failures for v2.0.0 operational diagnostics.

## [1.9.9] - 2026-04-14

### Changed

- **Sidebar Progressive Disclosure**: Wrapped brain destruction controls inside a dedicated `☢️ Danger Zone` expander and moved ingestion controls into a `📤 Upload Knowledge` popover to reduce baseline sidebar clutter.
- **Status Display Simplification**: Replaced large status cards with compact inline badge-style indicators for engine, version, retriever stack, and GPU state.
- **Chat Suggestion UX**: Moved quick-suggestion buttons from hero-center placement to compact horizontal pills directly above chat input, visible only when chat history is empty.
- **Model Section Hierarchy**: Kept `Active LLM` as the primary efficiency sub-control, reduced technical helper text prominence, and retained a concise premium provider label.

## [1.9.8] - 2026-04-14

### Changed

- **Hybrid Retrieval Calibration**: Rebalanced EnsembleRetriever fusion toward keyword precision (`BM25=0.7`, `Vector=0.3`) and raised base retrieval `k` to `15` in both tiers to improve exact-term recall for mechanics like "Lay on Hands".
- **Prompt Enforcement Update**: Added a hard rule instructing the model to widen scope or explicitly state missing specifics when exact query keywords appear in retrieved context but do not contain a definitive answer.
- **Citation Display Cleanup**: Removed inline `[Source: ...]` tags from the assistant message body in `app.py`; citations are now displayed only inside the **View Verified Sources** expander.
- **Hybrid "Why" Diagnostics**: Added top-5 retrieval debugging output showing BM25 proxy score vs vector proxy score (and weighted hybrid proxy) to explain which retriever dominates each candidate.

## [1.9.7] - 2026-04-14

### Changed

- **Universal Persona Convergence**: Replaced split efficiency/premium prompt personas with a single Lore Keeper / Dungeon Master core persona, then added mode-tuning overlays (efficiency: direct bullet-heavy structure; premium: narrative and common-ruling expansion).
- **Source-Use Integrity Fix**: Strengthened immutable prompt constraints so when source documents exist the model must use them and should not default to "Information not found" when relevant context is present.
- **No-Source Warning Contract**: Refined fallback enforcement so the `⚠️ NOTE: No verified sources found...` path is triggered only when retrieved source documents are physically empty.
- **Model UI Cleanup**: Finalized compact model section by keeping tier radio + conditional sub-controls and using a concise premium label (`Provider: GPT-5.4`).

## [1.9.6] - 2026-04-14

### Changed

- **Sidebar Model UX Consolidation**: Removed the separate "Model Settings" block and integrated local model selection directly under the existing "Model" tier control for a more compact hierarchy.
- **Conditional Model Controls**: `Auto Efficiency` now exposes an inline Ollama model selector, while `Premium Intelligence` shows a compact provider indicator for `gpt-5.4`.
- **State Coordination Hardening**: Tier/model switch paths now share cache invalidation + warmup refresh behavior so engine initialization remains deterministic when toggling between local and premium modes.

## [1.9.5] - 2026-04-14

### Changed

- **Model Settings UI**: Added sidebar "Model Settings" with an `Active LLM` selector (`llama3:8b-instruct-q4_K_M`, `llama3:latest`) that updates session model state, invalidates engine caches, and re-warms on change.
- **Anti-Fake Prompt Rules**: Strengthened immutable prompt constraints for source integrity: explicit empty-context behavior now mandates `⚠️ NOTE: No verified sources found. Using general knowledge.` and forbids `[Source: ...]` tags; context-present mode keeps mandatory citation discipline with relevance checks.
- **Generic Empty-Context Routing**: Removed hard-coded brain-name behavior and now rely purely on retrieved context/source presence to trigger no-source fallback handling, keeping brain logic name-agnostic.
- **Citation Hallucination Diagnostics**: Added response verification in `app.py` that compares model-emitted `[Source: ...]` tags against actual retrieved source filenames, raising UI warnings and terminal logs when mismatches are detected.

## [1.9.4] - 2026-04-14

### Changed

- **Prompt Integrity Guardrails**: Updated immutable prompt rules with a pre-response audit requirement, explicit general-knowledge disclaimer trigger, refined "Information not found" condition (only when no relevant context answer exists), and a relevance check that blocks citing unrelated source blocks as evidence.
- **Source Transparency UX**: Added assistant-warning rendering in `app.py`; when a response starts with the required `⚠️ NOTE` fallback disclaimer, the app now surfaces it as `st.warning` above the answer body.
- **Retrieval Diagnostics**: Added terminal retrieval debug logs in `main.py` that print source, page, and the first 100 characters of each retrieved chunk to help identify noisy or irrelevant retrieval.

## [1.9.3] - 2026-04-14

### Changed

- **Prompt Architecture**: Added `BaseSystemPrompt` in `main.py` to centralize immutable safety rules (strict grounding, citation format, and anti-hallucination constraints) and remove duplicated prompt logic from `LoreKeeper.__init__`.
- **Persona Toggles**: Implemented mode-specific overlays: **Efficiency** now appends the compact "Exhaustive Extractor" instructions, while **Intelligence** appends the "Dungeon Master Archivist" instructions including whale-oil handling and non-fantasy guardrails.
- **Token Optimization**: Consolidated overlapping rules into a shared base prompt and removed redundant wording/fluff, reducing prompt verbosity while preserving retrieval-grounding mechanics and response behavior.

## [1.9.2] - 2026-04-14

### Changed

- **Sidebar Layout**: Removed one redundant divider in the Brain Vault region to eliminate the visual double-line gap and tighten sidebar structure.
- **Brain Deletion UX**: Replaced checkbox-based destructive confirmation with a modal dialog flow (matching clear-history behavior), including explicit Yes/Cancel actions before any delete operation.
- **Safety Guardrails**: Improved the single-brain protection message to clearly enforce the invariant that at least one default brain must remain in the vault.

## [1.9.1] - 2026-04-14

### Changed

- **Entrypoint Warmup**: Updated `docker-entrypoint.sh` to issue a local prewarm request after Streamlit health is up, forcing early app execution so engine initialization starts before the first external user visit.
- **Process-Level Preload**: Added one-shot process prewarm orchestration in `app.py` that starts multithreaded engine warmup immediately at module load and records phase progress in shared cross-rerun state.
- **Boot UX / Flicker Guard**: Added an explicit "System Booting" splash gate using `st.empty()` and phase-based readiness; the UI defers full render until warmup reaches at least 10% to avoid early-frame layout flicker.
- **CPU Runtime Optimization**: Updated `Dockerfile` to install CPU-optimized `torch` wheels from the PyTorch CPU index and refreshed `numpy`, plus runtime OpenBLAS/GOMP libraries to speed non-GPU compute paths.

## [1.9.0] - 2026-04-14

### Changed

- **Multi-Tenant RAG Core**: Refactored `main.LoreKeeper` to accept `brain_id` and resolve each archive to an isolated Chroma path under `db/<brain_id>/`, preventing cross-brain retrieval leakage between unrelated corpora.
- **Brain Vault UI**: Added sidebar brain management in `app.py` with brain selection, creation, and destructive deletion controls (confirmation-gated), plus cache/warmup invalidation so brain switches rehydrate the correct engine immediately.
- **Engine State Sync**: Warmup/cache keys now include `(db_path, brain_id, llm_mode)`, and status rendering reports active brain context (`✅ Online | Brain: ...`) while preserving edition filter visibility.
- **Per-Brain Ingestion Routing**: Upload processing now stages PDFs under `data/<brain_id>/` and writes embeddings into `db/<brain_id>/`, keeping ingest and query namespaces aligned end-to-end.

## [1.8.1] - 2026-04-14

### Changed

- **Ingestion Metadata**: Added filename-based edition tagging during chunk creation in `injest.py`; PDFs containing `2024` or `Newest` now store `metadata["edition"] = "2024"`, while all others default to `2014`, enabling deterministic post-ingest filtering.
- **RAG Retrieval Core**: Extended `LoreKeeper` query paths with an optional edition filter and wired Chroma metadata constraints through vector retrieval, page-window fetches, and condition literal lookups so filtered and unfiltered retrieval behavior stays consistent.
- **Streamlit UI**: Added a sidebar `Ruleset Edition` selector (`All`/`2014`/`2024`), passed the selected value into streaming queries, and surfaced active filter state in the system-ready status caption for visibility during chat.
- **Data Operations UX**: Added a re-ingest warning when new PDFs are queued, clarifying that legacy chunks indexed before v1.8.1 may need re-ingestion to receive edition metadata.

## [1.8.0] - 2026-04-13

### Changed

- **App UI Bootstrap**: Moved `st.set_page_config` and initial sidebar shell placeholders to the top-level startup path so Streamlit paints structure immediately before engine initialization work begins.
- **RAG Engine Loading**: Replaced `get_lore_keeper` with cached `get_engine()` and kept `main.LoreKeeper` import inside the factory, ensuring heavy engine dependencies are loaded lazily on first build only.
- **Warmup UX**: Kept asynchronous warmup non-blocking while showing a live status placeholder and a disabled chat input state during engine construction, then unlocking input once the cache build finishes.
- **Docker Runtime I/O**: Narrowed `lore-keeper` bind mounts from `.:/app` to file-scoped module mounts, which prevents host `venv/` traversal in WSL2 runtime sync.

## [1.7.10] - 2026-04-13

### Fixed

- **Docker**: Restored persistent RAG sourcing by switching `lore-keeper` mounts from isolated named volumes (`/app/db`, `/app/data`) back to host bind mounts (`./db`, `./data`), so existing indexed vectors and PDFs are visible inside the app container after restarts.
- **RAG Engine**: Re-synced the running container’s `/app/db` and `/app/data` from host storage, recovering `6089` indexed chunks and re-enabling verified-source retrieval immediately without requiring re-ingestion.

## [1.7.9] - 2026-04-13

### Fixed

- **Chat UI**: Verified Sources now render for every assistant turn, including an explicit empty-state message when retrieval returns zero chunks, so the attribution section is consistently visible instead of silently disappearing.
- **GPU Acceleration**: Added a middle probe state (`🟡 Searching…`) between active and inactive, shown while the engine is still warming or during the first GPU probe window before declaring `Not detected`.

## [1.7.8] - 2026-04-13

### Fixed

- **RAG Engine**: Restored page-level verified-source citations by changing source aggregation from merged page spans per PDF back to stable per-page rows (`source + page`), so users can see explicit page-backed evidence under each answer again.
- **Chat UI**: Fixed quick-hint submit flow by introducing a queued `pending_query` handoff and a rerun on `Send`, which clears the draft form immediately and processes the question as a normal chat submission (preventing duplicate `Send/Cancel` state).

## [1.7.7] - 2026-04-13

### Fixed

- **Sourcing broken in Efficiency Mode:** The Exhaustive Rules Archivist prompt included a `"Cite everything"` directive instructing the LLM to embed `[Source: filename | Page N]` tags inline in its streamed response text. Because the RAG pipeline already extracts source metadata from retrieved chunks *before* the LLM runs and renders them as collapsible verified-source cards, these inline tags were redundant and leaked raw bracket text into the answer body. Removed the directive; source attribution is now handled exclusively by `_render_verified_sources_expander`.
- **GPU card slow to turn green:** Reduced the GPU re-probe TTL from 30 s to 10 s. The card now updates within 10 s of the warmup pre-load completing rather than waiting up to 30 s for the TTL window to expire.

## [1.7.6] - 2026-04-13

### Fixed

- **GPU card "Not detected" at startup (root cause):** Ollama uses *lazy loading* — a model is only placed into VRAM when the first query arrives. Our `/api/ps` probe fired at startup (before any query), found an empty model list, and permanently reported "Not detected" until a query was sent. Fixed by adding a **model pre-load step** at the end of the warmup background thread: after `get_lore_keeper()` completes, the thread issues `POST /api/generate {"model": …, "keep_alive": -1}` to Ollama. This loads the model into VRAM with no token generation; by the time the user sees the chat interface, the GPU card re-probes (TTL reset to 0) and correctly shows 🟢 Active.
- **Docker — OLLAMA_KEEP_ALIVE:** Added `OLLAMA_KEEP_ALIVE: "-1"` to the `ollama` service environment so models stay resident in VRAM indefinitely instead of unloading after 5 minutes of idle. Prevents the GPU card from reverting to 🔴 after a quiet period.

## [1.7.5] - 2026-04-13

### Fixed (Critical)

- **Root cause of persistent "🔄 Loading…" (never flips to ✅ Ready):** Streamlit re-executes `app.py`'s body on every rerun, which re-assigns all module-level variables (`_warmup_events = {}`, `_engine_built = {}`, etc.) to brand-new empty objects. Background threads from a previous rerun wrote to the old objects, which the current rerun's `_engine_is_ready()` never saw. Fixed by moving all coordination state into a single `_shared_state()` function decorated with `@st.cache_resource`, which returns the **same dict instance** on every rerun. `_engine_is_ready` now checks `_shared_state()["built"]` — a persistent set — and the status card reliably flips from 🔄 Loading → ✅ Online after the first build.
- **Root cause of permanent "🔴 Not detected" GPU card:** The old `@st.cache_resource` on `_check_gpu` ran once at process start, before Ollama had loaded any model. The False result was cached for the entire process lifetime. Replaced with a 30-second TTL re-check via `_shared_state()["gpu_ts"]`: after the Ollama container loads the model onto GPU (`size_vram > 0` in `/api/ps`), the card updates to 🟢 Active within 30 s automatically.

## [1.7.4] - 2026-04-13

### Fixed

- **Engine status race condition (critical):** `_engine_is_ready` now checks a new `_engine_built` process-level dict that is set **inside `get_lore_keeper`** before the `@st.cache_resource` internal lock is released. Previously, `_engine_is_ready` relied solely on `event.is_set()`, which the background thread calls in its `finally` block *after* releasing the cache lock — leaving a window where the UI thread could unblock, run the full query (1–2 min), reach the auto-refresh section, and still see `event.is_set() = False`, keeping the sidebar permanently on "🔄 Loading…". Belt-and-suspenders: the chat handler now also calls `event.set()` explicitly after `resolve_lore_keeper()` returns. `_reset_warmup` clears `_engine_built` so the transition fires correctly after re-ingestion.
- **GPU detection (Windows + Ollama ground-truth):** `_check_gpu()` now tries three probes in order: ① `nvidia-smi` with Windows-specific fallback paths (`C:\Windows\System32\nvidia-smi.exe`, NVSMI directory) for cases where the executable is not on the venv PATH; ② Ollama `/api/ps` — if `size_vram > 0`, at least one model layer is on GPU right now (the most accurate signal that GPU inference is actually happening); ③ `nvidia-container-cli info` for Container Toolkit presence in Docker.

## [1.7.3] - 2026-04-13

### Changed

- **Docker — GPU fix:** Corrected invalid `count: all/2` value (YAML parse error) in `docker-compose.yml` → `count: all`; the `ollama` service now unconditionally requests all available NVIDIA GPUs.
- **GPU detection (multi-probe):** `_check_gpu()` in `app.py` now runs two sequential probes: ① `nvidia-smi --query-gpu=name` (device name from driver); ② `nvidia-container-cli info` (NVIDIA Container Toolkit presence even when no device is mapped). Result labels: `"NVIDIA …"`, `"Toolkit present (no device mapped)"`, or `"Not detected"`.
- **Auto-refresh (state-transition):** Replaced the unconditional `time.sleep(1.5) → st.rerun()` polling loop with a **session-state transition tracker**. A `_engine_shown_ready:{mode}` flag records whether the current rerun already displayed `✅ Ready`. The loop now fires a single extra rerun on the exact frame the warmup event flips, then goes silent — eliminating redundant sleep/reruns in steady state.
- **Prompts (Efficiency):** Replaced **Rules Lawyer** with the **Exhaustive Rules Archivist** persona — completeness over brevity, no output-length cap, full extraction of every movement cost / reaction / condition detail present in Context. Anti-hallucination guardrails retained (`"Information not found in provided text."` fallback, citation mandate).
- **Diagnostic logging:** `stream_query` now wraps `token_iterator` with wall-clock timing; after the last token is yielded it prints `⏱ [INFERENCE] retrieval=Xs  llm=Ys  total=Zs  chunks=N  query=…` to stdout and logger — visible in `docker logs lore-keeper`.

## [1.7.2] - 2026-04-13

### Changed

- **Model (Efficiency):** Reverted from `phi3` to **`llama3:8b-instruct-q4_K_M`** — the 4-bit Q4_K_M quantized 8 B instruction model, tuned for 8 GB VRAM cards (~4.9 GB runtime footprint). Updated across `main.get_llm_engine`, `docker-compose.yml`, sidebar blurb, and error handler.
- **Docker — hard GPU passthrough:** `ollama` service `deploy.resources.reservations.devices` now uses `count: all` (was `1`). Added `NVIDIA_VISIBLE_DEVICES: all` and `NVIDIA_DRIVER_CAPABILITIES: compute,utility` to the `ollama` environment block so the NVIDIA Container Toolkit exposes all GPUs with the correct capability set (compute for CUDA kernels, utility for `nvidia-smi`).
- **RAG / prompts:** Efficiency-mode prompt replaced with the **Rules Lawyer** persona — explicit "Information not found in provided text" fallback, hard ban on fabricating mechanics/spells, numeric-detail extraction mandate, `[Source: … | Page N]` citations.
- **UX — GPU indicator:** Sidebar gains a full-width **GPU Acceleration** status card. A `subprocess` call to `nvidia-smi --query-gpu=name` (4 s timeout, `@st.cache_resource`) populates 🟢 Active + GPU name or 🔴 Inactive at process start.

## [1.7.1] - 2026-04-13

### Changed

- **Model (Efficiency):** Default local model switched from **`llama3`** (8 B, ~4.7 GB VRAM) to **`phi3`** (~3.8 B, ~2.3 GB 4-bit) across `main.get_llm_engine`, `docker-compose.yml` (`OLLAMA_PULL_MODEL`, `OLLAMA_CHAT_MODEL`), sidebar blurb, and Ollama-missing error handler. Reduces VRAM pressure and pull time on constrained hosts; override with `OLLAMA_CHAT_MODEL` env var.
- **RAG / prompts:** Efficiency-mode system prompt replaced with the **High-Density Mechanical Extractor** persona — category-grouped bullets (Movement, Combat, Actions, Conditions), explicit exhaustive-extraction mandate for costs/penalties/interactions, `[Source: … | Page N]` citation per claim, and strict anti-hallucination grounding.
- **UX — thinking animation:** Retrieval spinner replaced with **`st.status("The Lore Keeper is thinking…")`**; the status collapses to "Sources retrieved." once context is ready, then token streaming begins below it.
- **UX — auto-refresh:** A 1.5 s polling loop at the end of `app.py` calls `st.rerun()` while the background warmup is still running, so the sidebar Engine card flips from **🔄 Loading…** → **✅ Online** automatically without waiting for user interaction.

## [1.7.0] - 2026-04-13

### Added

- **Performance / UX:** **Non-blocking UI strategy** — `st.set_page_config`, CSS, sidebar, hero, and chat input render **instantly** at page load; `LoreKeeper` engine construction runs in a background `threading.Thread` via `_ensure_warmup()`, eliminating the ~100 s blocking wall. Sidebar shows a dynamic **Engine** status card (`🔄 Loading…` → `✅ Ready`) and caption that updates on each Streamlit rerun.
- **Profiling:** `_profile()` helper in `app.py` and per-import timing in `main._langchain_bundle()` / `LoreKeeper.__init__` print `⏱ [PROFILE …]` lines to stdout so operators can identify exactly which startup phase steals time.

### Changed

- **App architecture:** Replaced the synchronous `st.status("Initializing engines…")` blocking pattern with process-level `_warmup_events` / `_warmup_errors` dicts keyed by `(db_path, llm_mode)`. `@st.cache_resource` internal locking ensures the chat handler safely waits for the background build if the user sends a message before warmup completes (spinner: "Engine still warming up…").
- **Sidebar:** "Status" card renamed to **Engine** with tri-state rendering (`Online` / `Loading…` / `Error`); engine readiness caption replaces the old static text.
- **Tier switch:** `_ensure_warmup("db", _picked)` dispatches a background build for the new tier immediately on radio change.
- **Ingest:** `_reset_warmup()` clears process-level tracking after `get_lore_keeper.clear()` so the next rerun starts a fresh background build against the new vectors.

### Docs

- **Docker audit:** Confirmed `docker-entrypoint.sh` contains **no installs** — only `streamlit run app.py`; `ollama pull` is already background-only inside the compose `ollama` service entrypoint.

## [1.6.6] - 2026-04-12

### Changed

- **RAG / prompts:** Tightened **efficiency** system prompt—explicit grounding, no general-D&D filler when Context is silent, stricter rules for numeric mechanics, short bullets, and `[Source: … | Page …]` awareness without the full intelligence-tier persona.

## [1.6.5] - 2026-04-12

### Added

- **Docker:** `ollama` service **`deploy.resources.reservations.devices`** for **NVIDIA GPU** (driver `nvidia`, `capabilities: [gpu]`). Documented optional **`OLLAMA_NUM_GPU`** / **`OLLAMA_NUM_THREAD`** for `ChatOllama` in `main.get_llm_engine`.

### Changed

- **Performance:** **`app.py`** lazy-imports **`main`**, **`injest`**, and **`health_server`** so the worker process starts faster; **`get_lore_keeper`** injects a phase hook via **`st.session_state["_lk_on_phase"]`** so **`st.status`** shows labels from **`LoreKeeper`** (config, tracing, LangChain, embeddings/Chroma, chat model, hybrid BM25, Flashrank, prompts).
- **RAG / prompts:** **Efficiency** mode uses hybrid **k=5**, smaller rerank heads and merge caps, and a **bullet-only, context-strict** system prompt; **Intelligence** keeps **k=10** and the full **Dungeon Master** persona prompt.

### Docs

- **LEARNING_DEBT.md:** **GPU Passthrough** and **Lazy Imports** added to the historical table; task log row for v1.6.5.

## [1.6.4] - 2026-04-12

### Added

- **Docs:** `LEARNING_DEBT.md` — learning-debt tracker with a historical concept table and an append-only **task log** (first entry: **Infra Optimization** / v1.6.3 concepts).

### Changed

- **Governance:** `.cursorrules` §5–§6 now describe maintaining the task log and optional updates to the historical table in `LEARNING_DEBT.md`.

## [1.6.3] - 2026-04-12

### Added

- **Infra:** The `ollama` service now uses a persistent **`ollama_data`** volume at `/root/.ollama`, starts **`ollama serve`**, and runs **`ollama pull`** for **`OLLAMA_PULL_MODEL`** (default `llama3`) in the **background** so weights populate inside the Docker graph without a separate one-shot service.

### Changed

- **UX:** **Passive initialization** — after `set_page_config`, the app resolves **`get_lore_keeper`** immediately and shows **`st.status("System: Initializing engines...")`** on the first load of each model tier; **`@st.cache_resource`** keeps work to one build per tier per process. The compose **`ollama-pull`** service was removed as redundant.

### Fixed

- **Ollama errors:** The Streamlit error path now surfaces a **copy-paste** `docker exec -it ollama ollama pull …` using the configured **`OLLAMA_CHAT_MODEL`**, plus list/verify guidance.

## [1.6.2] - 2026-04-12

### Changed

- **Ollama defaults** align with the library’s usual **`ollama pull llama3`**: **`OLLAMA_CHAT_MODEL`** / **`OLLAMA_PULL_MODEL`** and `get_llm_engine` now default to **`llama3`** (not `llama3.2:3b`). Compose `ollama-pull` fallbacks try **`llama3:latest`** then **`llama3.2`**.
- **UI:** Removed the **Preload RAG engine** button; the engine still loads automatically on the **first chat message** (or after a model-tier change). Sidebar caption explains this.

### Fixed

- **Docs / errors:** Clarified that **Docker Ollama uses its own volume**—pulling a model on the **Windows host** does not populate the **`ollama` container**; the Streamlit error text now states this explicitly.

## [1.6.1] - 2026-04-12

### Changed

- **Infrastructure:** Migrated to named volumes (`chroma_data` → `/app/db`, `user_data` → `/app/data`) in Docker Compose to fix WSL2 I/O lag (avoids the Windows bind-mount bridge for Chroma and uploads). Code still bind-mounts from the host as `.:/app`; `db` and `data` are overlaid by those volumes.
- **UI:** Implemented non-blocking lazy initialization — the Streamlit shell (sidebar, theme, hero) renders immediately; `LoreKeeper` is created on first chat send (v1.6.2 removed the optional preload button). `get_lore_keeper` remains `@st.cache_resource` without an eager call at startup.

### Added

- Timestamped **logging** at `main.py` module setup, `LoreKeeper.__init__` (begin/end), `app.py` import boundary and after `set_page_config`, and around `get_lore_keeper` construction to trace startup delays.

### Fixed

- **Ollama 404:** Compose **`ollama-pull`** pre-pulls into the **container** volume; Streamlit shows a clear error instead of an uncaught traceback if the model is still missing or misnamed. (Defaults were later aligned to **`llama3`** in v1.6.2.)

## [1.6.0] - 2026-04-12

### Added

**Tiered LLM architecture**
- **`main.get_llm_engine(mode)`** selects the chat model: **Efficiency** → `langchain_ollama.ChatOllama` with `llama3` (base URL from **`OLLAMA_BASE_URL`**, default `http://127.0.0.1:11434`); **Intelligence** → `ChatOpenAI` with **`gpt-5.4`** (requires `OPENAI_API_KEY`). Embeddings stay on OpenAI for the vector store in both tiers.
- **`LoreKeeper(db_path, llm_mode=...)`** wires the chosen engine into the RAG chain.

**Docker**
- Compose service **`ollama`** (`ollama/ollama:latest`), port **11434**, named volume for **`/root/.ollama`**, and **`healthcheck`** via `ollama list` so **`lore-keeper`** starts only after Ollama is ready. **`OLLAMA_BASE_URL=http://ollama:11434`** is set on the app service.

**Streamlit**
- Sidebar **Model** section (Cursor-style tier labels + captions): **Auto Efficiency** vs **Premium Intelligence**; default **Efficiency**; choice stored in **`st.session_state`** for the session. **`st.cache_resource`** keys include **`llm_mode`** so tiers do not clobber each other in multi-session processes.

### Changed

- Dependency: **`langchain-ollama`** for local chat.
- **`eval_rag.py`**: RAGAS judge uses **`gpt-4o-mini`** even when the keeper uses Ollama; default eval **`LoreKeeper`** tier is **`intelligence`** unless **`LORE_KEEPER_LLM_MODE`** is set (avoids requiring Ollama for batch eval).

## [1.5.1] - 2026-04-12

### Changed

**Retrieval — targeted condition expansion**
- Queries that mention named D&D conditions (appendix-style glossary) now use higher hybrid **k** and a larger Flashrank **top_n** so first-stage reranking does not discard extra candidates.
- **Chroma `where_document`** substring pulls gather literal mentions of the condition name; chunks are **scored** to prefer Player’s Handbook / appendix wording over monster stat-block noise (heuristic).
- **Windowed context:** an in-memory index groups chunks by `(source, PDF page)`; when a retrieved chunk mentions the condition, the pipeline merges **all splits on that page**, **sequential neighbors** (index ±1 within the page bucket), and **adjacent PDF pages** (±1), improving recall for dense appendix text without new ingest metadata.

### Evaluation (RAGAS)

Post-change run on **2026-04-12** with `eval_rag.py --db db` (golden set: 3 questions):

- **Faithfulness (mean):** **0.9267** — up from the 1.5.0 baseline **0.6611**; the prone-condition row improved markedly (see below).
- **Answer relevancy (mean):** **0.9299** (prior baseline **0.9223**).

Per-question faithfulness / answer relevancy: flanking **1.0000 / 1.0000**, Counterspell **0.9231 / 0.8469**, prone **0.8571 / 0.9426**. Compared to 1.5.0 per-row faithfulness (**0.7500**, **0.8696**, **0.3636**), **prone faithfulness is the primary measured win** for this release.

*RAGAS scores remain LLM-judged and can vary slightly between runs.*

## [1.5.0] - 2026-04-12

### Added

**Docker & Infra**
- Multi-stage image with **uv**, BuildKit cache mounts, and a lean runtime layer speed reproducible installs and day-to-day rebuilds.
- Entrypoint script ships as `/entrypoint.sh` outside `/app`, uses `curl` plus Streamlit `/_stcore/health` for a `Ready` log line, and matches Compose healthchecks so automation sees real server readiness.
- Tighter `.dockerignore` and LF-normalized shell scripts shrink build context and avoid Windows CRLF breaking Linux entrypoints.

**Async & Performance**
- Overlapped retrieval and async-friendly ask/stream paths reduce wall-clock latency before answers and tokens arrive.
- `st.cache_resource` shares one `LoreKeeper` per process with first-load spinner/progress; cache clears after ingest so new vectors take effect without a full restart.
- Lazy LangChain and Phoenix imports defer heavy work until `LoreKeeper` constructs, keeping lightweight `import main` paths fast.

**Observability**
- Optional **Arize Phoenix** OTLP export instruments LangChain when `PHOENIX_COLLECTOR_ENDPOINT` is set.
- Tracing stays optional so local dev and minimal deployments run without a collector.

**Evaluation**
- `eval_rag.py` batch-runs **RAGAS** faithfulness and answer relevancy on a golden question set through the live RAG stack.
- Retrieved contexts feed the metrics so scores reflect actual retrieval, not stubbed text.

**Documentation & Governance**
- Core modules use Google-style plain Markdown docstrings with clearer architecture and intent.
- `VERSION` and `.cursorrules` document SemVer and changelog discipline for releases.

### Evaluation (RAGAS)

Baseline run on **2026-04-12** with `eval_rag.py --db db` (golden set: 3 questions; index ~6k chunks). Mean scores:

- **Faithfulness:** **0.6611** — answers stay largely grounded in retrieved context; lowest row was prone-condition wording versus strict ground-truth overlap.
- **Answer relevancy:** **0.9223** — generated answers align well with question intent across the set.

Per-question means from the same run: flanking **0.7500 / 1.0000**, Counterspell **0.8696 / 0.8914**, prone **0.3636 / 0.8755** (faithfulness / answer relevancy).

*RAGAS judge calls use the LLM; repeating `eval_rag.py` may nudge decimals. `eval_rag.py` now falls back to a console-safe print on Windows if the pandas frame contains non-encodable characters.*

### Changed

**UI — Sources**
- Verified citations group by PDF and show merged **page ranges** when metadata allows, cutting repeated lines in the expander.

## [1.4.0] - 2026-04-11
### Added
- **Evaluation:** Integrated RAGAS framework to measure Faithfulness and Answer Relevancy metrics.
- **Concurrency:** Refactored core retrieval logic to use `asyncio` for non-blocking I/O during LLM and Vector DB calls.
- **UI:** Implemented streaming responses in the Streamlit frontend to reduce perceived latency.

## [1.3.0] - 2026-04-9
### Added
- **Dockerization:** Containerized the entire application and database using Docker Compose.
- **Persistence:** Configured Docker Volumes for `db/`, `data/`, and `chat_history.json` to ensure data persistence.
- **Cross-Platform:** Implemented path normalization logic to handle Windows-style paths in Linux containers.

## [1.2.0] - 2026-03-7
### Added
- **Hybrid Search:** Developed an Ensemble Retriever combining Dense Vector search (ChromaDB) and Sparse Keyword search (BM25).
- **Reranking:** Integrated Flashrank to improve search precision via cross-encoder re-ordering.
- **Persona:** Optimized System Prompt for a specialized "Dungeon Master" persona with D&D 5e expertise.

### Fixed
- **Censorship Logic:** Fine-tuned guardrails to allow creative RPG scenarios while maintaining blocks on non-fantasy topics.

## [1.1.0] - 2026-03-6
### Added
- **Inference:** Connected GPT-4o-mini as the primary reasoning engine for the RAG pipeline.
- **Vector Store:** Implemented ChromaDB for efficient document storage and similarity search.
- **Data Pipeline:** Built initial `injest.py` for automated parsing and chunking of PDF manuals.

## [1.0.0] - 2026-04-05
### Added
- **MVP:** Initial launch of the Lore Keeper RAG system.
- **Frontend:** Basic Streamlit interface for PDF querying and interactive chat.
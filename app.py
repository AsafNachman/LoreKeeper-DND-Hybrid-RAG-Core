"""Streamlit front-end for Lore Keeper: chat UI, ingestion controls, and health server.

This file is the default container entrypoint (see `Dockerfile`): it serves the browser
on port 8501. Heavy modules (`main`, `ingest`) are **lazy-imported** so the process
starts faster; `get_engine` (`@st.cache_resource`) loads `main.LoreKeeper` in a
**background thread** at page load so the chat interface renders instantly and engine
readiness is reported via the sidebar status indicator (`🔄 Loading Engine…` → `✅ Ready`).

A background HTTP health listener from `health_server` is started once per session and
exposes port 8080 for Docker healthchecks. Phoenix tracing is configured in `main`
when `PHOENIX_COLLECTOR_ENDPOINT` is set.
"""

from __future__ import annotations

import html
import importlib
import json
import logging
import os
import re
import random
import shutil
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Optional

import streamlit as st

if TYPE_CHECKING:
    from main import LoreKeeper

from core.utils import normalize_stored_citation as core_normalize_stored_citation
from core.utils import source_filename as core_source_filename

try:
    # Optional: enables attaching Streamlit script context to background threads so they can
    # safely touch `st.session_state` and request reruns.
    from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx
except Exception:  # pragma: no cover
    add_script_run_ctx = None  # type: ignore[assignment]
    get_script_run_ctx = None  # type: ignore[assignment]

def _read_version() -> str:
    try:
        return Path("VERSION").read_text(encoding="utf-8").splitlines()[0].strip()
    except OSError:
        return "2.0.0"


_REPO_ROOT = Path(__file__).resolve().parent
_UI_SETTINGS_PATH = _REPO_ROOT / "storage" / "ui_settings.json"


def _normalize_brain_id_for_settings(brain_id: str | None) -> str:
    """Normalize a brain id for JSON hydration (same rules as `_normalize_brain_id`).

    Args:
        brain_id: Raw id from disk or user input.

    Returns:
        Filesystem-safe lowercase id, defaulting to ``dnd_core`` when empty.

    Intent:
        This helper is defined before the main `_normalize_brain_id` so session
        hydration can run at import time without forward references.
    """
    raw = (brain_id or "").strip().lower()
    if not raw:
        return "dnd_core"
    safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in raw).strip("_")
    return safe or "dnd_core"


def _load_ui_settings_dict() -> dict[str, Any]:
    """Load persisted sidebar settings from ``storage/ui_settings.json`` if present.

    Returns:
        Parsed JSON object, or an empty dict on missing/invalid files.

    Intent:
        Full browser refresh clears Streamlit session state; restoring from disk
        keeps ``llm_mode``, model, brain, and multi-query preferences stable.
    """
    try:
        raw = _UI_SETTINGS_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _coerce_ui_bool(value: Any, *, default: bool = True) -> bool:
    """Parse JSON or form-like truthy/falsey values for sidebar toggles.

    Args:
        value: Raw value from ``ui_settings.json`` (bool, int, or string).
        default: Fallback when the value is unrecognized.

    Returns:
        A strict boolean suitable for ``multi_query_enabled``.

    Intent:
        JSON may stringify booleans in some editors; ``bool(\"false\")`` is ``True`` in Python,
        which would incorrectly force multi-query ON after refresh.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) and value != 0
    s = str(value).strip().lower()
    if s in ("false", "0", "no", "off", ""):
        return False
    if s in ("true", "1", "yes", "on"):
        return True
    return default


def _hydrate_session_from_ui_settings() -> None:
    """Apply values from ``ui_settings.json`` onto ``st.session_state``.

    Intent:
        Runs once per browser session (guarded by ``_ui_settings_hydrated``) so
        widget defaults match the last saved sidebar state after refresh.
        Applied **before** default session keys so persisted ``multi_query_enabled: false``
        is not overwritten by Streamlit defaults.
    """
    data = _load_ui_settings_dict()
    lm = data.get("llm_mode")
    if lm in ("efficiency", "intelligence"):
        st.session_state.llm_mode = lm
    bid = data.get("brain_id")
    if isinstance(bid, str) and bid.strip():
        st.session_state.brain_id = _normalize_brain_id_for_settings(bid)
    am = data.get("active_model")
    if isinstance(am, str) and am.strip():
        st.session_state.active_model = am.strip()
    if "multi_query_enabled" in data:
        st.session_state.multi_query_enabled = _coerce_ui_bool(
            data["multi_query_enabled"], default=True
        )
    if "context_expansion_enabled" in data:
        st.session_state.context_expansion_enabled = _coerce_ui_bool(
            data["context_expansion_enabled"], default=False
        )


def _persist_ui_settings_to_disk() -> None:
    """Write current sidebar-related session keys back to ``ui_settings.json``.

    Intent:
        Keeps JSON in sync with the sidebar after each rerun so a browser refresh
        restores the same ``llm_mode``, model, brain, multi-query toggle, and context expansion.
    """
    _UI_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "llm_mode": st.session_state.get("llm_mode", "efficiency"),
        "brain_id": st.session_state.get("brain_id", "dnd_core"),
        "active_model": st.session_state.get("active_model", ""),
        "multi_query_enabled": bool(st.session_state.get("multi_query_enabled", True)),
        "context_expansion_enabled": bool(st.session_state.get("context_expansion_enabled", False)),
    }
    _UI_SETTINGS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


APP_VERSION = _read_version()
PRODUCTION_HEADER = f"Lore Keeper v{APP_VERSION}"
APP_TITLE = f"Lore Keeper v{APP_VERSION}"
GENERAL_KNOWLEDGE_WARNING = "⚠️ NOTE: No verified sources found. Using general knowledge."
LOW_RELEVANCE_SOFT_WARNING = "I've found relevant records, though they require careful interpretation."
_SOURCE_TAG_RE = re.compile(r"\[Source:\s*([^|\]]+?)\s*\|\s*Page[^\]]*\]", re.IGNORECASE)
SUGGESTION_POOL = [
    "What is a Paladin's Lay on Hands?",
    "Explain Wild Magic Surge",
    "How does multiclassing work?",
    "What does the *Shield* spell do, exactly?",
    "How does *Counterspell* work in 5e (including upcasting)?",
    "What is the difference between advantage and disadvantage?",
    "What are legendary actions, and when can a monster use them?",
    "How does concentration work, and what breaks it?",
    "What is an opportunity attack and when does it trigger?",
    "How does short rest vs long rest recovery work?",
]

_CUSTOM_CSS = """
<style>
/* ---------- global ---------- */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

:root {
    --gold:   #d4a843;
    --parch:  #faf3e0;
    --ink:    #1e1e1e;
    --accent: #7b2d26;
    --muted:  #6b6b6b;
    --card:   #ffffff;
    --border: #e0d5c1;
}

section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #1a1207 0%, #2a1f0e 100%);
    min-width: 360px;
    width: 360px;
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, Arial, sans-serif;
    font-size: 15.5px;
    line-height: 1.45;
}
section[data-testid="stSidebar"] * {
    color: #e8dcc8 !important;
}

/* Preserve Streamlit's Material icon font (fixes "keyboard_double_a…" artifacts). */
section[data-testid="stSidebar"] span[class*="material-symbols"] {
    font-family: 'Material Symbols Rounded' !important;
    font-size: 20px !important;
    line-height: 1 !important;
}

/* General text elements (avoid forcing font-size on widgets/buttons). */
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] li,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] small,
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] .stCaption {
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, Arial, sans-serif !important;
}

/* ---------- sidebar ---------- */
.sidebar-brand {
    text-align: center;
    padding: 1.2rem 0 0.6rem;
}
.sidebar-brand h2 {
    color: var(--gold) !important;
    margin: 0;
    font-size: 1.15rem;
    letter-spacing: 0.4px;
    font-weight: 650;
}
.sidebar-brand .tagline {
    font-size: 0.86rem;
    color: #a89878 !important;
    margin-top: 2px;
}

.sidebar-meta {
    font-size: 0.84rem;
    line-height: 1.35;
    padding: 0.25rem 0.1rem 0.6rem;
}
.meta-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    margin: 2px 0;
}
.meta-key {
    color: #a89878 !important;
    flex: 0 0 auto;
}
.meta-val {
    flex: 1 1 auto;
    text-align: right;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}

.danger-actions [data-testid="stButton"] button {
    border-radius: 10px !important;
}
.danger-actions [data-testid="stButton"] button[data-testid="baseButton-secondary"] {
    border: 1px solid rgba(212,168,67,0.28) !important;
    background: rgba(255,255,255,0.04) !important;
}
.danger-actions [data-testid="stButton"] button[data-testid="baseButton-secondary"]:hover {
    border-color: rgba(212,168,67,0.55) !important;
    background: rgba(212,168,67,0.08) !important;
}
.danger-actions [data-testid="stButton"] button[data-testid="baseButton-primary"] {
    background: #ff4d4d !important;
    border: 1px solid #ff4d4d !important;
    color: #1a1207 !important;
    font-weight: 650 !important;
}

/* ---------- hero / welcome ---------- */
.hero {
    text-align: center;
    /* Keep the cold-start screen compact so suggestion pills sit near chat input. */
    padding: 1.8rem 1rem 0.9rem;
}
.hero-icon { font-size: 2.4rem; }
.hero h1 {
    color: var(--ink);
    font-size: 2rem;
    margin: 0.4rem 0 0.3rem;
}
.hero p {
    color: var(--muted);
    max-width: 460px;
    margin: 0 auto;
    font-size: 0.95rem;
    line-height: 1.55;
}

/* suggestion blobs — larger "tile" buttons used for quick prompts */
.suggestion-blobs [data-testid="stButton"] button[data-testid="baseButton-secondary"] {
    border-radius: 14px !important;
    font-size: 0.9rem !important;
    padding: 10px 14px !important;
    background: rgba(255,255,255,0.04) !important;
    border: 1px solid rgba(212,168,67,0.35) !important;
    color: inherit !important;
    white-space: normal !important;
    width: 100% !important;
    min-height: 46px !important;
}
.suggestion-blobs [data-testid="stButton"] button[data-testid="baseButton-secondary"]:hover {
    background: rgba(212,168,67,0.10) !important;
    border-color: var(--gold) !important;
}
.suggestion-blobs [data-testid="stButton"] {
    width: 100% !important;
}

/* draft edit bar that replaces st.chat_input when a hint is pending */
.draft-bar [data-testid="stTextInput"] input {
    border-radius: 8px !important;
    font-size: 0.95rem !important;
}

/* ---------- source expander ---------- */
.src-item {
    border-left: 3px solid var(--gold);
    border-radius: 4px;
    padding: 8px 12px;
    margin-bottom: 8px;
    background: rgba(212,168,67,0.07);
}
.src-citation {
    font-size: 0.82rem;
    font-weight: 600;
    color: inherit;
    margin-bottom: 4px;
}
.src-excerpt {
    font-size: 0.78rem;
    opacity: 0.75;
    font-style: italic;
    line-height: 1.45;
    color: inherit;
}
.src-item details summary {
    cursor: pointer;
    font-size: 0.78rem;
    opacity: 0.85;
    margin-top: 4px;
}

/* ---------- file uploader: shrink only the invalid-file error pill ---------- */
[data-testid="stFileUploaderFile"] {
    padding: 4px 8px !important;
    font-size: 0.75rem !important;
    line-height: 1.3 !important;
}
[data-testid="stFileUploaderFile"] svg {
    width: 14px !important;
    height: 14px !important;
}

/* ---------- management section ---------- */
.mgmt-header {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: #a89878 !important;
    margin: 0.2rem 0 0.6rem;
}
.queued-file {
    display: flex;
    align-items: center;
    gap: 6px;
    background: rgba(255,255,255,0.05);
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 0.8rem;
    margin-bottom: 4px;
    color: #e8dcc8 !important;
    word-break: break-all;
}

/* ---------- model tier (Cursor-style selector) ---------- */
.model-tier-caption {
    font-size: 0.72rem !important;
    line-height: 1.4;
    color: #b8a88c !important;
    margin: 0.15rem 0 0.85rem;
    padding: 0 0.15rem;
}
section[data-testid="stSidebar"] div[data-testid="stRadio"] label {
    font-weight: 500 !important;
    font-size: 0.88rem !important;
}

/* Consistent vertical rhythm (prevents overlap when fonts scale). */
section[data-testid="stSidebar"] [data-testid="stExpander"] {
    margin: 0.35rem 0 0.5rem;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary {
    padding: 0.35rem 0.15rem !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary p {
    margin: 0 !important;
}
section[data-testid="stSidebar"] [data-testid="stButton"],
section[data-testid="stSidebar"] [data-testid="stSelectbox"],
section[data-testid="stSidebar"] [data-testid="stRadio"],
section[data-testid="stSidebar"] [data-testid="stTextInput"] {
    margin-bottom: 0.45rem;
}
</style>
"""

# Render the shell immediately; heavy engine work is deferred to warmup threads.
st.set_page_config(page_title=APP_TITLE, page_icon="\U0001F4DC", layout="centered")

# Disk-backed UI prefs before defaults so toggles like multi_query_enabled survive refresh.
if "_ui_settings_hydrated" not in st.session_state:
    _hydrate_session_from_ui_settings()
    st.session_state._ui_settings_hydrated = True

if "llm_mode" not in st.session_state:
    st.session_state.llm_mode = "efficiency"
if "edition_filter" not in st.session_state:
    st.session_state.edition_filter = "All"
if "brain_id" not in st.session_state:
    st.session_state.brain_id = "dnd_core"
if "active_model" not in st.session_state:
    st.session_state.active_model = (
        (os.getenv("OLLAMA_CHAT_MODEL") or "llama3:8b-instruct-q4_K_M").strip()
        or "llama3:8b-instruct-q4_K_M"
    )
if "multi_query_enabled" not in st.session_state:
    st.session_state.multi_query_enabled = True
if "context_expansion_enabled" not in st.session_state:
    st.session_state.context_expansion_enabled = False
if "pending_query" not in st.session_state:
    st.session_state.pending_query = None
if "pending_draft" not in st.session_state:
    st.session_state.pending_draft = None
if "_pending_assistant_query" not in st.session_state:
    st.session_state._pending_assistant_query = None
if "suggestion_triplet" not in st.session_state:
    st.session_state.suggestion_triplet = random.sample(SUGGESTION_POOL, k=3)
else:
    cur = list(st.session_state.suggestion_triplet or [])
    if len(cur) != 3 or any((not isinstance(x, str)) or (not x.strip()) for x in cur):
        st.session_state.suggestion_triplet = random.sample(SUGGESTION_POOL, k=3)

os.environ["OLLAMA_CHAT_MODEL"] = st.session_state.active_model
st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)
_MAIN_TITLE_SLOT = st.empty()
_MAIN_TITLE_SLOT.markdown(f"### \U0001F4DC {PRODUCTION_HEADER}")

# ---------------------------------------------------------------------------
# Profiling — prints wall-clock cost of every major startup phase to stdout
# ---------------------------------------------------------------------------
_APP_T0 = time.perf_counter()
logger = logging.getLogger("lorekeeper.app")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def _profile(label: str) -> None:
    """Print elapsed wall-clock time since app.py start for a given startup phase.

    Output goes to both stdout (visible in `docker logs`) and the Python logger so
    operators can see exactly which initialization step is stealing time.
    """
    elapsed = time.perf_counter() - _APP_T0
    msg = f"⏱ [PROFILE app] {label} — {elapsed:.3f}s"
    print(msg, flush=True)
    logger.info(msg)


_profile("lightweight imports done")


@st.cache_resource
def _shared_state() -> dict:
    """Return the single process-wide coordination dict for warmup and GPU state.

    Using `@st.cache_resource` is the ONLY correct way to hold mutable state that
    must survive across Streamlit reruns. Every rerun re-executes `app.py`'s body,
    which re-assigns every module-level variable (`_warmup_events = {}`, etc.) to a
    fresh object. The old object is garbage-collected; any data written to it by a
    background thread is lost. `@st.cache_resource` returns the SAME dict instance
    on every rerun, so background threads and rerun N+1 share the same memory.

    Keys:
        lock    — threading.Lock for events/errors mutations.
        events  — {key: threading.Event} for warmup completion signalling.
        errors  — {key: str} for warmup failure messages.
        built   — set of keys whose LoreKeeper build completed successfully.
        phase_pct — {key: int} rough warmup progress percent (0-100).
        prewarm_started — bool process-level guard for one-shot preload kick.
        gpu_ok  — bool, last GPU probe result.
        gpu_name — str, last GPU label.
        gpu_ts  — float, monotonic timestamp of last GPU probe (0 = never).
    """
    return {
        "lock": threading.Lock(),
        "events": {},
        "errors": {},
        "built": set(),
        "heartbeat": {},
        "heartbeat_threads": set(),
        "heartbeat_ts": {},
        "phase_pct": {},
        "prewarm_started": False,
        "gpu_ok": False,
        "gpu_name": "Not detected",
        "gpu_ts": 0.0,
    }


def _do_gpu_probes() -> tuple[bool, str]:
    """Run the three GPU detection probes and return (ok, label).

    Probe 1 — `nvidia-smi`: driver-level; tries Windows System32/NVSMI paths as fallback.
    Probe 2 — Ollama `/api/ps` `size_vram`: ground-truth that GPU inference is active.
    Probe 3 — `nvidia-container-cli info`: Container Toolkit presence without device.

    Returns:
        (gpu_available, label) — e.g. `(True, "NVIDIA RTX 3080")`, `(True, "GPU via Ollama")`,
        `(False, "Toolkit present (no device mapped)")`, or `(False, "Not detected")`.
    """
    # --- Probe 1: nvidia-smi (driver-level; tries Windows-specific paths too) ---
    _smi_candidates = ["nvidia-smi"]
    if os.name == "nt":  # Windows: smi is in System32 or NVSMI, often not on PATH in venv
        _smi_candidates += [
            r"C:\Windows\System32\nvidia-smi.exe",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
        ]
    for _smi in _smi_candidates:
        try:
            r = subprocess.run(
                [_smi, "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=4,
            )
            name = r.stdout.strip().splitlines()[0].strip() if r.returncode == 0 else ""
            if name:
                logger.info("GPU probe: nvidia-smi found %r via %r", name, _smi)
                return True, name
        except Exception as exc:
            logger.debug("GPU probe: %r unavailable (%s)", _smi, exc)

    # --- Probe 2: Ollama /api/ps — ground-truth GPU-in-use check ---
    # If size_vram > 0, Ollama has at least one model layer on the GPU right now.
    try:
        ollama_client = _ollama_client_singleton()
        if ollama_client is not None:
            _ps = ollama_client.ps()
        else:
            _base = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
            with urllib.request.urlopen(f"{_base}/api/ps", timeout=2) as _resp:
                _ps = json.loads(_resp.read())
        for _m in _ps.get("models", []):
            if int(_m.get("size_vram") or 0) > 0:
                _mname = _m.get("model") or _m.get("name") or "model"
                logger.info("GPU probe: Ollama /api/ps shows GPU layers for %r", _mname)
                return True, f"GPU via Ollama ({_mname})"
    except Exception as exc:
        logger.debug("GPU probe: Ollama /api/ps unavailable (%s)", exc)

    # --- Probe 3: nvidia-container-cli (NVIDIA Container Toolkit presence) ---
    try:
        r2 = subprocess.run(
            ["nvidia-container-cli", "info"],
            capture_output=True, text=True, timeout=4,
        )
        if r2.returncode == 0 and r2.stdout.strip():
            logger.info("GPU probe: nvidia-container-cli present but no device visible")
            return False, "Toolkit present (no device mapped)"
    except Exception as exc:
        logger.debug("GPU probe: nvidia-container-cli unavailable (%s)", exc)

    return False, "Not detected"


def _check_gpu() -> tuple[bool, str]:
    """Return GPU status, refreshing the probe at most once every 30 seconds.

    Reads from and writes to `_shared_state()` so the result persists across
    Streamlit reruns and updates automatically after Ollama loads a model onto GPU.
    """
    state = _shared_state()
    now = time.monotonic()
    if now - state["gpu_ts"] >= 10.0:
        gpu_ok, gpu_name = _do_gpu_probes()
        state["gpu_ok"] = gpu_ok
        state["gpu_name"] = gpu_name
        state["gpu_ts"] = now
        logger.info("GPU probe refreshed: ok=%s label=%r", gpu_ok, gpu_name)
    return state["gpu_ok"], state["gpu_name"]


def _gpu_badge_state(*, engine_ready: bool) -> tuple[str, str, bool, str]:
    """Compute GPU badge state and trigger a one-time rerun on activation.

    Intent:
        The GPU badge can flip from "Searching/Inactive" → "Active" while this script
        is running (because the background warmup thread pre-loads the model into VRAM).
        Without an explicit rerun, the sidebar can remain stale until the user interacts.

    Args:
        engine_ready: Whether the RAG engine is ready (used for the "Searching…" intermediate state).

    Returns:
        Tuple of `(badge_key, badge_html, gpu_ok, gpu_label)` where:
        - `badge_key` is one of `active|searching|inactive`
        - `badge_html` is the colored HTML fragment for the badge value
        - `gpu_ok` and `gpu_label` come from `_check_gpu()`
    """
    # Ensure a session-scoped default. The background prewarm thread updates this.
    if "gpu_status" not in st.session_state:
        st.session_state.gpu_status = "warming"  # warming|active|inactive

    # Kick off a non-blocking GPU prewarm. This must never block UI rendering.
    _ensure_gpu_prewarm_non_blocking()

    gpu_ok, gpu_name = _check_gpu()
    gpu_age_s = time.monotonic() - _shared_state().get("gpu_ts", 0.0)

    if gpu_ok:
        st.session_state.gpu_status = "active"
        badge_key = "active"
        badge_html = '<div class="value" style="color:#4caf50 !important;">🟢 Active</div>'
    elif (not engine_ready) or (gpu_name == "Not detected" and gpu_age_s < 20.0):
        if st.session_state.gpu_status != "active":
            st.session_state.gpu_status = "warming"
        badge_key = "searching"
        badge_html = '<div class="value" style="color:#ffd54f !important;">🟡 Searching…</div>'
        if not engine_ready and gpu_name == "Not detected":
            gpu_name = "Waiting for model preload…"
    else:
        if st.session_state.gpu_status != "active":
            st.session_state.gpu_status = "inactive"
        badge_key = "inactive"
        badge_html = '<div class="value" style="color:#e57373 !important;">🔴 Inactive</div>'

    # One-time rerun when the badge flips to Active so the sidebar turns green immediately.
    # Reset the latch when we are not active so future activations can trigger again.
    if badge_key != "active":
        st.session_state["_gpu_badge_activated_rerun"] = False
    prev_key = st.session_state.get("_gpu_badge_prev", None)
    st.session_state["_gpu_badge_prev"] = badge_key
    if prev_key in (None, "searching", "inactive") and badge_key == "active":
        if not st.session_state.get("_gpu_badge_activated_rerun", False):
            st.session_state["_gpu_badge_activated_rerun"] = True
            st.rerun()

    return badge_key, badge_html, gpu_ok, gpu_name


def _gpu_state_key_only(*, engine_ready: bool) -> str:
    """Return GPU state key without triggering UI reruns.

    Args:
        engine_ready: Whether the engine is ready (influences "Searching…" state).

    Returns:
        One of `active|searching|inactive`.
    """
    gpu_ok, gpu_name = _check_gpu()
    gpu_age_s = time.monotonic() - _shared_state().get("gpu_ts", 0.0)
    if gpu_ok:
        return "active"
    if (not engine_ready) or (gpu_name == "Not detected" and gpu_age_s < 20.0):
        return "searching"
    return "inactive"


DEFAULT_BRAIN_ID = "dnd_core"
DB_ROOT = Path("db")
DATA_ROOT = Path("data")
HISTORY_FILE = Path("storage") / "chat_history.json"
BRAIN_METADATA_FILE = DB_ROOT / "brain_metadata.json"
ERROR_LOG_FILE = Path("storage") / "error_log.json"


def _append_black_box_event(error_type: str, details: str) -> None:
    """Append an event to the rolling "black box" error log (last 5 only).

    Args:
        error_type: Short category key (e.g. `query`, `ingest`, `preflight`, `engine`).
        details: Human-readable details (sanitized for UI; keep concise).
    """
    try:
        now = _utc_now_iso()
        event = {"ts_utc": now, "type": str(error_type), "details": str(details)[:800]}
        try:
            existing = json.loads(ERROR_LOG_FILE.read_text(encoding="utf-8"))
            events = list(existing) if isinstance(existing, list) else []
        except (OSError, json.JSONDecodeError):
            events = []
        events.append(event)
        events = events[-5:]
        ERROR_LOG_FILE.write_text(json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        logger.debug("Black box logging failed: %s", exc)


def _restart_engine_now() -> None:
    """Clear cached engine and re-dispatch warmup for current brain/mode."""
    try:
        get_engine.clear()
        _reset_warmup("db", st.session_state.llm_mode, st.session_state.brain_id)
        _ensure_warmup("db", st.session_state.llm_mode, st.session_state.brain_id)
        _ensure_heartbeat_service("db", st.session_state.llm_mode, st.session_state.brain_id)
        st.session_state.pop(
            f"_engine_shown_ready:{st.session_state.brain_id}:{st.session_state.llm_mode}",
            None,
        )
    except Exception as exc:
        logger.debug("Engine restart attempt failed: %s", exc)


def render_error_state(error_type: str, details: str) -> None:
    """Render a user-friendly recovery panel instead of raw tracebacks.

    Args:
        error_type: Short category key.
        details: Human-readable details (keep short; avoid secrets).
    """
    _append_black_box_event(error_type, details)
    st.error(
        f"🛠️ **System Recovery**\n\n{details}",
        icon="🛠️",
    )
    col_a, col_b = st.columns([2, 3])
    with col_a:
        if st.button("Restart Engine", type="primary", use_container_width=True):
            _restart_engine_now()
            st.rerun()
    with col_b:
        st.caption("If this persists: check Docker/Ollama status, then retry.")


def _maybe_auto_memory_reclaim() -> None:
    """Best-effort memory reclaim for high-RAM setups when enabled by env.

    Set `LORE_KEEPER_AUTO_MEMORY_RECLAIM=1` to ask Ollama to unload currently
    resident models via `keep_alive=0` before model/tier transitions.
    """
    enabled = (os.getenv("LORE_KEEPER_AUTO_MEMORY_RECLAIM") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return
    try:
        client = _ollama_client_singleton()
        if client is None:
            return
        ps = client.ps() or {}
        models = ps.get("models", []) if isinstance(ps, dict) else []
        for m in models:
            name = m.get("model") or m.get("name")
            if name:
                client.generate(model=name, keep_alive=0)
    except Exception as exc:
        logger.debug("autoMemoryReclaim skipped: %s", exc)


@st.cache_resource
def _global_runtime_singleton() -> dict[str, Any]:
    """Create one process-wide runtime bundle for heavy imports and clients.

    This cache resource is intentionally strict: every Streamlit session receives
    the same bundle instance, which eliminates repeated import/constructor work.
    """
    _t0 = time.perf_counter()
    heavy_modules: dict[str, Any] = {}
    for mod_name in (
        "langchain",
        "langchain_chroma",
        "langchain_openai",
        "langchain_core",
        "langchain_classic",
        "langchain_community",
        "chromadb",
        "torch",
    ):
        try:
            heavy_modules[mod_name] = importlib.import_module(mod_name)
        except Exception as exc:
            logger.debug("Runtime preload skipped for %s: %s", mod_name, exc)

    from langchain_classic.retrievers import ContextualCompressionRetriever
    from langchain_chroma import Chroma
    from langchain_community.document_compressors import FlashrankRerank
    from langchain_community.retrievers import BM25Retriever
    from langchain_core.documents import Document
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings

    langchain_kit = SimpleNamespace(
        BM25Retriever=BM25Retriever,
        ChatOpenAI=ChatOpenAI,
        ChatPromptTemplate=ChatPromptTemplate,
        Chroma=Chroma,
        ContextualCompressionRetriever=ContextualCompressionRetriever,
        Document=Document,
        FlashrankRerank=FlashrankRerank,
        MessagesPlaceholder=MessagesPlaceholder,
        OpenAIEmbeddings=OpenAIEmbeddings,
        StrOutputParser=StrOutputParser,
    )

    base_url = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
    ollama_client = None
    try:
        from ollama import Client as OllamaClient

        ollama_client = OllamaClient(host=base_url)
    except Exception as exc:
        logger.debug("Ollama client singleton unavailable: %s", exc)

    elapsed = time.perf_counter() - _t0
    _profile(f"global runtime singleton ready ({elapsed:.3f}s)")
    return {
        "heavy_modules": heavy_modules,
        "langchain_kit": langchain_kit,
        "ollama_client": ollama_client,
        "ollama_base_url": base_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _ollama_client_singleton() -> Any:
    """Return the process-wide Ollama client, or None when unavailable."""
    return _global_runtime_singleton().get("ollama_client")


def _preflight_llm_ok(llm_mode: str) -> tuple[bool, str]:
    """Pre-flight check that the selected LLM tier is responsive.

    Args:
        llm_mode: `efficiency` (Ollama) or `intelligence` (OpenAI).

    Returns:
        (ok, message). If ok is False, message is UI-ready warning text.
    """
    key = (llm_mode or "efficiency").strip().lower()
    if key == "efficiency":
        try:
            client = _ollama_client_singleton()
            if client is None:
                return False, "⚠️ Local Engine is not reachable. Please check Docker/Ollama status."
            client.ps()
            return True, ""
        except Exception:
            return False, "⚠️ Local Engine is currently overloaded. Please wait or check Docker status."

    if key == "intelligence":
        if not (os.getenv("OPENAI_API_KEY") or "").strip():
            return False, "⚠️ Premium Intelligence requires `OPENAI_API_KEY`."
        try:
            req = urllib.request.Request(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}".strip()},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                _ = resp.read(1)
            return True, ""
        except Exception:
            return False, "⚠️ Premium Intelligence API is not responding. Please retry shortly."

    return False, f"⚠️ Unknown LLM mode {llm_mode!r}."

def _normalize_brain_id(brain_id: str | None) -> str:
    """Return a filesystem-safe brain id for per-archive isolation."""
    raw = (brain_id or "").strip().lower()
    if not raw:
        return DEFAULT_BRAIN_ID
    safe = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in raw).strip("_")
    return safe or DEFAULT_BRAIN_ID


def _brain_db_dir(brain_id: str) -> Path:
    """Return the per-brain Chroma directory under `db/`."""
    return DB_ROOT / _normalize_brain_id(brain_id)


def _brain_data_dir(brain_id: str) -> Path:
    """Return the per-brain upload staging directory under `data/`."""
    return DATA_ROOT / _normalize_brain_id(brain_id)


def _utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _dir_size_bytes(path: Path) -> int:
    """Compute total file size under a directory (recursive)."""
    if not path.exists():
        return 0
    total = 0
    for file_path in path.rglob("*"):
        if file_path.is_file():
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _load_brain_metadata() -> dict[str, dict[str, Any]]:
    """Load persisted brain metadata map from disk."""
    try:
        raw = json.loads(BRAIN_METADATA_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return {str(k): dict(v or {}) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_brain_metadata(payload: dict[str, dict[str, Any]]) -> None:
    """Persist the brain metadata map to disk."""
    DB_ROOT.mkdir(parents=True, exist_ok=True)
    BRAIN_METADATA_FILE.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _upsert_brain_metadata(brain_id: str, *, touch_last_used: bool = False) -> None:
    """Create/update one brain metadata record with current size and timestamps."""
    normalized = _normalize_brain_id(brain_id)
    now_iso = _utc_now_iso()
    payload = _load_brain_metadata()
    row = payload.get(normalized, {})
    next_row = {
        "creation_date": row.get("creation_date", now_iso),
        "size_bytes": int(_dir_size_bytes(_brain_db_dir(normalized))),
        "last_used": now_iso if touch_last_used else row.get("last_used", now_iso),
    }
    if row == next_row:
        return
    payload[normalized] = next_row
    _save_brain_metadata(payload)


def _delete_brain_metadata(brain_id: str) -> None:
    """Remove one brain metadata record if present."""
    normalized = _normalize_brain_id(brain_id)
    payload = _load_brain_metadata()
    if normalized in payload:
        payload.pop(normalized, None)
        _save_brain_metadata(payload)


def _list_brains() -> list[str]:
    """Enumerate available brain ids from `db/` subdirectories.

    If the repository still uses the legacy single-brain layout (files directly in
    `db/`), migrate that content into `db/dnd_core/` to preserve indexed vectors.
    """
    DB_ROOT.mkdir(exist_ok=True)
    visible_entries = [entry for entry in DB_ROOT.iterdir() if not entry.name.startswith(".")]
    brains = sorted(entry.name for entry in visible_entries if entry.is_dir())
    if not brains and visible_entries:
        default_dir = _brain_db_dir(DEFAULT_BRAIN_ID)
        default_dir.mkdir(parents=True, exist_ok=True)
        for entry in visible_entries:
            if entry.name == DEFAULT_BRAIN_ID:
                continue
            try:
                shutil.move(str(entry), str(default_dir / entry.name))
            except Exception as exc:
                logger.warning("Brain layout migration skipped for %s: %s", entry.name, exc)
        brains = [DEFAULT_BRAIN_ID]
    if not brains:
        _brain_db_dir(DEFAULT_BRAIN_ID).mkdir(parents=True, exist_ok=True)
        brains = [DEFAULT_BRAIN_ID]
    payload = _load_brain_metadata()
    changed = False
    for brain in brains:
        if brain not in payload:
            payload[brain] = {
                "creation_date": _utc_now_iso(),
                "size_bytes": int(_dir_size_bytes(_brain_db_dir(brain))),
                "last_used": _utc_now_iso(),
            }
            changed = True
    stale = [key for key in payload.keys() if key not in brains]
    if stale:
        for key in stale:
            payload.pop(key, None)
        changed = True
    if changed:
        _save_brain_metadata(payload)
    return brains


def _engine_cache_key(db_path: str, llm_mode: str, brain_id: str) -> str:
    """Stable key used by warmup tracking maps."""
    return f"{db_path}:{_normalize_brain_id(brain_id)}:{llm_mode}"


_PHASE_PROGRESS_STEPS = [
    "Loading configuration…",
    "Optional: Phoenix tracing…",
    "Loading LangChain libraries…",
    "Loading embeddings & vector index…",
    "Connecting chat model (Ollama / OpenAI)…",
    "Building hybrid & BM25 index…",
    "Loading Flashrank rerankers…",
    "Building prompts…",
]
_PHASE_PROGRESS_DENOM = len(_PHASE_PROGRESS_STEPS) + 2


def _update_phase_progress(cache_key: str, label: str) -> None:
    """Track coarse warmup progress by initialization phase label."""
    try:
        idx = _PHASE_PROGRESS_STEPS.index(label) + 1
    except ValueError:
        idx = 1
    pct = int(max(1, min(100, (idx / _PHASE_PROGRESS_DENOM) * 100)))
    state = _shared_state()
    with state["lock"]:
        prev = int(state["phase_pct"].get(cache_key, 0))
        if pct > prev:
            state["phase_pct"][cache_key] = pct


@st.cache_resource
def get_engine(db_path: str, llm_mode: str, brain_id: str) -> "LoreKeeper":
    """Return a process-wide `LoreKeeper` (Chroma + hybrid retrievers built once).

    Imports `main` on first cache miss. Safe to call from either the Streamlit main
    thread or the background warmup thread (`st.session_state` access is guarded).
    Use `get_engine.clear()` after ingest to rebuild indexes.

    Cache key dimensions include `llm_mode` and `brain_id`, so each archive namespace
    stays isolated even when users switch model tiers.
    """
    from main import LoreKeeper, register_runtime_singletons

    register_runtime_singletons(_global_runtime_singleton())

    normalized_brain = _normalize_brain_id(brain_id)
    cache_key = _engine_cache_key(db_path, llm_mode, normalized_brain)
    _t0 = time.perf_counter()
    _profile(f"get_engine [{llm_mode}/{normalized_brain}]: cache miss — constructing")
    logger.info("get_engine: constructing LoreKeeper (cache miss for this key)…")
    try:
        user_hook = st.session_state.pop("_lk_on_phase", None)
    except Exception:
        user_hook = None

    def _phase_hook(label: str) -> None:
        _update_phase_progress(cache_key, label)
        if user_hook:
            user_hook(label)

    _shared_state()["phase_pct"][cache_key] = max(
        1,
        int(_shared_state()["phase_pct"].get(cache_key, 0)),
    )
    keeper = LoreKeeper(
        db_path=db_path,
        llm_mode=llm_mode,
        brain_id=normalized_brain,
        on_phase=_phase_hook,
    )
    # Record the build in the shared persistent state so _engine_is_ready() returns True
    # on every subsequent rerun — including reruns that don't go through this code path
    # (cache hit). Module-level variables like _engine_built are re-created on each
    # Streamlit rerun; _shared_state() is not.
    _shared_state()["built"].add(cache_key)
    _shared_state()["phase_pct"][cache_key] = 100
    _profile(f"get_engine [{llm_mode}/{normalized_brain}]: LoreKeeper ready")
    logger.info("get_engine: LoreKeeper ready in %.3fs", time.perf_counter() - _t0)
    return keeper


def resolve_engine() -> LoreKeeper:
    """Return the cached `LoreKeeper` for the current session tier.

    If the background warmup thread is still constructing the engine, this call
    blocks at the `@st.cache_resource` internal lock until the build completes
    (callers should wrap it in a spinner for good UX).
    """
    return get_engine("db", st.session_state.llm_mode, st.session_state.brain_id)


# ---------------------------------------------------------------------------
# Background Engine Warmup — non-blocking, thread-safe
#
# ALL mutable coordination state lives in _shared_state() (a @st.cache_resource
# dict). Module-level variables would be re-created to empty on every Streamlit
# rerun because the script body is re-executed each time, making them useless for
# cross-rerun communication. _shared_state() returns the SAME dict instance on
# every rerun, so background threads and the next rerun always share the same data.
# ---------------------------------------------------------------------------

def _ensure_warmup(db_path: str, llm_mode: str, brain_id: str) -> threading.Event:
    """Start engine construction in a daemon thread (once per mode+brain per process).

    Guards against duplicate starts using the lock in `_shared_state()`. The
    `@st.cache_resource` inside `get_engine` ensures only one actual build
    runs even if concurrent calls arrive; subsequent calls are cache hits (~1 ms).

    Args:
        db_path: Chroma persist directory.
        llm_mode: Tier key (`efficiency` or `intelligence`).
        brain_id: Selected archive namespace.

    Returns:
        A `threading.Event` that is set when the warmup finishes (success or error).
    """
    state = _shared_state()
    normalized_brain = _normalize_brain_id(brain_id)
    key = _engine_cache_key(db_path, llm_mode, normalized_brain)
    with state["lock"]:
        if key in state["events"]:
            return state["events"][key]
        event = threading.Event()
        state["events"][key] = event

    def _run() -> None:
        try:
            _profile(f"warmup [{llm_mode}/{normalized_brain}]: background thread started")
            get_engine(db_path, llm_mode, normalized_brain)
            _profile(f"warmup [{llm_mode}/{normalized_brain}]: background thread complete")

        except Exception as exc:
            state["errors"][key] = str(exc)
            logger.error("Engine warmup failed [%s/%s]: %s", llm_mode, normalized_brain, exc)
        finally:
            # _shared_state()["built"] is also set inside get_engine on cache
            # miss; set it here too for the error path so _engine_is_ready never
            # blocks forever on a failed warmup.
            event.set()

    threading.Thread(
        target=_run,
        name=f"warmup-{llm_mode}-{normalized_brain}",
        daemon=True,
    ).start()
    return event


def _prewarm_ollama_model_for_gpu() -> None:
    """Best-effort: ask Ollama to load the active model into VRAM.

    Intent:
        The sidebar GPU badge uses Ollama's `/api/ps` `size_vram` field as a ground-truth
        signal that GPU inference is actually active. Ollama lazily loads models; without
        a prewarm, `/api/ps` may show no VRAM usage until the first real query.

        Previously, the warmup thread attempted to call `/api/generate` with only
        `{"keep_alive": -1}`. Ollama expects a `prompt` (even empty), so the request could
        fail and be silently swallowed, leaving the UI stuck in 🟡/🔴 until a manual
        "Restart Engine" happened later.

    Returns:
        None. Failures are logged at debug level because GPU is an optimization; the app
        can still answer on CPU.
    """
    if (os.getenv("LORE_KEEPER_DISABLE_OLLAMA_PREWARM") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return

    model = (os.getenv("OLLAMA_CHAT_MODEL") or "llama3:8b-instruct-q4_K_M").strip() or "llama3:8b-instruct-q4_K_M"
    base = (os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")

    def _ps_has_vram() -> bool:
        try:
            ollama_client = _ollama_client_singleton()
            if ollama_client is not None:
                ps = ollama_client.ps() or {}
            else:
                with urllib.request.urlopen(f"{base}/api/ps", timeout=2) as resp:
                    ps = json.loads(resp.read())
            for m in (ps.get("models", []) if isinstance(ps, dict) else []):
                if int(m.get("size_vram") or 0) > 0:
                    return True
        except Exception:
            return False
        return False

    # Fast path: already active on GPU (e.g., another session already loaded it).
    if _ps_has_vram():
        _shared_state()["gpu_ts"] = 0.0
        return

    # Retry briefly: Ollama may be up but still initializing; also GPU mapping can lag a bit.
    t0 = time.monotonic()
    deadline = t0 + 60.0
    backoff_s = 0.6
    last_err: str | None = None

    while time.monotonic() < deadline:
        try:
            ollama_client = _ollama_client_singleton()
            if ollama_client is not None:
                # `prompt=""` triggers a load without meaningful token generation.
                ollama_client.generate(model=model, prompt="", stream=False, keep_alive=-1)
            else:
                payload = json.dumps(
                    {"model": model, "prompt": "", "stream": False, "keep_alive": -1}
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"{base}/api/generate",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    _ = resp.read()

            if _ps_has_vram():
                _shared_state()["gpu_ts"] = 0.0
                _profile("warmup: Ollama model prewarm confirmed via /api/ps size_vram")
                return

            last_err = "prewarm did not activate VRAM yet"
        except Exception as exc:
            last_err = str(exc)

        time.sleep(backoff_s)
        backoff_s = min(2.0, backoff_s * 1.35)

    logger.debug("Ollama model prewarm timed out after %.1fs: %s", time.monotonic() - t0, last_err)


def _ensure_gpu_prewarm_non_blocking() -> None:
    """Start a session-scoped GPU prewarm in a daemon thread (non-blocking).

    Intent:
        - The main app must remain responsive on every rerun (no `sleep()` polling).
        - GPU prewarm must not delay engine warmup readiness.
        - When the thread confirms VRAM usage (`/api/ps size_vram > 0`), it flips
          `st.session_state.gpu_status` to `active` and requests a rerun so the
          sidebar badge turns 🟢 without manual interaction.

    Returns:
        None. This is a best-effort optimization; failures keep the UI usable on CPU.
    """
    # Session-level guard: don't spawn multiple prewarm threads per browser session.
    if st.session_state.get("_gpu_prewarm_thread_started", False):
        return
    st.session_state["_gpu_prewarm_thread_started"] = True

    # If we don't have the Streamlit script context utilities, fall back to the
    # existing probe + UI rerun mechanisms (no background session_state writes).
    if add_script_run_ctx is None or get_script_run_ctx is None:
        return

    ctx = get_script_run_ctx()
    if ctx is None:
        return

    def _runner() -> None:
        try:
            # Always start in warming unless already active.
            if st.session_state.get("gpu_status") != "active":
                st.session_state.gpu_status = "warming"

            _prewarm_ollama_model_for_gpu()

            # Confirm via the cached probe (which reads /api/ps) and flip state.
            ok, _name = _check_gpu()
            if ok:
                st.session_state.gpu_status = "active"
                # One-shot rerun so the sidebar badge flips immediately.
                st.rerun()
            else:
                if st.session_state.get("gpu_status") != "active":
                    st.session_state.gpu_status = "inactive"
        except Exception as exc:
            logger.debug("GPU prewarm thread failed: %s", exc)

    t = threading.Thread(target=_runner, name="gpu-prewarm", daemon=True)
    add_script_run_ctx(t, ctx)
    t.start()


def _engine_is_ready(db_path: str, llm_mode: str, brain_id: str) -> bool:
    """True when `get_engine` completed successfully for this mode.

    Checks `_shared_state()["built"]` — a persistent set written inside
    `get_engine` that survives across all Streamlit reruns. Falls back to
    `event.is_set()` for the short window between thread start and first cache miss
    completion (only relevant on the very first request after process start).
    """
    state = _shared_state()
    key = _engine_cache_key(db_path, llm_mode, brain_id)
    if key in state["built"]:
        return True
    event = state["events"].get(key)
    return event is not None and event.is_set() and key not in state["errors"]


def _ensure_heartbeat_service(db_path: str, llm_mode: str, brain_id: str) -> None:
    """Run a lightweight readiness heartbeat for one engine cache key."""
    state = _shared_state()
    key = _engine_cache_key(db_path, llm_mode, brain_id)
    with state["lock"]:
        if key in state["heartbeat_threads"]:
            return
        state["heartbeat_threads"].add(key)
        state["heartbeat"].setdefault(key, False)
        state["heartbeat_ts"][key] = time.monotonic()

    def _runner() -> None:
        while True:
            ready_now = _engine_is_ready(db_path, llm_mode, brain_id)
            with state["lock"]:
                state["heartbeat"][key] = ready_now
                state["heartbeat_ts"][key] = time.monotonic()
                errored = key in state["errors"]
            if ready_now or errored:
                return
            time.sleep(0.4)

    threading.Thread(
        target=_runner,
        name=f"heartbeat-{llm_mode}-{_normalize_brain_id(brain_id)}",
        daemon=True,
    ).start()


def _heartbeat_ready(db_path: str, llm_mode: str, brain_id: str) -> bool:
    """Return heartbeat-ready status for UI gating."""
    key = _engine_cache_key(db_path, llm_mode, brain_id)
    _ensure_heartbeat_service(db_path, llm_mode, brain_id)
    state = _shared_state()
    with state["lock"]:
        if state["heartbeat"].get(key, False):
            return True
    return _engine_is_ready(db_path, llm_mode, brain_id)


def _reset_warmup(db_path: str, llm_mode: str, brain_id: str) -> None:
    """Clear warmup tracking for one `(db_path, llm_mode, brain)` tuple.

    Removes entries from `events`, `errors`, AND `built` so the next
    `_ensure_warmup` call starts a fresh background build and the status card
    transitions through Loading → Ready correctly after a re-ingest.
    """
    state = _shared_state()
    key = _engine_cache_key(db_path, llm_mode, brain_id)
    with state["lock"]:
        state["events"].pop(key, None)
        state["errors"].pop(key, None)
        state["built"].discard(key)
        state["heartbeat"].pop(key, None)
        state["heartbeat_threads"].discard(key)
        state["heartbeat_ts"].pop(key, None)
        state["phase_pct"].pop(key, None)


def _reset_warmup_all() -> None:
    """Clear all warmup tracking entries after cross-brain cache invalidation."""
    state = _shared_state()
    with state["lock"]:
        state["events"].clear()
        state["errors"].clear()
        state["built"].clear()
        state["heartbeat"].clear()
        state["heartbeat_threads"].clear()
        state["heartbeat_ts"].clear()
        state["phase_pct"].clear()


def _kickoff_process_prewarm() -> None:
    """Launch a one-time process-level warmup thread for default brain/mode."""
    state = _shared_state()
    with state["lock"]:
        if state.get("prewarm_started"):
            return
        state["prewarm_started"] = True

    def _runner() -> None:
        try:
            _ensure_warmup("db", "efficiency", DEFAULT_BRAIN_ID)
        except Exception as exc:
            logger.debug("Process prewarm trigger failed: %s", exc)

    threading.Thread(target=_runner, name="process-prewarm", daemon=True).start()


def _source_filename(source_path: str | None) -> str:
    """Backward-compatible wrapper around `core.utils.source_filename`."""
    return core_source_filename(source_path)


def _normalize_stored_citation(citation: str) -> str:
    """Backward-compatible wrapper around `core.utils.normalize_stored_citation`."""
    return core_normalize_stored_citation(citation)


def _verified_source_html(citation: str, excerpt: str, *, score_label: str = "") -> str:
    """Render one verified source card as safe HTML (collapsible excerpt).

    Args:
        citation: Human-readable line such as `file.pdf (Page N)` or `file.pdf (Pages 12–15)`.
        excerpt: Short text snippet; may be empty.

    Returns:
        HTML fragment using `src-item` and `src-excerpt` CSS classes.
    """
    cit_esc = html.escape(citation)
    score_esc = html.escape(score_label) if score_label else ""
    excerpt_block = ""
    if excerpt:
        ex_esc = html.escape(excerpt)
        excerpt_block = (
            "<details>"
            "<summary>Excerpt</summary>"
            f'<div class="src-excerpt">&ldquo;{ex_esc}&hellip;&rdquo;</div>'
            "</details>"
        )
    return (
        f'<div class="src-item">'
        f'<div class="src-citation">\U0001F4D6 {cit_esc} {score_esc}</div>'
        f"{excerpt_block}"
        "</div>"
    )


def _render_verified_sources_expander(sources: list[dict[str, Any]] | None) -> None:
    """Show retrieved citations in a collapsible expander below an assistant message.

    Args:
        sources: List of dicts with `citation` and `excerpt` from `main`.
    """
    normalized_sources = sources or []
    with st.expander("\U0001F4DA  View Verified Sources"):
        if not normalized_sources:
            st.caption("No verified source chunks were retrieved for this answer.")
            return
        # Compact consecutive pages from the same PDF into a single entry.
        grouped: list[dict[str, Any]] = []
        for s in normalized_sources:
            raw_citation = s["citation"] if isinstance(s, dict) else str(s)
            citation = _normalize_stored_citation(raw_citation)
            excerpt = s.get("excerpt", "") if isinstance(s, dict) else ""
            sim = s.get("similarity_score", None) if isinstance(s, dict) else None
            rr = s.get("rerank_score", None) if isinstance(s, dict) else None

            m = re.match(r"^(?P<file>.+?)\s+\(Page\s+(?P<page>\d+)\)$", citation)
            if not m:
                grouped.append({"citation": citation, "excerpt": excerpt, "file": None, "page": None, "sim": sim, "rr": rr})
                continue
            file_ = m.group("file").strip()
            page = int(m.group("page"))
            grouped.append({"citation": citation, "excerpt": excerpt, "file": file_, "page": page, "sim": sim, "rr": rr})

        out: list[dict[str, Any]] = []
        i = 0
        while i < len(grouped):
            row = grouped[i]
            file_, page = row.get("file"), row.get("page")
            if not file_ or page is None:
                out.append({"citation": row["citation"], "excerpt": row.get("excerpt", ""), "sim": row.get("sim", None), "rr": row.get("rr", None)})
                i += 1
                continue
            start = page
            end = page
            excerpt = row.get("excerpt", "")
            sim = row.get("sim", None)
            rr = row.get("rr", None)
            j = i + 1
            while j < len(grouped) and grouped[j].get("file") == file_ and isinstance(grouped[j].get("page"), int) and grouped[j]["page"] == end + 1:
                gj_sim = grouped[j].get("sim", None)
                gj_rr = grouped[j].get("rr", None)
                try:
                    if gj_sim is not None:
                        sim = max(float(sim or 0.0), float(gj_sim))
                except (TypeError, ValueError):
                    pass
                try:
                    if gj_rr is not None:
                        rr = max(float(rr or 0.0), float(gj_rr))
                except (TypeError, ValueError):
                    pass
                end = grouped[j]["page"]
                j += 1
            if end > start:
                cit = f"{file_} (Pages {start}–{end})"
            else:
                cit = f"{file_} (Page {start})"
            out.append({"citation": cit, "excerpt": excerpt, "sim": sim, "rr": rr})
            i = j

        out.sort(
            key=lambda row: (
                float(row.get("rr", 0.0) or 0.0),
                float(row.get("sim", 0.0) or 0.0),
            ),
            reverse=True,
        )

        for s in out:
            sim = s.get("sim", None)
            rr = s.get("rr", None)
            score_label = ""
            try:
                if sim is not None and rr is not None:
                    score_label = f"[Score: {float(sim):.2f} | Rerank: {float(rr):.2f}]"
                elif sim is not None:
                    score_label = f"[Score: {float(sim):.2f}]"
                elif rr is not None:
                    score_label = f"[Rerank: {float(rr):.2f}]"
            except (TypeError, ValueError):
                score_label = ""
            st.markdown(
                _verified_source_html(s["citation"], s.get("excerpt", ""), score_label=score_label),
                unsafe_allow_html=True,
            )


# ===================================================================
# IMMEDIATE UI RENDER — set_page_config is the first st.* call, then
# CSS is injected before any heavy work so the layout never flashes.
# ===================================================================
_profile("st.set_page_config")
_global_runtime_singleton()
_kickoff_process_prewarm()

# Dispatch engine construction to a background thread (non-blocking).
_brains_at_boot = _list_brains()
if st.session_state.brain_id not in _brains_at_boot:
    st.session_state.brain_id = _brains_at_boot[0]
_upsert_brain_metadata(st.session_state.brain_id)
_mode = st.session_state.llm_mode
_ensure_warmup("db", _mode, st.session_state.brain_id)
_ensure_heartbeat_service("db", _mode, st.session_state.brain_id)
_profile("background warmup dispatched")

_boot_key = _engine_cache_key("db", st.session_state.llm_mode, st.session_state.brain_id)
_boot_pct = int(_shared_state().get("phase_pct", {}).get(_boot_key, 0))
_boot_ready = _heartbeat_ready("db", st.session_state.llm_mode, st.session_state.brain_id)
if (not _boot_ready) and _boot_pct < 10 and _boot_key not in _shared_state().get("errors", {}):
    _boot_slot = st.empty()
    _boot_slot.markdown(
        (
            "<div style='text-align:center;padding:20vh 1rem 0;'>"
            "<h2 style='margin-bottom:0.5rem;'>System Booting</h2>"
            "<p style='opacity:0.8;'>Paying the startup import tax in the background…</p>"
            f"<p style='opacity:0.7;'>Warmup progress: {_boot_pct}%</p>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )
    time.sleep(0.35)
    st.rerun()

_LEGACY_CSS = """
<style>
/* (deprecated) CSS injected early in v2.1.8; kept no-op */
/* ---------- global ---------- */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');

:root {
    --gold:   #d4a843;
    --parch:  #faf3e0;
    --ink:    #1e1e1e;
    --accent: #7b2d26;
    --muted:  #6b6b6b;
    --card:   #ffffff;
    --border: #e0d5c1;
}

section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #1a1207 0%, #2a1f0e 100%);
    min-width: 360px;
    width: 360px;
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, Arial, sans-serif;
    font-size: 15.5px;
    line-height: 1.45;
}
section[data-testid="stSidebar"] * {
    color: #e8dcc8 !important;
}

/* Preserve Streamlit's Material icon font (fixes "keyboard_double_a…" artifacts). */
section[data-testid="stSidebar"] span[class*="material-symbols"] {
    font-family: 'Material Symbols Rounded' !important;
    font-size: 20px !important;
    line-height: 1 !important;
}

/* General text elements (avoid forcing font-size on widgets/buttons). */
section[data-testid="stSidebar"] p,
section[data-testid="stSidebar"] li,
section[data-testid="stSidebar"] label,
section[data-testid="stSidebar"] small,
section[data-testid="stSidebar"] .stMarkdown,
section[data-testid="stSidebar"] .stCaption {
    font-family: 'Inter', 'Segoe UI', system-ui, -apple-system, Arial, sans-serif !important;
}

/* ---------- sidebar ---------- */
.sidebar-brand {
    text-align: center;
    padding: 1.2rem 0 0.6rem;
}
.sidebar-brand h2 {
    color: var(--gold) !important;
    margin: 0;
    font-size: 1.15rem;
    letter-spacing: 0.4px;
    font-weight: 650;
}
.sidebar-brand .tagline {
    font-size: 0.86rem;
    color: #a89878 !important;
    margin-top: 2px;
}

.sidebar-meta {
    font-size: 0.84rem;
    line-height: 1.35;
    padding: 0.25rem 0.1rem 0.6rem;
}
.meta-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    margin: 2px 0;
}
.meta-key {
    color: #a89878 !important;
    flex: 0 0 auto;
}
.meta-val {
    flex: 1 1 auto;
    text-align: right;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.sidebar-status {
    padding: 0.35rem 0.15rem 0.55rem;
    font-size: 0.92rem;
    color: #d9ccb4 !important;
}
.sidebar-status .muted {
    color: #a89878 !important;
}
.sidebar-status code {
    background: rgba(255,255,255,0.06) !important;
    border: 1px solid rgba(212,168,67,0.18) !important;
    padding: 1px 6px !important;
    border-radius: 999px !important;
    font-size: 0.84rem !important;
    color: #e8dcc8 !important;
}

/* v2.1.5: The legacy "sidebar-status" block is kept for compatibility but not used. */
.sidebar-status { display: none; }
.danger-actions [data-testid="stButton"] button {
    border-radius: 10px !important;
}
.danger-actions [data-testid="stButton"] button[data-testid="baseButton-secondary"] {
    border: 1px solid rgba(212,168,67,0.28) !important;
    background: rgba(255,255,255,0.04) !important;
}
.danger-actions [data-testid="stButton"] button[data-testid="baseButton-secondary"]:hover {
    border-color: rgba(212,168,67,0.55) !important;
    background: rgba(212,168,67,0.08) !important;
}
.danger-actions [data-testid="stButton"] button[data-testid="baseButton-primary"] {
    background: #ff4d4d !important;
    border: 1px solid #ff4d4d !important;
    color: #1a1207 !important;
    font-weight: 650 !important;
}

/* ---------- hero / welcome ---------- */
.hero {
    text-align: center;
    /* Keep the cold-start screen compact so suggestion pills sit near chat input. */
    padding: 1.8rem 1rem 0.9rem;
}
.hero-icon { font-size: 2.4rem; }
.hero h1 {
    color: var(--ink);
    font-size: 2rem;
    margin: 0.4rem 0 0.3rem;
}
.hero p {
    color: var(--muted);
    max-width: 460px;
    margin: 0 auto;
    font-size: 0.95rem;
    line-height: 1.55;
}
/* suggestion blobs — larger "tile" buttons used for quick prompts */
.suggestion-blobs [data-testid="stButton"] button[data-testid="baseButton-secondary"] {
    border-radius: 14px !important;
    font-size: 0.9rem !important;
    padding: 10px 14px !important;
    background: rgba(255,255,255,0.04) !important;
    border: 1px solid rgba(212,168,67,0.35) !important;
    color: inherit !important;
    white-space: normal !important;
    width: 100% !important;
    min-height: 46px !important;
}
.suggestion-blobs [data-testid="stButton"] button[data-testid="baseButton-secondary"]:hover {
    background: rgba(212,168,67,0.10) !important;
    border-color: var(--gold) !important;
}
.suggestion-blobs [data-testid="stButton"] {
    width: 100% !important;
}

/* draft edit bar that replaces st.chat_input when a hint is pending */
.draft-bar [data-testid="stTextInput"] input {
    border-radius: 8px !important;
    font-size: 0.95rem !important;
}

/* ---------- source expander ---------- */
.src-item {
    border-left: 3px solid var(--gold);
    border-radius: 4px;
    padding: 8px 12px;
    margin-bottom: 8px;
    background: rgba(212,168,67,0.07);
}
.src-citation {
    font-size: 0.82rem;
    font-weight: 600;
    color: inherit;
    margin-bottom: 4px;
}
.src-excerpt {
    font-size: 0.78rem;
    opacity: 0.75;
    font-style: italic;
    line-height: 1.45;
    color: inherit;
}
.src-item details summary {
    cursor: pointer;
    font-size: 0.78rem;
    opacity: 0.85;
    margin-top: 4px;
}

/* ---------- file uploader: shrink only the invalid-file error pill ---------- */
[data-testid="stFileUploaderFile"] {
    padding: 4px 8px !important;
    font-size: 0.75rem !important;
    line-height: 1.3 !important;
}
[data-testid="stFileUploaderFile"] svg {
    width: 14px !important;
    height: 14px !important;
}

/* ---------- management section ---------- */
.mgmt-header {
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: #a89878 !important;
    margin: 0.2rem 0 0.6rem;
}
.queued-file {
    display: flex;
    align-items: center;
    gap: 6px;
    background: rgba(255,255,255,0.05);
    border-radius: 6px;
    padding: 5px 9px;
    font-size: 0.8rem;
    margin-bottom: 4px;
    color: #e8dcc8 !important;
    word-break: break-all;
}

/* ---------- model tier (Cursor-style selector) ---------- */
.model-tier-caption {
    font-size: 0.72rem !important;
    line-height: 1.4;
    color: #b8a88c !important;
    margin: 0.15rem 0 0.85rem;
    padding: 0 0.15rem;
}
section[data-testid="stSidebar"] div[data-testid="stRadio"] label {
    font-weight: 500 !important;
    font-size: 0.88rem !important;
}

/* Consistent vertical rhythm (prevents overlap when fonts scale). */
section[data-testid="stSidebar"] [data-testid="stExpander"] {
    margin: 0.35rem 0 0.5rem;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary {
    padding: 0.35rem 0.15rem !important;
}
section[data-testid="stSidebar"] [data-testid="stExpander"] summary p {
    margin: 0 !important;
}
section[data-testid="stSidebar"] [data-testid="stButton"],
section[data-testid="stSidebar"] [data-testid="stSelectbox"],
section[data-testid="stSidebar"] [data-testid="stRadio"],
section[data-testid="stSidebar"] [data-testid="stTextInput"] {
    margin-bottom: 0.45rem;
}
</style>
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _save_history(messages: list[dict[str, Any]]) -> None:
    """Serialize chat messages to `chat_history.json` for reload across sessions.

    Args:
        messages: Session message list including `role`, `content`, optional `sources`.

    Raises:
        None intentionally; failures surface as Streamlit toasts only.
    """
    try:
        HISTORY_FILE.write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        # Log but don't crash — chat still works without persistence
        st.toast(f"⚠️ Could not save history: {exc}", icon="⚠️")


def _load_history() -> list[dict[str, Any]]:
    """Load prior messages from disk if the JSON file is valid.

    Returns:
        A list of message dicts, or an empty list on any parse or schema error.
    """
    try:
        if HISTORY_FILE.exists():
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def _wipe_history() -> None:
    """Reset session messages and remove the persisted history file.

    Raises:
        None intentionally; deletion errors are toasted, not raised.
    """
    st.session_state.messages = []
    # Clear any queued UI work so a rerun can't resurrect stale prompts.
    st.session_state.pending_query = None
    st.session_state.pending_draft = None
    st.session_state.pop("_pending_assistant_query", None)
    try:
        HISTORY_FILE.unlink(missing_ok=True)
    except Exception as exc:
        st.toast(f"⚠️ Could not delete history file: {exc}", icon="⚠️")


def _destroy_brain_and_reset(destroy_target: str, available_brains: list[str]) -> None:
    """Delete one brain archive and refresh warmup/cache state.

    Args:
        destroy_target: Brain id to remove.
        available_brains: Current sidebar brain list before deletion.

    Raises:
        None intentionally; directory deletion uses `ignore_errors=True`.
    """
    shutil.rmtree(_brain_db_dir(destroy_target), ignore_errors=True)
    shutil.rmtree(_brain_data_dir(destroy_target), ignore_errors=True)
    _delete_brain_metadata(destroy_target)
    get_engine.clear()
    _reset_warmup_all()
    for mode_name in ("efficiency", "intelligence"):
        st.session_state.pop(f"_engine_shown_ready:{destroy_target}:{mode_name}", None)
    if st.session_state.brain_id == destroy_target:
        remaining = [b for b in available_brains if b != destroy_target]
        st.session_state.brain_id = remaining[0]
    st.session_state.pending_uploads = {}
    st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1
    _upsert_brain_metadata(st.session_state.brain_id, touch_last_used=True)
    _ensure_warmup("db", st.session_state.llm_mode, st.session_state.brain_id)
    _ensure_heartbeat_service("db", st.session_state.llm_mode, st.session_state.brain_id)


def _history_for_keeper(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Map Streamlit message records to LangChain-compatible (role, content) tuples.

    Args:
        messages: Session history with string `role` and `content` fields.

    Returns:
        Ordered chat turns suitable for `main.LoreKeeper` prompts.
    """
    return [
        (m["role"], m["content"])
        for m in messages
        if m.get("role") in ("human", "assistant") and m.get("content", "").strip()
    ]


def _collect_bibliography(messages: list[dict[str, Any]]) -> list[str]:
    """Collect unique source citation strings from assistant turns."""
    seen: set[str] = set()
    ordered: list[str] = []
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for row in msg.get("sources") or []:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("citation") or "").strip()
            if not raw:
                continue
            norm = _normalize_stored_citation(raw)
            if norm not in seen:
                seen.add(norm)
                ordered.append(norm)
    return ordered


def _build_research_markdown(
    *,
    messages: list[dict[str, Any]],
    brain_id: str,
    edition_filter: str,
) -> str:
    """Build a markdown research export from the current chat session."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    sections: list[str] = []
    sections.append("# Lore Keeper Research Export")
    sections.append("")
    sections.append(f"- **Generated:** {ts}")
    sections.append(f"- **Brain:** `{brain_id}`")
    sections.append(f"- **Ruleset Filter:** `{edition_filter}`")
    sections.append("")
    sections.append("## Key Findings")
    sections.append("")

    pair_idx = 1
    pending_question: Optional[str] = None
    for msg in messages:
        role = msg.get("role")
        content = str(msg.get("content") or "").strip()
        if role == "human":
            pending_question = content
            continue
        if role != "assistant":
            continue
        answer = _strip_inline_source_tags(content)
        answer = _normalize_not_found_disclaimer(answer)
        answer = answer.replace("\n", " ").strip()
        if len(answer) > 340:
            answer = answer[:337].rstrip() + "..."
        q = pending_question or "Research query"
        sections.append(f"### Finding {pair_idx}")
        sections.append(f"- **Question:** {q}")
        sections.append(f"- **Answer Summary:** {answer or 'No answer text recorded.'}")
        sources = msg.get("sources") or []
        if sources:
            sections.append("- **Evidence Count:** " + str(len(sources)))
        sections.append("")
        pair_idx += 1
        pending_question = None

    bibliography = _collect_bibliography(messages)
    sections.append("## Bibliography")
    sections.append("")
    if bibliography:
        for item in bibliography:
            sections.append(f"- {item}")
    else:
        sections.append("- No verified source citations were captured in this session.")
    sections.append("")
    return "\n".join(sections)


def _extract_general_knowledge_warning(answer_text: str) -> tuple[Optional[str], str]:
    """Split model output into warning prefix and body.

    Args:
        answer_text: Raw assistant output text.

    Returns:
        Tuple of (warning_message_or_none, answer_without_warning_prefix).
    """
    stripped = (answer_text or "").lstrip()
    if stripped.startswith(GENERAL_KNOWLEDGE_WARNING):
        body = stripped[len(GENERAL_KNOWLEDGE_WARNING) :].lstrip("\n\r ")
        return GENERAL_KNOWLEDGE_WARNING, body
    soft = LOW_RELEVANCE_SOFT_WARNING
    if soft and stripped.startswith(soft):
        body = stripped[len(soft) :].lstrip("\n\r ")
        return soft, body
    return None, answer_text


def _normalize_not_found_disclaimer(answer_text: str) -> str:
    """Drop trailing negative disclaimer when contextual content exists.

    If the whole answer is exactly the fallback sentence, keep it unchanged.
    Otherwise remove standalone lines matching the fallback.
    """
    fallback = "Information not found in provided text."
    raw = (answer_text or "").strip()
    if raw == fallback:
        return raw
    lines = [ln for ln in (answer_text or "").splitlines() if ln.strip() != fallback]
    return "\n".join(lines).strip()


def _source_filenames_from_source_dicts(sources: list[dict[str, Any]]) -> set[str]:
    """Return normalized filenames from retrieved source citation rows."""
    names: set[str] = set()
    for row in sources or []:
        if not isinstance(row, dict):
            continue
        raw_cit = str(row.get("citation", "")).strip()
        if not raw_cit:
            continue
        normalized = _normalize_stored_citation(raw_cit)
        pivot = normalized.rfind(" (Page ")
        if pivot <= 0:
            pivot = normalized.rfind(" (Pages ")
        file_part = normalized[:pivot] if pivot > 0 else normalized
        name = _source_filename(file_part)
        if name and name != "Unknown Archive":
            names.add(name)
    return names


def _citation_hallucination_tags(answer_text: str, sources: list[dict[str, Any]]) -> list[str]:
    """Find `[Source: ...]` tags that do not match retrieved filenames."""
    valid_names = _source_filenames_from_source_dicts(sources)
    claimed = {m.group(1).strip() for m in _SOURCE_TAG_RE.finditer(answer_text or "")}
    if not claimed:
        return []
    if not valid_names:
        return sorted(claimed)
    bad = []
    for c in claimed:
        normalized = _source_filename(c)
        if normalized not in valid_names:
            bad.append(c)
    return sorted(set(bad))


def _strip_inline_source_tags(answer_text: str) -> str:
    """Remove inline `[Source: ...]` tags from assistant message body."""
    return _SOURCE_TAG_RE.sub("", answer_text or "").strip()


@st.dialog("🗑️ Clear Conversation?")
def _confirm_clear_dialog() -> None:
    """Render a confirmation modal for clearing chat and deleting history on disk.

    Raises:
        None intentionally; Streamlit controls reruns via callbacks.
    """
    st.markdown("This removes **all messages** and deletes `chat_history.json` from disk.")
    col_yes, col_no = st.columns(2)
    with col_yes:
        if st.button("Yes, clear it", type="primary", use_container_width=True):
            _wipe_history()
            st.rerun()
    with col_no:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


@st.dialog("🧠 Destroy Brain?")
def _confirm_destroy_brain_dialog(destroy_target: str) -> None:
    """Render a confirmation modal before permanently deleting a brain."""
    brains_now = _list_brains()
    if destroy_target not in brains_now:
        st.info(f"Brain '{destroy_target}' was already removed.")
        if st.button("Close", use_container_width=True):
            st.rerun()
        return

    st.markdown(
        f"This permanently deletes **{destroy_target}** from `db/` and `data/`, including all indexed vectors."
    )
    can_destroy = len(brains_now) > 1
    if not can_destroy:
        st.error("At least one default brain must always remain in the vault.")

    col_yes, col_no = st.columns(2)
    with col_yes:
        if st.button(
            "Yes, destroy brain",
            type="primary",
            use_container_width=True,
            disabled=not can_destroy,
        ):
            _destroy_brain_and_reset(destroy_target, brains_now)
            st.success(f"Brain '{destroy_target}' destroyed.")
            st.rerun()
    with col_no:
        if st.button("Cancel", use_container_width=True):
            st.rerun()


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown(
        '<div class="sidebar-brand">'
        f'<h2>\U0001F4DC {PRODUCTION_HEADER}</h2>'
        '<div class="tagline">Production D&D Archivist</div>'
        '</div>',
        unsafe_allow_html=True,
    )

    _available_brains = _list_brains()
    if st.session_state.brain_id not in _available_brains:
        st.session_state.brain_id = _available_brains[0]

    _wk = _engine_cache_key("db", st.session_state.llm_mode, st.session_state.brain_id)
    _wk_errors = _shared_state()["errors"]
    _engine_ready = _heartbeat_ready("db", st.session_state.llm_mode, st.session_state.brain_id)
    _gpu_state_key, _gpu_html, _gpu_ok, _gpu_name = _gpu_badge_state(engine_ready=_engine_ready)

    _engine_state = "Online" if _engine_ready else ("Error" if _wk in _wk_errors else "Loading")
    _gpu_state = "Active" if _gpu_state_key == "active" else ("Searching" if _gpu_state_key == "searching" else "Inactive")
    _mode_label = "Auto Efficiency" if (st.session_state.llm_mode or "efficiency") == "efficiency" else "Premium Intelligence"

    st.markdown(
        "\n".join([
            f"Status: **{_engine_state}** | v {APP_VERSION}",
            "",
            f"Engine: Hybrid + FlashRank | GPU: **{_gpu_state}**",
            "",
            f"Brain: `{st.session_state.brain_id}` | Filter: `{st.session_state.edition_filter}`",
            "",
            f"Mode: **{_mode_label}**",
        ])
    )

    with st.expander("🧠 Brain Vault", expanded=False):
        _selected_brain = st.selectbox(
            "Select Brain",
            options=_available_brains,
            index=_available_brains.index(st.session_state.brain_id),
        )
        if _selected_brain != st.session_state.brain_id:
            previous_brain = st.session_state.brain_id
            st.session_state.brain_id = _selected_brain
            _upsert_brain_metadata(_selected_brain, touch_last_used=True)
            get_engine.clear()
            _reset_warmup_all()
            st.session_state.pending_uploads = {}
            st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1
            for mode_name in ("efficiency", "intelligence"):
                st.session_state.pop(f"_engine_shown_ready:{previous_brain}:{mode_name}", None)
                st.session_state.pop(f"_engine_shown_ready:{_selected_brain}:{mode_name}", None)
            _ensure_warmup("db", st.session_state.llm_mode, st.session_state.brain_id)
            _ensure_heartbeat_service("db", st.session_state.llm_mode, st.session_state.brain_id)
            st.rerun()

        _new_brain_name = st.text_input(
            "Brain Name",
            value="",
            key="brain_name_input",
            placeholder="e.g., cs_degree",
        )
        if st.button("Create Brain", use_container_width=True):
            if not (_new_brain_name or "").strip():
                st.warning("Enter a brain name.")
            else:
                new_brain = _normalize_brain_id(_new_brain_name)
                _brains_fresh = _list_brains()
                if new_brain in _brains_fresh:
                    st.warning(f"Brain '{new_brain}' already exists.")
                else:
                    _brain_db_dir(new_brain).mkdir(parents=True, exist_ok=True)
                    _brain_data_dir(new_brain).mkdir(parents=True, exist_ok=True)
                    _upsert_brain_metadata(new_brain, touch_last_used=True)
                    st.session_state.brain_id = new_brain
                    get_engine.clear()
                    _reset_warmup_all()
                    st.session_state.pending_uploads = {}
                    st.session_state.uploader_key = st.session_state.get("uploader_key", 0) + 1
                    for mode_name in ("efficiency", "intelligence"):
                        st.session_state.pop(f"_engine_shown_ready:{new_brain}:{mode_name}", None)
                    _ensure_warmup("db", st.session_state.llm_mode, st.session_state.brain_id)
                    _ensure_heartbeat_service("db", st.session_state.llm_mode, st.session_state.brain_id)
                    st.success(f"Brain '{new_brain}' created.")
                    st.rerun()

    with st.expander("⚡ Model", expanded=False):
        st.caption("How should answers be generated?")
        _tier_labels = {"efficiency": "Auto Efficiency", "intelligence": "Premium Intelligence"}
        _picked = st.radio(
            "Model tier",
            options=["efficiency", "intelligence"],
            index=0 if st.session_state.llm_mode == "efficiency" else 1,
            format_func=lambda m: _tier_labels[m],
            label_visibility="collapsed",
        )
        _llm_options = ["llama3:8b-instruct-q4_K_M", "llama3:latest"]
        if st.session_state.active_model not in _llm_options:
            st.session_state.active_model = _llm_options[0]
        if _picked == "efficiency":
            st.markdown("**Active LLM**")
            _model_pick = st.selectbox(
                "Active LLM",
                options=_llm_options,
                index=_llm_options.index(st.session_state.active_model),
                label_visibility="collapsed",
            )
            st.markdown(
                '<div style="font-size:0.9rem;opacity:0.82;margin-top:-0.15rem;">Local Ollama model</div>',
                unsafe_allow_html=True,
            )
            if _model_pick != st.session_state.active_model:
                _maybe_auto_memory_reclaim()
                st.session_state.active_model = _model_pick
                os.environ["OLLAMA_CHAT_MODEL"] = _model_pick
                get_engine.clear()
                _reset_warmup_all()
                for mode_name in ("efficiency", "intelligence"):
                    st.session_state.pop(
                        f"_engine_shown_ready:{st.session_state.brain_id}:{mode_name}",
                        None,
                    )
                _ensure_warmup("db", "efficiency", st.session_state.brain_id)
                _ensure_heartbeat_service("db", "efficiency", st.session_state.brain_id)
                st.rerun()
        else:
            st.caption("Provider: GPT-5.4")
        if _picked != st.session_state.llm_mode:
            _maybe_auto_memory_reclaim()
            st.session_state.llm_mode = _picked
            os.environ["OLLAMA_CHAT_MODEL"] = st.session_state.active_model
            get_engine.clear()
            _reset_warmup_all()
            for mode_name in ("efficiency", "intelligence"):
                st.session_state.pop(
                    f"_engine_shown_ready:{st.session_state.brain_id}:{mode_name}",
                    None,
                )
            _ensure_warmup("db", _picked, st.session_state.brain_id)
            _ensure_heartbeat_service("db", _picked, st.session_state.brain_id)
            st.rerun()

        st.caption("Engine warms in the background at page load.")

        st.markdown("**Retrieval**")
        st.session_state.multi_query_enabled = st.checkbox(
            "Multi-query expansion",
            value=bool(st.session_state.multi_query_enabled),
            help="When enabled, the Lore Keeper generates up to 3 semantic query variants and merges their BM25 + vector hits with Reciprocal Rank Fusion before reranking (better coverage, sometimes more latency).",
        )
        st.session_state.context_expansion_enabled = st.checkbox(
            "Context Expansion (Page +/-)",
            value=bool(st.session_state.get("context_expansion_enabled", False)),
            help="Adds neighboring pages when confidence is high. Increases detail but takes longer.",
        )

    st.markdown("")
    st.markdown('<div class="mgmt-header">📚 Ruleset Filter</div>', unsafe_allow_html=True)
    _edition_options = ["All", "2014", "2024"]
    if st.session_state.edition_filter not in _edition_options:
        st.session_state.edition_filter = "All"
    st.session_state.edition_filter = st.selectbox(
        "Ruleset Edition",
        options=_edition_options,
        index=_edition_options.index(st.session_state.edition_filter),
    )

    with st.expander("☢️ Danger Zone", expanded=False):
        st.markdown('<div class="danger-actions">', unsafe_allow_html=True)
        if st.button("\U0001F5D1\uFE0F  Clear Conversation", type="secondary", use_container_width=True):
            _confirm_clear_dialog()
        _destroy_target = st.selectbox(
            "Brain to destroy",
            options=_available_brains,
            index=_available_brains.index(st.session_state.brain_id),
        )
        if st.button("Destroy Brain", type="primary", use_container_width=True):
            _confirm_destroy_brain_dialog(_destroy_target)
        st.markdown("</div>", unsafe_allow_html=True)

    # ── Upload Knowledge ────────────────────────────────────────────────────
    # uploader_key is incremented after processing to reset the widget (clears the UI)
    if "uploader_key" not in st.session_state:
        st.session_state.uploader_key = 0
    # pending_uploads holds UploadedFile objects in memory until Process is clicked
    if "pending_uploads" not in st.session_state:
        st.session_state.pending_uploads = {}
    _active_brain_data_dir = _brain_data_dir(st.session_state.brain_id)
    _active_brain_db_dir = _brain_db_dir(st.session_state.brain_id)
    with st.popover("📤 Upload Knowledge", use_container_width=True):
        uploaded_files = st.file_uploader(
            "Add PDFs to the knowledge base",
            type="pdf",
            accept_multiple_files=True,
            label_visibility="collapsed",
            key=f"uploader_{st.session_state.brain_id}_{st.session_state.uploader_key}",
        )

        # Buffer newly selected files in session state (don't touch disk yet)
        if uploaded_files:
            for uf in uploaded_files:
                st.session_state.pending_uploads[uf.name] = uf.getvalue()

        # Show queued files list
        if st.session_state.pending_uploads:
            st.markdown(
                "".join(
                    f'<div class="queued-file">📄 {name}</div>'
                    for name in st.session_state.pending_uploads
                ),
                unsafe_allow_html=True,
            )
            st.warning(
                "Edition tags are applied during ingestion. Re-ingest older PDFs in this brain "
                "indexed before v1.8.1 if you want edition filtering to work on legacy chunks."
            )

        n_queued = len(st.session_state.pending_uploads)
        btn_label = f"⚙️  Process {n_queued} File{'s' if n_queued != 1 else ''}"
        process_btn = st.button(btn_label, use_container_width=True, disabled=n_queued == 0)

        if process_btn:
            from ingest import LoreIngestor

            status_text = st.empty()
            progress_bar = st.progress(0)

            try:
                ok, msg = _preflight_llm_ok("intelligence")
                if not ok:
                    st.warning(msg)
                    render_error_state("preflight", msg)
                    raise RuntimeError(msg)
                # Write buffered files to data/ right before ingestion
                _active_brain_data_dir.mkdir(parents=True, exist_ok=True)
                _active_brain_db_dir.mkdir(parents=True, exist_ok=True)
                for filename, file_bytes in st.session_state.pending_uploads.items():
                    (_active_brain_data_dir / filename).write_bytes(file_bytes)

                ingestor = LoreIngestor(
                    data_path=str(_active_brain_data_dir),
                    db_path=str(_active_brain_db_dir),
                )

                def on_ingestion_progress(done: int, total: int, filename: str) -> None:
                    progress_bar.progress(done / total)
                    status_text.markdown(
                        f'<span style="font-size:0.8rem;color:inherit">✔ {filename}</span>',
                        unsafe_allow_html=True,
                    )

                processed = ingestor.ingest_pdf_to_vector_store(
                    progress_callback=on_ingestion_progress
                )
                for w in getattr(ingestor, "warnings", []) or []:
                    st.warning(w)

                progress_bar.progress(1.0)
                status_text.empty()

                if processed:
                    # Clear buffer + reset uploader widget
                    st.session_state.pending_uploads = {}
                    st.session_state.uploader_key += 1
                    st.success(
                        f"✅ Ingested {processed} file{'s' if processed != 1 else ''} — reloading engine…"
                    )
                    # Drop cached retriever stack so the next run rebuilds against new vectors.
                    # Also clear the "shown ready" flag so the flip-rerun fires again after warmup.
                    get_engine.clear()
                    _reset_warmup("db", st.session_state.llm_mode, st.session_state.brain_id)
                    _upsert_brain_metadata(st.session_state.brain_id, touch_last_used=True)
                    _ensure_heartbeat_service("db", st.session_state.llm_mode, st.session_state.brain_id)
                    st.session_state.pop(
                        f"_engine_shown_ready:{st.session_state.brain_id}:{st.session_state.llm_mode}",
                        None,
                    )
                    st.rerun()
                else:
                    st.info("No new files found in data/.")

            except Exception as e:
                progress_bar.empty()
                status_text.empty()
                logger.exception("Ingestion failure: %s", e)
                render_error_state("ingest", f"Ingestion failed: {e}")

    st.markdown('<div class="mgmt-header">🧾 Export Research</div>', unsafe_allow_html=True)
    _report_md = _build_research_markdown(
        messages=st.session_state.get("messages", []),
        brain_id=st.session_state.brain_id,
        edition_filter=st.session_state.edition_filter,
    )
    _report_file = (
        f"lorekeeper_research_{st.session_state.brain_id}_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.md"
    )
    st.download_button(
        "Export Research (.md)",
        data=_report_md,
        file_name=_report_file,
        mime="text/markdown",
        use_container_width=True,
    )
    st.caption("Built with LangChain, ChromaDB & Streamlit")
    _persist_ui_settings_to_disk()
    _profile("sidebar rendered")


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
if "_health_server_started" not in st.session_state:
    from services.health_server import start_health_server_background

    start_health_server_background(db_path="db")
    st.session_state._health_server_started = True

if "messages" not in st.session_state:
    # Restore the previous session from disk (refresh-safe).
    st.session_state.messages = _load_history()
elif not isinstance(st.session_state.get("messages"), list):
    # Defensive: if some earlier run left an invalid value, repair it.
    st.session_state.messages = _load_history()

if "pending_query" not in st.session_state:
    st.session_state.pending_query = None
if "pending_draft" not in st.session_state:
    st.session_state.pending_draft = None


# ---------------------------------------------------------------------------
# UI Components
# ---------------------------------------------------------------------------
def render_welcome_hero(messages: list[dict[str, Any]]) -> None:
    """Render the welcome hero when no chat messages exist.

    Args:
        messages: Current session chat list.

    Returns:
        None. Writes Streamlit UI components directly.
    """
    if messages:
        return
    st.markdown(
        '<div class="hero">'
        '  <div class="hero-icon">\U0001F4DC</div>'
        f'  <h1>{PRODUCTION_HEADER}</h1>'
        '  <p>Your personal D&D archivist. Ask any question about rules, lore, '
        'classes, spells, or monsters and get answers backed by verified sources.</p>'
        '</div>',
        unsafe_allow_html=True,
    )


def render_chat_history(messages: list[dict[str, Any]]) -> None:
    """Render all prior chat messages and source cards.

    Args:
        messages: Ordered session chat list.

    Returns:
        None. Uses Streamlit chat containers for rendering.
    """
    for msg in messages:
        role = msg["role"]
        with st.chat_message("user" if role == "human" else "assistant"):
            if role == "assistant" and msg.get("warning_note"):
                st.warning(msg["warning_note"])
            if role == "assistant" and msg.get("citation_warning"):
                st.warning(msg["citation_warning"])
            body = msg["content"] if role == "human" else _strip_inline_source_tags(msg["content"])
            st.markdown(body)

            if role == "assistant":
                _render_verified_sources_expander(msg.get("sources") or [])


def render_suggestion_pills(suggestions: list[str], *, disabled: bool) -> None:
    """Render quick suggestion blobs above chat input.

    Args:
        suggestions: Exactly three preset question strings.
        disabled: If True, disable buttons (engine warming / responding).

    Returns:
        None. Clicking a blob fills a draft "mid-gate" with Send/Cancel controls.
    """
    st.markdown('<div class="suggestion-blobs">', unsafe_allow_html=True)
    blob_cols = st.columns(len(suggestions), gap="small")
    for idx, (col, suggestion) in enumerate(zip(blob_cols, suggestions)):
        with col:
            if st.button(
                suggestion or "Suggestion",
                key=f"suggestion_blob__{idx}__{abs(hash(suggestion)) % 10_000}",
                type="secondary",
                use_container_width=True,
                disabled=disabled,
            ):
                st.session_state.pending_draft = suggestion
                # If a mid-gate draft is already open, replace its text immediately.
                st.session_state["_draft_input"] = suggestion
                st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)


def render_draft_midgate(*, disabled: bool) -> Optional[str]:
    """Render a draft bar with Send/Cancel controls.

    Intent:
        Clicking a suggestion blob should not instantly fire a query. This mid-gate
        gives users a chance to edit the suggested text, then explicitly Send or Cancel.

    Args:
        disabled: If True, Send is disabled (engine not ready).

    Returns:
        The submitted query text on Send, otherwise None.
    """
    # If an assistant run is pending, never show the draft bar.
    if (st.session_state.get("_pending_assistant_query") or "").strip():
        return None
    draft = st.session_state.get("pending_draft")
    if not (draft or "").strip():
        return None

    st.markdown('<div class="draft-bar">', unsafe_allow_html=True)
    edited = st.text_input(
        "Draft",
        value=str(draft),
        label_visibility="collapsed",
        disabled=disabled,
        key="_draft_input",
    )
    col_send, col_cancel = st.columns([1, 1])
    sent = False
    with col_send:
        if st.button("Send", type="primary", use_container_width=True, disabled=disabled):
            sent = True
    with col_cancel:
        if st.button("Cancel", use_container_width=True):
            st.session_state.pending_draft = None
            st.session_state.pop("_draft_input", None)
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    if sent:
        text = (edited or "").strip()
        st.session_state.pending_draft = None
        st.session_state.pop("_draft_input", None)
        # Queue the query and rerun so the mid-gate disappears immediately.
        if text:
            st.session_state.pending_query = text
        st.rerun()
    return None


history_container = st.container()
response_container = st.container()

with history_container:
    render_welcome_hero(st.session_state.messages)
    render_chat_history(st.session_state.messages)

response_slot = response_container.empty()


# ---------------------------------------------------------------------------
# Chat input
# ---------------------------------------------------------------------------
if "_pending_assistant_query" not in st.session_state:
    st.session_state._pending_assistant_query = None

incoming_query = st.session_state.pop("pending_query", None)
_chat_disabled = not _engine_ready

if incoming_query is None:
    # Suggestions stay visible across the session; always show 3.
    disabled_suggestions = bool(_chat_disabled or (st.session_state.get("_pending_assistant_query") or "").strip())
    triplet = list(st.session_state.get("suggestion_triplet") or random.sample(SUGGESTION_POOL, k=3))
    triplet = (triplet + random.sample(SUGGESTION_POOL, k=3))[:3]
    q1, q2, q3 = triplet[0], triplet[1], triplet[2]

    sugg_cols = st.columns([3, 3, 3, 1], gap="small")
    for idx, (col, suggestion) in enumerate(zip(sugg_cols[:3], [q1, q2, q3])):
        with col:
            if st.button(
                suggestion or "Suggestion",
                key=f"suggestion_blob__bottom__{idx}__{abs(hash(suggestion)) % 10_000}",
                type="secondary",
                use_container_width=True,
                disabled=disabled_suggestions,
            ):
                st.session_state.pending_draft = suggestion
                st.session_state["_draft_input"] = suggestion
                st.rerun()
    with sugg_cols[3]:
        if st.button("🔄", help="Refresh suggested questions", use_container_width=True, disabled=disabled_suggestions):
            st.session_state.suggestion_triplet = random.sample(SUGGESTION_POOL, k=3)
            st.rerun()
    render_draft_midgate(disabled=_chat_disabled)
    incoming_query = st.chat_input("Ask the Lore Keeper \u2026", disabled=_chat_disabled)

if _chat_disabled:
    st.caption("Engine warmup in progress. Chat input unlocks when initialization finishes.")
else:
    # GPU is informational: if it is still yellow, Ollama may still be preloading the model
    # and queries can run on CPU until the badge flips green.
    if (st.session_state.llm_mode or "efficiency") == "efficiency":
        _gpu_state_for_hint = _gpu_state_key_only(engine_ready=_engine_ready)
        if _gpu_state_for_hint != "active":
            st.caption("GPU Acceleration is still warming up. Queries may run on CPU until the badge turns 🟢.")

if incoming_query:
    text = str(incoming_query).strip()
    if text and not _chat_disabled:
        st.session_state._pending_assistant_query = text
        # Append exactly once: if the last message is already this user query, don't add it again.
        if not st.session_state.messages or st.session_state.messages[-1].get("role") != "human" or st.session_state.messages[-1].get("content") != text:
            st.session_state.messages.append({"role": "human", "content": text, "sources": None})
            _save_history(st.session_state.messages)
        # Rerun so the appended user message is rendered via the single history pass above.
        st.rerun()

# Phase 2: run the assistant only after history has been rendered.
pending_assistant_query = str(st.session_state.get("_pending_assistant_query") or "").strip()
if pending_assistant_query and not _chat_disabled:
    query = pending_assistant_query
    history = _history_for_keeper(st.session_state.messages[:-1] if st.session_state.messages else [])
    _upsert_brain_metadata(st.session_state.brain_id, touch_last_used=True)
    _mq_state = bool(st.session_state.get("multi_query_enabled", True))
    _ctx_exp_state = bool(st.session_state.get("context_expansion_enabled", False))

    with response_slot.container():
        with st.chat_message("assistant"):
            if not _heartbeat_ready("db", st.session_state.llm_mode, st.session_state.brain_id):
                with st.spinner("Engine still warming up — please wait…"):
                    keeper = resolve_engine()
            else:
                keeper = resolve_engine()
            # Per-session retrieval config toggles (do not require engine rebuild).
            try:
                keeper.multi_query_enabled = _mq_state
                keeper.context_expansion_enabled = _ctx_exp_state
            except Exception:
                pass
            _wup_key = _engine_cache_key("db", st.session_state.llm_mode, st.session_state.brain_id)
            _shared_state()["built"].add(_wup_key)
            _wup_ev = _shared_state()["events"].get(_wup_key)
            if _wup_ev:
                _wup_ev.set()

            answer = ""
            warning_note: Optional[str] = None
            citation_warning: Optional[str] = None
            sources: list[dict[str, Any]] = []
            warning_slot = st.empty()
            citation_slot = st.empty()
            answer_slot = st.empty()
            verify_slot = st.empty()
            try:
                ok, msg = _preflight_llm_ok(st.session_state.llm_mode)
                if not ok:
                    st.warning(msg)
                    render_error_state("preflight", msg)
                    raise RuntimeError(msg)
                with st.spinner("Retrieving from the archives \u2026"):
                    token_stream, sources = keeper.stream_query(
                        query,
                        history,
                        edition_filter=st.session_state.edition_filter,
                    )
                if not sources:
                    verify_slot.info("🧠 General Knowledge Mode: No verified chunks retrieved.", icon="🧠")
                with st.spinner("The Lore Keeper is thinking\u2026"):
                    verify_slot.caption("🔍 Verifying answer integrity...")
                    acc: list[str] = []
                    for chunk in token_stream:
                        acc.append(str(chunk))
                        answer_slot.markdown("".join(acc))
                    verify_slot.empty()
                    raw_answer = "".join(acc)
                    warning_note, answer = _extract_general_knowledge_warning(raw_answer)
                    answer = _normalize_not_found_disclaimer(answer)
                    answer = _strip_inline_source_tags(answer)
                    if warning_note:
                        warning_slot.warning(warning_note)
                        answer_slot.markdown(answer)
                    bad_tags = _citation_hallucination_tags(raw_answer, sources)
                    if bad_tags:
                        citation_warning = (
                            "Citation Hallucination detected: "
                            + ", ".join(f"`{b}`" for b in bad_tags)
                            + " not present in retrieved source set."
                        )
                        citation_slot.warning(citation_warning)
                        logger.warning(
                            "Citation Hallucination | claimed=%s | retrieved=%s",
                            bad_tags,
                            sorted(_source_filenames_from_source_dicts(sources)),
                        )
            except Exception as exc:
                err_s = str(exc).lower()
                if (
                    "404" in str(exc)
                    or "not found" in err_s
                    or "responseerror" in err_s.replace(" ", "")
                ):
                    _ollama_model = (
                        (os.getenv("OLLAMA_CHAT_MODEL") or "llama3:8b-instruct-q4_K_M").strip()
                        or "llama3:8b-instruct-q4_K_M"
                    )
                    _pull_cmd = f"docker exec -it ollama ollama pull {_ollama_model}"
                    st.error(
                        "**Ollama model missing or still downloading.** The app uses the **Docker** "
                        "Ollama service (`ollama` container)—host-only pulls are invisible there. "
                        "If the automatic pull has not finished, run this on the host:"
                    )
                    st.code(_pull_cmd, language="bash")
                    st.caption(
                        "Then run `docker exec -it ollama ollama list` and set **OLLAMA_CHAT_MODEL** "
                        "to that exact name, or switch to **Premium Intelligence**."
                    )
                else:
                    logger.exception("Query loop engine failure: %s", exc)
                    answer = (
                        "System Recovering: A temporary engine fault was detected. "
                        "Please retry in a few seconds."
                    )
                    warning_slot.warning(answer)
                    render_error_state("query", str(exc))
            verify_slot.empty()
            if answer is None:
                answer = ""
            _render_verified_sources_expander(sources)

        st.session_state.messages.append({
            "role": "assistant",
            "content": answer,
            "warning_note": warning_note,
            "citation_warning": citation_warning,
            "sources": sources or [],
        })
        _save_history(st.session_state.messages)
        st.session_state._pending_assistant_query = None
        st.rerun()


# ---------------------------------------------------------------------------
# Auto-refresh — state-transition approach (avoids sleeping on every rerun).
#
# Design: each rerun reads two booleans:
#   _was_shown_ready  — did the *previous* rerun render the "✅ Ready" state?
#   _is_now_ready     — has the warmup thread actually finished right now?
#
# Three cases:
#   1. Still loading, no error → sleep 1.5 s then rerun (polling).
#   2. Just became ready (_was_shown_ready was False, now True) → mark flag
#      in session_state and rerun ONCE so the sidebar flips to ✅ Ready.
#   3. Already shown ready → do nothing (stable).
#
# This eliminates the race condition where the thread finishes between the
# `_engine_is_ready` check and the next user interaction.
# ---------------------------------------------------------------------------
_poll_mode_key = st.session_state.llm_mode
_poll_brain_key = st.session_state.brain_id
_ready_shown_key = f"_engine_shown_ready:{_poll_brain_key}:{_poll_mode_key}"
_wk_poll = _engine_cache_key("db", _poll_mode_key, _poll_brain_key)
_was_shown_ready = st.session_state.get(_ready_shown_key, False)
_is_now_ready = _heartbeat_ready("db", _poll_mode_key, _poll_brain_key)

if not _is_now_ready and _wk_poll not in _shared_state()["errors"]:
    # Still loading — poll every 1.5 s until the warmup event fires.
    time.sleep(1.5)
    st.rerun()
elif _is_now_ready and not _was_shown_ready:
    # Transition: engine just became ready. Mark and rerun once to flip UI.
    st.session_state[_ready_shown_key] = True
    # Also poll briefly for GPU activation so the badge updates without user clicks.
    st.session_state["_gpu_poll_until"] = time.monotonic() + 20.0
    st.rerun()

# After the engine is ready, keep rerunning briefly until GPU flips active (or timeout).
_poll_until = float(st.session_state.get("_gpu_poll_until") or 0.0)
if _is_now_ready and time.monotonic() < _poll_until:
    if _gpu_state_key_only(engine_ready=True) != "active":
        time.sleep(0.8)
        st.rerun()
    else:
        st.session_state["_gpu_poll_until"] = 0.0

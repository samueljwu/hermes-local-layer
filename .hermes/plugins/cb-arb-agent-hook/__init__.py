"""Token-efficient cb-arb specialist guidance hook.

The original hook auto-ran a multi-round five-agent panel for cb-arb agent
requests, which was complete but expensive. The default now runs one concise
cross-functional reviewer that covers the same PM/trader, architecture, data/API,
UI/UX, and product checklist. Explicit panels share one evidence packet and avoid
broad rediscovery so they preserve review quality with much lower startup cost.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = "/home/hermes/projects/cb-arb"
PROJECT_SKILL = f"{PROJECT_ROOT}/SKILL.md"

_TRIGGER_RE = re.compile(r"\b(cb[-_ ]?arb|convertible[- ]bond arbitrage|convertible bond)\b", re.I)
_AGENT_RE = re.compile(
    r"\b(agent|agents|subagent|subagents|delegate|delegation|panel|reviewers?|specialists?|work on|build|implement|audit|review)\b",
    re.I,
)
_EXPLICIT_PANEL_RE = re.compile(
    r"\b(run|invoke|use|start|launch)\b.*\b(cbarbpanel|cb[-_ ]?arb panel|five[- ]role panel|specialist panel)\b",
    re.I,
)

_LOCK = threading.RLock()
_ACTIVE = False
_SEEN: set[str] = set()
_CTX = None

_EXCLUDE_PARTS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "data/raw",
    "data/reports",
    "data/price_history/raw",
}
_EXCLUDE_SUFFIXES = {".pdf", ".xlsx", ".sqlite", ".db", ".pyc"}
_KEY_FILE_GLOBS = (
    "SKILL.md",
    "README.md",
    "REVIEW_PROTOCOL.md",
    "pyproject.toml",
    "cb_arb/**/*.py",
    "tests/**/*.py",
    "docs/**/*.md",
    "data/contracts/*.json",
    "data/coverage/*.json",
    "data/prospectus_text/*.json",
)


def _text(user_message: Any) -> str:
    return user_message if isinstance(user_message, str) else ""


def _want_auto_context(user_message: Any) -> bool:
    text = _text(user_message)
    if not text.strip():
        return False
    lowered = text.lower()
    # Manual escape hatch for a single turn. Also treat natural-language
    # "without triggering the hook/panel" as an escape hatch so users can edit
    # this plugin without paying for its own automation.
    if (
        "no-cbarb-panel" in lowered
        or "skip-cbarb-panel" in lowered
        or re.search(r"\bwithout triggering\b.*\b(cbarb|cb-arb|hook|panel)\b", lowered)
    ):
        return False
    return bool(_TRIGGER_RE.search(text) and _AGENT_RE.search(text))


def _auto_mode() -> str:
    return os.getenv("CB_ARB_HOOK_MODE", "review").strip().lower()


def _want_expensive_auto_panel(user_message: Any) -> bool:
    """Only the heavier multi-subagent panel is opt-in."""
    text = _text(user_message)
    if not _want_auto_context(text):
        return False
    mode = _auto_mode()
    if mode in {"off", "none", "disabled", "light", "brief", "context"}:
        # Still honor a direct natural-language request for the panel.
        return bool(_EXPLICIT_PANEL_RE.search(text))
    if mode in {"panel", "full", "delegate", "triad", "five"}:
        return True
    return bool(_EXPLICIT_PANEL_RE.search(text))


def _want_brief_auto_review(user_message: Any) -> bool:
    """Default single-subagent auto review: broad coverage, much lower cost."""
    text = _text(user_message)
    if not _want_auto_context(text):
        return False
    mode = _auto_mode()
    if mode in {"review", "single", "brief", "brief-review", "one"}:
        return not _want_expensive_auto_panel(text)
    return False


def _dedupe_key(session_id: str, user_message: Any) -> str:
    text = user_message if isinstance(user_message, str) else repr(user_message)
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{session_id or 'no-session'}:{digest}"


def _lightweight_context() -> str:
    return (
        "CB-ARB LIGHTWEIGHT HOOK CONTEXT:\n"
        f"- Project root: {PROJECT_ROOT}\n"
        f"- Read/follow project-local guidance at {PROJECT_SKILL} before editing or reviewing.\n"
        "- Treat cb-arb as an isolated standalone project; do not change unrelated Hermes systems.\n"
        "- Do not invent prospectus/CB facts; use raw-PDF/file evidence for CB facts.\n"
        "- Keep PM/trader-facing changes auditable and efficient.\n"
        "- Default auto mode runs one cross-functional specialist review covering PM/engineering/data/UX/product. "
        "Use CB_ARB_HOOK_MODE=light for context-only, /cbarbpanel [request] or "
        "CB_ARB_HOOK_MODE=panel for heavier review. Use no-cbarb-panel to skip this hook for one turn."
    )


def _read_excerpt(path: Path, *, max_chars: int = 1400) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"[unavailable: {exc}]"
    if len(text) <= max_chars:
        return text.strip()
    return text[:max_chars].rstrip() + "\n...[excerpt truncated]"


def _path_is_excluded(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return True
    parts = set(Path(rel).parts)
    if parts & {".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}:
        return True
    if rel.startswith("data/raw/") or rel.startswith("data/reports/") or rel.startswith("data/price_history/raw/"):
        return True
    return path.suffix.lower() in _EXCLUDE_SUFFIXES


def _file_inventory(root: Path, *, limit: int = 90) -> str:
    seen: set[str] = set()
    rows: List[str] = []
    for glob in _KEY_FILE_GLOBS:
        for path in sorted(root.glob(glob)):
            if not path.is_file() or _path_is_excluded(path, root):
                continue
            rel = path.relative_to(root).as_posix()
            if rel in seen:
                continue
            seen.add(rel)
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            rows.append(f"- {rel} ({size} bytes)")
            if len(rows) >= limit:
                rows.append("- ... inventory truncated; use targeted search_files/read_file if needed")
                return "\n".join(rows)
    return "\n".join(rows) if rows else "[no included files found]"


def _shared_evidence_packet(user_message: str) -> str:
    """Cheap shared orientation for explicit panel workers.

    This intentionally avoids shelling out, running tests, or scanning ignored/raw
    trees. Subagents can still inspect/run what they need, but they start from the
    same compact map instead of each paying broad discovery costs.
    """
    root = Path(PROJECT_ROOT)
    skill = Path(PROJECT_SKILL)
    readme = root / "README.md"
    review = root / "REVIEW_PROTOCOL.md"
    pyproject = root / "pyproject.toml"
    skill_hash = "unavailable"
    try:
        skill_hash = hashlib.sha256(skill.read_bytes()).hexdigest()[:16]
    except Exception:
        pass
    return f"""
SHARED CB-ARB EVIDENCE PACKET (generated once by cb-arb-agent-hook)
Project root: {PROJECT_ROOT}
Project skill: {PROJECT_SKILL}
Project skill sha256-prefix: {skill_hash}
Original user request:
{user_message}

Hard constraints from project-local guidance:
- cb-arb is an isolated standalone project; do not add Hermes hooks/plugins/subagents as product/runtime dependencies.
- Do not invent prospectus, market, or CB facts. Use project files/raw-PDF/page-text evidence and cite inspected paths.
- Raw PDFs/private exports stay under ignored data/raw/ or outside the repo; broad scans should exclude .venv, caches, raw PDFs, generated reports, SQLite DBs, and binary exports.
- GUI/PM outputs must keep assumptions explicit, replayable, and unit-safe.
- For pricing latency work, distinguish interactive preview from audit/final pricing instead of silently downgrading quality.

Recommended scoped file discovery:
Include: SKILL.md, README.md, REVIEW_PROTOCOL.md, pyproject.toml, cb_arb/**/*.py, tests/**/*.py, docs/**/*.md, data/contracts/*.json, data/coverage/*.json, data/prospectus_text/*.json.
Exclude: .venv/**, __pycache__/**, .pytest_cache/**, data/raw/**, data/price_history/raw/**, data/reports/**, *.pdf, *.xlsx, *.sqlite, *.db.

Key file inventory (filtered):
{_file_inventory(root)}

SKILL.md excerpt:
{_read_excerpt(skill, max_chars=1700)}

README.md excerpt:
{_read_excerpt(readme, max_chars=900)}

REVIEW_PROTOCOL.md excerpt:
{_read_excerpt(review, max_chars=700)}

pyproject.toml excerpt:
{_read_excerpt(pyproject, max_chars=700)}

Panel efficiency rules:
- Start from this packet. Do not repeat broad recursive inventories unless the packet is insufficient.
- Inspect only files relevant to the role and the user's request.
- Run focused tests/profiling only when they materially affect the answer.
- If measuring pricing speed, use the project .venv and report exact command/timings.
- Return only material blockers/recommendations with paths/tests/evidence.
""".strip()


def _shared_context(user_message: str, round_label: str, peer_context: str = "") -> str:
    base = f"""
You are part of the optional cb-arb specialist panel.
{_shared_evidence_packet(user_message)}

Panel operating rules:
- Accuracy and auditability beat speed for prospectus, pricing, schema, and PM-facing outputs.
- Do not invent convertible-bond facts; cite file paths/tests/evidence you inspected.
- Keep recommendations actionable and concise: exact files, APIs, tests, risks, and priority.
- If user/portfolio-manager feedback is needed, state the question for the parent agent to ask.
- Separate blockers from nice-to-haves.
Round: {round_label}
""".strip()
    if peer_context:
        base += "\n\nPeer feedback from the previous panel round. Reconcile disagreements and call out unresolved conflicts:\n" + peer_context
    return base


def _brief_review_task(user_message: str) -> List[Dict[str, Any]]:
    context = f"""
You are the token-efficient cb-arb specialist reviewer.
{_shared_evidence_packet(user_message)}

Cover all five perspectives in one concise pass:
1. PM/trader: decision quality, assumptions controls, useful outputs.
2. Architecture: likely code risks, maintainability, tests.
3. Data/API: schema/storage/API/frontend-backend consistency.
4. UI/UX: PM workflow, labels, forms, charts, friction.
5. Product coordination: priorities, acceptance criteria, unresolved PM questions.

Rules: cite inspected file paths/tests/evidence; do not invent CB/prospectus facts; raw-PDF/file evidence for CB facts; separate blockers from nice-to-haves; return only material findings.
""".strip()
    return [
        {
            "goal": "cb-arb cross-functional PM/engineering/data/UX/product review",
            "context": context,
            "toolsets": ["terminal", "file"],
            "role": "leaf",
        }
    ]


def _role_tasks(user_message: str, round_label: str, peer_context: str = "") -> List[Dict[str, Any]]:
    shared = _shared_context(user_message, round_label, peer_context)
    size = os.getenv("CB_ARB_PANEL_SIZE", "triad").strip().lower()
    tasks: List[Dict[str, Any]] = [
        {
            "goal": "cb-arb PM/trader strategy review",
            "context": shared + "\n\nRole: Senior PM/trader. Focus on decision quality, auditability, assumptions controls, pricing quality/speed tradeoffs, and PM-facing outputs. Return only high-impact findings.",
            "toolsets": ["terminal", "file"],
            "role": "leaf",
        },
        {
            "goal": "cb-arb architecture and data/API review",
            "context": shared + "\n\nRole: Senior engineer. Inspect likely affected architecture, schemas, persisted data, APIs, tests, runtime performance, and maintainability risks. Return concrete remediation guidance.",
            "toolsets": ["terminal", "file"],
            "role": "leaf",
        },
        {
            "goal": "cb-arb product and UX coordination review",
            "context": shared + "\n\nRole: Product/UX lead. Align PM needs, engineering constraints, workflows, labels, and acceptance criteria. Ask only essential PM questions.",
            "toolsets": ["terminal", "file"],
            "role": "leaf",
        },
    ]
    if size in {"full", "five", "5"}:
        tasks.insert(
            2,
            {
                "goal": "cb-arb database schema and API wiring review",
                "context": shared + "\n\nRole: Database/API engineer. Check schemas, migrations/storage conventions, data-source alignment, endpoint contracts, and frontend/backend consistency.",
                "toolsets": ["terminal", "file"],
                "role": "leaf",
            },
        )
        tasks.insert(
            3,
            {
                "goal": "cb-arb UI/UX portfolio-manager workflow review",
                "context": shared + "\n\nRole: UI/UX designer. Inspect workflows, labels, forms, charts, accessibility, and information hierarchy. Suggest low-friction improvements.",
                "toolsets": ["terminal", "file"],
                "role": "leaf",
            },
        )
    return tasks


def _dispatch_delegate(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    if _CTX is None:
        return {"error": "plugin context unavailable"}
    raw = _CTX.dispatch_tool("delegate_task", {"tasks": tasks})
    try:
        data = json.loads(raw)
    except Exception:
        return {"raw": raw}
    return data if isinstance(data, dict) else {"raw": data}


def _compact_result(data: Dict[str, Any], *, limit: int = 6000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= limit:
        return text
    return text[: limit - 180].rstrip() + "\n... [truncated by cb-arb-agent-hook; run /cbarbpanel for follow-up details if needed]"


def _panel_context(user_message: str) -> Dict[str, str]:
    global _ACTIVE
    with _LOCK:
        if _ACTIVE:
            return {"context": "cb-arb specialist panel is already active; skipping nested/recursive panel spawn."}
        _ACTIVE = True
    try:
        round1 = _dispatch_delegate(_role_tasks(user_message, "single round: focused specialist review"))
        if isinstance(round1, dict) and round1.get("error"):
            return {
                "context": (
                    "CB-ARB SPECIALIST PANEL REQUESTED, but direct plugin dispatch failed: "
                    + str(round1.get("error"))
                    + "\nIf specialist review is still necessary, call delegate_task with three concise tasks: "
                    "(1) PM/trader strategy reviewer, (2) architecture/data/API engineer, "
                    "(3) product/UX coordinator. Keep outputs brief."
                )
            }

        rounds = int(os.getenv("CB_ARB_PANEL_ROUNDS", "1") or "1")
        if rounds >= 2:
            peer_context = _compact_result(round1, limit=4000)
            round2 = _dispatch_delegate(_role_tasks(user_message, "round 2: only reconcile material conflicts", peer_context))
        else:
            round2 = {"skipped": "default one-round panel for token efficiency; set CB_ARB_PANEL_ROUNDS=2 to enable reconciliation"}

        combined = {
            "hook": "cb-arb-agent-hook",
            "project_root": PROJECT_ROOT,
            "mode": "explicit focused panel",
            "panel_size": os.getenv("CB_ARB_PANEL_SIZE", "triad").strip().lower(),
            "instruction_to_parent_agent": "Use only material findings before implementing/reviewing cb-arb; ask the PM only for unresolved blockers.",
            "round1_focused_reviews": round1,
            "round2_reconciliation": round2,
        }
        return {"context": "CB-ARB FOCUSED SPECIALIST PANEL RESULTS:\n" + _compact_result(combined)}
    finally:
        with _LOCK:
            _ACTIVE = False


def _brief_review_context(user_message: str) -> Dict[str, str]:
    global _ACTIVE
    with _LOCK:
        if _ACTIVE:
            return {"context": "cb-arb specialist review is already active; skipping nested/recursive review spawn."}
        _ACTIVE = True
    try:
        result = _dispatch_delegate(_brief_review_task(user_message))
        combined = {
            "hook": "cb-arb-agent-hook",
            "project_root": PROJECT_ROOT,
            "mode": "single-reviewer auto review",
            "instruction_to_parent_agent": "Use only material findings before implementing/reviewing cb-arb; ask the PM only for unresolved blockers.",
            "review": result,
        }
        return {"context": "CB-ARB SINGLE SPECIALIST REVIEW RESULTS:\n" + _compact_result(combined)}
    finally:
        with _LOCK:
            _ACTIVE = False


def _on_pre_llm_call(*, session_id: str = "", user_message: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    if not _want_auto_context(user_message):
        return None
    key = _dedupe_key(session_id, user_message)
    with _LOCK:
        if key in _SEEN:
            return None
        _SEEN.add(key)
    text = user_message if isinstance(user_message, str) else str(user_message)
    if _want_expensive_auto_panel(user_message):
        return _panel_context(text)
    if _want_brief_auto_review(user_message):
        return _brief_review_context(text)
    return {"context": _lightweight_context()}


def _handle_cbarbpanel(raw_args: str = "") -> str:
    prompt = (raw_args or "").strip() or "Run the cb-arb focused specialist panel for the current cb-arb work request."
    result = _panel_context(prompt)
    return result.get("context", "cb-arb panel produced no context")


def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    ctx.register_command(
        "cbarbpanel",
        handler=_handle_cbarbpanel,
        description="Run the optional cb-arb focused PM/engineering/product specialist panel.",
        args_hint="[request]",
    )

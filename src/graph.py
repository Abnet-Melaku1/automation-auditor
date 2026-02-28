"""
Automaton Auditor — LangGraph StateGraph (Final Submission).

Graph topology (final)
----------------------

    START
      ├────────────────────────────────────────────┐
      │                                            │
      ▼                                            ▼
  repo_investigator                           doc_analyst
  (RepoInvestigator)                          (DocAnalyst)
      │  evidences["git_forensic_analysis"]        │  evidences["theoretical_depth"]
      │  evidences["state_management_rigor"]        │  evidences["report_accuracy"]
      │  evidences["graph_orchestration"]           │
      │  evidences["safe_tool_engineering"]         │
      │  evidences["structured_output_enforcement"] │
      │                                            │
      └──────────────────┬─────────────────────────┘
                         │  operator.ior merges both branches
                         ▼
               evidence_aggregator  ← Detective Fan-In
               - completeness check
               - secondary report_accuracy cross-reference
               - conditional route: no evidence → END (graceful abort)
                         │
           ┌─────────────┼─────────────┐
           │             │             │
           ▼             ▼             ▼
       prosecutor     defense       tech_lead
           │             │             │
           └─────────────┼─────────────┘
                         │  operator.add concatenates all three opinion lists
                         ▼
               judicial_aggregator  ← Judicial Fan-In
               - verifies all three judges submitted opinions
               - logs judicial phase summary
                         │
                         ▼
                  chief_justice  ← Deterministic synthesis
                  - 5 named conflict-resolution rules
                  - writes audit/<repo>_<timestamp>.md
                         │
                         ▼
                        END

State reducers — why parallel branches are safe
-----------------------------------------------
``AgentState.evidences`` — ``operator.ior`` (dict merge)
    Detective branches write to disjoint criterion keys → non-destructive.

``AgentState.opinions`` — ``operator.add`` (list concat)
    Judge branches each append their opinions list → all opinions preserved.

Conditional routing
-------------------
``evidence_aggregator`` routes via ``_route_after_evidence()``:
  • Empty/failed evidence → ``END`` (graceful abort with log)
  • Valid evidence present → fan-out to ["prosecutor", "defense", "tech_lead"]

LangGraph executes all three judge nodes concurrently.  The judicial_aggregator
fan-in fires only after all three branches have completed and their state updates
have been reduced by ``operator.add``.

LangSmith tracing
-----------------
Automatic when ``LANGCHAIN_TRACING_V2=true`` is set in the environment.
``build_graph()`` attaches ``run_name``, ``tags``, and ``metadata`` via
``.with_config()`` so every trace is identifiable in LangSmith.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from src.nodes.detectives import doc_analyst_node, repo_investigator_node
from src.nodes.judges import defense_node, prosecutor_node, tech_lead_node
from src.nodes.justice import chief_justice_node
from src.state import AgentState, Evidence, JudicialOpinion

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — criteria sets and judge roster
# ---------------------------------------------------------------------------

#: Rubric criteria covered by RepoInvestigator
_REPO_CRITERIA: frozenset[str] = frozenset(
    {
        "git_forensic_analysis",
        "state_management_rigor",
        "graph_orchestration",
        "safe_tool_engineering",
        "structured_output_enforcement",
    }
)

#: Rubric criteria covered by DocAnalyst
_PDF_CRITERIA: frozenset[str] = frozenset(
    {
        "theoretical_depth",
        "report_accuracy",
    }
)

#: All detective-phase criteria (used for completeness check)
REQUIRED_INTERIM_CRITERIA: frozenset[str] = _REPO_CRITERIA | _PDF_CRITERIA

#: Full rubric (detective + judicial coverage)
REQUIRED_ALL_CRITERIA: frozenset[str] = REQUIRED_INTERIM_CRITERIA

#: Judge personas expected in state["opinions"] after the judicial phase
_EXPECTED_JUDGES: frozenset[str] = frozenset({"Prosecutor", "Defense", "TechLead"})

# ---------------------------------------------------------------------------
# Utility: load rubric dimensions from rubric.json
# ---------------------------------------------------------------------------

_RUBRIC_PATH: Path = Path(__file__).parent.parent / "rubric" / "rubric.json"


def _load_rubric_dimensions() -> list[dict[str, Any]]:
    """Load the rubric.json dimensions array; return empty list on failure."""
    try:
        data = json.loads(_RUBRIC_PATH.read_text(encoding="utf-8"))
        return data.get("dimensions", [])
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load rubric from %s: %s", _RUBRIC_PATH, exc)
        return []


# ---------------------------------------------------------------------------
# Node: EvidenceAggregator (Detective Fan-In)
# ---------------------------------------------------------------------------


def evidence_aggregator_node(state: AgentState) -> dict[str, Any]:
    """Detective fan-in node — runs after both detective branches complete.

    Responsibilities
    ----------------
    1. **Completeness check** — log warnings for any missing criteria.
    2. **Secondary cross-reference** — use repo file locations from
       RepoInvestigator to finalise the report_accuracy analysis that
       DocAnalyst deferred during parallel execution.
    3. **Summary logging** — emit concise audit progress to LangSmith.

    Returns
    -------
    dict
        ``{}`` when no cross-reference update is needed, or
        ``{"evidences": {"report_accuracy": [...]}}`` with the cross-reference
        result appended.
    """
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})  # type: ignore[call-overload]

    # ── 1. Completeness check ─────────────────────────────────────────────
    present = frozenset(evidences.keys())
    missing = REQUIRED_INTERIM_CRITERIA - present

    if missing:
        logger.warning(
            "[EvidenceAggregator] INCOMPLETE — missing criteria: %s",
            sorted(missing),
        )
    else:
        logger.info(
            "[EvidenceAggregator] All %d detective criteria present.",
            len(REQUIRED_INTERIM_CRITERIA),
        )

    # ── 2. Summary statistics ─────────────────────────────────────────────
    total = sum(len(v) for v in evidences.values())
    found = sum(1 for evs in evidences.values() for e in evs if e.found)
    logger.info(
        "[EvidenceAggregator] Evidence summary — "
        "criteria: %d, total items: %d, found: %d, not_found: %d",
        len(present),
        total,
        found,
        total - found,
    )

    # ── 3. Secondary cross-reference for report_accuracy ─────────────────
    xref_update = _cross_reference_report_accuracy(state)
    if xref_update:
        return {"evidences": xref_update}

    return {}


def _cross_reference_report_accuracy(
    state: AgentState,
) -> dict[str, list[Evidence]] | None:
    """Finalise the report_accuracy cross-reference using the repo file catalog.

    DocAnalyst extracted path claims during parallel execution but could not
    verify them (the repo file list wasn't available yet).  Now that both
    branches have merged, we use the complete file catalog populated by
    RepoInvestigator (stored in ``state["repo_files"]``) to cross-reference.

    Falls back to deriving known paths from Evidence.location strings when
    ``repo_files`` is empty (e.g. if the repo clone failed).
    """
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})  # type: ignore[call-overload]

    # ── Primary: use the pre-built repo file catalog ──────────────────────
    repo_files: list[str] = state.get("repo_files", [])  # type: ignore[call-overload]

    # ── Fallback: derive known paths from Evidence.location strings ───────
    if not repo_files:
        for criterion_id in _REPO_CRITERIA:
            for ev in evidences.get(criterion_id, []):
                loc = ev.location.replace("\\", "/").split(":")[0].strip()
                if "/" in loc and not loc.startswith("http") and "." in loc.split("/")[-1]:
                    repo_files.append(loc)
        if not repo_files:
            return None  # Repo clone failed — cannot cross-reference

    # Collect the claimed paths DocAnalyst extracted
    claimed_paths: list[str] = []
    for ev in evidences.get("report_accuracy", []):
        if ev.content:
            for line in ev.content.splitlines():
                line = line.strip()
                if line.startswith("claimed:"):
                    claimed_paths.append(line.replace("claimed:", "").strip())

    if not claimed_paths:
        return None

    # Perform the cross-reference
    repo_norm = {p.replace("\\", "/").lstrip("./") for p in repo_files}
    verified: list[str] = []
    hallucinated: list[str] = []
    for path in claimed_paths:
        norm = path.replace("\\", "/").lstrip("./")
        (verified if norm in repo_norm else hallucinated).append(path)

    rate = len(hallucinated) / len(claimed_paths)
    passes = rate == 0.0

    xref_evidence = Evidence(
        goal=(
            "Cross-reference file paths claimed in PDF report "
            "against actual repository structure"
        ),
        found=passes and bool(verified),
        content=(
            "Verified paths:\n"
            + "\n".join(f"  ✓ {p}" for p in verified)
            + "\nHallucinated paths:\n"
            + "\n".join(f"  ✗ {p}" for p in hallucinated)
        ),
        location="evidence_aggregator (cross-reference)",
        rationale=(
            f"Claimed: {len(claimed_paths)}, "
            f"Verified: {len(verified)}, "
            f"Hallucinated: {len(hallucinated)} "
            f"(hallucination_rate={rate:.1%})."
        ),
        confidence=0.88,
        criterion_id="report_accuracy",
    )

    existing = list(evidences.get("report_accuracy", []))
    return {"report_accuracy": existing + [xref_evidence]}


# ---------------------------------------------------------------------------
# Conditional routing — evidence_aggregator → judges OR END
# ---------------------------------------------------------------------------


def _route_after_evidence(state: AgentState) -> str | list[str]:
    """Routing function for the conditional edge out of evidence_aggregator.

    Returns
    -------
    str | list[str]
        ``END`` (``"__end__"``) when no valid evidence was collected,
        triggering a graceful graph termination before the judicial phase.

        A list of three judge node names when evidence is present,
        which LangGraph interprets as a parallel fan-out to all three
        nodes simultaneously.
    """
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})  # type: ignore[call-overload]

    has_any_evidence = any(bool(ev_list) for ev_list in evidences.values())

    if not has_any_evidence:
        logger.warning(
            "[Graph] No evidence collected by any detective — "
            "aborting gracefully before judicial phase."
        )
        return END

    # Fan-out: LangGraph runs all three judge nodes concurrently
    logger.info("[Graph] Evidence confirmed. Routing to judicial fan-out.")
    return ["prosecutor", "defense", "tech_lead"]


# ---------------------------------------------------------------------------
# Node: JudicialAggregator (Judicial Fan-In)
# ---------------------------------------------------------------------------


def judicial_aggregator_node(state: AgentState) -> dict[str, Any]:
    """Judicial fan-in node — runs after all three judge branches complete.

    operator.add has already concatenated all opinions into state["opinions"]
    by the time this node executes.  This node verifies coverage and logs a
    summary before routing to the ChiefJustice.

    Returns
    -------
    dict
        ``{}`` — no state mutation required; verification is log-only.
    """
    opinions: list[JudicialOpinion] = state.get("opinions", [])  # type: ignore[call-overload]

    # Group by criterion to check judge coverage
    coverage: dict[str, set[str]] = {}
    for op in opinions:
        coverage.setdefault(op.criterion_id, set()).add(op.judge)

    fully_covered = sum(
        1 for judges in coverage.values() if judges >= _EXPECTED_JUDGES
    )
    partially_covered = len(coverage) - fully_covered
    missing_judges_report = {
        cid: sorted(_EXPECTED_JUDGES - judges)
        for cid, judges in coverage.items()
        if judges < _EXPECTED_JUDGES
    }

    if missing_judges_report:
        logger.warning(
            "[JudicialAggregator] Incomplete coverage — criteria missing judges: %s",
            missing_judges_report,
        )
    else:
        logger.info(
            "[JudicialAggregator] All %d criteria have full 3-judge coverage.",
            fully_covered,
        )

    logger.info(
        "[JudicialAggregator] Judicial phase complete — "
        "%d total opinions | %d criteria fully covered | %d partially covered",
        len(opinions),
        fully_covered,
        partially_covered,
    )
    return {}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph() -> Any:
    """Construct and compile the full Automaton Auditor StateGraph.

    Graph structure (final submission)
    ------------------------------------

        START → [repo_investigator ‖ doc_analyst]          (detective fan-out)
              → evidence_aggregator                         (detective fan-in)
              → conditional: empty evidence → END           (graceful abort)
              → [prosecutor ‖ defense ‖ tech_lead]          (judicial fan-out)
              → judicial_aggregator                         (judicial fan-in)
              → chief_justice                               (deterministic synthesis)
              → END

    Two distinct parallel fan-out / fan-in patterns satisfy the rubric's
    graph_orchestration criterion:
      • Detective fan-out/fan-in (START → detectives → evidence_aggregator)
      • Judicial fan-out/fan-in  (evidence_aggregator → judges → judicial_aggregator)

    Returns
    -------
    CompiledStateGraph
        Pre-configured with LangSmith metadata via ``.with_config()``.
    """
    builder: StateGraph = StateGraph(AgentState)

    # ── Register all nodes ────────────────────────────────────────────────
    builder.add_node("repo_investigator", repo_investigator_node)
    builder.add_node("doc_analyst", doc_analyst_node)
    builder.add_node("evidence_aggregator", evidence_aggregator_node)
    builder.add_node("prosecutor", prosecutor_node)
    builder.add_node("defense", defense_node)
    builder.add_node("tech_lead", tech_lead_node)
    builder.add_node("judicial_aggregator", judicial_aggregator_node)
    builder.add_node("chief_justice", chief_justice_node)

    # ── Detective Fan-Out — both nodes start concurrently from START ──────
    builder.add_edge(START, "repo_investigator")
    builder.add_edge(START, "doc_analyst")

    # ── Detective Fan-In — aggregator waits for BOTH branches ─────────────
    builder.add_edge("repo_investigator", "evidence_aggregator")
    builder.add_edge("doc_analyst", "evidence_aggregator")

    # ── Conditional routing — abort gracefully OR fan-out to judges ───────
    # _route_after_evidence returns END (abort) or ["prosecutor","defense","tech_lead"]
    # LangGraph interprets a returned list as a parallel fan-out.
    builder.add_conditional_edges("evidence_aggregator", _route_after_evidence)

    # ── Judicial Fan-In — aggregator waits for ALL THREE judges ───────────
    builder.add_edge("prosecutor", "judicial_aggregator")
    builder.add_edge("defense", "judicial_aggregator")
    builder.add_edge("tech_lead", "judicial_aggregator")

    # ── Supreme Court → END ───────────────────────────────────────────────
    builder.add_edge("judicial_aggregator", "chief_justice")
    builder.add_edge("chief_justice", END)

    compiled = builder.compile()

    return compiled.with_config(
        {
            "run_name": "automaton-auditor",
            "tags": ["detective-layer", "judicial-layer", "final-submission"],
            "metadata": {"version": "1.0.0", "layer": "full"},
        }
    )


# ---------------------------------------------------------------------------
# State factory
# ---------------------------------------------------------------------------


def create_initial_state(repo_url: str, pdf_path: str) -> AgentState:
    """Build a fully typed initial ``AgentState`` for an audit run.

    Parameters
    ----------
    repo_url:
        GitHub repository URL of the submission to audit.
    pdf_path:
        Absolute or relative path to the trainee's architectural PDF report.
    """
    return {
        "repo_url": repo_url,
        "pdf_path": pdf_path,
        "rubric_dimensions": _load_rubric_dimensions(),
        "evidences": {},   # identity for operator.ior (dict merge)
        "opinions": [],    # identity for operator.add (list concat)
        "repo_files": [],  # populated by repo_investigator_node after clone
        "final_report": None,
    }


# ---------------------------------------------------------------------------
# Convenience runners
# ---------------------------------------------------------------------------


def run_full_audit(
    repo_url: str,
    pdf_path: str,
    langsmith_project: str = "automaton-auditor",
) -> AgentState:
    """Run the complete Digital Courtroom pipeline and return the final AgentState.

    Pipeline
    --------
    1. Detective phase  — RepoInvestigator + DocAnalyst run in parallel
    2. Aggregation      — EvidenceAggregator cross-references and validates
    3. Judicial phase   — Prosecutor, Defense, TechLead run in parallel
    4. Synthesis        — ChiefJustice applies deterministic rules
    5. Report           — AuditReport serialised to audit/<repo>_<ts>.md

    Environment
    -----------
    Set ``LANGCHAIN_TRACING_V2=true`` and ``LANGCHAIN_API_KEY`` in ``.env``
    to stream traces to LangSmith.

    Parameters
    ----------
    repo_url:
        GitHub URL of the repository to audit.
    pdf_path:
        Path to the trainee's architectural PDF report.
    langsmith_project:
        LangSmith project name (default: ``"automaton-auditor"``).

    Returns
    -------
    AgentState
        Final state containing ``state["evidences"]``, ``state["opinions"]``,
        and ``state["final_report"]`` (an ``AuditReport`` or ``None`` on abort).
    """
    os.environ.setdefault("LANGCHAIN_PROJECT", langsmith_project)

    graph = build_graph()
    initial_state = create_initial_state(repo_url, pdf_path)

    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    run_config: dict[str, Any] = {
        "run_name": f"automaton-auditor | {repo_name}",
        "tags": ["detective-layer", "judicial-layer", "final-submission"],
        "metadata": {
            "repo_url": repo_url,
            "pdf_path": pdf_path,
            "submission": "final",
        },
    }

    logger.info(
        "[Graph] Launching full Digital Courtroom audit — repo: %s | pdf: %s",
        repo_url,
        pdf_path,
    )
    result: AgentState = graph.invoke(initial_state, config=run_config)

    final_report = result.get("final_report")  # type: ignore[call-overload]
    if final_report is not None:
        logger.info(
            "[Graph] Audit complete — overall_score=%.2f/5.0 | "
            "criteria=%d | opinions=%d",
            final_report.overall_score,
            len(final_report.criteria),
            len(result.get("opinions", [])),  # type: ignore[call-overload]
        )
    else:
        logger.warning("[Graph] Audit ended without a final report (graceful abort or error).")

    return result


def run_interim_audit(
    repo_url: str,
    pdf_path: str,
    langsmith_project: str = "automaton-auditor",
) -> AgentState:
    """Backward-compatible alias for ``run_full_audit()``.

    .. deprecated::
        Use ``run_full_audit()`` directly.  This alias remains to avoid
        breaking any existing scripts that reference the interim runner.
    """
    logger.warning(
        "[Graph] run_interim_audit() is deprecated — use run_full_audit() instead."
    )
    return run_full_audit(repo_url, pdf_path, langsmith_project)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "build_graph",
    "create_initial_state",
    "run_full_audit",
    "run_interim_audit",
    "evidence_aggregator_node",
    "judicial_aggregator_node",
    "REQUIRED_INTERIM_CRITERIA",
    "REQUIRED_ALL_CRITERIA",
]

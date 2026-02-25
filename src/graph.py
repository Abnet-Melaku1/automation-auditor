"""
Automaton Auditor — LangGraph StateGraph (Interim Submission).

Graph topology (interim)
------------------------

    START
      ├──────────────────────────────────────────────────────┐
      │                                                      │
      ▼                                                      ▼
  repo_investigator                                   doc_analyst
  (RepoInvestigator)                               (DocAnalyst)
      │  evidences["git_forensic_analysis"]             │  evidences["theoretical_depth"]
      │  evidences["state_management_rigor"]            │  evidences["report_accuracy"]
      │  evidences["graph_orchestration"]               │    (cross-ref deferred →)
      │  evidences["safe_tool_engineering"]             │
      │  evidences["structured_output_enforcement"]     │
      │                                                  │
      └──────────────────────┬───────────────────────────┘
                             │  operator.ior merges both branches
                             ▼
                   evidence_aggregator  ← Fan-In synchronisation node
                   - verifies all required criteria keys are present
                   - runs secondary report_accuracy cross-reference
                   - logs completeness summary
                             │
                             ▼
                      interim_end_node  ← Placeholder for Judicial Layer
                      (Prosecutor / Defense / TechLead added in final submission)
                             │
                             ▼
                            END

State reducers — why parallel branches are safe
-----------------------------------------------
``AgentState.evidences`` is annotated with ``operator.ior`` (dict merge).
When both detective branches complete, LangGraph applies:

    state["evidences"] |= repo_investigator_output["evidences"]
    state["evidences"] |= doc_analyst_output["evidences"]

Each branch writes to *disjoint* criterion keys, so the merge is
non-destructive.  If the same key were written by two branches the
later branch's list would win — which is why the cross-reference update
in ``evidence_aggregator`` reads the existing list from state first.

LangSmith tracing
-----------------
Automatic when ``LANGCHAIN_TRACING_V2=true`` is set in the environment.
``build_graph()`` attaches a default ``run_name`` via ``.with_config()``,
which LangSmith uses as the trace title.  Override per-invocation by
passing ``config={"run_name": "my-custom-name"}`` to ``graph.invoke()``.
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
from src.state import AgentState, Evidence

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — criteria expected after the detective fan-in
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

#: All criteria expected after the parallel detective phase
REQUIRED_INTERIM_CRITERIA: frozenset[str] = _REPO_CRITERIA | _PDF_CRITERIA

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
# Node: EvidenceAggregator (Fan-In synchronisation)
# ---------------------------------------------------------------------------


def evidence_aggregator_node(state: AgentState) -> dict[str, Any]:
    """Fan-in synchronisation node — runs after both detective branches complete.

    Responsibilities
    ----------------
    1. **Completeness check** — verify every required criterion has at least
       one Evidence entry; log warnings for any gaps.

    2. **Secondary cross-reference** — use file-path locations from the
       repo evidence to finalise the ``report_accuracy`` cross-reference
       that DocAnalyst could not complete during parallel execution.

    3. **Summary logging** — emit a concise audit summary so the LangSmith
       trace shows progress without opening every Evidence blob.

    Returns
    -------
    dict
        Partial state update.  Returns ``{}`` if everything is present, or
        updates ``evidences["report_accuracy"]`` with the cross-reference
        result if repo location data is available.
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
            "[EvidenceAggregator] All %d criteria present. Detective phase complete.",
            len(REQUIRED_INTERIM_CRITERIA),
        )

    # ── 2. Summary statistics ─────────────────────────────────────────────
    total = sum(len(v) for v in evidences.values())
    found = sum(1 for evs in evidences.values() for e in evs if e.found)
    not_found = total - found

    logger.info(
        "[EvidenceAggregator] Evidence summary — "
        "criteria: %d, total items: %d, found: %d, not_found: %d",
        len(present),
        total,
        found,
        not_found,
    )

    # ── 3. Secondary cross-reference for report_accuracy ─────────────────
    xref_update = _cross_reference_report_accuracy(state)
    if xref_update:
        return {"evidences": xref_update}

    # No state mutation needed when cross-reference is unavailable
    return {}


def _cross_reference_report_accuracy(
    state: AgentState,
) -> dict[str, list[Evidence]] | None:
    """Attempt to complete the report_accuracy cross-reference.

    Extracts repo file locations from the repo Evidence entries, then
    compares them against the paths extracted by DocAnalyst.  If the
    repo Evidence locations are not informative (e.g. clone failed),
    returns ``None`` so no state update is made.

    Approach
    --------
    Collects the ``location`` field from every repo Evidence item.
    These are file paths like ``"src/state.py"`` or path ranges like
    ``"src/graph.py:42-68"``.  Strip line-range suffixes and use the
    clean paths as the "verified repo file" list.
    """
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})  # type: ignore[call-overload]

    # Collect known repo file paths from all repo-detective Evidence
    repo_locations: set[str] = set()
    for criterion_id in _REPO_CRITERIA:
        for ev in evidences.get(criterion_id, []):
            loc = ev.location
            # Strip "path:line_range" suffix if present
            clean = loc.split(":")[0].strip()
            # Only keep plausible file paths (not URLs, not "(not found)")
            if "/" in clean and not clean.startswith("http") and "." in clean.split("/")[-1]:
                repo_locations.add(clean)

    if not repo_locations:
        # Repo clone may have failed — cannot do cross-reference
        return None

    # Collect the claimed paths that DocAnalyst extracted
    claimed_paths: list[str] = []
    for ev in evidences.get("report_accuracy", []):
        if ev.content:
            for line in ev.content.splitlines():
                line = line.strip()
                if line.startswith("claimed:"):
                    claimed_paths.append(line.replace("claimed:", "").strip())

    if not claimed_paths:
        return None  # Nothing to cross-reference

    # Perform the cross-reference
    repo_norm = {p.replace("\\", "/").lstrip("./") for p in repo_locations}
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

    # Read existing list and append — operator.ior will update the key
    existing = list(evidences.get("report_accuracy", []))
    return {"report_accuracy": existing + [xref_evidence]}


# ---------------------------------------------------------------------------
# Node: InterimEnd (placeholder — replaced by Judicial Layer in final)
# ---------------------------------------------------------------------------


def interim_end_node(state: AgentState) -> dict[str, Any]:
    """Placeholder terminal node for the interim submission.

    In the final submission this node is replaced by:
      - Parallel judge fan-out: Prosecutor, Defense, TechLead
      - ChiefJusticeNode (deterministic conflict resolution)
      - AuditReport serialisation to Markdown

    For now it logs the detective-phase summary and exits gracefully.
    """
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})  # type: ignore[call-overload]

    total = sum(len(v) for v in evidences.values())
    found = sum(1 for evs in evidences.values() for e in evs if e.found)

    logger.info(
        "[InterimEnd] Detective phase complete — "
        "%d criteria, %d evidence items (%d found / %d not found). "
        "⚠ INTERIM SUBMISSION: Judicial Layer (Prosecutor → Defense → TechLead "
        "→ ChiefJustice) pending for final submission.",
        len(evidences),
        total,
        found,
        total - found,
    )
    return {}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def build_graph() -> Any:
    """Construct and compile the Automaton Auditor StateGraph.

    Graph structure (interim)
    -------------------------

        START → [repo_investigator ‖ doc_analyst]
              → evidence_aggregator
              → interim_end
              → END

    The fan-out is expressed as two separate edges from START.  LangGraph
    runs both destination nodes concurrently.  The fan-in at
    ``evidence_aggregator`` is implicit: the node executes only after
    *both* upstream branches have finished and their state updates have
    been merged by the ``operator.ior`` reducer.

    LangSmith tracing
    -----------------
    ``.with_config()`` attaches a default run name so every trace in
    LangSmith is labeled "automaton-auditor" rather than an opaque UUID.
    This can be overridden per-invocation.

    Returns
    -------
    Compiled ``StateGraph`` (``CompiledStateGraph``) pre-configured with
    LangSmith metadata via ``.with_config()``.
    """
    builder: StateGraph = StateGraph(AgentState)

    # ── Register nodes ────────────────────────────────────────────────────
    builder.add_node("repo_investigator", repo_investigator_node)
    builder.add_node("doc_analyst", doc_analyst_node)
    builder.add_node("evidence_aggregator", evidence_aggregator_node)
    builder.add_node("interim_end", interim_end_node)

    # ── Detective Fan-Out — both nodes start concurrently from START ──────
    builder.add_edge(START, "repo_investigator")
    builder.add_edge(START, "doc_analyst")

    # ── Detective Fan-In — aggregator waits for BOTH branches ─────────────
    # LangGraph only executes evidence_aggregator once both upstream nodes
    # have completed and their state updates have been reduced.
    builder.add_edge("repo_investigator", "evidence_aggregator")
    builder.add_edge("doc_analyst", "evidence_aggregator")

    # ── Sequential terminal (interim placeholder) ─────────────────────────
    builder.add_edge("evidence_aggregator", "interim_end")
    builder.add_edge("interim_end", END)

    compiled = builder.compile()

    # Attach a default LangSmith run name so every trace is identifiable.
    # LANGCHAIN_TRACING_V2=true must be set in .env for traces to appear.
    return compiled.with_config(
        {
            "run_name": "automaton-auditor",
            "tags": ["detective-layer", "interim-submission"],
            "metadata": {"version": "0.1.0", "layer": "detective"},
        }
    )


# ---------------------------------------------------------------------------
# State factory
# ---------------------------------------------------------------------------


def create_initial_state(repo_url: str, pdf_path: str) -> AgentState:
    """Build a fully typed initial ``AgentState`` for an audit run.

    All fields required by the TypedDict are present.  The ``evidences``
    and ``opinions`` reducers start from their identity elements (``{}``
    and ``[]`` respectively) so the first node update is a clean merge.

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
        "evidences": {},    # identity for operator.ior (dict merge)
        "opinions": [],     # identity for operator.add (list concat)
        "final_report": None,
    }


# ---------------------------------------------------------------------------
# Convenience runner
# ---------------------------------------------------------------------------


def run_interim_audit(
    repo_url: str,
    pdf_path: str,
    langsmith_project: str = "automaton-auditor",
) -> AgentState:
    """Run the full detective swarm and return the enriched ``AgentState``.

    Environment
    -----------
    Set ``LANGCHAIN_TRACING_V2=true`` and ``LANGCHAIN_API_KEY`` in ``.env``
    to stream traces to LangSmith.  The function calls ``load_dotenv()``
    at module import, so a local ``.env`` file is picked up automatically.

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
        Final state after the detective phase; ``state["evidences"]``
        contains all collected Evidence keyed by ``criterion_id``.
    """
    # Ensure the LangSmith project name is set before the graph runs.
    # LANGCHAIN_TRACING_V2 is loaded from .env via load_dotenv() above.
    os.environ.setdefault("LANGCHAIN_PROJECT", langsmith_project)

    graph = build_graph()
    initial_state = create_initial_state(repo_url, pdf_path)

    # Per-run config overrides the default set in build_graph().with_config()
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    run_config: dict[str, Any] = {
        "run_name": f"automaton-auditor | {repo_name}",
        "tags": ["detective-layer", "interim-submission"],
        "metadata": {
            "repo_url": repo_url,
            "pdf_path": pdf_path,
            "submission": "interim",
        },
    }

    logger.info(
        "[Graph] Launching detective swarm — repo: %s | pdf: %s",
        repo_url,
        pdf_path,
    )
    result: AgentState = graph.invoke(initial_state, config=run_config)
    logger.info(
        "[Graph] Audit complete — %d criteria, %d evidence items",
        len(result.get("evidences", {})),  # type: ignore[call-overload]
        sum(len(v) for v in result.get("evidences", {}).values()),  # type: ignore[call-overload]
    )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "build_graph",
    "create_initial_state",
    "run_interim_audit",
    "evidence_aggregator_node",
    "interim_end_node",
    "REQUIRED_INTERIM_CRITERIA",
]

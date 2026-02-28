"""
Detective Layer — LangGraph node functions for the Automaton Auditor swarm.

Each node is a *pure state transformer*:

    Input  — reads ``repo_url``, ``pdf_path``, and ``rubric_dimensions``
              from the shared ``AgentState``.

    Output — returns a partial state dict:
              ``{"evidences": {criterion_id: [Evidence, ...]}}``

Because ``AgentState.evidences`` is typed as
``Annotated[Dict[str, List[Evidence]], operator.ior]``,
LangGraph merges each node's returned dict with the running state via
``state["evidences"] |= node_output["evidences"]``.
Parallel branches write to *different* criterion keys, so they never
overwrite each other — the reducer makes this safe by design.

Parallel execution contract
---------------------------
``repo_investigator_node`` and ``doc_analyst_node`` run concurrently
from START.  Neither may read the other's output from state during
execution — that data is only available AFTER the fan-in
(``evidence_aggregator_node`` in graph.py).

Cross-referencing note
----------------------
``report_accuracy`` requires comparing PDF-claimed file paths against
the actual repository structure.  Because the DocAnalyst runs in
*parallel* with the RepoInvestigator, the repo file listing is not
available at this node's execution time.  The DocAnalyst therefore
extracts claimed paths and records them as Evidence with
``found=False`` (no cross-reference yet).  The ``evidence_aggregator``
performs the secondary cross-reference once both branches have merged
into state.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.state import AgentState, Evidence
from src.tools.doc_tools import DocumentAuditor
from src.tools.repo_tools import CloneError, RepoInvestigator
from src.tools.vision_tools import VisionInspector

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level rubric (loaded once — reliable path via __file__)
# ---------------------------------------------------------------------------

_RUBRIC_PATH: Path = Path(__file__).parent.parent.parent / "rubric" / "rubric.json"


def _load_rubric() -> dict[str, Any]:
    try:
        return json.loads(_RUBRIC_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load rubric from %s: %s", _RUBRIC_PATH, exc)
        return {"dimensions": []}


_RUBRIC: dict[str, Any] = _load_rubric()

# Criterion IDs grouped by the detective responsible for them
_REPO_CRITERIA: frozenset[str] = frozenset(
    d["id"]
    for d in _RUBRIC.get("dimensions", [])
    if d.get("target_artifact") == "github_repo"
)
_PDF_CRITERIA: frozenset[str] = frozenset(
    d["id"]
    for d in _RUBRIC.get("dimensions", [])
    if d.get("target_artifact") in ("pdf_report", "pdf_images")
)

# ---------------------------------------------------------------------------
# RepoInvestigatorNode
# ---------------------------------------------------------------------------


def repo_investigator_node(state: AgentState) -> dict[str, Any]:
    """Clone the target repository and run all forensic protocols.

    Wraps ``RepoInvestigator.run_all()`` which:
    * Validates the URL before any syscall.
    * Clones into a ``tempfile.TemporaryDirectory`` sandbox.
    * Runs AST-based analysis for every ``github_repo`` rubric criterion.
    * Returns criterion-keyed ``Evidence`` objects.

    On any exception the node degrades gracefully: it returns a single
    failure ``Evidence`` for ``git_forensic_analysis`` so the graph can
    continue to the aggregator without crashing.

    Returns
    -------
    dict
        ``{"evidences": {criterion_id: [Evidence, ...]}}``
        merged into ``AgentState.evidences`` via ``operator.ior``.
    """
    repo_url: str = state["repo_url"]
    logger.info("[RepoInvestigator] Starting forensic analysis → %s", repo_url)

    try:
        investigator = RepoInvestigator(repo_url)
        evidence_map = investigator.run_all()
        logger.info(
            "[RepoInvestigator] Completed. Criteria collected: %s | repo_files: %d",
            sorted(evidence_map.keys()),
            len(investigator.repo_files),
        )
        return {"evidences": evidence_map, "repo_files": investigator.repo_files}

    except ValueError as exc:
        # URL validation failure — not a transient error; no retry
        logger.error("[RepoInvestigator] Invalid repository URL: %s", exc)
        return {"evidences": _clone_failure_map(str(exc), repo_url), "repo_files": []}

    except CloneError as exc:
        logger.error("[RepoInvestigator] Clone failed: %s", exc)
        return {"evidences": _clone_failure_map(str(exc), repo_url), "repo_files": []}

    except Exception as exc:  # noqa: BLE001 — surface unexpected errors as Evidence
        logger.exception("[RepoInvestigator] Unexpected error during analysis")
        return {"evidences": _clone_failure_map(str(exc), repo_url), "repo_files": []}


# ---------------------------------------------------------------------------
# DocAnalystNode
# ---------------------------------------------------------------------------


def doc_analyst_node(state: AgentState) -> dict[str, Any]:
    """Parse the architectural PDF and collect document-level Evidence.

    Runs two forensic protocols in sequence:

    1. **Theoretical depth** — searches the PDF for the five rubric-required
       terms (Dialectical Synthesis, Fan-In/Fan-Out, Metacognition, …) and
       assesses whether each appears in a substantive architectural
       explanation or is merely a buzzword.

    2. **Report accuracy (path extraction only)** — extracts every
       ``src/…/file.py`` path mentioned in the PDF.  The cross-reference
       against the actual repository structure is deferred to the
       ``evidence_aggregator_node`` which executes after both parallel
       branches have merged their state.

    Returns
    -------
    dict
        ``{"evidences": {"theoretical_depth": [...], "report_accuracy": [...]}}``
        merged into ``AgentState.evidences`` via ``operator.ior``.
    """
    pdf_path: str = state["pdf_path"]
    logger.info("[DocAnalyst] Starting analysis → %s", pdf_path or "(no PDF provided)")

    # ── Guard: missing or nonexistent PDF ─────────────────────────────────
    if not pdf_path:
        logger.warning("[DocAnalyst] pdf_path is empty — returning absence Evidence")
        return {"evidences": _missing_pdf_map("(not specified)")}

    if not Path(pdf_path).exists():
        logger.warning("[DocAnalyst] PDF not found at '%s'", pdf_path)
        return {"evidences": _missing_pdf_map(pdf_path)}

    # ── Ingest PDF via docling (with automatic fallback) ──────────────────
    auditor = DocumentAuditor()
    try:
        auditor.ingest(pdf_path)
        logger.info("[DocAnalyst] PDF ingested successfully")
    except Exception as exc:  # noqa: BLE001
        logger.error("[DocAnalyst] Ingest failed: %s", exc)
        return {"evidences": _ingest_failure_map(pdf_path, str(exc))}

    # ── Protocol 1: Theoretical depth ─────────────────────────────────────
    theoretical_depth_evidence = auditor.build_theoretical_depth_evidence()
    logger.info(
        "[DocAnalyst] Theoretical depth evidence: found=%s, substantive_terms=%s",
        theoretical_depth_evidence.found,
        # Count substantive hits from the content summary
        theoretical_depth_evidence.content.count("✓ substantive")
        if theoretical_depth_evidence.content
        else 0,
    )

    # ── Protocol 2: Report accuracy — extraction only ─────────────────────
    # Cross-reference is deferred to evidence_aggregator (requires repo
    # file listing from RepoInvestigator which runs in parallel).
    claimed_paths = auditor.extract_file_paths()
    report_accuracy_evidence = Evidence(
        goal=(
            "Extract file paths cited in the PDF and cross-reference against "
            "the actual repository structure"
        ),
        found=bool(claimed_paths),
        content="\n".join(f"  claimed: {p}" for p in claimed_paths) or None,
        location=pdf_path,
        rationale=(
            f"Extracted {len(claimed_paths)} file path(s) from the PDF. "
            "Cross-reference with repository structure is performed by the "
            "EvidenceAggregator after the parallel detective branches merge."
        ),
        confidence=0.85,
        criterion_id="report_accuracy",
    )
    logger.info(
        "[DocAnalyst] Report accuracy: %d paths extracted", len(claimed_paths)
    )

    return {
        "evidences": {
            "theoretical_depth": [theoretical_depth_evidence],
            "report_accuracy": [report_accuracy_evidence],
        }
    }


# ---------------------------------------------------------------------------
# Private helper factories — construct Evidence for failure conditions
# ---------------------------------------------------------------------------


def _clone_failure_map(error_msg: str, repo_url: str) -> dict[str, list[Evidence]]:
    """Return a failure evidence dict for all repo criteria when clone fails."""
    failure_evidence = Evidence(
        goal="Clone repository and run all forensic protocols",
        found=False,
        content=error_msg,
        location=repo_url,
        rationale=(
            "Repository could not be accessed. All downstream forensic "
            "protocols (state management, graph wiring, tool safety, "
            "structured output) cannot be executed."
        ),
        confidence=1.0,
        criterion_id="git_forensic_analysis",
    )
    # Propagate the same failure Evidence to every repo criterion
    # so the aggregator can detect the complete failure in one pass.
    return {criterion: [failure_evidence] for criterion in _REPO_CRITERIA}


def _missing_pdf_map(pdf_path: str) -> dict[str, list[Evidence]]:
    """Return absence Evidence for all PDF criteria when no file is found."""
    absence = Evidence(
        goal="Parse PDF report and run document forensic protocols",
        found=False,
        content=None,
        location=pdf_path,
        rationale="PDF file was not provided or does not exist at the given path.",
        confidence=1.0,
        criterion_id="theoretical_depth",
    )
    return {criterion: [absence] for criterion in _PDF_CRITERIA}


def _ingest_failure_map(
    pdf_path: str, error_msg: str
) -> dict[str, list[Evidence]]:
    """Return failure Evidence for all PDF criteria when docling ingest fails."""
    failure = Evidence(
        goal="Parse PDF report and run document forensic protocols",
        found=False,
        content=error_msg,
        location=pdf_path,
        rationale=(
            "PDF file exists but could not be parsed. "
            "Docling conversion or fallback text extraction failed."
        ),
        confidence=0.90,
        criterion_id="theoretical_depth",
    )
    return {criterion: [failure] for criterion in _PDF_CRITERIA}


# ---------------------------------------------------------------------------
# VisionInspectorNode
# ---------------------------------------------------------------------------


def vision_inspector_node(state: AgentState) -> dict[str, Any]:
    """Render PDF pages and classify architectural diagrams via Claude vision.

    Runs as the third parallel detective branch alongside
    ``repo_investigator_node`` and ``doc_analyst_node``.

    Evaluates rubric criterion ``swarm_visual`` (target_artifact: pdf_images):
    checks whether the report's architectural diagram accurately represents
    the multi-agent swarm with two fan-out/fan-in patterns (Detectives and
    Judges), or whether it shows a misleading linear pipeline.

    Returns
    -------
    dict
        ``{"evidences": {"swarm_visual": [Evidence]}}``
        merged into ``AgentState.evidences`` via ``operator.ior``.
    """
    pdf_path: str = state["pdf_path"]
    logger.info(
        "[VisionInspector] Starting diagram analysis → %s",
        pdf_path or "(no PDF provided)",
    )

    if not pdf_path or not Path(pdf_path).exists():
        logger.warning("[VisionInspector] PDF not found at '%s'", pdf_path)
        absent_evidence = Evidence(
            goal="Verify architectural diagram accurately shows parallel swarm structure",
            found=False,
            content=None,
            location=pdf_path or "(not specified)",
            rationale="PDF file was not provided or does not exist at the given path.",
            confidence=1.0,
            criterion_id="swarm_visual",
        )
        return {"evidences": {"swarm_visual": [absent_evidence]}}

    inspector = VisionInspector()
    try:
        inspector.ingest(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("[VisionInspector] Ingest failed: %s", exc)
        return {
            "evidences": {
                "swarm_visual": [
                    Evidence(
                        goal="Verify architectural diagram accurately shows parallel swarm structure",
                        found=False,
                        content=str(exc),
                        location=pdf_path,
                        rationale="PDF could not be ingested for visual analysis.",
                        confidence=0.90,
                        criterion_id="swarm_visual",
                    )
                ]
            }
        }

    evidence = inspector.build_swarm_visual_evidence()
    logger.info(
        "[VisionInspector] Diagram analysis complete — found=%s confidence=%.2f",
        evidence.found,
        evidence.confidence,
    )
    return {"evidences": {"swarm_visual": [evidence]}}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "repo_investigator_node",
    "doc_analyst_node",
    "vision_inspector_node",
]

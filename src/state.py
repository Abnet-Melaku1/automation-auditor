"""
State definitions for the Automaton Auditor LangGraph swarm.

All shared data structures live here so every layer of the Digital Courtroom
has a single source of truth for types and validation.

Architecture overview
---------------------
          ┌─────────────────────────────────┐
          │           AgentState            │
          │                                 │
          │  repo_url       str             │
          │  pdf_path       str             │
          │  rubric_dimensions  List[Dict]  │
          │                                 │
          │  evidences ──── operator.ior ─► │  (dict merge, safe for
          │                                 │   parallel Detectives)
          │  opinions  ──── operator.add ─► │  (list concat, safe for
          │                                 │   parallel Judges)
          │  final_report   AuditReport     │
          └─────────────────────────────────┘

Reducers
--------
``operator.ior``  ( ``|=`` on dicts )  lets each Detective write its
evidence keyed by ``criterion_id`` without clobbering sibling Detectives.

``operator.add``  ( ``+``  on lists )  lets each Judge append its
``JudicialOpinion`` without clobbering sibling Judges.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

# ---------------------------------------------------------------------------
# Rubric / configuration models (read from rubric.json)
# ---------------------------------------------------------------------------


class RubricDimension(BaseModel):
    """A single scoreable dimension from the machine-readable rubric JSON.

    Loaded at runtime from ``rubric.json`` and injected into
    ``AgentState.rubric_dimensions`` so agents can dynamically adapt
    to rubric updates without code changes.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(description="Unique snake_case identifier for this criterion")
    name: str = Field(description="Human-readable criterion name")
    target_artifact: str = Field(
        description=(
            "Which artifact the detective should inspect: "
            "'github_repo', 'pdf_report', or 'pdf_images'"
        )
    )
    forensic_instruction: str = Field(
        description="Step-by-step evidence-collection protocol for Detectives"
    )
    success_pattern: str = Field(description="Observable pattern indicating a passing grade")
    failure_pattern: str = Field(description="Observable pattern indicating a failing grade")


# ---------------------------------------------------------------------------
# Detective Layer — output models
# ---------------------------------------------------------------------------


class Evidence(BaseModel):
    """Structured, opinion-free output produced by a Detective agent.

    Detectives record *facts* — what exists, where it lives, and how
    confident they are.  They never assign scores; that is the Judiciary's job.

    Fields
    ------
    goal            The specific forensic objective this evidence addresses
                    (maps 1-to-1 with a ``RubricDimension.id``).
    found           Whether the artifact / pattern was discovered.
    content         Extracted snippet (code block, commit list, text excerpt).
    location        Canonical reference: file path + line range OR commit hash.
    rationale       Detective's chain-of-thought explaining confidence.
    confidence      0.0–1.0 numeric certainty score.
    criterion_id    The rubric dimension this evidence belongs to.
    """

    model_config = ConfigDict(frozen=True)

    goal: str = Field(description="Forensic objective this evidence addresses")
    found: bool = Field(description="Whether the artifact or pattern exists")
    content: Optional[str] = Field(
        default=None,
        description="Extracted content snippet (code block, commit messages, text excerpt, etc.)",
    )
    location: str = Field(
        description="File path with optional line range (e.g. 'src/graph.py:42-68') or commit hash",
    )
    rationale: str = Field(
        description="Detective's chain-of-thought rationale supporting the confidence score",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Certainty between 0.0 (complete uncertainty) and 1.0 (irrefutable proof)",
    )
    criterion_id: str = Field(
        description="The RubricDimension.id this evidence was collected for",
    )


# ---------------------------------------------------------------------------
# Judicial Layer — output models
# ---------------------------------------------------------------------------


class JudicialOpinion(BaseModel):
    """Structured verdict produced by one Judge for one rubric criterion.

    Three judges — Prosecutor, Defense, TechLead — each produce a
    ``JudicialOpinion`` independently (in parallel) for every dimension,
    creating the dialectical tension that the ChiefJustice must resolve.

    Fields
    ------
    judge           One of the three judicial personas.
    criterion_id    The rubric dimension being evaluated.
    score           1 (failure) to 5 (excellence).
    argument        Full reasoning and persona-specific analysis.
    cited_evidence  ``Evidence.goal`` values or ``Evidence.location`` refs
                    supporting this opinion.
    """

    judge: Literal["Prosecutor", "Defense", "TechLead"] = Field(
        description="The judicial persona rendering this opinion"
    )
    criterion_id: str = Field(description="RubricDimension.id being evaluated")
    score: int = Field(
        ge=1,
        le=5,
        description="Score from 1 (critical failure) to 5 (exemplary)",
    )
    argument: str = Field(
        description="Full persona-specific reasoning and citation of evidence",
    )
    cited_evidence: List[str] = Field(
        default_factory=list,
        description=(
            "Evidence.goal values or Evidence.location refs cited to "
            "substantiate this opinion"
        ),
    )


# ---------------------------------------------------------------------------
# Supreme Court — output models
# ---------------------------------------------------------------------------


class CriterionResult(BaseModel):
    """Final, binding verdict for one rubric dimension.

    Produced by the ``ChiefJusticeNode`` after applying deterministic
    conflict-resolution rules to the three competing ``JudicialOpinion``
    objects.  When score variance across judges exceeds 2, ``dissent_summary``
    is mandatory.

    Fields
    ------
    dimension_id    Mirrors ``RubricDimension.id``.
    dimension_name  Mirrors ``RubricDimension.name``.
    final_score     Authoritative score after synthesis rules are applied.
    judge_opinions  All three opinions (preserved for transparency).
    dissent_summary Required when max(scores) − min(scores) > 2.
    remediation     Specific, file-level remediation instructions.
    """

    dimension_id: str = Field(description="RubricDimension.id for this verdict")
    dimension_name: str = Field(description="Human-readable dimension name")
    final_score: int = Field(
        ge=1,
        le=5,
        description="Authoritative score after Chief Justice synthesis rules",
    )
    judge_opinions: List[JudicialOpinion] = Field(
        description="All three judicial opinions (Prosecutor, Defense, TechLead)"
    )
    dissent_summary: Optional[str] = Field(
        default=None,
        description=(
            "Mandatory explanation of disagreement when "
            "max(scores) − min(scores) > 2"
        ),
    )
    remediation: str = Field(
        description=(
            "Actionable, file-level instructions for the trainee "
            "(e.g. 'Add operator.ior reducer to evidences field in src/state.py:31')"
        ),
    )


class AuditReport(BaseModel):
    """The authoritative final output of the entire Automaton Auditor swarm.

    Serialized to Markdown by the ``ChiefJusticeNode`` and stored at
    ``AgentState.final_report``.  Structure mirrors the required deliverable
    format: Executive Summary → Criterion Breakdown → Remediation Plan.

    Fields
    ------
    repo_url            Target repository that was audited.
    executive_summary   High-level verdict with aggregate score context.
    overall_score       Weighted mean across all ``CriterionResult.final_score``
                        values (1.0–5.0).
    criteria            Per-dimension breakdowns with opinions and dissent.
    remediation_plan    Prioritised, actionable fix list grouped by criterion.
    """

    repo_url: str = Field(description="The GitHub repository URL that was audited")
    executive_summary: str = Field(
        description="High-level verdict, aggregate score context, and key findings"
    )
    overall_score: float = Field(
        ge=1.0,
        le=5.0,
        description="Weighted mean of all criterion final scores (1.0–5.0)",
    )
    criteria: List[CriterionResult] = Field(
        description="Per-dimension verdicts with judge opinions and dissent summaries"
    )
    remediation_plan: str = Field(
        description=(
            "Prioritised, file-level remediation instructions "
            "grouped by rubric criterion"
        )
    )


# ---------------------------------------------------------------------------
# Graph State — shared across all LangGraph nodes
# ---------------------------------------------------------------------------


class AgentState(TypedDict):
    """Shared mutable state threaded through the entire LangGraph graph.

    Reducer annotations on ``evidences`` and ``opinions`` allow the parallel
    Detective and Judge branches to write concurrently without clobbering
    each other's contributions.

    Reducers
    --------
    evidences : ``operator.ior``
        Dict merge (``|=``).  Each Detective writes its findings keyed by
        ``criterion_id``; parallel branches merge rather than overwrite.

    opinions : ``operator.add``
        List concatenation (``+``).  Each of the three parallel Judge nodes
        appends its ``JudicialOpinion`` objects; all opinions accumulate.

    Lifecycle
    ---------
    1. ``repo_url``, ``pdf_path``, ``rubric_dimensions`` — populated by the
       entry node (ContextBuilder) before any parallel work begins.
    2. ``evidences`` — populated in parallel by RepoInvestigator, DocAnalyst,
       and VisionInspector; merged via ``operator.ior``.
    3. ``opinions`` — populated in parallel by Prosecutor, Defense, TechLead;
       concatenated via ``operator.add``.
    4. ``final_report`` — written once by ChiefJusticeNode at the end.
    """

    # ── Input ───────────────────────────────────────────────────────────────
    repo_url: str
    """GitHub repository URL supplied by the user."""

    pdf_path: str
    """Local filesystem path to the trainee's PDF report."""

    rubric_dimensions: List[Dict[str, Any]]
    """Rubric loaded from ``rubric.json``; each entry is a ``RubricDimension``
    serialized to a plain dict so it is JSON-serialisable by LangGraph."""

    # ── Detective Layer accumulator ─────────────────────────────────────────
    evidences: Annotated[Dict[str, List[Evidence]], operator.ior]
    """Mapping of ``criterion_id`` → list of ``Evidence`` objects.

    ``operator.ior`` (dict ``|=``) merges parallel Detectives' contributions
    so that RepoInvestigator's evidence for 'state_management_rigor' is never
    overwritten by DocAnalyst's evidence for 'theoretical_depth'.
    """

    # ── Judicial Layer accumulator ──────────────────────────────────────────
    opinions: Annotated[List[JudicialOpinion], operator.add]
    """Flat list of all ``JudicialOpinion`` objects from all three Judges.

    ``operator.add`` (list ``+``) concatenates each parallel Judge's output
    so every opinion is preserved for the ChiefJustice to deliberate over.
    """

    # ── Repository file catalog ──────────────────────────────────────────────
    repo_files: List[str]
    """Flat list of all source file paths (POSIX, relative to repo root) found
    in the cloned repository.  Populated by ``repo_investigator_node`` once the
    clone succeeds; empty list when the clone fails.  Consumed by
    ``evidence_aggregator_node`` to cross-reference paths claimed in the PDF
    report.  Not Annotated — only one node writes to this field."""

    # ── Supreme Court output ────────────────────────────────────────────────
    final_report: Optional[AuditReport]
    """The fully synthesised audit report; ``None`` until ChiefJusticeNode runs."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: List[str] = [
    # Rubric / config
    "RubricDimension",
    # Detective layer
    "Evidence",
    # Judicial layer
    "JudicialOpinion",
    # Supreme Court
    "CriterionResult",
    "AuditReport",
    # Graph state
    "AgentState",
]

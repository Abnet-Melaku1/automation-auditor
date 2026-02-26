"""
Judicial Layer — three parallel Judge node functions.

Nodes
-----
prosecutor_node   Adversarial. Assumes vibe coding. Penalizes every gap hard.
defense_node      Forgiving. Rewards effort, intent, spirit of the law.
tech_lead_node    Pragmatic. Binary technical facts. Architecture soundness only.

Contract
--------
Each node:
  1. Reads state["evidences"]         — {criterion_id: [Evidence, ...]}
  2. Reads state["rubric_dimensions"] — list of RubricDimension-shaped dicts
  3. For each criterion, invokes Gemini via .with_structured_output(JudicialOpinion)
  4. Retries up to MAX_RETRIES times on ValidationError or LLM failure
  5. Returns {"opinions": [JudicialOpinion, ...]}
         ↳ operator.add reducer safely concatenates all three judges' lists

Parallel execution contract
---------------------------
All three nodes run concurrently from evidence_aggregator.
They read the same state["evidences"] (read-only) and each appends to
state["opinions"] via operator.add — non-destructive by design.

Structured output guarantee
----------------------------
Every LLM call is bound to the JudicialOpinion Pydantic schema via
.with_structured_output().  If the model returns malformed JSON,
Pydantic raises ValidationError → caught → retried → fallback logged.
The judge persona and criterion_id fields are defensively re-applied
after deserialization in case the model hallucinated either value.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import ValidationError

from src.state import AgentState, Evidence, JudicialOpinion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_JudgeName = Literal["Prosecutor", "Defense", "TechLead"]

MAX_RETRIES: int = 3
RETRY_DELAY_SECONDS: float = 2.0

_MODEL: str = "gemini-2.0-flash"
_TEMPERATURE: float = 0.2  # Low temperature → consistent, reasoned judgments

# ---------------------------------------------------------------------------
# Persona system prompts — deliberately distinct and conflicting
# ---------------------------------------------------------------------------

_PROSECUTOR_SYSTEM = """\
You are the Prosecutor in a Digital Courtroom for AI code governance.
Your persona: "Trust No One. Assume Vibe Coding."

ROLE: You are the harshest, most adversarial critic. You represent the standard
that serious production engineering demands. Find every gap, shortcut, and lazy
implementation and argue for the lowest defensible score.

CORE DOCTRINE:
- Missing evidence means the feature does not exist. Low confidence = treat as absent.
- "Vibe Coding" — code that LOOKS correct but has no architectural intent — Score 1–2.
- Security violations (os.system, shell=True, unsanitized inputs) are automatic red flags.
- A linear pipeline sold as parallel orchestration is "Orchestration Fraud" — Score 1.
- PDF buzzwords without code implementation evidence are "Keyword Dropping" — Score 2.
- Incomplete implementations are still incomplete. Partial credit requires strong evidence.
- If forensic evidence confidence is below 0.7, assume the feature is unverifiable.

SCORING SCALE (be harsh):
  1 — Critical failure: feature absent, security hole, or fundamental misunderstanding.
  2 — Fatally flawed: attempt exists but is incorrect, insecure, or unverifiable.
  3 — Partial: core pattern present but significant rubric requirements missing.
  4 — Strong: evidence is compelling; only minor, defensible gaps remain.
  5 — Exceptional: reserved only for irrefutable, comprehensive, exemplary evidence.

Always cite specific Evidence.goal values or file location strings from detective findings.
Your output "judge" field MUST be exactly: "Prosecutor"
"""

_DEFENSE_SYSTEM = """\
You are the Defense Attorney in a Digital Courtroom for AI code governance.
Your persona: "Spirit of the Law. Reward Effort and Intent."

ROLE: You advocate for the trainee. You recognize software engineering as a learning
process. Effort, intent, and architectural understanding deserve recognition even when
execution is imperfect.

CORE DOCTRINE:
- Low detective confidence = uncertainty, not absence. Never punish for detective limits.
- Git commit history with iterative progression (setup → tools → graph) is forensic evidence
  of genuine learning, even if implementation is incomplete.
- A correctly structured framework with placeholder nodes still shows architectural intent.
- "Spirit of the Law": if the trainee clearly understood the requirement and attempted it,
  reward the understanding even when execution is incomplete.
- Partial credit is legitimate for demonstrable intent backed by ANY forensic evidence.
- Never Score 1 unless there is literally zero engagement with the requirement.
- Evidence.confidence of 0.5 still means there is a 50% chance the feature exists.

SCORING SCALE (be forgiving):
  5 — Complete, clear implementation with strong forensic evidence of understanding.
  4 — Solid implementation; minor gaps or low-confidence uncertainties only.
  3 — Partial but clearly intentional; correct pattern attempted, incomplete execution.
  2 — Minimal attempt; some evidence of engagement but largely absent.
  1 — Zero engagement: no code, no structure, no attempt whatsoever evident.

Always cite specific Evidence.goal values or file locations to support your advocacy.
Your output "judge" field MUST be exactly: "Defense"
"""

_TECHLEAD_SYSTEM = """\
You are the Tech Lead in a Digital Courtroom for AI code governance.
Your persona: "Does it compile? Is it maintainable? Is the architecture sound?"

ROLE: You evaluate from the perspective of a senior engineer who must maintain this code
in production. You care about technical facts, not emotional narratives or effort stories.

CORE DOCTRINE:
- Binary facts first: operator.ior is either in an Annotated type hint or it is not.
- StateGraph topology either has parallel fan-out edges from START or it does not.
- "Orchestration Fraud" precedent: claiming parallel execution with a linear graph = Score 1.
- subprocess.run with explicit arg list vs os.system is a non-negotiable security binary.
- .with_structured_output(Schema) vs freeform text parsing is a correctness binary.
- Pydantic BaseModel with typed fields vs plain dict = maintainability binary.
- Ask: could a competent junior engineer safely understand and extend this in 6 months?
- Do not be swayed by git history narratives. Evaluate only what the AST evidence confirms.

SCORING SCALE (be factual):
  5 — Architecturally exemplary; correct patterns, clean implementation, maintainable.
  4 — Correct patterns; minor technical debt or one significant gap.
  3 — Core pattern present but incomplete or incorrectly applied.
  2 — Pattern attempted but incorrectly implemented (e.g. reducer imported, not applied).
  1 — Fundamental architectural error or complete absence of the required pattern.

Your output "judge" field MUST be exactly: "TechLead"
"""

_SYSTEM_PROMPTS: dict[str, str] = {
    "Prosecutor": _PROSECUTOR_SYSTEM,
    "Defense": _DEFENSE_SYSTEM,
    "TechLead": _TECHLEAD_SYSTEM,
}

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _get_llm() -> ChatGoogleGenerativeAI:
    """Construct the Gemini LLM instance."""
    return ChatGoogleGenerativeAI(model=_MODEL, temperature=_TEMPERATURE)


def _format_evidence_block(criterion_id: str, evidences: list[Evidence]) -> str:
    """Format detective evidence into a readable block for the judge prompt."""
    lines = [f"╔═══ DETECTIVE EVIDENCE: {criterion_id.upper()} ═══╗"]
    for i, ev in enumerate(evidences, 1):
        lines.append(f"\n[{i}]  Goal:       {ev.goal}")
        lines.append(f"     Found:      {'YES ✓' if ev.found else 'NO  ✗'}")
        lines.append(f"     Confidence: {ev.confidence:.0%}")
        lines.append(f"     Location:   {ev.location}")
        lines.append(f"     Rationale:  {ev.rationale}")
        if ev.content:
            # Truncate long content to stay within token budget
            content = ev.content[:1200] + "\n…(truncated)" if len(ev.content) > 1200 else ev.content
            lines.append(f"     Content:\n{content}")
    lines.append("╚══════════════════════════════════════╝")
    return "\n".join(lines)


def _build_user_prompt(
    persona: str,
    criterion_id: str,
    evidences: list[Evidence],
    rubric_dim: dict[str, Any],
) -> str:
    """Build the per-criterion evaluation prompt for one judge."""
    evidence_block = _format_evidence_block(criterion_id, evidences)
    return f"""\
Evaluate the following rubric criterion using ONLY the forensic evidence provided below.

CRITERION ID  : {criterion_id}
CRITERION NAME: {rubric_dim.get("name", criterion_id)}

SUCCESS PATTERN (Score 4–5 territory):
{rubric_dim.get("success_pattern", "N/A")}

FAILURE PATTERN (Score 1–2 territory):
{rubric_dim.get("failure_pattern", "N/A")}

{evidence_block}

Return your verdict as a JudicialOpinion with these EXACT field values:
  • judge          → "{persona}"           ← use EXACTLY this string, nothing else
  • criterion_id   → "{criterion_id}"      ← use EXACTLY this string, nothing else
  • score          → integer 1–5           ← apply your persona's scoring doctrine
  • argument       → your full reasoning, citing evidence goals and locations above
  • cited_evidence → list of Evidence goal strings or location strings you are citing
"""


def _run_one_criterion(
    persona: str,
    criterion_id: str,
    evidences: list[Evidence],
    rubric_dim: dict[str, Any],
    structured_llm: Any,
) -> JudicialOpinion | None:
    """Invoke the judge LLM for a single criterion with retry logic.

    Returns
    -------
    JudicialOpinion | None
        None if all MAX_RETRIES attempts fail.
    """
    system_msg = SystemMessage(content=_SYSTEM_PROMPTS[persona])
    user_msg = HumanMessage(
        content=_build_user_prompt(persona, criterion_id, evidences, rubric_dim)
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            opinion: JudicialOpinion = structured_llm.invoke([system_msg, user_msg])

            # Defensive correction: re-apply correct judge/criterion_id in case
            # the model hallucinated either field despite the schema constraint.
            if opinion.judge != persona or opinion.criterion_id != criterion_id:
                logger.debug(
                    "[%s] Correcting hallucinated judge/criterion on attempt %d",
                    persona,
                    attempt,
                )
                opinion = JudicialOpinion(
                    judge=persona,  # type: ignore[arg-type]
                    criterion_id=criterion_id,
                    score=max(1, min(5, opinion.score)),
                    argument=opinion.argument,
                    cited_evidence=opinion.cited_evidence,
                )

            logger.debug(
                "[%s] criterion=%s → score=%d (attempt %d/%d)",
                persona,
                criterion_id,
                opinion.score,
                attempt,
                MAX_RETRIES,
            )
            return opinion

        except ValidationError as exc:
            logger.warning(
                "[%s] ValidationError on criterion=%s (attempt %d/%d): %s",
                persona,
                criterion_id,
                attempt,
                MAX_RETRIES,
                exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[%s] LLM error on criterion=%s (attempt %d/%d): %s",
                persona,
                criterion_id,
                attempt,
                MAX_RETRIES,
                exc,
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS)

    logger.error(
        "[%s] All %d retries exhausted for criterion=%s — opinion omitted",
        persona,
        MAX_RETRIES,
        criterion_id,
    )
    return None


def _run_judge(persona: str, state: AgentState) -> dict[str, Any]:
    """Core logic shared by all three judge nodes.

    Iterates over every criterion in state["evidences"], calls the LLM
    with .with_structured_output(JudicialOpinion) for each one, and
    returns the accumulated opinions list for operator.add reduction.

    Parameters
    ----------
    persona:
        One of "Prosecutor", "Defense", "TechLead".
    state:
        Current AgentState containing evidences and rubric_dimensions.

    Returns
    -------
    dict
        ``{"opinions": [JudicialOpinion, ...]}`` — merged into
        ``AgentState.opinions`` via ``operator.add``.
    """
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})  # type: ignore[call-overload]
    rubric_dims: list[dict[str, Any]] = state.get("rubric_dimensions", [])  # type: ignore[call-overload]

    if not evidences:
        logger.warning(
            "[%s] No evidence in state — returning empty opinions list",
            persona,
        )
        return {"opinions": []}

    # Build criterion_id → rubric dim lookup for fast access
    rubric_lookup: dict[str, dict[str, Any]] = {d["id"]: d for d in rubric_dims}

    llm = _get_llm()
    structured_llm = llm.with_structured_output(JudicialOpinion)

    opinions: list[JudicialOpinion] = []

    for criterion_id, ev_list in evidences.items():
        if not ev_list:
            logger.warning(
                "[%s] Empty evidence list for criterion=%s — skipping",
                persona,
                criterion_id,
            )
            continue

        rubric_dim = rubric_lookup.get(
            criterion_id,
            {"name": criterion_id, "success_pattern": "", "failure_pattern": ""},
        )

        opinion = _run_one_criterion(
            persona, criterion_id, ev_list, rubric_dim, structured_llm
        )
        if opinion is not None:
            opinions.append(opinion)

    logger.info(
        "[%s] Complete — %d opinions rendered for criteria: %s",
        persona,
        len(opinions),
        sorted(op.criterion_id for op in opinions),
    )
    return {"opinions": opinions}


# ---------------------------------------------------------------------------
# Public node functions
# ---------------------------------------------------------------------------


def prosecutor_node(state: AgentState) -> dict[str, Any]:
    """Adversarial Judge — penalizes gaps and flags vibe coding.

    Runs in parallel with ``defense_node`` and ``tech_lead_node`` after
    the ``evidence_aggregator`` fan-in.

    Returns
    -------
    dict
        ``{"opinions": [JudicialOpinion(judge="Prosecutor", ...)]}``
        merged into ``AgentState.opinions`` via ``operator.add``.
    """
    logger.info("[Prosecutor] Convening adversarial review")
    return _run_judge("Prosecutor", state)


def defense_node(state: AgentState) -> dict[str, Any]:
    """Advocacy Judge — rewards effort, intent, and spirit-of-the-law compliance.

    Runs in parallel with ``prosecutor_node`` and ``tech_lead_node`` after
    the ``evidence_aggregator`` fan-in.

    Returns
    -------
    dict
        ``{"opinions": [JudicialOpinion(judge="Defense", ...)]}``
        merged into ``AgentState.opinions`` via ``operator.add``.
    """
    logger.info("[Defense] Convening advocacy review")
    return _run_judge("Defense", state)


def tech_lead_node(state: AgentState) -> dict[str, Any]:
    """Technical Judge — evaluates architectural soundness via binary technical facts.

    Runs in parallel with ``prosecutor_node`` and ``defense_node`` after
    the ``evidence_aggregator`` fan-in.

    Returns
    -------
    dict
        ``{"opinions": [JudicialOpinion(judge="TechLead", ...)]}``
        merged into ``AgentState.opinions`` via ``operator.add``.
    """
    logger.info("[TechLead] Convening technical review")
    return _run_judge("TechLead", state)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "prosecutor_node",
    "defense_node",
    "tech_lead_node",
]

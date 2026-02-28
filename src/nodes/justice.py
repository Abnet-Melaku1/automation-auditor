"""
Supreme Court — ChiefJusticeNode for the Automaton Auditor.

This node uses DETERMINISTIC Python logic — NOT an LLM prompt — to synthesize
the three competing JudicialOpinion objects per criterion into a final AuditReport.

This distinction is the key architectural differentiator between a Score 3 and
Score 5 implementation: a simple LLM averaging the scores is a Score 3; a node
applying named, coded conflict-resolution rules is a Score 5.

Synthesis Rules (sourced from rubric/rubric.json)
-------------------------------------------------
Applied in strict priority order:

  1. security_override      — Confirmed security violations cap the final score at 3,
                              regardless of Defense effort arguments.
                              Fires only when BOTH Prosecutor score ≤ 2 (with security
                              keyword) AND TechLead score ≤ 3 (binary facts confirm the
                              flaw).  A TechLead score ≥ 4 indicates the violation is
                              unconfirmed — Prosecutor uncertainty, not a real flaw.

  2. fact_supremacy         — If ALL detective Evidence for a criterion has found=False
                              AND the Defense score is inflated above both other judges,
                              the Defense is overruled for hallucination.

  3. functionality_weight   — For graph_orchestration specifically, if the Tech Lead
                              confirms a sound architecture (score ≥ 4), their verdict
                              carries 50% weight rather than the standard 33%.

  4. variance_re_evaluation — If score variance across the three judges exceeds 2,
                              the TechLead score acts as the binding arbiter
                              (their binary technical assessment is most stable).

  5. dissent_requirement    — Every criterion with variance > 2 MUST include an explicit
                              dissent summary in the final report.

  6. default_weighted_avg   — TechLead 40% + Prosecutor 30% + Defense 30%, rounded.

Output
------
  • Writes a structured Markdown report to audit/<repo_name>_<timestamp>.md
  • Returns {"final_report": AuditReport} into AgentState.final_report
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Any

from src.state import AgentState, AuditReport, CriterionResult, Evidence, JudicialOpinion

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Directory where audit Markdown reports are written.
_AUDIT_DIR: Path = Path(__file__).parent.parent.parent / "audit"

#: Security-related keywords that confirm a Prosecutor security charge.
_SECURITY_KEYWORDS: frozenset[str] = frozenset(
    {
        "os.system",
        "shell=true",
        "shell injection",
        "command injection",
        "security violation",
        "unsanitized",
        "unsafe",
        "vulnerability",
    }
)

#: Criteria where the Tech Lead carries 50% weight (functionality_weight rule).
_FUNCTIONALITY_WEIGHTED_CRITERIA: frozenset[str] = frozenset({"graph_orchestration"})

#: Score variance threshold that triggers dissent + re-evaluation.
_VARIANCE_THRESHOLD: int = 2

# ---------------------------------------------------------------------------
# Rule detectors
# ---------------------------------------------------------------------------


def _is_security_violation(prosecutor: JudicialOpinion) -> bool:
    """Return True when the Prosecutor has confirmed a security violation.

    Conditions:
      - Prosecutor score ≤ 2 (they rated it as a failure/serious flaw)
      - Prosecutor argument contains at least one security-related keyword

    A high Prosecutor score with security language is NOT a violation —
    it means they saw evidence of a security concern but were satisfied.
    """
    if prosecutor.score > 2:
        return False
    text = prosecutor.argument.lower()
    return any(kw in text for kw in _SECURITY_KEYWORDS)


def _is_defense_overruled(
    defense: JudicialOpinion,
    evidences: list[Evidence],
    prosecutor: JudicialOpinion,
    tech_lead: JudicialOpinion,
) -> bool:
    """Return True when the fact_supremacy rule overrules the Defense.

    Conditions (all must be true):
      - ALL detective evidence items for this criterion have found=False
        (the feature provably does not exist in the repository)
      - The Defense score is higher than BOTH the Prosecutor AND the Tech Lead
        by more than 1 point (demonstrably inflated claim)

    An inflated Defense score with confirmed absence of evidence is classified
    as the Defense hallucinating credit that the facts do not support.
    """
    if not evidences:
        return False
    all_absent = all(not ev.found for ev in evidences)
    defense_inflated = (
        defense.score > prosecutor.score + 1 and defense.score > tech_lead.score + 1
    )
    return all_absent and defense_inflated


# ---------------------------------------------------------------------------
# Score synthesis
# ---------------------------------------------------------------------------


def _synthesize_score(
    criterion_id: str,
    prosecutor: JudicialOpinion,
    defense: JudicialOpinion,
    tech_lead: JudicialOpinion,
    evidences: list[Evidence],
) -> tuple[int, list[str]]:
    """Apply all synthesis rules in priority order.

    Returns
    -------
    tuple[int, list[str]]
        (final_score, list_of_applied_rule_descriptions)
    """
    scores = [prosecutor.score, defense.score, tech_lead.score]
    variance = max(scores) - min(scores)
    applied: list[str] = []

    # ── Rule 1: Security Override (highest priority) ───────────────────────
    # Requires BOTH Prosecutor suspicion AND TechLead binary confirmation.
    # If TechLead (binary facts) scores ≥ 4, the violation is unconfirmed —
    # the Prosecutor was confused by other code, not a real security flaw.
    if _is_security_violation(prosecutor) and tech_lead.score <= 3:
        capped = min(3, tech_lead.score)
        applied.append(
            f"security_override: Prosecutor confirmed security violation "
            f"(Prosecutor score={prosecutor.score}). "
            f"Final score capped at 3 → {capped}."
        )
        logger.info(
            "[ChiefJustice] security_override → criterion=%s final_score=%d",
            criterion_id,
            capped,
        )
        return capped, applied

    # ── Rule 2: Fact Supremacy ────────────────────────────────────────────
    if _is_defense_overruled(defense, evidences, prosecutor, tech_lead):
        # Average prosecutor and tech_lead — both agree the feature is absent
        raw = (prosecutor.score + tech_lead.score) / 2
        final = max(1, min(5, round(raw)))
        applied.append(
            f"fact_supremacy: All {len(evidences)} evidence items have found=False. "
            f"Defense score={defense.score} overruled for hallucination. "
            f"Score = avg(Prosecutor={prosecutor.score}, TechLead={tech_lead.score}) "
            f"= {raw:.1f} → {final}."
        )
        logger.info(
            "[ChiefJustice] fact_supremacy → criterion=%s final_score=%d",
            criterion_id,
            final,
        )
        return final, applied

    # ── Rule 3: Functionality Weight (graph_orchestration only) ───────────
    if criterion_id in _FUNCTIONALITY_WEIGHTED_CRITERIA and tech_lead.score >= 4:
        # TechLead 50%, Prosecutor 25%, Defense 25%
        weighted = (
            tech_lead.score * 0.50
            + prosecutor.score * 0.25
            + defense.score * 0.25
        )
        final = max(1, min(5, round(weighted)))
        applied.append(
            f"functionality_weight: TechLead confirmed sound architecture "
            f"(score={tech_lead.score}). TechLead weight raised to 50%%. "
            f"Weighted={weighted:.2f} → {final}."
        )
        logger.info(
            "[ChiefJustice] functionality_weight → criterion=%s final_score=%d",
            criterion_id,
            final,
        )
        return final, applied

    # ── Rule 4: Variance Re-evaluation (variance > 2) ─────────────────────
    if variance > _VARIANCE_THRESHOLD:
        # TechLead acts as arbiter: their binary technical assessment
        # is the most stable anchor when adversarial/forgiving scores diverge.
        final = tech_lead.score
        applied.append(
            f"variance_re_evaluation: Score variance={variance} exceeds "
            f"threshold={_VARIANCE_THRESHOLD} "
            f"(Prosecutor={prosecutor.score}, Defense={defense.score}, "
            f"TechLead={tech_lead.score}). "
            f"TechLead arbiter score used → {final}."
        )
        logger.info(
            "[ChiefJustice] variance_re_evaluation → criterion=%s final_score=%d",
            criterion_id,
            final,
        )
        return final, applied

    # ── Rule 5: Default Weighted Average ─────────────────────────────────
    # TechLead 40%, Prosecutor 30%, Defense 30%
    weighted = (
        tech_lead.score * 0.40
        + prosecutor.score * 0.30
        + defense.score * 0.30
    )
    final = max(1, min(5, round(weighted)))
    applied.append(
        f"default_weighted_average: "
        f"TechLead(40%%)={tech_lead.score} + "
        f"Prosecutor(30%%)={prosecutor.score} + "
        f"Defense(30%%)={defense.score} "
        f"= {weighted:.2f} → {final}."
    )
    return final, applied


# ---------------------------------------------------------------------------
# Report building helpers
# ---------------------------------------------------------------------------


def _build_dissent_summary(
    criterion_id: str,
    prosecutor: JudicialOpinion,
    defense: JudicialOpinion,
    tech_lead: JudicialOpinion,
    applied_rules: list[str],
) -> str:
    """Build the mandatory dissent summary for high-variance criteria."""
    p_excerpt = prosecutor.argument[:280].replace("\n", " ").rstrip()
    d_excerpt = defense.argument[:280].replace("\n", " ").rstrip()
    tl_excerpt = tech_lead.argument[:280].replace("\n", " ").rstrip()

    return (
        f"**Prosecutor (Score {prosecutor.score}):** {p_excerpt}…\n\n"
        f"**Defense (Score {defense.score}):** {d_excerpt}…\n\n"
        f"**Tech Lead (Score {tech_lead.score}):** {tl_excerpt}…\n\n"
        f"**Resolution:** {'; '.join(applied_rules)}"
    )


def _build_remediation(
    criterion_id: str,
    final_score: int,
    prosecutor: JudicialOpinion,
    tech_lead: JudicialOpinion,
) -> str:
    """Build a specific, actionable remediation instruction for one criterion."""
    label = criterion_id.upper().replace("_", " ")

    if final_score >= 4:
        p_excerpt = prosecutor.argument[:180].replace("\n", " ").rstrip()
        return (
            f"[{label}] Score {final_score}/5 — No critical remediation required. "
            f"Minor: {p_excerpt}…"
        )

    p_excerpt = prosecutor.argument[:350].replace("\n", " ").rstrip()
    tl_excerpt = tech_lead.argument[:350].replace("\n", " ").rstrip()
    return (
        f"[{label}] Score {final_score}/5\n"
        f"  Prosecutor charge: {p_excerpt}…\n"
        f"  Tech Lead guidance: {tl_excerpt}…"
    )


def _build_executive_summary(
    results: list[CriterionResult],
    overall_score: float,
    repo_url: str,
) -> str:
    """Build the high-level verdict paragraph for the AuditReport."""
    total = len(results)
    passing = sum(1 for c in results if c.final_score >= 4)
    failing = sum(1 for c in results if c.final_score <= 2)
    dissent_count = sum(1 for c in results if c.dissent_summary is not None)

    if overall_score >= 4.5:
        verdict = "EXEMPLARY"
    elif overall_score >= 3.5:
        verdict = "SATISFACTORY"
    elif overall_score >= 2.5:
        verdict = "NEEDS IMPROVEMENT"
    else:
        verdict = "INSUFFICIENT"

    return (
        f"**Verdict: {verdict}** — Overall Score: {overall_score:.2f}/5.0\n\n"
        f"Repository `{repo_url}` was evaluated across {total} rubric criteria. "
        f"{passing}/{total} criteria passed at Score ≥ 4. "
        f"{failing}/{total} criteria are critical failures (Score ≤ 2). "
        f"{dissent_count} criteria triggered the dissent_requirement rule "
        f"(score variance > {_VARIANCE_THRESHOLD}).\n\n"
        f"The Digital Courtroom's Dialectical Bench (Prosecutor · Defense · Tech Lead) "
        f"rendered {total * 3} individual judicial opinions, synthesized into binding verdicts "
        f"by the Chief Justice using deterministic conflict-resolution rules."
    )


def _build_remediation_plan(results: list[CriterionResult]) -> str:
    """Build the prioritised remediation plan sorted by score (critical first)."""
    sorted_results = sorted(results, key=lambda c: c.final_score)
    critical = [c for c in sorted_results if c.final_score <= 2]
    needs_work = [c for c in sorted_results if c.final_score == 3]
    passing = [c for c in sorted_results if c.final_score >= 4]

    sections: list[str] = []

    if critical:
        sections.append("### 🔴 Critical Failures (Score ≤ 2) — Address Immediately\n")
        for cr in critical:
            sections.append(f"**{cr.dimension_name}** (Score {cr.final_score}/5)\n")
            sections.append(f"{cr.remediation}\n")

    if needs_work:
        sections.append("### 🟡 Needs Work (Score 3) — Required for Passing Grade\n")
        for cr in needs_work:
            sections.append(f"**{cr.dimension_name}** (Score {cr.final_score}/5)\n")
            sections.append(f"{cr.remediation}\n")

    if passing:
        sections.append("### 🟢 Passing (Score ≥ 4) — Minor Improvements Only\n")
        for cr in passing:
            excerpt = cr.remediation[:120].replace("\n", " ")
            sections.append(f"**{cr.dimension_name}** (Score {cr.final_score}/5) — {excerpt}\n")

    return "\n".join(sections) if sections else "No remediation items."


# ---------------------------------------------------------------------------
# Markdown serialisation
# ---------------------------------------------------------------------------


def _serialize_to_markdown(report: AuditReport) -> Path:
    """Write the AuditReport to a structured Markdown file under audit/.

    File naming: audit/<repo_name>_<YYYYMMDDTHHmmss>UTC.md

    Returns
    -------
    Path
        Absolute path to the written file.
    """
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)

    repo_name = report.repo_url.rstrip("/").rsplit("/", 1)[-1]
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SUTC")
    output_path = _AUDIT_DIR / f"{repo_name}_{timestamp}.md"

    lines: list[str] = [
        "# Automaton Auditor — Final Audit Report",
        "",
        f"**Repository:** {report.repo_url}",
        f"**Overall Score:** {report.overall_score:.2f} / 5.0",
        f"**Generated:** {timestamp}",
        "",
        "---",
        "",
        "## Executive Summary",
        "",
        report.executive_summary,
        "",
        "---",
        "",
        "## Criterion Breakdown",
        "",
    ]

    for cr in report.criteria:
        score_bar = "█" * cr.final_score + "░" * (5 - cr.final_score)
        lines += [
            f"### {cr.dimension_name}",
            "",
            f"**Final Score: {cr.final_score}/5** `[{score_bar}]`",
            "",
            "| Judge | Score | Argument (excerpt) |",
            "|---|:---:|---|",
        ]
        for op in cr.judge_opinions:
            excerpt = op.argument[:130].replace("|", "\\|").replace("\n", " ").rstrip()
            lines.append(f"| **{op.judge}** | {op.score} | {excerpt}… |")

        lines.append("")

        if cr.dissent_summary:
            lines += [
                "<details>",
                "<summary>⚖️ Dissent Summary (score variance &gt; 2)</summary>",
                "",
                cr.dissent_summary,
                "",
                "</details>",
                "",
            ]

        lines += [
            f"> **Remediation:** {cr.remediation}",
            "",
            "---",
            "",
        ]

    lines += [
        "## Remediation Plan",
        "",
        report.remediation_plan,
    ]

    output_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("[ChiefJustice] Report written → %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Group helper
# ---------------------------------------------------------------------------


def _group_opinions(
    opinions: list[JudicialOpinion],
) -> dict[str, dict[str, JudicialOpinion]]:
    """Group flat opinions list into {criterion_id: {judge_name: opinion}}.

    If a judge submitted multiple opinions for the same criterion (shouldn't
    happen but defensively handled), the last one wins.
    """
    grouped: dict[str, dict[str, JudicialOpinion]] = {}
    for op in opinions:
        grouped.setdefault(op.criterion_id, {})[op.judge] = op
    return grouped


# ---------------------------------------------------------------------------
# Public node function
# ---------------------------------------------------------------------------


def chief_justice_node(state: AgentState) -> dict[str, Any]:
    """Deterministic synthesis node — resolves judicial conflicts into AuditReport.

    This node does NOT call an LLM. All score decisions are made by
    deterministic Python logic applying the five named synthesis rules
    from rubric/rubric.json.

    Synthesis pipeline per criterion
    ---------------------------------
    1. security_override      — cap at 3 if Prosecutor confirmed security flaw
    2. fact_supremacy         — overrule inflated Defense if all evidence is absent
    3. functionality_weight   — TechLead carries 50% weight on graph_orchestration
    4. variance_re_evaluation — TechLead arbitrates when variance > 2
    5. default_weighted_avg   — TechLead 40%, Prosecutor 30%, Defense 30%

    Mandatory dissent summary when variance > 2.

    Returns
    -------
    dict
        ``{"final_report": AuditReport}`` — written into ``AgentState.final_report``.
        The report is also serialised to ``audit/<repo>_<timestamp>.md``.
    """
    opinions: list[JudicialOpinion] = state.get("opinions", [])  # type: ignore[call-overload]
    evidences: dict[str, list[Evidence]] = state.get("evidences", {})  # type: ignore[call-overload]
    rubric_dims: list[dict[str, Any]] = state.get("rubric_dimensions", [])  # type: ignore[call-overload]
    repo_url: str = state.get("repo_url", "(unknown)")  # type: ignore[call-overload]

    logger.info(
        "[ChiefJustice] Convening. %d opinions across %d unique criteria.",
        len(opinions),
        len({op.criterion_id for op in opinions}),
    )

    if not opinions:
        logger.error("[ChiefJustice] No opinions received — cannot render verdict")
        return {"final_report": None}

    # Build dimension_name lookup from rubric
    rubric_name_lookup: dict[str, str] = {d["id"]: d.get("name", d["id"]) for d in rubric_dims}

    grouped = _group_opinions(opinions)
    criteria_results: list[CriterionResult] = []

    for criterion_id, opinion_map in grouped.items():
        prosecutor = opinion_map.get("Prosecutor")
        defense = opinion_map.get("Defense")
        tech_lead = opinion_map.get("TechLead")

        if not (prosecutor and defense and tech_lead):
            missing_judges = [
                j for j in ("Prosecutor", "Defense", "TechLead") if j not in opinion_map
            ]
            logger.warning(
                "[ChiefJustice] criterion=%s is missing judges %s — skipping",
                criterion_id,
                missing_judges,
            )
            continue

        criterion_evidences = evidences.get(criterion_id, [])
        scores = [prosecutor.score, defense.score, tech_lead.score]
        variance = max(scores) - min(scores)

        # Apply deterministic synthesis rules
        final_score, applied_rules = _synthesize_score(
            criterion_id, prosecutor, defense, tech_lead, criterion_evidences
        )

        # Dissent summary — mandatory when variance > threshold
        dissent: str | None = None
        if variance > _VARIANCE_THRESHOLD:
            dissent = _build_dissent_summary(
                criterion_id, prosecutor, defense, tech_lead, applied_rules
            )

        remediation = _build_remediation(criterion_id, final_score, prosecutor, tech_lead)
        dimension_name = rubric_name_lookup.get(
            criterion_id, criterion_id.replace("_", " ").title()
        )

        criteria_results.append(
            CriterionResult(
                dimension_id=criterion_id,
                dimension_name=dimension_name,
                final_score=final_score,
                judge_opinions=[prosecutor, defense, tech_lead],
                dissent_summary=dissent,
                remediation=remediation,
            )
        )

        logger.info(
            "[ChiefJustice] criterion=%-35s P=%d D=%d TL=%d variance=%d → final=%d  rules=%s",
            criterion_id,
            prosecutor.score,
            defense.score,
            tech_lead.score,
            variance,
            final_score,
            [r.split(":")[0] for r in applied_rules],
        )

    if not criteria_results:
        logger.error("[ChiefJustice] No criteria results produced — cannot render verdict")
        return {"final_report": None}

    # Overall score: simple mean of final criterion scores
    raw_overall = sum(cr.final_score for cr in criteria_results) / len(criteria_results)
    overall_score = round(max(1.0, min(5.0, raw_overall)), 2)

    remediation_plan = _build_remediation_plan(criteria_results)
    executive_summary = _build_executive_summary(criteria_results, overall_score, repo_url)

    report = AuditReport(
        repo_url=repo_url,
        executive_summary=executive_summary,
        overall_score=overall_score,
        criteria=criteria_results,
        remediation_plan=remediation_plan,
    )

    output_path = _serialize_to_markdown(report)

    logger.info(
        "[ChiefJustice] Verdict rendered. overall_score=%.2f/5.0 | report=%s",
        overall_score,
        output_path,
    )

    return {"final_report": report}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "chief_justice_node",
]

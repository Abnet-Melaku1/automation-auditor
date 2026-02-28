"""
VisionInspector — multimodal PDF diagram analysis for the Automaton Auditor.

Responsibilities
----------------
1. Render each PDF page to a PNG image using pypdfium2 (already bundled
   as a docling dependency — zero additional installs required).
2. For each rendered page, invoke Claude claude-haiku via the existing
   LangChain–Anthropic integration to classify the diagram.
3. Return a typed ``Evidence`` object for the ``swarm_visual`` rubric
   criterion.

Graceful degradation
--------------------
Every external dependency (pypdfium2, Pillow, LangChain, API key) is
guarded with a try/except.  If any layer fails, the node falls back to
returning an ``Evidence(found=False)`` rather than crashing the graph.

Rubric criterion evaluated
--------------------------
``swarm_visual`` (target_artifact: "pdf_images")
  Success: Diagram accurately shows two fan-out/fan-in patterns —
           one for Detectives, one for Judges — with explicit
           parallel branches.
  Failure: Generic linear flowchart, or no diagram at all.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.state import Evidence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vision prompt — sent to Claude claude-haiku with each page image
# ---------------------------------------------------------------------------

_VISION_PROMPT = """\
You are a technical diagram auditor reviewing an architectural diagram from a \
student LangGraph project report.

Examine this page carefully and answer ONLY these questions (one per line):

has_diagram: yes or no
diagram_type: one of [langgraph_state_machine, sequence_diagram, flowchart, other, none]
shows_parallel: yes or no  (concurrent/parallel branches visible)
fan_out_fan_in: yes or no  (single node splitting into multiple, then merging back)
detective_branch: yes or no  (multiple detector/investigator agents in parallel)
judge_branch: yes or no  (multiple judge/evaluator agents in parallel)
linear_pipeline: yes or no  (purely sequential, no parallel branches shown)
assessment: one of [accurate, misleading, absent]
notes: one sentence summary

Definitions:
- accurate = diagram correctly shows a multi-agent swarm with parallel branches
- misleading = diagram shows incorrect/linear flow that contradicts parallel architecture
- absent = no architectural diagram on this page
"""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


@dataclass
class DiagramAnalysis:
    """Classification result for one rendered PDF page."""

    page_number: int
    has_diagram: bool
    diagram_type: str
    shows_parallel: bool
    fan_out_fan_in: bool
    detective_branch: bool
    judge_branch: bool
    linear_pipeline: bool
    assessment: str  # "accurate" | "misleading" | "absent"
    notes: str


# ---------------------------------------------------------------------------
# VisionInspector
# ---------------------------------------------------------------------------


class VisionInspector:
    """Render PDF pages and classify architectural diagrams via Claude vision.

    Lifecycle
    ---------
    1. ``ingest(pdf_path)`` — render pages to base64-encoded PNG images.
    2. ``build_swarm_visual_evidence()`` — invoke Claude vision per page,
       aggregate results, return a typed ``Evidence`` for ``swarm_visual``.
    """

    #: Maximum pages to render and analyse (cost/latency guard)
    MAX_PAGES: int = 6

    def __init__(self) -> None:
        self._pages_b64: list[tuple[int, str]] = []  # [(page_no, b64_png)]
        self._pdf_path: str = ""
        self._ingested: bool = False

    # ── Ingestion ──────────────────────────────────────────────────────────

    def ingest(self, pdf_path: str) -> None:
        """Render the first ``MAX_PAGES`` pages of *pdf_path* to PNG images.

        Parameters
        ----------
        pdf_path:
            Path to the PDF file.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        """
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        self._pdf_path = str(path)
        self._pages_b64 = self._render_pages(path)
        self._ingested = True
        logger.info(
            "[VisionInspector] Ingested %d page(s) from %s",
            len(self._pages_b64),
            path.name,
        )

    def _render_pages(self, path: Path) -> list[tuple[int, str]]:
        """Render pages to base64 PNG using pypdfium2 + Pillow."""
        try:
            import pypdfium2 as pdfium  # type: ignore[import]
        except ImportError:
            logger.warning("[VisionInspector] pypdfium2 not available — cannot render pages")
            return []

        try:
            from PIL import Image  # type: ignore[import]  # noqa: F401
        except ImportError:
            logger.warning("[VisionInspector] Pillow not available — cannot encode PNG")
            return self._render_pages_raw(path)

        results: list[tuple[int, str]] = []
        try:
            pdf = pdfium.PdfDocument(str(path))
            total = len(pdf)
            limit = min(total, self.MAX_PAGES)
            logger.info("[VisionInspector] Rendering %d/%d pages", limit, total)

            for page_no in range(1, limit + 1):
                try:
                    page = pdf[page_no - 1]
                    bitmap = page.render(scale=1.5)
                    pil_image = bitmap.to_pil()
                    buf = io.BytesIO()
                    pil_image.save(buf, format="PNG")
                    b64 = base64.b64encode(buf.getvalue()).decode()
                    results.append((page_no, b64))
                except Exception as exc:
                    logger.debug(
                        "[VisionInspector] Page %d render failed: %s", page_no, exc
                    )
        except Exception as exc:
            logger.warning("[VisionInspector] PDF render failed: %s", exc)

        return results

    def _render_pages_raw(self, path: Path) -> list[tuple[int, str]]:
        """Fallback: render using pypdfium2 without Pillow (BGRA raw bytes → PNG header)."""
        try:
            import pypdfium2 as pdfium  # type: ignore[import]
            import struct
            import zlib

            results: list[tuple[int, str]] = []
            pdf = pdfium.PdfDocument(str(path))
            limit = min(len(pdf), self.MAX_PAGES)

            for page_no in range(1, limit + 1):
                try:
                    page = pdf[page_no - 1]
                    bitmap = page.render(scale=1.0)
                    # Extract raw bitmap — BGRA format from pypdfium2
                    width, height = bitmap.width, bitmap.height
                    raw: bytes = bytes(bitmap)

                    # Convert BGRA → RGBA for PNG encoding
                    rgba = bytearray(len(raw))
                    for i in range(0, len(raw), 4):
                        rgba[i] = raw[i + 2]   # R ← B
                        rgba[i + 1] = raw[i + 1]  # G
                        rgba[i + 2] = raw[i]   # B ← R
                        rgba[i + 3] = raw[i + 3]  # A

                    png_bytes = _encode_png(bytes(rgba), width, height)
                    b64 = base64.b64encode(png_bytes).decode()
                    results.append((page_no, b64))
                except Exception as exc:
                    logger.debug("[VisionInspector] Raw render page %d failed: %s", page_no, exc)
            return results
        except Exception as exc:
            logger.warning("[VisionInspector] Raw render failed: %s", exc)
            return []

    # ── Vision analysis ────────────────────────────────────────────────────

    def analyze_diagrams(self) -> list[DiagramAnalysis]:
        """Call Claude claude-haiku vision on each rendered page.

        Returns
        -------
        list[DiagramAnalysis]
            One entry per page that was successfully analysed.
            Empty list when the LLM is unavailable or all calls fail.
        """
        if not self._ingested or not self._pages_b64:
            return []

        llm = _build_vision_llm()
        if llm is None:
            return []

        analyses: list[DiagramAnalysis] = []
        for page_no, b64 in self._pages_b64:
            try:
                from langchain_core.messages import HumanMessage  # type: ignore[import]

                response = llm.invoke(
                    [
                        HumanMessage(
                            content=[
                                {"type": "text", "text": _VISION_PROMPT},
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{b64}"
                                    },
                                },
                            ]
                        )
                    ]
                )
                analysis = _parse_vision_response(str(response.content), page_no)
                analyses.append(analysis)
                logger.debug(
                    "[VisionInspector] Page %d → has_diagram=%s assessment=%s",
                    page_no,
                    analysis.has_diagram,
                    analysis.assessment,
                )
            except Exception as exc:
                logger.warning(
                    "[VisionInspector] Vision call failed for page %d: %s", page_no, exc
                )

        return analyses

    # ── Evidence builder ───────────────────────────────────────────────────

    def build_swarm_visual_evidence(self) -> Evidence:
        """Return typed Evidence for the ``swarm_visual`` rubric criterion.

        Calls ``analyze_diagrams()`` internally; safe to call even when
        rendering or LLM analysis failed (degrades to ``found=False``).
        """
        if not self._ingested:
            return _absent_evidence(
                self._pdf_path or "(not ingested)",
                "VisionInspector was not ingested before calling build_swarm_visual_evidence().",
                confidence=0.50,
            )

        if not self._pages_b64:
            return _absent_evidence(
                self._pdf_path,
                "Could not render PDF pages — pypdfium2 or Pillow unavailable, or PDF is empty.",
                confidence=0.70,
            )

        analyses = self.analyze_diagrams()

        if not analyses:
            return Evidence(
                goal="Verify architectural diagram accurately shows parallel swarm structure",
                found=False,
                content=(
                    f"PDF rendered {len(self._pages_b64)} page(s) but vision analysis "
                    "could not be completed (LLM unavailable or all calls failed)."
                ),
                location=self._pdf_path,
                rationale=(
                    f"Rendered {len(self._pages_b64)} page(s) from PDF. "
                    "Vision analysis step failed — API key missing or LLM error."
                ),
                confidence=0.40,
                criterion_id="swarm_visual",
            )

        # Aggregate findings
        diagrams = [a for a in analyses if a.has_diagram]
        accurate = [a for a in diagrams if a.assessment == "accurate"]
        misleading = [a for a in diagrams if a.assessment == "misleading"]
        has_parallel = any(a.shows_parallel for a in diagrams)
        has_fan_pattern = any(a.fan_out_fan_in for a in diagrams)
        has_detective_branch = any(a.detective_branch for a in diagrams)
        has_judge_branch = any(a.judge_branch for a in diagrams)
        linear_only = all(a.linear_pipeline for a in diagrams) if diagrams else True

        passes = (
            bool(accurate)
            and has_parallel
            and has_fan_pattern
            and not linear_only
        )

        # Build content summary
        lines = [f"Pages analysed: {len(analyses)} | Pages with diagrams: {len(diagrams)}"]
        for a in diagrams:
            lines.append(
                f"  Page {a.page_number}: type={a.diagram_type} "
                f"parallel={'yes' if a.shows_parallel else 'no'} "
                f"fan-out/fan-in={'yes' if a.fan_out_fan_in else 'no'} "
                f"detective_branch={'yes' if a.detective_branch else 'no'} "
                f"judge_branch={'yes' if a.judge_branch else 'no'} "
                f"assessment={a.assessment}"
            )
            if a.notes:
                lines.append(f"    {a.notes}")

        return Evidence(
            goal="Verify architectural diagram accurately shows parallel swarm structure",
            found=passes,
            content="\n".join(lines),
            location=self._pdf_path,
            rationale=(
                f"Analysed {len(analyses)} page(s). "
                f"Diagrams found: {len(diagrams)} (accurate={len(accurate)}, misleading={len(misleading)}). "
                f"Parallel execution shown: {'yes' if has_parallel else 'no'}. "
                f"Fan-out/fan-in shown: {'yes' if has_fan_pattern else 'no'}. "
                f"Detective parallel branch: {'yes' if has_detective_branch else 'no'}. "
                f"Judge parallel branch: {'yes' if has_judge_branch else 'no'}. "
                f"Linear-only: {'yes' if linear_only else 'no'}."
            ),
            confidence=0.88 if analyses else 0.40,
            criterion_id="swarm_visual",
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _build_vision_llm() -> Any | None:
    """Build a Claude claude-haiku LLM instance for vision analysis, or return None."""
    try:
        from langchain_anthropic import ChatAnthropic  # type: ignore[import]
    except ImportError:
        logger.warning("[VisionInspector] langchain_anthropic not installed")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("[VisionInspector] ANTHROPIC_API_KEY not set — skipping vision analysis")
        return None

    try:
        return ChatAnthropic(
            model="claude-haiku-4-5-20251001",
            api_key=api_key,
            max_tokens=512,
        )
    except Exception as exc:
        logger.warning("[VisionInspector] Failed to build LLM: %s", exc)
        return None


def _parse_vision_response(response: str, page_no: int) -> DiagramAnalysis:
    """Parse Claude's key: value text response into a DiagramAnalysis."""

    def _bool(key: str) -> bool:
        for line in response.lower().splitlines():
            stripped = line.strip()
            if stripped.startswith(key.lower() + ":"):
                return "yes" in stripped.split(":", 1)[-1]
        return False

    def _str(key: str, default: str = "") -> str:
        for line in response.lower().splitlines():
            stripped = line.strip()
            if stripped.startswith(key.lower() + ":"):
                return stripped.split(":", 1)[-1].strip()
        return default

    return DiagramAnalysis(
        page_number=page_no,
        has_diagram=_bool("has_diagram"),
        diagram_type=_str("diagram_type", "unknown"),
        shows_parallel=_bool("shows_parallel"),
        fan_out_fan_in=_bool("fan_out_fan_in"),
        detective_branch=_bool("detective_branch"),
        judge_branch=_bool("judge_branch"),
        linear_pipeline=_bool("linear_pipeline"),
        assessment=_str("assessment", "absent"),
        notes=_str("notes", ""),
    )


def _absent_evidence(location: str, rationale: str, confidence: float) -> Evidence:
    return Evidence(
        goal="Verify architectural diagram accurately shows parallel swarm structure",
        found=False,
        content=None,
        location=location,
        rationale=rationale,
        confidence=confidence,
        criterion_id="swarm_visual",
    )


def _encode_png(rgba: bytes, width: int, height: int) -> bytes:
    """Minimal pure-Python RGBA → PNG encoder (no Pillow required)."""
    import struct
    import zlib

    def png_chunk(chunk_type: bytes, data: bytes) -> bytes:
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = png_chunk(b"IHDR", ihdr_data)

    raw_rows = b""
    row_size = width * 4
    for y in range(height):
        raw_rows += b"\x00" + rgba[y * row_size: (y + 1) * row_size]

    idat = png_chunk(b"IDAT", zlib.compress(raw_rows, 6))
    iend = png_chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__: list[str] = [
    "DiagramAnalysis",
    "VisionInspector",
]

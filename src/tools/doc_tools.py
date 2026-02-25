"""
Forensic document analysis tools for the DocAnalyst detective.

Design overview
---------------
DocumentAuditor  — stateful class that owns one parsed document lifecycle:
                   ingest once, then query many times.

The "RAG-lite" pipeline
-----------------------
1. ``ingest(pdf_path)``
       Uses docling's ``DocumentConverter`` to parse the PDF into a structured
       ``DoclingDocument``.  Falls back to raw text extraction if the optional
       ``HybridChunker`` model download is unavailable.

2. Paragraph-level chunking
       The full markdown export is split on blank lines.  Each paragraph becomes
       a ``DocumentChunk`` with an index and an estimated page number derived
       from docling's structural output.

3. ``search_term(term)``
       Scans every chunk for *term* (case-insensitive).  For each hit, grabs the
       surrounding paragraph as context.  Then applies a *substantiveness check*:
       a term is "substantive" only when it appears alongside implementation
       keywords ("implemented", "via", "using", "through", "by", "which",
       "because", "fan-out", "graph", etc.) — not just dropped as a buzzword.

4. ``extract_file_paths()``
       Applies a regex over the full text to collect every Python file path
       mentioned in the report (``src/…/file.py`` patterns).

5. ``cross_reference_paths(repo_files)``
       Compares the extracted paths against a list of files that actually exist
       in the repository, producing Verified and Hallucinated lists.

6. ``build_evidence_*`` helpers
       Wrap each analysis result in a typed ``Evidence`` object ready for
       ``AgentState.evidences``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.state import Evidence

# ---------------------------------------------------------------------------
# Regex for extracting file paths from PDF text
# ---------------------------------------------------------------------------

# Matches patterns like src/state.py, src/nodes/judges.py, src/tools/repo_tools.py
# Also captures paths quoted with backticks or inside "…"
_FILE_PATH_RE = re.compile(
    r"""
    (?:                          # optional surrounding quote characters
        [`"']?
    )
    (                            # capture group 1: the path itself
        (?:src|tests?|docs?|scripts?)  # must start with a known top-level dir
        /                        # first separator
        [\w./\-]+                # path body (word chars, dots, slashes, dashes)
        \.(?:py|json|toml|md|txt|yaml|yml)  # must end with a known extension
    )
    (?:[`"']?)                   # optional closing quote
    """,
    re.VERBOSE,
)

# Substantiveness indicators: terms that signal genuine explanation rather
# than keyword-dropping
_SUBSTANTIVE_VERBS = frozenset(
    {
        "implemented",
        "implement",
        "achieved",
        "achieve",
        "executes",
        "execute",
        "using",
        "through",
        "via",
        "by",
        "which",
        "because",
        "whereby",
        "enables",
        "allows",
        "ensures",
        "represents",
        "in our",
        "in this",
        "the three",
        "the judges",
        "the detectives",
        "fan-out",
        "fan-in",
        "parallel",
        "graph",
        "node",
    }
)

# ---------------------------------------------------------------------------
# Intermediate result models
# ---------------------------------------------------------------------------


class DocumentChunk(BaseModel):
    """A single paragraph extracted from the PDF."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(description="Zero-based position in the chunk sequence")
    text: str = Field(description="Raw paragraph text")
    page_number: Optional[int] = Field(
        default=None, description="Best-effort page number (None if unavailable)"
    )
    heading: Optional[str] = Field(
        default=None, description="Nearest section heading, if captured by docling"
    )


class TermOccurrence(BaseModel):
    """A single hit for a searched term within a ``DocumentChunk``."""

    model_config = ConfigDict(frozen=True)

    chunk_index: int
    page_number: Optional[int]
    context: str = Field(
        description="The full paragraph surrounding the term"
    )
    in_substantive_context: bool = Field(
        description=(
            "True when the occurrence is accompanied by implementation-level language, "
            "not merely dropped as a buzzword"
        )
    )


class TermSearchResult(BaseModel):
    """Complete analysis for one searched term across the full document."""

    model_config = ConfigDict(frozen=True)

    term: str
    found: bool
    occurrences: list[TermOccurrence] = Field(default_factory=list)
    is_substantive: bool = Field(
        description=(
            "True if at least one occurrence appears in a genuine architectural "
            "explanation (not keyword-dropping)"
        )
    )
    substantive_count: int = Field(
        description="Number of occurrences judged to be substantive"
    )
    total_count: int = Field(description="Total number of occurrences")


class PathCrossReferenceResult(BaseModel):
    """Cross-reference result: claimed paths vs. actual repository structure."""

    model_config = ConfigDict(frozen=True)

    claimed_paths: list[str] = Field(
        description="All file paths extracted from the PDF report"
    )
    verified_paths: list[str] = Field(
        description="Paths that exist in the actual repository"
    )
    hallucinated_paths: list[str] = Field(
        description="Paths mentioned in the report that do NOT exist in the repo"
    )
    hallucination_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="Fraction of claimed paths that are hallucinated (0.0 = perfect)",
    )


# ---------------------------------------------------------------------------
# Internal dataclass for mutable auditor state (not exposed publicly)
# ---------------------------------------------------------------------------


@dataclass
class _AuditorState:
    """Mutable internal state owned by DocumentAuditor."""

    chunks: list[DocumentChunk] = field(default_factory=list)
    full_text: str = ""
    source_path: str = ""
    ingested: bool = False


# ---------------------------------------------------------------------------
# DocumentAuditor — the main class
# ---------------------------------------------------------------------------


class DocumentAuditor:
    """Parse an architectural PDF report and expose forensic query methods.

    The auditor follows an ingest-once / query-many lifecycle:

    >>> auditor = DocumentAuditor()
    >>> auditor.ingest("reports/interim_report.pdf")
    >>> result = auditor.search_term("Dialectical Synthesis")
    >>> cross_ref = auditor.cross_reference_paths(repo_files=[...])
    """

    # Terms the rubric explicitly requires to appear in the report
    REQUIRED_TERMS: tuple[str, ...] = (
        "Dialectical Synthesis",
        "Fan-In",
        "Fan-Out",
        "Metacognition",
        "State Synchronization",
    )

    def __init__(self) -> None:
        self._state = _AuditorState()

    # ── Ingestion ──────────────────────────────────────────────────────────

    def ingest(self, pdf_path: str) -> None:
        """Parse *pdf_path* and build the internal chunk index.

        Uses docling's ``DocumentConverter`` as the primary parser.  If docling
        is unavailable (e.g. missing model weights in CI), falls back to a
        simple binary-text extraction so the auditor remains usable.

        Parameters
        ----------
        pdf_path:
            Absolute or relative path to the PDF file.

        Raises
        ------
        FileNotFoundError
            If the PDF does not exist at the given path.
        """
        path = Path(pdf_path)
        if not path.exists():
            raise FileNotFoundError(f"PDF not found: {pdf_path}")

        full_text, chunks = self._parse_with_docling(path)
        self._state.full_text = full_text
        self._state.chunks = chunks
        self._state.source_path = str(path)
        self._state.ingested = True

    def _parse_with_docling(
        self, path: Path
    ) -> tuple[str, list[DocumentChunk]]:
        """Attempt docling parse; fall back to paragraph splitting on failure."""
        try:
            return self._docling_parse(path)
        except Exception:  # noqa: BLE001 — intentional broad catch for optional dep
            return self._fallback_parse(path)

    def _docling_parse(self, path: Path) -> tuple[str, list[DocumentChunk]]:
        """Primary parser using docling's DocumentConverter.

        Docling converts the PDF into a ``DoclingDocument``, then we:
        1. Export to markdown for full-text search.
        2. Attempt to use ``HybridChunker`` for semantic chunking.
        3. Fall back to paragraph splitting if the chunker is unavailable.
        """
        from docling.document_converter import DocumentConverter  # type: ignore[import]

        converter = DocumentConverter()
        result = converter.convert(str(path))
        doc = result.document
        full_text: str = doc.export_to_markdown()

        chunks = self._chunk_with_docling(doc, full_text)
        return full_text, chunks

    def _chunk_with_docling(self, doc: object, full_text: str) -> list[DocumentChunk]:
        """Try HybridChunker; fall back to paragraph splitting."""
        try:
            return self._hybrid_chunk(doc)
        except Exception:  # noqa: BLE001
            return _paragraph_chunks(full_text)

    def _hybrid_chunk(self, doc: object) -> list[DocumentChunk]:
        """Use docling's HybridChunker for semantically coherent chunks."""
        from docling.chunking import HybridChunker  # type: ignore[import]

        chunker = HybridChunker()
        chunks: list[DocumentChunk] = []

        for idx, chunk in enumerate(chunker.chunk(doc)):  # type: ignore[attr-defined]
            text = chunk.text
            page_no: Optional[int] = None
            heading: Optional[str] = None

            # Safely extract page number and heading from chunk metadata
            try:
                meta = chunk.meta
                if hasattr(meta, "doc_items"):
                    for item, _ in meta.doc_items:
                        if hasattr(item, "prov") and item.prov:
                            page_no = item.prov[0].page_no
                            break
                if hasattr(meta, "headings") and meta.headings:
                    heading = meta.headings[-1]
            except (AttributeError, IndexError, TypeError):
                pass  # metadata shape varies across docling versions

            chunks.append(
                DocumentChunk(
                    index=idx,
                    text=text,
                    page_number=page_no,
                    heading=heading,
                )
            )
        return chunks

    def _fallback_parse(self, path: Path) -> tuple[str, list[DocumentChunk]]:
        """Binary-safe text extraction when docling is unavailable."""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise RuntimeError(f"Could not read PDF file: {path}") from exc
        return text, _paragraph_chunks(text)

    # ── Term search ────────────────────────────────────────────────────────

    def search_term(self, term: str) -> TermSearchResult:
        """Search for *term* across all chunks and assess substantiveness.

        Substantiveness check
        ---------------------
        A hit is "substantive" if the containing paragraph also contains at
        least one word from ``_SUBSTANTIVE_VERBS``.  This filters out term-
        dropping in executive summaries and detects genuine architectural
        explanation.

        Parameters
        ----------
        term:
            Case-insensitive search term (e.g. "Dialectical Synthesis").
        """
        self._assert_ingested()
        term_lower = term.lower()

        occurrences: list[TermOccurrence] = []
        for chunk in self._state.chunks:
            if term_lower not in chunk.text.lower():
                continue
            is_sub = _is_substantive(chunk.text)
            occurrences.append(
                TermOccurrence(
                    chunk_index=chunk.index,
                    page_number=chunk.page_number,
                    context=chunk.text,
                    in_substantive_context=is_sub,
                )
            )

        substantive_count = sum(1 for o in occurrences if o.in_substantive_context)

        return TermSearchResult(
            term=term,
            found=bool(occurrences),
            occurrences=occurrences,
            is_substantive=substantive_count > 0,
            substantive_count=substantive_count,
            total_count=len(occurrences),
        )

    def search_all_required_terms(self) -> dict[str, TermSearchResult]:
        """Run ``search_term`` for every term required by the rubric."""
        return {term: self.search_term(term) for term in self.REQUIRED_TERMS}

    # ── File path extraction ───────────────────────────────────────────────

    def extract_file_paths(self) -> list[str]:
        """Return every Python/config file path mentioned in the report.

        Uses a regex that matches ``src/…/file.py`` patterns (quoted or bare).
        """
        self._assert_ingested()
        return sorted(set(_FILE_PATH_RE.findall(self._state.full_text)))

    def cross_reference_paths(
        self, repo_files: list[str]
    ) -> PathCrossReferenceResult:
        """Compare report-claimed paths against *repo_files* from the actual repo.

        Parameters
        ----------
        repo_files:
            List of POSIX-style relative paths that exist in the repository
            (e.g. ``["src/state.py", "src/tools/repo_tools.py", …]``).
        """
        claimed = self.extract_file_paths()
        normalised_repo = {f.replace("\\", "/").lstrip("./") for f in repo_files}

        verified: list[str] = []
        hallucinated: list[str] = []
        for path in claimed:
            normalised = path.replace("\\", "/").lstrip("./")
            (verified if normalised in normalised_repo else hallucinated).append(path)

        rate = len(hallucinated) / len(claimed) if claimed else 0.0

        return PathCrossReferenceResult(
            claimed_paths=claimed,
            verified_paths=verified,
            hallucinated_paths=hallucinated,
            hallucination_rate=rate,
        )

    # ── Evidence builders ──────────────────────────────────────────────────

    def build_theoretical_depth_evidence(self) -> Evidence:
        """Run all required-term searches and return an Evidence object.

        Covers rubric criterion ``theoretical_depth`` (target: pdf_report).
        """
        self._assert_ingested()
        results = self.search_all_required_terms()

        found_substantive = {
            term for term, r in results.items() if r.is_substantive
        }
        found_present = {term for term, r in results.items() if r.found}
        missing = set(self.REQUIRED_TERMS) - found_present

        all_substantive = len(found_substantive) == len(self.REQUIRED_TERMS)
        any_found = bool(found_present)

        summary_lines = []
        for term, r in results.items():
            tag = (
                "✓ substantive"
                if r.is_substantive
                else ("~ keyword-drop" if r.found else "✗ absent")
            )
            summary_lines.append(f"  {tag}  {term}  (×{r.total_count})")

        content = "\n".join(summary_lines)

        return Evidence(
            goal=(
                "Verify 'Dialectical Synthesis', 'Fan-In/Fan-Out', 'Metacognition', "
                "'State Synchronization' appear in substantive architectural explanations"
            ),
            found=any_found and len(found_substantive) >= 3,
            content=content,
            location=self._state.source_path,
            rationale=(
                f"Substantive hits: {len(found_substantive)}/{len(self.REQUIRED_TERMS)}. "
                f"Missing terms: {sorted(missing) or 'none'}. "
                f"{'All key terms explained with context.' if all_substantive else 'Some terms only dropped as buzzwords.'}"
            ),
            confidence=0.90 if any_found else 0.95,
            criterion_id="theoretical_depth",
        )

    def build_report_accuracy_evidence(
        self, repo_files: list[str]
    ) -> Evidence:
        """Cross-reference claimed file paths against the repo and return Evidence.

        Covers rubric criterion ``report_accuracy`` (target: pdf_report).

        Parameters
        ----------
        repo_files:
            Flat list of POSIX paths that exist in the cloned repository.
        """
        self._assert_ingested()
        xref = self.cross_reference_paths(repo_files)

        passes = (
            bool(xref.claimed_paths)
            and xref.hallucination_rate == 0.0
        )

        content_lines = [
            f"Claimed paths ({len(xref.claimed_paths)}):",
            *[f"  ✓ {p}" for p in xref.verified_paths],
            *[f"  ✗ HALLUCINATED: {p}" for p in xref.hallucinated_paths],
        ]

        return Evidence(
            goal=(
                "Verify all file paths cited in the PDF report "
                "exist in the actual repository"
            ),
            found=passes,
            content="\n".join(content_lines),
            location=self._state.source_path,
            rationale=(
                f"Claimed: {len(xref.claimed_paths)}, "
                f"Verified: {len(xref.verified_paths)}, "
                f"Hallucinated: {len(xref.hallucinated_paths)} "
                f"(hallucination_rate={xref.hallucination_rate:.1%})."
            ),
            confidence=0.90 if xref.claimed_paths else 0.70,
            criterion_id="report_accuracy",
        )

    # ── Guard ──────────────────────────────────────────────────────────────

    def _assert_ingested(self) -> None:
        if not self._state.ingested:
            raise RuntimeError(
                "DocumentAuditor.ingest(pdf_path) must be called before querying."
            )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _paragraph_chunks(text: str) -> list[DocumentChunk]:
    """Split *text* on blank lines and wrap each paragraph as a DocumentChunk.

    This is the fallback chunking strategy when docling's HybridChunker is
    unavailable.  It is simple but effective for keyword search and context
    retrieval.
    """
    raw_paragraphs = re.split(r"\n{2,}", text.strip())
    chunks: list[DocumentChunk] = []
    for idx, para in enumerate(raw_paragraphs):
        para = para.strip()
        if not para:
            continue
        chunks.append(
            DocumentChunk(
                index=idx,
                text=para,
                page_number=None,
                heading=None,
            )
        )
    return chunks


def _is_substantive(text: str) -> bool:
    """Return True if *text* contains at least one substantive-context indicator.

    The heuristic checks for words that signal genuine architectural
    explanation rather than keyword-dropping.
    """
    text_lower = text.lower()
    return any(indicator in text_lower for indicator in _SUBSTANTIVE_VERBS)


__all__: list[str] = [
    # Result models
    "DocumentChunk",
    "TermOccurrence",
    "TermSearchResult",
    "PathCrossReferenceResult",
    # Main class
    "DocumentAuditor",
]

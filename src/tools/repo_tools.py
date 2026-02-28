"""
Forensic repository analysis tools for the RepoInvestigator detective.

Four-layer design
-----------------
1. Result models     — Pydantic structs for every intermediate result so all
                       analysis output is typed, serialisable, and inspectable.
2. RepoManager       — Context-managed, sandboxed workspace.  Cloned code
                       *never* touches the live project directory.
3. GraphForensics    — Stateless, pure-utility class.  Accepts file Paths and
                       returns typed result models using Python's ``ast`` module
                       exclusively (no regex on code).
4. RepoInvestigator  — High-level facade that orchestrates layers 2 and 3 and
                       converts findings into ``Evidence`` objects ready for
                       ``AgentState.evidences``.

Security contract
-----------------
* ``subprocess.run`` with an explicit argument list (no ``shell=True``).
* Repository URL validated via ``urllib.parse`` before any syscall.
* All git operations run inside ``tempfile.TemporaryDirectory``; the sandbox
  is cleaned up automatically on context-manager exit even if an exception
  is raised.
* No ``os.system`` calls anywhere in this module.
"""

from __future__ import annotations

import ast
import subprocess
import tempfile
import urllib.parse
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.state import Evidence

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class CloneError(RuntimeError):
    """Raised when ``git clone`` fails or times out."""


class GitLogError(RuntimeError):
    """Raised when ``git log`` cannot be read from the cloned repository."""


# ---------------------------------------------------------------------------
# Layer 1 — Intermediate result models
# ---------------------------------------------------------------------------


class CommitRecord(BaseModel):
    """A single entry from ``git log``."""

    model_config = ConfigDict(frozen=True)

    hash: str = Field(description="Full 40-character SHA-1 commit hash")
    message: str = Field(description="First line (subject) of the commit message")
    timestamp: datetime = Field(description="Author date in UTC")


class GitHistoryReport(BaseModel):
    """Forensic analysis derived from ``git log --reverse``."""

    model_config = ConfigDict(frozen=True)

    commit_count: int
    commits: list[CommitRecord]
    has_progression: bool = Field(
        description=(
            "True when early commits contain setup keywords and later commits "
            "contain graph/orchestration keywords — indicating iterative work"
        )
    )
    is_bulk_upload: bool = Field(
        description="True when all commits are clustered within 10 minutes"
    )
    progression_notes: str = Field(
        description="Human-readable explanation of the history pattern"
    )


class ReducerInfo(BaseModel):
    """A single field annotated with an ``operator.ior`` or ``operator.add`` reducer."""

    model_config = ConfigDict(frozen=True)

    field_name: str
    reducer: str  # "operator.ior" | "operator.add" | "other"
    annotation_source: str = Field(description="``ast.unparse`` of the annotation")


class StateClassInfo(BaseModel):
    """A Pydantic ``BaseModel`` or ``TypedDict`` class found by AST analysis."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: str = Field(description="'BaseModel' | 'TypedDict' | 'other'")
    lineno: int
    has_reducer: bool
    reducer_fields: list[ReducerInfo] = Field(default_factory=list)
    snippet: str = Field(description="Source lines covering the class definition")


class StateAnalysisResult(BaseModel):
    """Complete forensic result for a state-definition file."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    classes_found: list[StateClassInfo]
    has_pydantic_models: bool
    has_typed_dict: bool
    has_operator_ior: bool
    has_operator_add: bool
    has_evidence_model: bool
    has_judicial_opinion_model: bool
    has_agent_state: bool


class EdgeCall(BaseModel):
    """A single ``builder.add_edge()`` or ``add_conditional_edges()`` call."""

    model_config = ConfigDict(frozen=True)

    source: str
    destination: str
    lineno: int
    is_conditional: bool = False


class ParallelismReport(BaseModel):
    """Fan-out / fan-in topology derived from ``add_edge`` AST calls."""

    model_config = ConfigDict(frozen=True)

    fan_out_nodes: list[str] = Field(
        description="Source nodes with 2+ outgoing edges (fan-out)"
    )
    fan_in_nodes: list[str] = Field(
        description="Destination nodes with 2+ incoming edges (fan-in)"
    )
    has_parallel_detectives: bool = Field(
        description="True if detective-named nodes appear in a fan-out branch"
    )
    has_parallel_judges: bool = Field(
        description="True if judge-named nodes appear in a fan-out branch"
    )
    is_purely_linear: bool = Field(
        description="True when no fan-out exists at all — 'Orchestration Fraud'"
    )


class GraphStructureReport(BaseModel):
    """Forensic analysis of a LangGraph graph-definition file."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    has_stategraph: bool
    stategraph_variable: Optional[str] = None
    edge_calls: list[EdgeCall]
    parallelism: ParallelismReport
    has_conditional_edges: bool
    builder_snippet: str = Field(
        description="The source region containing the graph-builder calls"
    )


class ToolSafetyReport(BaseModel):
    """Security analysis of a single tool-implementation file."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    uses_os_system: bool = Field(description="Security violation when True")
    uses_tempfile: bool = Field(description="Sandbox hygiene indicator")
    uses_subprocess_run: bool
    subprocess_has_check: bool = Field(
        description="At least one subprocess.run has check=True"
    )
    clone_in_tempdir: bool = Field(
        description="Heuristic: clone target is inside a temp directory"
    )
    os_system_snippets: list[str] = Field(
        default_factory=list,
        description="Source snippets of offending os.system() calls",
    )


class StructuredOutputReport(BaseModel):
    """Analysis of structured-output enforcement in Judge node files."""

    model_config = ConfigDict(frozen=True)

    file_path: str
    uses_with_structured_output: bool
    uses_bind_tools: bool
    has_retry_logic: bool
    llm_call_snippets: list[str] = Field(
        default_factory=list,
        description="Up to 5 source snippets showing LLM invocations",
    )


# ---------------------------------------------------------------------------
# Layer 2 — RepoManager: context-managed sandboxed git workspace
# ---------------------------------------------------------------------------


def _validate_repo_url(url: str) -> None:
    """Reject URLs that are not valid, safe git remote addresses.

    Using ``subprocess.run`` with an explicit list prevents shell injection
    regardless, but rejecting bad URLs early gives cleaner error messages.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("https", "http", "git", "ssh"):
        raise ValueError(
            f"Unsupported URL scheme '{parsed.scheme}'. Use https://github.com/..."
        )
    if not parsed.netloc:
        raise ValueError(f"No hostname found in URL: '{url}'")


class RepoManager:
    """Context manager providing an isolated temporary workspace for git operations.

    The cloned repository lives entirely inside a ``tempfile.TemporaryDirectory``
    that is guaranteed to be removed when the ``with`` block exits — even if an
    exception is raised.

    Example
    -------
    >>> with RepoManager() as mgr:
    ...     repo_path = mgr.clone("https://github.com/user/repo")
    ...     commits   = mgr.git_log(repo_path)
    # sandbox is wiped here automatically
    """

    def __init__(self) -> None:
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._root: Path | None = None

    # ── Context-manager protocol ───────────────────────────────────────────

    def __enter__(self) -> "RepoManager":
        self._tmpdir = tempfile.TemporaryDirectory(prefix="automaton_auditor_")
        self._root = Path(self._tmpdir.name)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
        self._tmpdir = None
        self._root = None

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        """Absolute path to the sandbox root.  Raises if used outside ``with``."""
        if self._root is None:
            raise RuntimeError(
                "RepoManager must be used as a context manager:\n"
                "    with RepoManager() as mgr: ..."
            )
        return self._root

    def clone(self, url: str, depth: int = 100) -> Path:
        """Shallow-clone *url* into the sandbox.

        Parameters
        ----------
        url:
            Remote repository URL (validated before any subprocess call).
        depth:
            Shallow-clone depth.  100 commits is sufficient for the git-history
            forensic check while keeping network transfer fast.

        Returns
        -------
        Path
            Absolute path to the repository root inside the sandbox.

        Raises
        ------
        CloneError
            If ``git clone`` exits non-zero or times out.
        """
        _validate_repo_url(url)
        dest = self.root / "repo"
        try:
            subprocess.run(
                ["git", "clone", "--depth", str(depth), url, str(dest)],
                capture_output=True,
                text=True,
                check=True,
                timeout=180,
            )
        except subprocess.CalledProcessError as exc:
            raise CloneError(
                f"git clone failed for '{url}'.\nstderr: {exc.stderr.strip()}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CloneError(
                f"git clone timed out after 180 s for '{url}'."
            ) from exc
        return dest

    def git_log(self, repo_path: Path) -> list[CommitRecord]:
        """Extract the full (shallow) commit history, oldest-first.

        Format string ``%H|%aI|%s`` gives: full hash, ISO-8601 author date,
        and the commit subject (first line), all pipe-separated for safe
        splitting without relying on shell field splitting.

        Raises
        ------
        GitLogError
            If ``git log`` exits non-zero.
        """
        try:
            result = subprocess.run(
                ["git", "log", "--format=%H|%aI|%s", "--reverse"],
                capture_output=True,
                text=True,
                check=True,
                cwd=repo_path,
                timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            raise GitLogError(
                f"git log failed in '{repo_path}'.\nstderr: {exc.stderr.strip()}"
            ) from exc

        records: list[CommitRecord] = []
        for line in result.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                hash_, ts_str, message = parts
                try:
                    records.append(
                        CommitRecord(
                            hash=hash_.strip(),
                            message=message.strip(),
                            timestamp=datetime.fromisoformat(ts_str.strip()),
                        )
                    )
                except (ValueError, TypeError):
                    # Malformed line — skip silently; do not abort the analysis
                    continue
        return records


# ---------------------------------------------------------------------------
# Layer 3 — GraphForensics: stateless AST-analysis utility
# ---------------------------------------------------------------------------

# Keyword sets used for parallelism heuristics (lowercased)
_DETECTIVE_KEYWORDS: frozenset[str] = frozenset(
    {"repoinvestigator", "docanalyst", "visioninspector", "repo", "doc", "vision"}
)
_JUDGE_KEYWORDS: frozenset[str] = frozenset(
    {"prosecutor", "defense", "techlead", "judge"}
)


class GraphForensics:
    """Stateless AST-based forensic analysis of Python source files.

    Every public method is pure: it reads a file, builds an AST, extracts
    structural information, and returns a typed Pydantic result model.
    No side effects, no mutable state.
    """

    # ── State file analysis ────────────────────────────────────────────────

    def analyze_state_file(self, path: Path) -> StateAnalysisResult:
        """Parse *path* and extract TypedDict / BaseModel definitions and reducers.

        Uses ``ast.parse`` + ``ast.walk`` — never regex — so the analysis is
        robust to formatting differences and syntactically valid even for
        complex nested annotations.
        """
        source = self._read_source(path)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return StateAnalysisResult(
                file_path=str(path),
                classes_found=[],
                has_pydantic_models=False,
                has_typed_dict=False,
                has_operator_ior=False,
                has_operator_add=False,
                has_evidence_model=False,
                has_judicial_opinion_model=False,
                has_agent_state=False,
            )

        classes: list[StateClassInfo] = [
            self._classify_class(node, source)
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        ]
        names = {c.name for c in classes}

        return StateAnalysisResult(
            file_path=str(path),
            classes_found=classes,
            has_pydantic_models=any(c.kind == "BaseModel" for c in classes),
            has_typed_dict=any(c.kind == "TypedDict" for c in classes),
            has_operator_ior="operator.ior" in source,
            has_operator_add="operator.add" in source,
            has_evidence_model="Evidence" in names,
            has_judicial_opinion_model="JudicialOpinion" in names,
            has_agent_state="AgentState" in names,
        )

    # ── Graph wiring analysis ──────────────────────────────────────────────

    def analyze_graph_file(self, path: Path) -> GraphStructureReport:
        """Parse a LangGraph graph-definition file and extract wiring topology.

        Identifies:
        * Whether a ``StateGraph`` is instantiated.
        * All ``add_edge`` / ``add_conditional_edges`` calls with their source
          and destination nodes.
        * Fan-out / fan-in patterns from the extracted edge set.
        * The presence of conditional error-handling edges.
        """
        source = self._read_source(path)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            empty = ParallelismReport(
                fan_out_nodes=[],
                fan_in_nodes=[],
                has_parallel_detectives=False,
                has_parallel_judges=False,
                is_purely_linear=True,
            )
            return GraphStructureReport(
                file_path=str(path),
                has_stategraph=False,
                edge_calls=[],
                parallelism=empty,
                has_conditional_edges=False,
                builder_snippet="[SyntaxError — file could not be parsed]",
            )

        sg_var = self._find_stategraph_variable(tree)
        edge_calls = self._extract_edge_calls(tree)
        parallelism = self._analyze_parallelism(edge_calls)
        snippet = self._extract_builder_snippet(source, tree)

        return GraphStructureReport(
            file_path=str(path),
            has_stategraph=sg_var is not None,
            stategraph_variable=sg_var,
            edge_calls=edge_calls,
            parallelism=parallelism,
            has_conditional_edges=any(e.is_conditional for e in edge_calls),
            builder_snippet=snippet,
        )

    # ── Tool safety analysis ───────────────────────────────────────────────

    def analyze_tool_safety(self, path: Path) -> ToolSafetyReport:
        """Scan a tool-implementation file for security and hygiene issues.

        Detects:
        * ``os.system`` calls — a confirmed security violation.
        * ``tempfile.TemporaryDirectory`` usage — required for sandboxing.
        * ``subprocess.run`` calls with/without ``check=True``.
        """
        source = self._read_source(path)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return ToolSafetyReport(
                file_path=str(path),
                uses_os_system=False,
                uses_tempfile=False,
                uses_subprocess_run=False,
                subprocess_has_check=False,
                clone_in_tempdir=False,
            )

        os_system_snippets: list[str] = []
        uses_subprocess = False
        check_values: list[bool] = []
        uses_tempfile = "TemporaryDirectory" in source or "tempfile" in source

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_src = ast.unparse(node.func)

            if func_src == "os.system":
                os_system_snippets.append(
                    self._extract_lines(source, node.lineno, node.lineno)
                )

            if func_src in ("subprocess.run", "subprocess.check_call", "subprocess.Popen"):
                uses_subprocess = True
                check_kw = next(
                    (kw for kw in node.keywords if kw.arg == "check"), None
                )
                if check_kw and isinstance(check_kw.value, ast.Constant):
                    check_values.append(bool(check_kw.value.value))

        return ToolSafetyReport(
            file_path=str(path),
            uses_os_system=bool(os_system_snippets),
            uses_tempfile=uses_tempfile,
            uses_subprocess_run=uses_subprocess,
            subprocess_has_check=any(check_values),
            clone_in_tempdir=uses_tempfile and uses_subprocess and not bool(os_system_snippets),
            os_system_snippets=os_system_snippets,
        )

    # ── Structured-output enforcement ─────────────────────────────────────

    def analyze_structured_output(self, path: Path) -> StructuredOutputReport:
        """Check whether Judge nodes enforce Pydantic-schema-bound LLM output."""
        source = self._read_source(path)
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            return StructuredOutputReport(
                file_path=str(path),
                uses_with_structured_output=False,
                uses_bind_tools=False,
                has_retry_logic=False,
            )

        uses_wso = False
        uses_bt = False
        snippets: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func_src = ast.unparse(node.func)
            if "with_structured_output" in func_src:
                uses_wso = True
                snippets.append(
                    self._extract_lines(source, node.lineno, node.lineno + 2)
                )
            if "bind_tools" in func_src:
                uses_bt = True
                snippets.append(
                    self._extract_lines(source, node.lineno, node.lineno + 2)
                )

        retry_keywords = ("retry", "tenacity", "max_retries", "backoff", "for_attempt")
        has_retry = any(kw in source.lower() for kw in retry_keywords)

        return StructuredOutputReport(
            file_path=str(path),
            uses_with_structured_output=uses_wso,
            uses_bind_tools=uses_bt,
            has_retry_logic=has_retry,
            llm_call_snippets=snippets[:5],
        )

    # ── Private AST helpers ────────────────────────────────────────────────

    def _classify_class(self, node: ast.ClassDef, source: str) -> StateClassInfo:
        """Determine the kind (BaseModel / TypedDict / other) and reducers."""
        kind = "other"
        for base in node.bases:
            base_name = _name_from_node(base)
            if base_name == "BaseModel":
                kind = "BaseModel"
                break
            if base_name == "TypedDict":
                kind = "TypedDict"
                break

        reducer_fields = self._extract_reducer_fields(node)
        end_line = getattr(node, "end_lineno", node.lineno + 30)
        snippet = self._extract_lines(source, node.lineno, end_line)

        return StateClassInfo(
            name=node.name,
            kind=kind,
            lineno=node.lineno,
            has_reducer=bool(reducer_fields),
            reducer_fields=reducer_fields,
            snippet=snippet,
        )

    def _extract_reducer_fields(self, class_node: ast.ClassDef) -> list[ReducerInfo]:
        """Find annotated class-body fields that use Annotated[..., operator.*] reducers."""
        fields: list[ReducerInfo] = []
        for stmt in class_node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                ann_src = ast.unparse(stmt.annotation)
                if "operator.ior" in ann_src:
                    fields.append(
                        ReducerInfo(
                            field_name=stmt.target.id,
                            reducer="operator.ior",
                            annotation_source=ann_src,
                        )
                    )
                elif "operator.add" in ann_src:
                    fields.append(
                        ReducerInfo(
                            field_name=stmt.target.id,
                            reducer="operator.add",
                            annotation_source=ann_src,
                        )
                    )
        return fields

    def _find_stategraph_variable(self, tree: ast.AST) -> Optional[str]:
        """Return the variable name bound to ``StateGraph(...)``, or ``None``.

        Handles both plain assignments and annotated assignments:
          builder = StateGraph(AgentState)          → ast.Assign
          builder: StateGraph = StateGraph(AgentState) → ast.AnnAssign
        """
        _SG_NAMES = ("StateGraph", "CompiledStateGraph")
        for node in ast.walk(tree):
            # Plain assignment: builder = StateGraph(AgentState)
            if isinstance(node, ast.Assign):
                if isinstance(node.value, ast.Call) and _name_from_node(node.value.func) in _SG_NAMES:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            return target.id
            # Annotated assignment: builder: StateGraph = StateGraph(AgentState)
            elif isinstance(node, ast.AnnAssign):
                if (
                    node.value is not None
                    and isinstance(node.value, ast.Call)
                    and _name_from_node(node.value.func) in _SG_NAMES
                    and isinstance(node.target, ast.Name)
                ):
                    return node.target.id
        return None

    def _extract_edge_calls(self, tree: ast.AST) -> list[EdgeCall]:
        """Walk the AST for every ``add_edge`` / ``add_conditional_edges`` call."""
        edges: list[EdgeCall] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method not in ("add_edge", "add_conditional_edges"):
                continue

            is_cond = method == "add_conditional_edges"
            args = node.args

            if is_cond:
                src = _string_from_arg(args[0]) if args else "?"
                # Try to expand the destination mapping (3rd positional argument)
                if len(args) >= 3 and isinstance(args[2], ast.Dict):
                    for val in args[2].values:
                        edges.append(
                            EdgeCall(
                                source=src,
                                destination=_string_from_arg(val),
                                lineno=node.lineno,
                                is_conditional=True,
                            )
                        )
                else:
                    # Router function without an explicit mapping
                    edges.append(
                        EdgeCall(
                            source=src,
                            destination="<conditional_router>",
                            lineno=node.lineno,
                            is_conditional=True,
                        )
                    )
            else:
                if len(args) >= 2:
                    edges.append(
                        EdgeCall(
                            source=_string_from_arg(args[0]),
                            destination=_string_from_arg(args[1]),
                            lineno=node.lineno,
                            is_conditional=False,
                        )
                    )
        return edges

    def _analyze_parallelism(self, edges: list[EdgeCall]) -> ParallelismReport:
        """Derive fan-out / fan-in topology and heuristic detective/judge flags."""
        out_map: dict[str, list[str]] = defaultdict(list)
        in_map: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            out_map[edge.source].append(edge.destination)
            in_map[edge.destination].append(edge.source)

        fan_out = [src for src, dsts in out_map.items() if len(dsts) >= 2]
        fan_in = [dst for dst, srcs in in_map.items() if len(srcs) >= 2]

        all_dsts_lower = {e.destination.lower() for e in edges}

        has_detectives = any(
            kw in dst for dst in all_dsts_lower for kw in _DETECTIVE_KEYWORDS
        ) and bool(fan_out)

        has_judges = any(
            kw in dst for dst in all_dsts_lower for kw in _JUDGE_KEYWORDS
        ) and bool(fan_out)

        return ParallelismReport(
            fan_out_nodes=fan_out,
            fan_in_nodes=fan_in,
            has_parallel_detectives=has_detectives,
            has_parallel_judges=has_judges,
            is_purely_linear=not bool(fan_out) and not bool(fan_in),
        )

    def _extract_builder_snippet(self, source: str, tree: ast.AST) -> str:
        """Extract the contiguous source region containing graph-builder calls."""
        linenos: list[int] = []
        builder_methods = {"add_edge", "add_conditional_edges", "add_node", "compile", "set_entry_point"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in builder_methods:
                    linenos.append(node.lineno)
        if not linenos:
            return "[No graph builder calls detected]"
        return self._extract_lines(source, max(1, min(linenos) - 2), max(linenos) + 2)

    # ── General helpers ────────────────────────────────────────────────────

    @staticmethod
    def _read_source(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _extract_lines(source: str, start: int, end: int) -> str:
        """Return lines [start, end] (1-indexed, inclusive) from *source*."""
        lines = source.splitlines()
        return "\n".join(lines[max(0, start - 1) : min(len(lines), end)])


# ---------------------------------------------------------------------------
# Module-level pure helpers (reused by both RepoManager and GraphForensics)
# ---------------------------------------------------------------------------


def _name_from_node(node: ast.expr) -> str:
    """Best-effort string name from an AST expression node."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ast.unparse(node)


def _string_from_arg(node: ast.expr) -> str:
    """Extract a string literal or identifier name from an AST argument node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id  # covers START, END, and other LangGraph constants
    return ast.unparse(node)


# ---------------------------------------------------------------------------
# Layer 4 — RepoInvestigator: high-level forensic facade
# ---------------------------------------------------------------------------


class RepoInvestigator:
    """Orchestrates RepoManager and GraphForensics to produce Evidence objects.

    Implements every forensic protocol required by the rubric for the
    ``github_repo`` target artifact.

    Usage
    -----
    >>> investigator = RepoInvestigator("https://github.com/user/repo")
    >>> evidence_map = investigator.run_all()
    >>> # Dict[criterion_id, List[Evidence]] — ready for AgentState.evidences
    """

    def __init__(self, repo_url: str) -> None:
        _validate_repo_url(repo_url)
        self.repo_url = repo_url
        self._forensics = GraphForensics()

    def run_all(self) -> dict[str, list[Evidence]]:
        """Clone once, run all protocols, return criterion-keyed Evidence map.

        The returned dict is safe to merge directly into ``AgentState.evidences``
        via the ``operator.ior`` reducer.  A ``CloneError`` is captured and
        surfaced as an Evidence object so the graph can continue gracefully.
        """
        try:
            with RepoManager() as mgr:
                repo_path = mgr.clone(self.repo_url)
                commits = mgr.git_log(repo_path)
                return {
                    "git_forensic_analysis": [
                        self._investigate_git_history(commits)
                    ],
                    "state_management_rigor": [
                        self._investigate_state_management(repo_path)
                    ],
                    "graph_orchestration": [
                        self._investigate_graph_orchestration(repo_path)
                    ],
                    "safe_tool_engineering": [
                        self._investigate_tool_safety(repo_path)
                    ],
                    "structured_output_enforcement": [
                        self._investigate_structured_output(repo_path)
                    ],
                }
        except CloneError as exc:
            return {
                "git_forensic_analysis": [
                    Evidence(
                        goal="Clone repository and inspect all artifacts",
                        found=False,
                        content=str(exc),
                        location=self.repo_url,
                        rationale=(
                            "Repository could not be cloned. "
                            "All downstream forensic protocols are impossible."
                        ),
                        confidence=1.0,
                        criterion_id="git_forensic_analysis",
                    )
                ]
            }

    # ── Per-criterion private methods ──────────────────────────────────────

    def _investigate_git_history(self, commits: list[CommitRecord]) -> Evidence:
        report = _build_git_history_report(commits)
        passes = report.commit_count > 3 and not report.is_bulk_upload
        return Evidence(
            goal="Verify >3 commits with Environment→Tools→Graph progression",
            found=passes,
            content=report.model_dump_json(indent=2),
            location=f"{self.repo_url} :: git log --reverse",
            rationale=(
                f"{report.commit_count} commits total. "
                + report.progression_notes
            ),
            confidence=0.95 if passes else 0.70,
            criterion_id="git_forensic_analysis",
        )

    def _investigate_state_management(self, repo_path: Path) -> Evidence:
        candidates = [
            repo_path / "src" / "state.py",
            repo_path / "src" / "graph.py",
            repo_path / "state.py",
        ]
        result: StateAnalysisResult | None = None
        location = "src/state.py (not found)"
        for candidate in candidates:
            if candidate.exists():
                result = self._forensics.analyze_state_file(candidate)
                location = str(candidate.relative_to(repo_path))
                break

        if result is None:
            return Evidence(
                goal="Locate AgentState with Pydantic BaseModel classes and Annotated reducers",
                found=False,
                content=None,
                location="src/state.py",
                rationale="No state definition file found at expected locations.",
                confidence=0.98,
                criterion_id="state_management_rigor",
            )

        all_required = (
            result.has_pydantic_models
            and result.has_typed_dict
            and result.has_operator_ior
            and result.has_operator_add
            and result.has_evidence_model
            and result.has_agent_state
        )
        confidence = 0.95 if all_required else (0.60 if result.has_pydantic_models else 0.30)

        return Evidence(
            goal="Locate AgentState with Pydantic BaseModel classes and Annotated reducers",
            found=all_required,
            content=result.model_dump_json(indent=2),
            location=location,
            rationale=(
                f"Pydantic={'✓' if result.has_pydantic_models else '✗'}  "
                f"TypedDict={'✓' if result.has_typed_dict else '✗'}  "
                f"operator.ior={'✓' if result.has_operator_ior else '✗'}  "
                f"operator.add={'✓' if result.has_operator_add else '✗'}  "
                f"Evidence model={'✓' if result.has_evidence_model else '✗'}  "
                f"AgentState={'✓' if result.has_agent_state else '✗'}"
            ),
            confidence=confidence,
            criterion_id="state_management_rigor",
        )

    def _investigate_graph_orchestration(self, repo_path: Path) -> Evidence:
        graph_file = repo_path / "src" / "graph.py"
        if not graph_file.exists():
            return Evidence(
                goal="Verify parallel fan-out/fan-in StateGraph wiring",
                found=False,
                content=None,
                location="src/graph.py (not found)",
                rationale="src/graph.py does not exist; graph topology cannot be verified.",
                confidence=0.98,
                criterion_id="graph_orchestration",
            )

        report = self._forensics.analyze_graph_file(graph_file)
        p = report.parallelism
        passes = (
            report.has_stategraph
            and not p.is_purely_linear
            and (p.has_parallel_detectives or p.has_parallel_judges)
        )
        confidence = 0.90 if passes else (0.60 if report.has_stategraph else 0.95)

        return Evidence(
            goal="Verify parallel fan-out/fan-in StateGraph wiring",
            found=passes,
            content=report.model_dump_json(indent=2),
            location="src/graph.py",
            rationale=(
                f"StateGraph={'✓' if report.has_stategraph else '✗'}  "
                f"linear_only={p.is_purely_linear}  "
                f"fan_out_nodes={p.fan_out_nodes}  "
                f"fan_in_nodes={p.fan_in_nodes}  "
                f"conditional_edges={'✓' if report.has_conditional_edges else '✗'}"
            ),
            confidence=confidence,
            criterion_id="graph_orchestration",
        )

    def _investigate_tool_safety(self, repo_path: Path) -> Evidence:
        tools_dir = repo_path / "src" / "tools"
        if not tools_dir.exists():
            return Evidence(
                goal="Verify sandboxed git clone with tempfile.TemporaryDirectory",
                found=False,
                content=None,
                location="src/tools/ (not found)",
                rationale="src/tools/ directory absent; sandboxing cannot be verified.",
                confidence=0.98,
                criterion_id="safe_tool_engineering",
            )

        reports: list[ToolSafetyReport] = [
            self._forensics.analyze_tool_safety(f)
            for f in sorted(tools_dir.glob("*.py"))
        ]

        any_violation = any(r.uses_os_system for r in reports)
        all_tempfile = all(
            r.uses_tempfile for r in reports if r.uses_subprocess_run
        )
        all_check = all(
            r.subprocess_has_check for r in reports if r.uses_subprocess_run
        )
        passes = not any_violation and all_tempfile and all_check
        confidence = 0.90 if passes else (0.70 if not any_violation else 0.95)

        return Evidence(
            goal="Verify sandboxed git clone with tempfile.TemporaryDirectory",
            found=passes,
            content="\n---\n".join(r.model_dump_json(indent=2) for r in reports) or None,
            location="src/tools/",
            rationale=(
                f"os.system violations={'SECURITY ISSUE' if any_violation else 'none'}  "
                f"tempfile={'✓' if all_tempfile else '✗ missing'}  "
                f"subprocess check=True={'✓' if all_check else '✗ missing'}"
            ),
            confidence=confidence,
            criterion_id="safe_tool_engineering",
        )

    def _investigate_structured_output(self, repo_path: Path) -> Evidence:
        judges_file = repo_path / "src" / "nodes" / "judges.py"
        if not judges_file.exists():
            return Evidence(
                goal="Verify .with_structured_output() bound to JudicialOpinion",
                found=False,
                content=None,
                location="src/nodes/judges.py (not found)",
                rationale="src/nodes/judges.py does not exist.",
                confidence=0.98,
                criterion_id="structured_output_enforcement",
            )

        report = self._forensics.analyze_structured_output(judges_file)
        passes = report.uses_with_structured_output or report.uses_bind_tools

        return Evidence(
            goal="Verify .with_structured_output() bound to JudicialOpinion",
            found=passes,
            content=report.model_dump_json(indent=2),
            location="src/nodes/judges.py",
            rationale=(
                f"with_structured_output={'✓' if report.uses_with_structured_output else '✗'}  "
                f"bind_tools={'✓' if report.uses_bind_tools else '✗'}  "
                f"retry_logic={'✓' if report.has_retry_logic else '✗'}"
            ),
            confidence=0.90 if passes else 0.95,
            criterion_id="structured_output_enforcement",
        )


# ---------------------------------------------------------------------------
# Private pure helper
# ---------------------------------------------------------------------------


def _build_git_history_report(commits: list[CommitRecord]) -> GitHistoryReport:
    """Derive a ``GitHistoryReport`` from a list of commit records."""
    n = len(commits)
    if n == 0:
        return GitHistoryReport(
            commit_count=0,
            commits=[],
            has_progression=False,
            is_bulk_upload=True,
            progression_notes="No commits found in repository.",
        )

    # Bulk-upload heuristic: all timestamps within 10 minutes
    is_bulk = (
        n == 1
        or (commits[-1].timestamp - commits[0].timestamp).total_seconds() < 600
    )

    # Progression heuristic: keyword presence in early vs. late commit messages
    third = max(1, n // 3)
    early_msgs = [c.message.lower() for c in commits[:third]]
    late_msgs = [c.message.lower() for c in commits[-third:]]

    setup_kws = {"init", "setup", "install", "env", "config", "pyproject", "dependencies"}
    tool_kws = {"tool", "ast", "repo", "clone", "git", "parse", "detect"}
    graph_kws = {"graph", "node", "edge", "langgraph", "judge", "detective", "orchestrat"}

    has_setup = any(kw in msg for msg in early_msgs for kw in setup_kws)
    has_tools = any(kw in msg for msg in early_msgs + late_msgs for kw in tool_kws)
    has_graph = any(kw in msg for msg in late_msgs for kw in graph_kws)
    has_progression = has_setup and has_graph and n > 3 and not is_bulk

    notes = (
        f"{n} commits. "
        + ("Bulk upload suspected (all within 10 min). " if is_bulk else "Iterative history. ")
        + (
            "Progression detected (setup → tools → graph). "
            if has_progression
            else (
                f"Partial progression (setup={'✓' if has_setup else '✗'} "
                f"tools={'✓' if has_tools else '✗'} "
                f"graph={'✓' if has_graph else '✗'}). "
            )
        )
    )
    return GitHistoryReport(
        commit_count=n,
        commits=commits,
        has_progression=has_progression,
        is_bulk_upload=is_bulk,
        progression_notes=notes,
    )


__all__: list[str] = [
    # Exceptions
    "CloneError",
    "GitLogError",
    # Result models
    "CommitRecord",
    "GitHistoryReport",
    "ReducerInfo",
    "StateClassInfo",
    "StateAnalysisResult",
    "EdgeCall",
    "ParallelismReport",
    "GraphStructureReport",
    "ToolSafetyReport",
    "StructuredOutputReport",
    # Core classes
    "RepoManager",
    "GraphForensics",
    "RepoInvestigator",
]

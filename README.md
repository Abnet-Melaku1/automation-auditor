# Automaton Auditor — The Digital Courtroom

## 1. Project Overview

Automaton Auditor is a "Digital Courtroom" engineered for the autonomous governance and evaluation of AI-generated code. Rather than relying on rigid, deterministic linters, this system employs a **Hierarchical State Graph** architecture built upon [LangGraph](https://langchain-ai.github.io/langgraph/). It orchestrates a swarm of specialized agent personas—Detectives and Judges—that operate concurrently to investigate multi-modal artifacts (GitHub repositories and architectural PDFs) and synthesize dialectical verdicts based on complex qualitative rubrics.

At its core, Automaton Auditor treats code evaluation as a legal proceeding, ensuring every architectural decision is substantiated by verifiable evidence and debated from multiple technical viewpoints before a final consensus is reached.

---

## 2. Senior-Level Features

- **Forensic Accuracy via AST:** The RepoInvestigator detective leverages abstract syntax tree (AST) analysis—strictly avoiding brittle regex approaches. This allows the system to accurately parse Python source files to verify complex structural requirements such as graph topology (e.g., proper LangGraph `StateGraph` instantiation) and intentional fan-out/fan-in parallel patterns.
- **Thread-Safe State Management:** The global `AgentState` utilizes `typing_extensions.TypedDict` powered by robust Pydantic models (e.g., `Evidence`, `JudicialOpinion`). Crucially, the orchestrator employs Python's `operator.ior` (dictionary merge) and `operator.add` (list concatenation) as LangGraph reducers. This guarantees mathematically safe, non-destructive state accumulation across highly parallelized Detective and Judge execution branches.
- **Sandboxed Tooling:** To prevent execution contamination and adhere to strict security hygiene, the repository ingestion logic clones target GitHub submissions directly into ephemeral `tempfile.TemporaryDirectory` sandboxes. The workspace is guaranteed to be securely wiped via context managers upon completion or exception, ensuring isolation across audit sessions.
- **Docling Integration ("RAG-lite"):** Architectural PDF ingestion is backed by IBM's `docling` library to generate markdown chunks complete with semantic structure. The document analysis pipeline features specialized "substantiveness heuristics" to differentiate between legitimate architectural exposition (Metacognition) and superficial keyword-dropping.

---

## 3. Installation & Setup

This project uses `uv` for lightning-fast dependency resolution and virtual environment management.

**1. Install `uv` (if not already installed):**

```bash
# On macOS and Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# On Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Clone and lock dependencies:**

```bash
git clone <your-repo-url>
cd automation-auditor
uv sync
```

_`uv sync` will automatically create a `.venv` virtual environment and firmly lock the exact versions specified in `pyproject.toml`._

**3. Configure Environment Variables:**
Copy the template to instantiate your local `.env`:

```bash
cp .env.example .env
```

Populate the `.env` file with the required keys:

- `GOOGLE_API_KEY`: Required for navigating the Gemini model endpoints.
- `LANGCHAIN_API_KEY`: Your LangSmith observability credential.
- `LANGCHAIN_TRACING_V2=true`: Enables implicit graph tracing required for auditing multi-agent fan-out operations.

---

## 4. Usage (Interim Submission)

The interim submission features the parallel execution of the **Detective Layer** (`RepoInvestigator` and `DocAnalyst`), synchronizing via a Fan-In `EvidenceAggregator`.

**Running the Detective Swarm:**
You can invoke the auditor CLI using `uv run`, targeting a specific GitHub repository and a structural PDF:

```bash
uv run automaton-auditor \
  --repo-url https://github.com/target-user/target-repo \
  --pdf reports/architecture_report.pdf
```

_(Alternatively, you can trigger `run_interim_audit()` directly from `src/graph.py` programmatically.)_

**Observability via LangSmith:**
Due to the parallel nature of our LangGraph node orchestration, terminal outputs only tell half the story. To inspect the true dialectical workflow and view AST evidence payloads passed between agents:

1. Log into your [LangSmith Dashboard](https://smith.langchain.com/).
2. Navigate to the `automaton-auditor` project.
3. Open the latest run trace to visualize the fan-out/fan-in graph execution path and the accumulated JSON representation of the `AgentState`.

---

## 5. Repository Structure

```text
automaton-auditor/
├── .env.example
├── pyproject.toml
├── rubric/
│   └── rubric.json             # Qualitative dimensions instructing the swarm
└── src/
    ├── state.py                # Reducers (ior/add) and Pydantic Evidence models
    ├── graph.py                # LangGraph topology and Fan-In node aggregators
    ├── nodes/
    │   └── detectives.py       # Parallel DocAnalyst & RepoInvestigator nodes
    └── tools/
        ├── doc_tools.py        # Docling RAG-lite & heuristical parsing logic
        └── repo_tools.py       # AST-based syntax tree scanning & secure sandboxes
```

> **A Note on Metacognition:** The Automaton Auditor transcends simple unit-testing by exhibiting qualitative design understanding. Through the synergy of AST forensics and heuristical document inspection, the swarm validates not just _if_ code runs, but evaluates the _methodology_ and _dialectical intent_ of the software architectures it is assigned to judge.

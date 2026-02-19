# Arc-Reactor Agent: Autonomous Multi-Tool AI Agent with MCP Protocol

A modular, reasoning-driven AI agent that autonomously solves complex multi-step queries by orchestrating multiple tool servers through the **Model Context Protocol (MCP)**. The agent perceives, plans, executes, evaluates, and replans — all in a closed-loop architecture powered by Google Gemini.

> Ask it anything — from math computations to document analysis to live web searches — and watch it reason through the problem step by step.

---

## Demo

https://www.youtube.com/watch?v=axn0NaeZ65Y

---

## What Makes This Different

| Feature | Description |
|---|---|
| **Autonomous Multi-Step Reasoning** | The agent doesn't just call one tool — it builds plans, executes steps, evaluates results, and replans when things fail |
| **MCP Protocol** | Tool servers run as separate processes communicating via the Model Context Protocol, making the system modular and extensible |
| **Sandboxed Code Execution** | LLM-generated code runs in a restricted Python sandbox with AST-level safety (no `os`, `subprocess`, etc.) |
| **Session Memory** | Past conversations are stored and fuzzy-matched, so the agent learns from previous sessions |
| **Perception-Decision-Action Loop** | Inspired by cognitive architectures — each module has a single responsibility, making the system debuggable and maintainable |

---

## Architecture

```
User Query
    │
    ▼
┌──────────┐     ┌──────────┐     ┌──────────┐
│ MEMORY   │────▶│PERCEPTION│────▶│ DECISION │
│ (fuzzy   │     │ (analyze │     │ (plan +  │
│  search  │     │  query   │     │  pick    │
│  past    │     │  with    │     │  tools + │
│  sessions│     │  Gemini) │     │  write   │
│  )       │     │          │     │  code)   │
└──────────┘     └──────────┘     └─────┬────┘
                                        │
                      ┌─────────────────┘
                      ▼
               ┌──────────┐
               │  ACTION  │ ← sandboxed executor
               │(run code │   runs LLM-generated code
               │ via MCP) │   with AST safety transforms
               └─────┬────┘
                     │
                     ▼
               ┌──────────┐
               │PERCEPTION│ ← evaluates the result
               │(step eval)│
               └─────┬────┘
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
      GOAL MET   LOCAL OK    FAILED
      (done!)   (next step) (replan)
```

### How One Query Flows Through the System

1. **Memory Search** — Fuzzy-matches your query against past sessions using `rapidfuzz`. If a similar question was answered before, the agent can short-circuit without calling any tools.

2. **Perception (Initial)** — Gemini analyzes the query: extracts entities, determines what kind of result is needed, and checks if the answer is already available from memory.

3. **Decision** — Gemini receives the perception output + the full list of available MCP tools, and produces:
   - A natural language plan (e.g., "Step 0: Search DuckDuckGo, Step 1: Extract price, Step 2: Conclude")
   - The first executable step as a JSON object with actual Python code

4. **Action (Executor)** — The generated code runs in a sandboxed environment:
   - Tool calls like `add(2, 3)` are transparently routed to MCP servers via async proxies
   - The AST is transformed to auto-await async calls and strip keyword arguments
   - Restricted builtins and allowed modules only — no filesystem or network access outside MCP

5. **Perception (Step Eval)** — Evaluates the execution result:
   - `original_goal_achieved` → done, return answer
   - `local_goal_achieved` → this step worked, get next step from Decision
   - Both false → step failed, trigger a replan

6. **Loop continues** until the goal is met, a conclusion is reached, or clarification is needed.

---

## MCP Tool Servers

The agent connects to multiple MCP servers, each exposing different capabilities:

| Server | Tools | Description |
|---|---|---|
| **Math** (`mcp_server_1.py`) | `add`, `subtract`, `multiply`, `divide`, `power`, `cbrt`, `factorial`, `sin`, `cos`, `tan`, `fibonacci_numbers`, `strings_to_chars_to_int`, `int_list_to_exponential_sum` | Arithmetic, trigonometry, and special conversions |
| **Documents** (`mcp_server_2.py`) | `search_stored_documents_rag`, `convert_webpage_url_into_markdown`, `extract_pdf` | FAISS-powered RAG over local PDFs/documents, webpage conversion |
| **Web Search** (`mcp_server_3.py`) | `duckduckgo_search_results`, `download_raw_html_from_url` | Live web search and page content extraction |
| **Mixed** (`mcp_server_4.py`) | `add`, `subtract`, `multiply`, `no_input`, `strings_to_chars_to_int`, `int_list_to_exponential_sum` | Additional utility tools |

Adding a new tool server is as simple as creating a new MCP server script and adding it to `config/mcp_server_config.yaml`.

---

## Safety & Guard Rails

Since the LLM generates and executes code, multiple layers of protection are in place:

| Guard Rail | What It Prevents |
|---|---|
| **Restricted Builtins** | Only `range`, `len`, `int`, `float`, `str`, `list`, `dict`, `print`, `sum` are available. No `open`, `eval`, `exec`, `compile` |
| **Module Whitelist** | Only safe standard library modules (`math`, `json`, `re`, `collections`, etc.). No `os`, `subprocess`, `socket`, `shutil` |
| **Max Function Calls** | Code is limited to 5 tool calls per step to prevent runaway execution |
| **Execution Timeout** | Each step has a timeout (500s per function call) to kill hung processes |
| **AST Keyword Stripping** | Removes keyword arguments from tool calls (common LLM mistake) to prevent schema mismatches |
| **AST Auto-Await** | Automatically wraps MCP tool calls in `await` so the LLM doesn't need to know about async |
| **Perception Validation** | Every step result is evaluated — lazy or incorrect answers trigger replanning |
| **Prompt-Level Rules** | Decision prompt instructs: "Use ONLY listed tools", "Don't reuse failed tools", "End with return" |

---

## Project Structure

```
├── action/
│   └── executor.py          # Sandboxed code executor with AST transforms
├── agent/
│   ├── agent_loop2.py       # Main agent loop (Perception → Decision → Action cycle)
│   └── agentSession.py      # Session state tracking (steps, plans, perceptions)
├── config/
│   └── mcp_server_config.yaml  # MCP server definitions
├── decision/
│   └── decision.py          # LLM-powered planner (Gemini generates plans + code)
├── mcp_servers/
│   ├── mcp_server_1.py      # Math tools
│   ├── mcp_server_2.py      # Document/RAG tools (FAISS + LlamaIndex)
│   ├── mcp_server_3.py      # Web search tools (DuckDuckGo)
│   ├── mcp_server_4.py      # Mixed utility tools
│   ├── multiMCP.py          # Multi-server connector & tool router
│   ├── models.py            # Pydantic input/output models
│   ├── documents/           # Local documents for RAG
│   └── faiss_index/         # FAISS vector index + metadata
├── memory/
│   ├── memory_search.py     # Fuzzy search over past sessions (rapidfuzz)
│   ├── session_log.py       # Session persistence (JSON by date)
│   └── session_logs/        # Stored session history (YYYY/MM/DD/<id>.json)
├── perception/
│   └── perception.py        # LLM-powered query analyzer & result evaluator
├── prompts/
│   ├── perception_prompt.txt # System prompt for Perception module
│   └── decision_prompt.txt  # System prompt for Decision module
├── main.py                  # Interactive CLI entry point
├── pyproject.toml           # Dependencies
└── .env                     # GEMINI_API_KEY (not committed)
```

---

## Getting Started

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A Google Gemini API key

### Installation

```bash
# Clone the repository
git clone https://github.com/dhruvLearner8/Arc-Reactor-Agent-for-Browser.git
cd Arc-Reactor-Agent-for-Browser

# Install dependencies
uv sync
# or: pip install -r requirements.txt

# Set up your API key
echo "GEMINI_API_KEY=your_key_here" > .env
```

### Configuration

Update the `cwd` paths in `config/mcp_server_config.yaml` to point to your local `mcp_servers/` directory:

```yaml
mcp_servers:
  - id: math
    script: mcp_server_1.py
    cwd: /your/path/to/mcp_servers
  - id: documents
    script: mcp_server_2.py
    cwd: /your/path/to/mcp_servers
  - id: websearch
    script: mcp_server_3.py
    cwd: /your/path/to/mcp_servers
```

### Run

```bash
uv run main.py
```

---

## Example Queries

| Query | What Happens |
|---|---|
| `What is 2 + 3?` | Decision calls `add(2, 3)` via MCP math server → returns 5 |
| `What is the current stock price of Tesla?` | Searches DuckDuckGo → extracts URL → downloads page → parses price |
| `What challenges did Tesla face in China?` | RAG search over local Tesla PDF → extracts relevant chunks → summarizes |
| `What is the log value of the DLF apartment price?` | RAG search for price → math computation on the result → returns log value |
| `Who was the first prime minister of Canada?` | If asked before: answers from session memory instantly. If new: web search → conclude |

---

## How Session Memory Works

Every completed session is saved as a JSON file in `memory/session_logs/YYYY/MM/DD/<session_id>.json`. When a new query comes in:

1. All past session files are loaded
2. The query is fuzzy-matched (using `rapidfuzz`) against past queries and their solutions
3. Top 3 matches are fed into Perception
4. If a match is strong enough, Perception short-circuits and returns the answer without any tool calls

This means the agent gets faster over time — frequently asked questions are answered from memory in milliseconds.

---

## Tech Stack

| Component | Technology |
|---|---|
| LLM | Google Gemini 2.0 Flash |
| Protocol | Model Context Protocol (MCP) |
| Vector Search | FAISS + LlamaIndex + Google GenAI Embeddings |
| Fuzzy Matching | rapidfuzz |
| Web Scraping | httpx, BeautifulSoup, trafilatura |
| Document Parsing | markitdown, pymupdf4llm |
| Code Safety | Python AST module (NodeTransformer) |
| Validation | Pydantic |
| Package Manager | uv |

---

## Key Design Decisions

1. **Perception as a separate module** — Instead of having the Decision module evaluate its own output, a separate Perception module acts as an unbiased judge. This prevents the planner from marking its own homework.

2. **Code generation over function calling** — The agent generates executable Python code rather than using Gemini's native function calling API. This allows multi-step chaining, variable assignment, and mixing standard library code with tool calls in a single step.

3. **AST transforms over string manipulation** — Keyword stripping and auto-await are done at the AST level, not with regex. This is precise and handles edge cases (nested calls, complex expressions) correctly.

4. **Fuzzy memory over vector memory** — Session memory uses `rapidfuzz` string matching instead of embeddings. For exact/near-exact query repetitions, fuzzy matching is faster, cheaper, and more predictable than vector similarity.

5. **Per-file session storage** — Each session is a separate JSON file organized by date, not a single growing database. This makes debugging trivial (just read the file) and avoids corruption from concurrent writes.

---

## License

MIT

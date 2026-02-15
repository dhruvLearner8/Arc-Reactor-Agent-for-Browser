# Cortex-R: Multi-Agent AI System with MCP Protocol

A reasoning-driven AI agent that leverages the **Model Context Protocol (MCP)** to orchestrate multiple tool servers, enabling complex multi-step task solving through LLM-powered perception, memory, and decision-making.

The agent receives queries via **Telegram**, reasons through problems step-by-step using external tools (web search, document retrieval, math computation, code execution), and delivers answers back to the user — all driven autonomously by an LLM in a closed-loop architecture.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [System Flow](#system-flow)
- [Project Structure](#project-structure)
- [Core Components](#core-components)
  - [Agent Loop](#agent-loop-corelooppy)
  - [Perception](#perception-modulesperceptionpy)
  - [Memory](#memory-modulesmemorypy)
  - [Decision / Strategy](#decision--strategy)
  - [Action Parser](#action-parser-modulesactionpy)
  - [MCP Session Manager](#mcp-session-manager-coresessionpy)
- [MCP Servers](#mcp-servers)
  - [MCP Server 1 — Math & Code Execution](#mcp-server-1--math--code-execution-mcp_server_1py)
  - [MCP Server 2 — Document RAG](#mcp-server-2--document-rag-mcp_server_2py)
  - [MCP Server 3 — Web Search](#mcp-server-3--web-search-mcp_server_3py)
- [Transport Protocols](#transport-protocols)
  - [stdio Transport](#stdio-transport)
  - [SSE Transport](#sse-transport-server-sent-events)
- [Configuration](#configuration)
- [Setup & Installation](#setup--installation)
- [How to Run](#how-to-run)
- [Example Queries](#example-queries)
- [Technologies Used](#technologies-used)

---

## Architecture Overview

### High-Level System Architecture

```
┌───────────────────────────────────────────────────────────────────────────────────┐
│                                 USER INTERFACE                                    │
│                                                                                   │
│   ┌─────────────┐         ┌──────────────────┐         ┌──────────────────┐      │
│   │  Telegram    │         │   CLI Terminal   │         │  (Extensible to  │      │
│   │  Bot Client  │         │   agent.py       │         │   Web/API/Slack) │      │
│   └──────┬──────┘         └────────┬─────────┘         └──────────────────┘      │
│          │                         │                                              │
└──────────┼─────────────────────────┼──────────────────────────────────────────────┘
           │   user query            │   user query
           ▼                         ▼
┌───────────────────────────────────────────────────────────────────────────────────┐
│                                                                                   │
│                          ╔══════════════════════╗                                 │
│                          ║     AGENT LOOP       ║                                 │
│                          ║     (loop.py)        ║                                 │
│                          ╚══════════╤═══════════╝                                 │
│                                     │                                             │
│       ┌─────────────────────────────┼─────────────────────────────────┐           │
│       │                             │                                 │           │
│       │         ╔═══════════════════▼════════════════════╗            │           │
│       │         ║          1. PERCEPTION                 ║            │           │
│       │         ║          (perception.py)                ║            │           │
│       │         ║                                        ║            │           │
│       │         ║  Input:  raw query string              ║            │           │
│       │         ║  LLM:    Gemini 2.0 Flash              ║            │           │
│       │         ║  Output: PerceptionResult {            ║            │           │
│       │         ║            intent:    "compare ranks"  ║            │           │
│       │         ║            entities:  ["India","Canada"]║            │           │
│       │         ║            tool_hint: "search"         ║            │           │
│       │         ║          }                             ║            │           │
│       │         ╚═══════════════════╤════════════════════╝            │           │
│       │                             │                                 │           │
│       │         ╔═══════════════════▼════════════════════╗            │           │
│       │         ║          2. MEMORY RETRIEVAL           ║            │           │
│       │         ║          (memory.py)                   ║            │           │
│       │         ║                                        ║            │           │
│       │         ║  Query → Ollama (nomic-embed-text)     ║            │           │
│       │         ║       → 768-dim vector                 ║            │           │
│       │         ║       → FAISS L2 nearest neighbor      ║            │           │
│       │         ║       → top-k MemoryItems              ║            │           │
│       │         ║                                        ║            │           │
│       │         ║  Returns: past tool results relevant   ║            │           │
│       │         ║  to current query (semantic search)    ║            │           │
│       │         ╚═══════════════════╤════════════════════╝            │           │
│       │                             │                                 │           │
│       │         ╔═══════════════════▼════════════════════╗            │           │
│       │         ║          3. DECISION / PLANNING        ║            │           │
│       │         ║          (decision.py + strategy.py)   ║            │           │
│       │         ║                                        ║            │           │
│       │         ║  Inputs:                               ║            │           │
│       │         ║    • PerceptionResult (from step 1)    ║            │           │
│       │         ║    • Retrieved memories (from step 2)  ║            │           │
│       │         ║    • Available tool descriptions       ║            │           │
│       │         ║    • Step number / max steps           ║            │           │
│       │         ║                                        ║            │           │
│       │         ║  LLM: Gemini 2.0 Flash                 ║            │           │
│       │         ║                                        ║            │           │
│       │         ║  Output (one of):                      ║            │           │
│       │         ║    FUNCTION_CALL: tool|param=value     ║            │           │
│       │         ║    FINAL_ANSWER: [result]              ║            │           │
│       │         ╚════════════╤══════════════╤════════════╝            │           │
│       │                      │              │                         │           │
│       │            ┌─────────▼───┐   ┌──────▼──────────┐             │           │
│       │            │FINAL_ANSWER │   │ FUNCTION_CALL   │             │           │
│       │            │→ break loop │   │                 │             │           │
│       │            │→ return     │   │                 │             │           │
│       │            └─────────────┘   └──────┬──────────┘             │           │
│       │                                     │                         │           │
│       │         ╔═══════════════════════════▼════════════════════╗    │           │
│       │         ║          4. ACTION PARSING                     ║    │           │
│       │         ║          (action.py)                           ║    │           │
│       │         ║                                                ║    │           │
│       │         ║  Input:  "FUNCTION_CALL: search|query=India"  ║    │           │
│       │         ║                                                ║    │           │
│       │         ║  Parsing:                                      ║    │           │
│       │         ║    • Split on "|" → tool_name + params        ║    │           │
│       │         ║    • Parse values via ast.literal_eval         ║    │           │
│       │         ║    • Support nested keys: input.string → {}   ║    │           │
│       │         ║                                                ║    │           │
│       │         ║  Output: ("search", {"query": "India..."})    ║    │           │
│       │         ╚═══════════════════════════╤════════════════════╝    │           │
│       │                                     │                         │           │
│       │         ╔═══════════════════════════▼════════════════════╗    │           │
│       │         ║          5. MCP TOOL EXECUTION                 ║    │           │
│       │         ║          (session.py → MultiMCP)               ║    │           │
│       │         ║                                                ║    │           │
│       │         ║  tool_map["search"] → mcp_server_3.py config  ║    │           │
│       │         ║                                                ║    │           │
│       │         ║  ┌─────────────────────────────────────────┐   ║    │           │
│       │         ║  │ Spawn subprocess: python mcp_server_3.py│   ║    │           │
│       │         ║  │                                         │   ║    │           │
│       │         ║  │  stdin ──► JSON-RPC request             │   ║    │           │
│       │         ║  │  stdout ◄── JSON-RPC response           │   ║    │           │
│       │         ║  │                                         │   ║    │           │
│       │         ║  │  Server runs tool → returns result      │   ║    │           │
│       │         ║  │  Subprocess terminates                  │   ║    │           │
│       │         ║  └─────────────────────────────────────────┘   ║    │           │
│       │         ╚═══════════════════════════╤════════════════════╝    │           │
│       │                                     │                         │           │
│       │         ╔═══════════════════════════▼════════════════════╗    │           │
│       │         ║          6. MEMORY STORAGE                     ║    │           │
│       │         ║                                                ║    │           │
│       │         ║  Tool result → Ollama embedding (768-dim)     ║    │           │
│       │         ║            → FAISS index.add(vector)          ║    │           │
│       │         ║            → self.data.append(MemoryItem)     ║    │           │
│       │         ║                                                ║    │           │
│       │         ║  Memory is now available for next iteration   ║    │           │
│       │         ╚═══════════════════════════╤════════════════════╝    │           │
│       │                                     │                         │           │
│       │                          ┌──────────▼──────────┐              │           │
│       │                          │  UPDATE QUERY with  │              │           │
│       │                          │  tool result context │              │           │
│       │                          │  for next iteration  │              │           │
│       │                          └──────────┬──────────┘              │           │
│       │                                     │                         │           │
│       │                                     │  ◄── LOOP BACK ──►     │           │
│       └─────────────────────────────────────┼─────────────────────────┘           │
│                                             │                                     │
│                          max_steps reached or FINAL_ANSWER                        │
│                                             │                                     │
│                                             ▼                                     │
│                                   Return answer to user                           │
│                                                                                   │
└───────────────────────────────────────────────────────────────────────────────────┘


                    ┌──────────────────────────────────────────────────────────┐
                    │                   MCP SERVER LAYER                       │
                    │                   (Tool Providers)                       │
                    │                                                          │
                    │  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐ │
                    │  │  MCP Server 1  │ │  MCP Server 2  │ │ MCP Server 3 │ │
                    │  │  Math & Code   │ │  Document RAG  │ │  Web Search  │ │
                    │  │                │ │                │ │              │ │
                    │  │ • add/subtract │ │ • search_docs  │ │ • search     │ │
                    │  │ • multiply/div │ │ • extract_web  │ │ • fetch_page │ │
                    │  │ • sqrt/cbrt    │ │ • extract_pdf  │ │              │ │
                    │  │ • factorial    │ │                │ │  DuckDuckGo  │ │
                    │  │ • sin/cos/tan  │ │  FAISS Index   │ │  + Scraping  │ │
                    │  │ • fibonacci    │ │  + Ollama Emb  │ │              │ │
                    │  │ • python_exec  │ │  + Semantic    │ │  Rate-limited│ │
                    │  │ • shell_cmd    │ │    Chunking    │ │  30 req/min  │ │
                    │  │ • sql_query    │ │                │ │              │ │
                    │  │                │ │                │ │              │ │
                    │  │  Transport:    │ │  Transport:    │ │  Transport:  │ │
                    │  │  stdio         │ │  stdio         │ │  stdio       │ │
                    │  └────────────────┘ └────────────────┘ └──────────────┘ │
                    │                                                          │
                    └──────────────────────────────────────────────────────────┘


                    ┌──────────────────────────────────────────────────────────┐
                    │                    LLM / EMBEDDING LAYER                  │
                    │                                                          │
                    │  ┌──────────────────────┐   ┌──────────────────────────┐ │
                    │  │  Gemini 2.0 Flash     │   │  Ollama (local)          │ │
                    │  │  (Google AI)           │   │                          │ │
                    │  │                        │   │  nomic-embed-text        │ │
                    │  │  Used for:             │   │  (768-dim embeddings)    │ │
                    │  │  • Perception (intent  │   │                          │ │
                    │  │    extraction)          │   │  Used for:              │ │
                    │  │  • Decision (planning,  │   │  • Session memory       │ │
                    │  │    tool selection)      │   │    embeddings           │ │
                    │  │  • Final answer         │   │  • Document RAG         │ │
                    │  │    generation           │   │    embeddings           │ │
                    │  │                        │   │  • Semantic similarity   │ │
                    │  │  API: Google GenAI     │   │    search (FAISS)       │ │
                    │  └──────────────────────┘   └──────────────────────────┘ │
                    │                                                          │
                    └──────────────────────────────────────────────────────────┘
```

### Data Flow for a Single Agent Step

```
     query: "What is India's passport ranking?"
       │
       │  ┌──────────────────────────────────────────────────────────────┐
       ├─►│  PERCEPTION (Gemini)                                        │
       │  │                                                              │
       │  │  Prompt: "Extract intent, entities, tool_hint from: ..."    │
       │  │                                                              │
       │  │  Returns: {                                                  │
       │  │    intent: "get passport ranking",                           │
       │  │    entities: ["India", "passport", "ranking"],               │
       │  │    tool_hint: "search",                                      │
       │  │    user_input: "What is India's passport ranking?"           │
       │  │  }                                                           │
       │  └──────────────────────────┬───────────────────────────────────┘
       │                             │
       │                             ▼
       │  ┌──────────────────────────────────────────────────────────────┐
       │  │  MEMORY RETRIEVAL                                            │
       │  │                                                              │
       │  │  query ──► Ollama embed ──► [0.12, -0.45, ..., 0.89]        │
       │  │                                    768-dim vector            │
       │  │                                        │                     │
       │  │                           FAISS L2 Search (top_k=3)          │
       │  │                                        │                     │
       │  │                                        ▼                     │
       │  │  Step 1: [] (empty — no prior context)                       │
       │  │  Step 2: [MemoryItem("search(India)→75th...")]              │
       │  │  Step 3: [MemoryItem("search(India)→75th"),                  │
       │  │           MemoryItem("search(Canada)→8th")]                  │
       │  └──────────────────────────┬───────────────────────────────────┘
       │                             │
       │                             ▼
       │  ┌──────────────────────────────────────────────────────────────┐
       │  │  DECISION (Gemini)                                           │
       │  │                                                              │
       │  │  Prompt includes:                                            │
       │  │    • Perception output (intent, entities, tool_hint)         │
       │  │    • Retrieved memories (past tool results)                  │
       │  │    • Available tools with descriptions + usage patterns      │
       │  │    • Step N of max_steps                                     │
       │  │    • Rules: no tool invention, no repeats, structured output │
       │  │                                                              │
       │  │  Returns exactly ONE line:                                   │
       │  │    "FUNCTION_CALL: search|query=India passport ranking"      │
       │  │              OR                                              │
       │  │    "FINAL_ANSWER: India is ranked 75th..."                   │
       │  └──────────────────────────┬───────────────────────────────────┘
       │                             │
       │                             ▼
       │  ┌──────────────────────────────────────────────────────────────┐
       │  │  ACTION PARSER                                               │
       │  │                                                              │
       │  │  "FUNCTION_CALL: search|query=India passport ranking"        │
       │  │       │                                                      │
       │  │       ├─ split(":") → "search|query=India passport ranking"  │
       │  │       ├─ split("|") → ["search", "query=India passport..."]  │
       │  │       ├─ split("=") → key="query", val="India passport..."   │
       │  │       └─ ast.literal_eval(val) or string fallback            │
       │  │                                                              │
       │  │  Result: ("search", {"query": "India passport ranking"})     │
       │  └──────────────────────────┬───────────────────────────────────┘
       │                             │
       │                             ▼
       │  ┌──────────────────────────────────────────────────────────────┐
       │  │  MCP TOOL EXECUTION (session.py)                             │
       │  │                                                              │
       │  │  tool_map["search"] → {config: mcp_server_3.py, cwd: ...}   │
       │  │                                                              │
       │  │  ┌────────────────────────────────────────────────────┐      │
       │  │  │           SUBPROCESS (stdio transport)             │      │
       │  │  │                                                    │      │
       │  │  │  Agent ──stdin──► {"jsonrpc":"2.0",                │      │
       │  │  │                    "method":"tools/call",          │      │
       │  │  │                    "params":{"name":"search",      │      │
       │  │  │                     "arguments":{"query":"..."}}}  │      │
       │  │  │                                                    │      │
       │  │  │  mcp_server_3.py: DuckDuckGo search → 10 results  │      │
       │  │  │                                                    │      │
       │  │  │  Agent ◄─stdout── {"result": "Found 10 results:   │      │
       │  │  │                    1. India 75th most powerful..."} │      │
       │  │  │                                                    │      │
       │  │  │  Subprocess terminates ✓                           │      │
       │  │  └────────────────────────────────────────────────────┘      │
       │  └──────────────────────────┬───────────────────────────────────┘
       │                             │
       │                             ▼
       │  ┌──────────────────────────────────────────────────────────────┐
       │  │  MEMORY STORAGE                                              │
       │  │                                                              │
       │  │  MemoryItem {                                                │
       │  │    text: "search({query:India...}) → Found 10 results...",   │
       │  │    type: "tool_output",                                      │
       │  │    tool_name: "search",                                      │
       │  │    session_id: "session-17710988-a3f2b1",                    │
       │  │    tags: ["search"]                                          │
       │  │  }                                                           │
       │  │       │                                                      │
       │  │       ├─ Ollama embed → 768-dim vector                       │
       │  │       ├─ FAISS index.add(vector)                             │
       │  │       └─ self.data.append(MemoryItem)                        │
       │  │                                                              │
       │  │  Memory state: [vector_1] in FAISS, [item_1] in RAM         │
       │  └──────────────────────────────────────────────────────────────┘
       │
       └──► LOOP BACK TO PERCEPTION (with updated query context)
```

### Multi-Step Execution Example

Query: **"What is India's passport ranking and how much below Canada is it?"**

```
┌─────────┬────────────────────────────┬─────────────────────────────────┬──────────────────────────┐
│  Step   │  Perception                │  Decision                       │  Tool Execution          │
├─────────┼────────────────────────────┼─────────────────────────────────┼──────────────────────────┤
│         │  intent: compare ranks     │                                 │                          │
│  1      │  entities: [India, Canada] │  FUNCTION_CALL:                 │  mcp_server_3 (stdio)    │
│         │  hint: search              │  search|query="India passport"  │  → DuckDuckGo → 10 hits │
│         │                            │                                 │  → "India ranked 75th"   │
├─────────┼────────────────────────────┼─────────────────────────────────┼──────────────────────────┤
│         │  intent: compare ranks     │                                 │                          │
│  2      │  entities: [India, Canada] │  FUNCTION_CALL:                 │  mcp_server_3 (stdio)    │
│         │  hint: search              │  search|query="Canada passport" │  → DuckDuckGo → 10 hits │
│         │  memory: [India=75th]      │                                 │  → "Canada ranked 8th"   │
├─────────┼────────────────────────────┼─────────────────────────────────┼──────────────────────────┤
│         │  intent: compare ranks     │                                 │                          │
│  3      │  entities: [India, Canada] │  FINAL_ANSWER:                  │  (no tool call)          │
│         │  memory: [India=75th,      │  India=75th, Canada=8th.        │                          │
│         │   Canada=8th]              │  India is 67 ranks below.       │  → Return to user        │
└─────────┴────────────────────────────┴─────────────────────────────────┴──────────────────────────┘
```

---

## Project Structure

```
S8 Share/
├── telegram_agent.py        # Entry point — Telegram bot integration
├── agent.py                 # Alternative entry point — CLI-based
│
├── core/
│   ├── loop.py              # Main agent loop (Perception → Memory → Decision → Action)
│   ├── context.py           # Session state, agent profile, memory manager
│   ├── session.py           # MCP client — manages connections to MCP servers
│   └── strategy.py          # Strategy wrapper for decision-making
│
├── modules/
│   ├── perception.py        # LLM-based intent/entity extraction
│   ├── decision.py          # LLM-based planning (tool call or final answer)
│   ├── action.py            # Parses FUNCTION_CALL strings into executable format
│   ├── memory.py            # FAISS-based semantic memory (in-RAM)
│   ├── model_manager.py     # Unified LLM interface (Gemini, Ollama)
│   └── tools.py             # Tool summarization and filtering utilities
│
├── mcp_server_1.py          # MCP Server — Math, code sandbox, shell commands
├── mcp_server_2.py          # MCP Server — Document RAG (FAISS + Ollama embeddings)
├── mcp_server_3.py          # MCP Server — Web search (DuckDuckGo) + content fetching
├── models.py                # Pydantic models for tool I/O schemas
│
├── config/
│   ├── profiles.yaml        # Agent configuration (strategy, memory, MCP servers)
│   └── models.json          # LLM model configurations (Gemini, Ollama, etc.)
│
├── documents/               # Source documents for RAG indexing
├── faiss_index/             # Persisted FAISS index + metadata for document search
│
├── .env                     # API keys (GEMINI_API_KEY, TELEGRAM_BOT_KEY)
├── pyproject.toml           # Python dependencies (managed by uv)
└── .gitignore               # Excludes secrets, venv, indices
```

---

## Core Components

### Agent Loop (`core/loop.py`)

The central orchestrator. Runs a `for` loop up to `max_steps` (configurable, default 10):

```
for each step:
    1. extract_perception(query)     → PerceptionResult {intent, entities, tool_hint}
    2. memory.retrieve(query)        → List[MemoryItem] (top-k similar past results)
    3. decide_next_action(...)       → "FUNCTION_CALL: tool|args" or "FINAL_ANSWER: ..."
    4. if FINAL_ANSWER → break
    5. parse_function_call(plan)     → (tool_name, arguments)
    6. mcp.call_tool(tool_name, args) → result from MCP server
    7. memory.add(result)            → embed and store in FAISS
    8. update query with result context
```

Each step's tool output is stored in FAISS memory, allowing subsequent steps to build on previous results.

### Perception (`modules/perception.py`)

Uses **Gemini 2.0 Flash** to extract structured information from raw input:

| Field | Description |
|-------|-------------|
| `intent` | What the user wants (e.g., "compare passport rankings") |
| `entities` | Key terms (e.g., ["India", "Canada", "passport"]) |
| `tool_hint` | Suggested tool name (e.g., "search") |
| `user_input` | Original query text |

Returns a `PerceptionResult` (Pydantic model) used by downstream decision-making.

### Memory (`modules/memory.py`)

**In-RAM semantic memory** using FAISS (Facebook AI Similarity Search):

- **Storage**: Each tool output is embedded via Ollama (`nomic-embed-text`) into a 768-dimensional vector
- **Index**: FAISS `IndexFlatL2` for exact L2 nearest-neighbor search
- **Retrieval**: Given a query, embeds it and finds the `top_k` most similar past results
- **Filtering**: Supports filtering by `type` (tool_output, fact, query), `tags`, and `session_id`
- **Lifecycle**: Memory exists only for the duration of one agent session (RAM-only, no persistence)

This allows the agent to recall what it learned in previous steps — e.g., "I already searched India's ranking, now I need Canada's."

### Decision / Strategy

**`modules/decision.py`** — Generates the next action using Gemini:
- Receives perception, memory, and available tool descriptions
- Outputs exactly one line: `FUNCTION_CALL: tool|args` or `FINAL_ANSWER: result`
- Enforced rules: no tool invention, no repeated calls, structured output only

**`core/strategy.py`** — Wraps decision with strategy logic:
- `conservative`: Use filtered tools (based on perception's `tool_hint`)
- `retry_once`: If filtered tools fail, retry with all available tools
- `explore_all`: (placeholder) Parallel tool exploration

### Action Parser (`modules/action.py`)

Parses LLM-generated function call strings into executable format:

```
Input:  "FUNCTION_CALL: search_documents|query=India passport ranking"
Output: ("search_documents", {"query": "India passport ranking"})

Input:  "FUNCTION_CALL: strings_to_chars_to_int|input.string=INDIA"
Output: ("strings_to_chars_to_int", {"input": {"string": "INDIA"}})
```

Supports:
- Nested keys via dot notation (`input.string` → `{"input": {"string": ...}}`)
- Python literal parsing via `ast.literal_eval` (lists, dicts, numbers)
- String fallback for unparseable values

### MCP Session Manager (`core/session.py`)

Manages connections to multiple MCP servers:

- **`MultiMCP`** class: Discovers tools from all configured servers at startup
- **Tool routing**: Maintains a `tool_map` (tool_name → server config) so the right server is called
- **Stateless**: Each `call_tool()` spawns a fresh subprocess — no persistent connections
- **Protocol**: JSON-RPC over stdio (stdin/stdout pipes to subprocess)

```python
# Discovery phase (initialize)
for each server in profiles.yaml:
    spawn subprocess → list_tools() → register in tool_map

# Execution phase (call_tool)
tool_map["search"] → config for mcp_server_3.py
spawn subprocess → call_tool("search", args) → return result → subprocess dies
```

---

## MCP Servers

### MCP Server 1 — Math & Code Execution (`mcp_server_1.py`)

**Transport**: stdio | **27 tools** including:

| Tool | Description |
|------|-------------|
| `add`, `subtract`, `multiply`, `divide` | Basic arithmetic |
| `power`, `sqrt`, `cbrt` | Exponentiation and roots |
| `factorial`, `remainder` | Number theory |
| `sin`, `cos`, `tan` | Trigonometry |
| `strings_to_chars_to_int` | Convert string to ASCII values |
| `int_list_to_exponential_sum` | Sum of e^x for each value |
| `fibonacci_numbers` | Generate Fibonacci sequence |
| `run_python_sandbox` | Execute arbitrary Python code safely |
| `run_shell_command` | Run whitelisted shell commands |
| `run_sql_query` | Execute SELECT-only SQL queries |

### MCP Server 2 — Document RAG (`mcp_server_2.py`)

**Transport**: stdio | **Retrieval-Augmented Generation**

- Processes documents from `documents/` folder at startup
- Creates a persistent FAISS index (`faiss_index/`) with Ollama embeddings
- Semantic chunking using local LLMs for intelligent document splitting

| Tool | Description |
|------|-------------|
| `search_documents` | Semantic search over indexed documents |
| `extract_webpage` | Fetch and convert webpage to markdown |
| `extract_pdf` | Extract text from PDF files |

### MCP Server 3 — Web Search (`mcp_server_3.py`)

**Transport**: stdio | **Live internet access**

| Tool | Description |
|------|-------------|
| `search` | DuckDuckGo web search (returns top 10 results) |
| `fetch_content` | Fetch and clean webpage content |

Includes rate limiting (30 req/min) and retry logic for robustness.

---

## Transport Protocols

### stdio Transport

The default transport for MCP servers. The agent spawns each server as a **subprocess** and communicates via **stdin/stdout pipes** using JSON-RPC:

```
Agent (loop.py) → session.py → spawns: python mcp_server_1.py
                                  │
                     stdin ◄──────┤──────► stdout
                     (JSON-RPC request)    (JSON-RPC response)
                                  │
                          subprocess dies after response
```

- **Pros**: Simple, isolated, no port management
- **Cons**: Cold start per call (subprocess spawn overhead)

### SSE Transport (Server-Sent Events)

An alternative transport where the MCP server runs as a **persistent HTTP server**:

```
MCP server starts once → listens on http://localhost:PORT/sse
                              │
Agent connects via HTTP ──────┤
                              │
Requests/responses flow as Server-Sent Events
                              │
Server stays running (no respawn)
```

- **Pros**: No cold start, persistent state, can serve multiple clients
- **Cons**: Requires manual server startup, port management

To use SSE in a FastMCP server:
```python
mcp.run(transport="sse", host="0.0.0.0", port=8002)
```

---

## Configuration

### `config/profiles.yaml`

Central configuration file controlling agent behavior:

```yaml
agent:
  name: Cortex-R                    # Agent identity
  id: cortex_r_001

strategy:
  type: conservative                # conservative | retry_once | explore_all
  max_steps: 10                     # Max tool-use iterations per query

memory:
  top_k: 3                          # Number of memories to retrieve per step
  type_filter: tool_output          # Only retrieve tool outputs (not all memory types)
  embedding_model: nomic-embed-text # Ollama embedding model
  embedding_url: http://localhost:11434/api/embeddings

llm:
  text_generation: gemini           # Primary LLM for perception + decision
  embedding: nomic                  # Embedding model for memory

mcp_servers:                        # List of MCP servers to connect to
  - id: math
    script: mcp_server_1.py
    cwd: /path/to/project
  - id: documents
    script: mcp_server_2.py
    cwd: /path/to/project
  - id: websearch
    script: mcp_server_3.py
    cwd: /path/to/project
```

### `config/models.json`

LLM provider configurations:

```json
{
  "models": {
    "gemini": {
      "type": "gemini",
      "model": "gemini-2.0-flash",
      "api_key_env": "GEMINI_API_KEY"
    },
    "phi4": {
      "type": "ollama",
      "model": "phi4",
      "url": {
        "generate": "http://localhost:11434/api/generate",
        "embed": "http://localhost:11434/api/embeddings"
      }
    }
  }
}
```

---

## Setup & Installation

### Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — Fast Python package manager
- **[Ollama](https://ollama.ai/)** — Local LLM runtime (for embeddings)
- **Gemini API Key** — From [Google AI Studio](https://aistudio.google.com/)
- **Telegram Bot Token** — From [@BotFather](https://t.me/BotFather) on Telegram

### Step 1: Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/cortex-r-agent.git
cd cortex-r-agent
```

### Step 2: Install dependencies

```bash
uv sync
```

Or with pip:

```bash
pip install -r requirements.txt
```

Key dependencies:
- `mcp[cli]` — Model Context Protocol SDK
- `google-genai` — Gemini API client
- `faiss-cpu` — Vector similarity search
- `python-telegram-bot` — Telegram Bot API
- `requests`, `httpx` — HTTP clients
- `pydantic` — Data validation
- `trafilatura`, `markitdown`, `pymupdf4llm` — Content extraction

### Step 3: Setup Ollama

```bash
# Install Ollama from https://ollama.ai
# Pull the embedding model
ollama pull nomic-embed-text

# Verify it's running
curl http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "test"}'
```

### Step 4: Create `.env` file

```bash
GEMINI_API_KEY=your_gemini_api_key_here
TELEGRAM_BOT_KEY=your_telegram_bot_token_here
```

### Step 5: Update `config/profiles.yaml`

Update the `cwd` paths under `mcp_servers` to match your local project directory:

```yaml
mcp_servers:
  - id: math
    script: mcp_server_1.py
    cwd: /your/actual/path/to/project
```

### Step 6: (Optional) Add documents for RAG

Place `.md`, `.pdf`, or `.txt` files in the `documents/` folder. The RAG server (`mcp_server_2.py`) will index them automatically on first run.

---

## How to Run

### Option A: Telegram Bot (Primary)

```bash
cd "path/to/S8 Share"
uv run telegram_agent.py
```

Then open Telegram, find your bot, and send a message. The agent will:
1. Receive your message
2. Process it through the agent loop
3. Send the answer back on Telegram

### Option B: CLI Mode

```bash
cd "path/to/S8 Share"
uv run agent.py
```

Type your query when prompted. The answer is printed to the terminal.

---

## Example Queries

### Web Search + Reasoning
```
What is India's passport ranking and how much below Canada is it?
```
Agent: search India → search Canada → subtract → FINAL_ANSWER

### Multi-Tool Chain (ASCII → Math)
```
Find the ASCII values of characters in INDIA and return the sum of exponentials of those values.
```
Agent: strings_to_chars_to_int("INDIA") → [73,78,68,73,65] → int_list_to_exponential_sum → FINAL_ANSWER

### Code Execution
```
What is the log base 10 of 1024? Use Python to compute it.
```
Agent: run_python_sandbox("import math; result = math.log(1024, 10)") → FINAL_ANSWER

### Document RAG
```
How much did Anmol Singh pay for his DLF apartment via Capbridge?
```
Agent: search_documents("Anmol Singh DLF Capbridge") → extracts answer from indexed documents

### Math
```
What is the factorial of 12 divided by the factorial of 9?
```
Agent: factorial(12) → factorial(9) → divide → FINAL_ANSWER

---

## Technologies Used

| Technology | Purpose |
|-----------|---------|
| **MCP (Model Context Protocol)** | Inter-agent communication protocol for tool calling |
| **FastMCP** | Python framework for building MCP servers |
| **Gemini 2.0 Flash** | Primary LLM for perception and decision-making |
| **Ollama** | Local LLM runtime for embeddings (`nomic-embed-text`) |
| **FAISS** | In-memory vector similarity search for semantic memory |
| **Python Telegram Bot** | Telegram bot integration |
| **DuckDuckGo** | Web search provider (no API key needed) |
| **Pydantic** | Data validation and schema definitions |
| **Trafilatura** | Web content extraction |
| **MarkItDown** | Document-to-markdown conversion |
| **uv** | Fast Python package management |

---

## License

MIT

---

*Built as part of The School of AI — EAG (Engineered Agentic Growth) Program*

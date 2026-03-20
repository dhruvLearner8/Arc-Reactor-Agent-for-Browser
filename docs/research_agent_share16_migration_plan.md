# Research Agent Migration Plan (S8 Share <- share16)

## Scope
- Keep current `S8 Share` system and Arc Reactor progress.
- Port only Research-Agent-relevant architecture from `share16`.
- Explicitly exclude REMME/user-preference subsystems for now.

## What share16 improves for research reliability
- **Loop resilience**: retry-with-backoff for transient agent failures, step retries before DAG failure, safer stop/task cancellation.
- **Retriever behavior hardening**: enforce extraction after URL discovery; defensive re-prompting on code/runtime failures.
- **MCP resilience**: circuit breaker in tool routing to fail fast on repeatedly failing tools.
- **Browser MCP tooling**: richer retrieval tools (`fetch_search_urls`, `search_web_with_text_content`, `webpage_url_to_raw_text`) and stable JSON outputs.
- **Retriever prompt contract**: stricter `writes` key usage and defensive parsing patterns for iteration handoffs.

## Gap analysis against current S8 Share
- Current `core/loop.py` already has strong ReAct/tool loop and clarification callback flow, but does not have:
  - centralized retry_with_backoff for `run_agent` calls
  - DAG step retry policy before marking failure
- Current `mcp_servers/multi_mcp.py` has no circuit-breaker guard around tool routing.
- Current `mcp_servers/server_browser.py` lacks bulk-research tools that share16 added and returns `str(urls)` for `web_search`, which can create parsing fragility.
- Current retriever prompt is improved but not as strict as share16 on mandatory defensive parsing and multi-step extraction contract.

## Migration strategy (safe phased rollout)

### Phase 1 (implemented)
- Add `core/circuit_breaker.py`.
- Integrate circuit breaker into `MultiMCP.route_tool_call`.
- Add loop-level `retry_with_backoff` helper in `core/loop.py`.
- Use backoff for Planner and Distiller invocations in `run()`.
- Add step-level retries in `_execute_dag()` for transient/agent failures.
- Keep existing callback/event architecture unchanged.

### Phase 2 (implemented)
- Upgrade browser MCP API surface in `server_browser.py`:
  - `web_search` returns JSON string (`json.dumps`).
  - add `fetch_search_urls`.
  - add `webpage_url_to_raw_text`.
  - add `search_web_with_text_content` for one-shot URL+content research.
- Preserve existing tool names to avoid breaking current callers.

### Phase 3 (implemented)
- Tighten `prompts/retriever.md`:
  - enforce exact `writes` keys in JSON and return payload.
  - mandate defensive parsing when data may be serialized string/list.
  - require extraction pass for factual queries after URL discovery.

### Phase 4 (in progress)
- Auto-clarification injection from planner confidence metadata.
- Trigger only when:
  - `interpretation_confidence < 0.7`
  - `ambiguity_notes` exists
  - no existing ClarificationAgent node
  - runtime has clarification callback available
- Cost guardrails in execution loop:
  - warning at `WARN_AT_COST` (default `0.25`)
  - hard stop at `MAX_COST_PER_RUN` (default `0.50`)
  - graph status marked `cost_exceeded`

### Phase 5 (defer)
- Full dynamic MCP config/caching subsystem from share16 `multi_mcp.py`.
- Mid-session replanning `_handle_failures`.
- Metrics aggregation and GAIA benchmark harness.

## Porting status matrix
| Capability | share16 state | S8 Share status | Notes |
|---|---|---|---|
| Agent backoff retry | present | done | Added `retry_with_backoff` for Planner/Distiller/step runs |
| Step-level retry before fail | present | done | `_retry_count` per node in DAG loop |
| Tool circuit breaker | present | done | `core/circuit_breaker.py` + `MultiMCP.route_tool_call` |
| Bulk research MCP tools | present | done | Added 3 retrieval helpers in `server_browser.py` |
| JSON-stable web search output | present | done | `web_search` now returns JSON string |
| Retriever strict writes contract | present | done | Prompt tightened for exact `writes` usage |
| Auto clarification from confidence | present | done | Injected when planner provides confidence metadata |
| Cost threshold stop | present | done | Env-based thresholds, no settings loader dependency |
| Mid-session replanning | stub | partial | Failure-handler scaffold + env gate added |
| Metrics aggregator | present | pending | Deferred until core run path stabilized |
| Research smoke benchmark | present (GAIA-lite style) | done | Added `benchmarks/research_smoke/runner.py` |

## Execution checkpoints
1. **Compile integrity**: `py_compile` on modified core/mcp files.
2. **Tool contract check**: ensure retriever can call newly exposed tools.
3. **Research run smoke tests**:
   - factual query requiring search + extract
   - JS-heavy website extraction
   - transient MCP/tool failure simulation
4. **Failure-mode validation**:
   - circuit opens after repeated failures
   - step retries before DAG fail
   - no URL-only final output for factual queries

## Risk controls
- Keep backward-compatible tool names and expected return types where already used.
- Avoid broad refactors in context/session code paths.
- Validate with syntax checks and focused lint diagnostics on modified files.

## Success criteria
- Fewer research runs failing from transient tool/LLM errors.
- URL-only dead-end outputs reduced by stricter retriever flow.
- Faster degradation under external tool outages due to circuit breaker behavior.

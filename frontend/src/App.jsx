import { useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
} from "reactflow";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import TurndownService from "turndown";

const statusColor = {
  pending: "#9ca3af",
  running: "#f59e0b",
  completed: "#10b981",
  failed: "#ef4444",
};

const turndown = new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced" });

function statusSubtitle(node) {
  const agent = (node.agent || "").toLowerCase();
  const status = node.status || "pending";
  if (status === "running") {
    if (agent.includes("planner")) return "Planning execution graph...";
    if (agent.includes("retriever")) return "Retrieving sources and facts...";
    if (agent.includes("thinker")) return "Reasoning over collected evidence...";
    if (agent.includes("distiller")) return "Distilling insights...";
    if (agent.includes("formatter")) return "Formatting final report...";
    return "Running...";
  }
  if (status === "completed") return "Completed";
  if (status === "failed") return "Failed";
  return "Waiting on dependencies...";
}

function AgentNode({ data }) {
  return (
    <div>
      <Handle type="target" position={Position.Top} style={{ background: "#60a5fa" }} />
      <div className="agent-node-title">{data.label}</div>
      <div className="agent-node-subtitle">{data.subtitle}</div>
      {data.description ? <div className="agent-node-desc">{data.description}</div> : null}
      <Handle type="source" position={Position.Bottom} style={{ background: "#60a5fa" }} />
    </div>
  );
}

const nodeTypes = { agentNode: AgentNode };

function buildFlowData(run) {
  if (!run) return { nodes: [], edges: [] };
  const rawNodes = run.nodes ?? [];
  const rawEdges = run.links ?? [];

  const ids = new Set(rawNodes.map((n) => n.id));
  const incoming = new Map();
  rawNodes.forEach((n) => incoming.set(n.id, []));
  rawEdges.forEach((e) => {
    if (ids.has(e.target)) incoming.get(e.target).push(e.source);
  });

  const levels = new Map();
  const queue = [];
  if (ids.has("ROOT")) {
    levels.set("ROOT", 0);
    queue.push("ROOT");
  }

  while (queue.length > 0) {
    const current = queue.shift();
    const currentLevel = levels.get(current) ?? 0;
    rawEdges
      .filter((e) => e.source === current)
      .forEach((e) => {
        const next = e.target;
        const candidate = currentLevel + 1;
        if (!levels.has(next) || candidate > levels.get(next)) {
          levels.set(next, candidate);
          queue.push(next);
        }
      });
  }

  rawNodes.forEach((n) => {
    if (!levels.has(n.id)) levels.set(n.id, 1);
  });

  const byLevel = new Map();
  rawNodes.forEach((n) => {
    const level = levels.get(n.id) ?? 1;
    if (!byLevel.has(level)) byLevel.set(level, []);
    byLevel.get(level).push(n);
  });

  const xGap = 280;
  const yGap = 170;
  const maxPerLevel = Math.max(...Array.from(byLevel.values()).map((arr) => arr.length), 1);

  const nodes = [];
  Array.from(byLevel.keys())
    .sort((a, b) => a - b)
    .forEach((level) => {
      const arr = byLevel.get(level) ?? [];
      arr.forEach((node, idx) => {
        const offset = (maxPerLevel - arr.length) * 0.5;
        const x = 120 + (idx + offset) * xGap;
        const y = 80 + level * yGap;
        const status = node.status ?? "pending";
        nodes.push({
          id: node.id,
          type: "agentNode",
          position: { x, y },
          data: {
            label: `${node.id} · ${node.agent ?? "Agent"}`,
            description: node.description ?? "",
            status,
            subtitle: statusSubtitle(node),
          },
          style: {
            width: 230,
            borderRadius: 12,
            border: `2px solid ${statusColor[status] ?? "#6b7280"}`,
            padding: 10,
            background: "#0f172a",
            color: "#f8fafc",
          },
        });
      });
    });

  const edges = rawEdges.map((e, idx) => ({
    id: `${e.source}-${e.target}-${idx}`,
    source: e.source,
    target: e.target,
    animated: true,
    style: { stroke: "#60a5fa", strokeWidth: 2 },
    markerEnd: {
      type: MarkerType.ArrowClosed,
      color: "#60a5fa",
      width: 18,
      height: 18,
    },
  }));

  return { nodes, edges };
}

function renderNodeOutput(node) {
  if (!node) return "Select a node to view details.";
  const normalizeString = (value) => {
    if (typeof value !== "string") return "";
    let text = value.trim();
    if (
      (text.startsWith('"') && text.endsWith('"')) ||
      (text.startsWith("'") && text.endsWith("'"))
    ) {
      try {
        text = JSON.parse(text);
      } catch {
        text = text.slice(1, -1);
      }
    }
    return text.replaceAll("\\n", "\n");
  };

  const pickBestString = (obj) => {
    if (!obj || typeof obj !== "object") return "";
    const priority = [
      "final_answer",
      "formatted_output",
      "formatted_report",
      "formatted_dollar_report_T008",
      "summary",
    ];
    for (const key of priority) {
      if (typeof obj[key] === "string") return normalizeString(obj[key]);
    }
    const dynamicFormatted = Object.keys(obj).find(
      (k) => k.toLowerCase().includes("formatted") && typeof obj[k] === "string"
    );
    if (dynamicFormatted) return normalizeString(obj[dynamicFormatted]);

    const firstString = Object.values(obj).find((v) => typeof v === "string");
    return firstString ? normalizeString(firstString) : "";
  };

  const htmlToMarkdownIfNeeded = (text) => {
    if (typeof text !== "string") return text;
    const looksLikeHtml = /<\/?[a-z][\s\S]*>/i.test(text) || text.includes("<div");
    if (!looksLikeHtml) return text;
    try {
      return turndown.turndown(text);
    } catch {
      return text;
    }
  };

  const normalizeMarkdownTables = (text) => {
    if (typeof text !== "string" || !text.includes("|")) return text;
    let normalized = text;

    // Split header row and separator row when model emits:
    // | h1 | h2 | |---|---|
    normalized = normalized.replace(/\|\s+\|(?=\s*:?-{3,}:?\s*\|)/g, "|\n|");

    // Split concatenated data rows in malformed one-line tables:
    // | row1... | | row2... |
    normalized = normalized.replace(/\|\s+\|(?=\s*[A-Za-z0-9(\["'])/g, "|\n|");

    return normalized;
  };

  const output = node.output;
  if (typeof output === "string") return normalizeString(output);
  if (!output) return "No output captured for this node.";
  const best = pickBestString(output);
  if (best) return normalizeMarkdownTables(htmlToMarkdownIfNeeded(best));
  return `\`\`\`json\n${JSON.stringify(output, null, 2)}\n\`\`\``;
}

export default function App() {
  const [runs, setRuns] = useState([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [runDetail, setRunDetail] = useState(null);
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [query, setQuery] = useState("");
  const [loadingRuns, setLoadingRuns] = useState(false);
  const [creatingRun, setCreatingRun] = useState(false);
  const [error, setError] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const [viewMode, setViewMode] = useState("rendered");
  const [inspectorExpanded, setInspectorExpanded] = useState(false);

  const selectedNode = useMemo(
    () => runDetail?.nodes?.find((n) => n.id === selectedNodeId),
    [runDetail, selectedNodeId]
  );

  const flowData = useMemo(() => buildFlowData(runDetail), [runDetail]);

  async function fetchRuns() {
    setLoadingRuns(true);
    setError("");
    try {
      const res = await fetch("/api/runs");
      const data = await res.json();
      setRuns(Array.isArray(data) ? data : []);
      if (!selectedRunId && Array.isArray(data) && data.length > 0) {
        setSelectedRunId(data[0].run_id);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setLoadingRuns(false);
    }
  }

  async function fetchRunDetail(runId) {
    if (!runId) return;
    setError("");
    try {
      const res = await fetch(`/api/runs/${runId}`);
      if (!res.ok) throw new Error(`Failed to load run ${runId}`);
      const data = await res.json();
      setRunDetail(data);
      const firstNode = (data.nodes ?? []).find((n) => n.id !== "ROOT");
      setSelectedNodeId(firstNode?.id ?? "ROOT");
    } catch (e) {
      setError(String(e));
    }
  }

  async function createRun() {
    if (!query.trim()) return;
    setCreatingRun(true);
    setError("");
    try {
      const res = await fetch("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query.trim() }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail ?? "Run creation failed");
      }
      const data = await res.json();
      setSelectedRunId(data.run_id);
      setQuery("");
      await fetchRuns();
    } catch (e) {
      setError(String(e));
    } finally {
      setCreatingRun(false);
    }
  }

  useEffect(() => {
    fetchRuns();
  }, []);

  useEffect(() => {
    if (selectedRunId) fetchRunDetail(selectedRunId);
  }, [selectedRunId]);

  useEffect(() => {
    if (!selectedRunId || !selectedRunId.startsWith("run_")) {
      setIsStreaming(false);
      return;
    }
    const stream = new EventSource(`/api/runs/${selectedRunId}/events`);
    setIsStreaming(true);

    stream.addEventListener("run_update", (evt) => {
      try {
        const payload = JSON.parse(evt.data);
        if (payload?.snapshot) {
          setRunDetail(payload.snapshot);
          const running = runs.find((r) => r.run_id === selectedRunId);
          if (!selectedNodeId && payload.snapshot.nodes?.length) {
            const firstNode = payload.snapshot.nodes.find((n) => n.id !== "ROOT");
            setSelectedNodeId(firstNode?.id ?? "ROOT");
          } else if (running?.status === "running") {
            const activeNode = payload.snapshot.nodes?.find((n) => n.status === "running");
            if (activeNode) setSelectedNodeId(activeNode.id);
          }
        }
      } catch {
        // no-op
      }
    });

    stream.addEventListener("run_complete", (evt) => {
      try {
        const payload = JSON.parse(evt.data);
        if (payload?.snapshot) setRunDetail(payload.snapshot);
      } catch {
        // no-op
      }
      setIsStreaming(false);
      fetchRuns();
      stream.close();
    });

    stream.addEventListener("run_error", (evt) => {
      try {
        const payload = JSON.parse(evt.data);
        setError(payload?.error || "Run failed");
      } catch {
        setError("Run failed");
      }
      setIsStreaming(false);
      fetchRuns();
      stream.close();
    });

    stream.onerror = () => {
      setIsStreaming(false);
      stream.close();
    };

    return () => {
      stream.close();
      setIsStreaming(false);
    };
  }, [selectedRunId]);

  return (
    <div className={`app-shell ${inspectorExpanded ? "inspector-expanded" : ""}`}>
      <aside className="left-panel">
        <h2>Runs</h2>
        <div className="new-run-box">
          <textarea
            rows={4}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Ask something..."
          />
          <button onClick={createRun} disabled={creatingRun}>
            {creatingRun ? "Running..." : "New Run"}
          </button>
        </div>
        <button className="refresh-btn" onClick={fetchRuns} disabled={loadingRuns}>
          {loadingRuns ? "Refreshing..." : "Refresh History"}
        </button>
        <div className="run-list">
          {runs.map((run) => (
            <button
              key={run.run_id}
              className={`run-item ${selectedRunId === run.run_id ? "active" : ""}`}
              onClick={() => setSelectedRunId(run.run_id)}
            >
              <div className="run-id">#{run.run_id}</div>
              <div className="run-query">{run.query || "No query text"}</div>
              <div className="run-meta">
                {run.completed_steps}/{run.total_steps} completed
              </div>
            </button>
          ))}
        </div>
      </aside>

      <main className="graph-panel">
        <div className="panel-title">Execution Graph</div>
        {isStreaming ? <div className="live-pill">Live</div> : null}
        <ReactFlow
          nodes={flowData.nodes}
          edges={flowData.edges}
          nodeTypes={nodeTypes}
          fitView
          onNodeClick={(_, node) => setSelectedNodeId(node.id)}
        >
          <MiniMap />
          <Controls />
          <Background />
        </ReactFlow>
      </main>

      <aside className="right-panel">
        <div className="panel-title inspector-title">
          <span>Inspector</span>
          <button
            className="inspector-toggle-btn"
            onClick={() => setInspectorExpanded((v) => !v)}
          >
            {inspectorExpanded ? "Collapse" : "Expand"}
          </button>
        </div>
        {runDetail ? (
          <>
            <div className="run-header">
              <div>
                <strong>Run:</strong> #{runDetail.run_id}
              </div>
              <div>
                <strong>Status:</strong> {runDetail.status}
              </div>
            </div>
            <div className="node-meta">
              <div>
                <strong>Node:</strong> {selectedNode?.id ?? "-"}
              </div>
              <div>
                <strong>Agent:</strong> {selectedNode?.agent ?? "-"}
              </div>
              <div>
                <strong>Reads:</strong> {(selectedNode?.reads ?? []).join(", ") || "-"}
              </div>
              <div>
                <strong>Writes:</strong> {(selectedNode?.writes ?? []).join(", ") || "-"}
              </div>
            </div>
            <div className="output-tabs">
              <button
                className={viewMode === "rendered" ? "active-tab" : ""}
                onClick={() => setViewMode("rendered")}
              >
                Rendered
              </button>
              <button
                className={viewMode === "json" ? "active-tab" : ""}
                onClick={() => setViewMode("json")}
              >
                JSON
              </button>
            </div>
            <div className="markdown-view">
              {viewMode === "rendered" ? (
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {renderNodeOutput(selectedNode)}
                </ReactMarkdown>
              ) : (
                <pre>{JSON.stringify(selectedNode?.output ?? {}, null, 2)}</pre>
              )}
            </div>
          </>
        ) : (
          <div className="empty">Select a run to view details.</div>
        )}
        {error ? <div className="error-box">{error}</div> : null}
      </aside>
    </div>
  );
}

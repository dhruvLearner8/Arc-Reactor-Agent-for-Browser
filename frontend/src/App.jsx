import { useEffect, useMemo, useRef, useState } from "react";
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
import { supabase } from "./lib/supabase";

const statusColor = {
  pending: "#9ca3af",
  running: "#f59e0b",
  completed: "#10b981",
  failed: "#ef4444",
};

const turndown = new TurndownService({ headingStyle: "atx", codeBlockStyle: "fenced" });
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/+$/, "");
const apiUrl = (path) => (API_BASE_URL ? `${API_BASE_URL}${path}` : path);
const SAMPLE_QUERIES = [
  "Plan a USA road trip from Canada with 3 friends under CAD 3,000 per person, including route, stays, activities, and budget split.",
  "What should I buy in 2026: gold or stocks? Do detailed research with macro trends, risks, scenarios, and a practical recommendation.",
];

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
  if (status === "completed") {
    if (agent.includes("formatter")) return "Final answer ready - click this node.";
    return "Completed";
  }
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

function buildFlowData(run, theme = "dark", blinkFormatter = false) {
  if (!run) return { nodes: [], edges: [] };
  const rawNodes = run.nodes ?? [];
  const rawEdges = run.links ?? [];
  const isLight = theme === "light";

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
        const isFormatter = (node.agent || "").toLowerCase().includes("formatter");
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
            background: isLight ? "#ffffff" : "#0f172a",
            color: isLight ? "#0f172a" : "#f8fafc",
            animation: blinkFormatter && isFormatter ? "formatter-blink 1s ease-in-out infinite" : undefined,
            boxShadow:
              blinkFormatter && isFormatter
                ? "0 0 0 3px rgba(56,189,248,0.45)"
                : undefined,
            borderWidth: blinkFormatter && isFormatter ? 3 : 2,
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

function estimateDepth(run) {
  if (!run) return 1;
  const nodes = Array.isArray(run.nodes) ? run.nodes : [];
  const links = Array.isArray(run.links) ? run.links : [];
  if (!nodes.length) return 1;
  const levelById = new Map();
  levelById.set("ROOT", 0);
  const sorted = links.slice();
  // Relax edges repeatedly for DAG-like graphs.
  for (let i = 0; i < nodes.length; i += 1) {
    let changed = false;
    for (const edge of sorted) {
      const src = edge?.source;
      const dst = edge?.target;
      if (!src || !dst) continue;
      const srcLevel = levelById.get(src);
      if (srcLevel == null) continue;
      const next = srcLevel + 1;
      if ((levelById.get(dst) ?? -1) < next) {
        levelById.set(dst, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  let maxLevel = 1;
  for (const node of nodes) {
    if (node?.id === "ROOT") continue;
    maxLevel = Math.max(maxLevel, levelById.get(node?.id) ?? 1);
  }
  return maxLevel;
}

function formatEta(seconds) {
  const safe = Math.max(0, Math.round(seconds || 0));
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  if (mins <= 0) return `~${secs}s`;
  if (secs === 0) return `~${mins} min`;
  return `~${mins}m ${secs}s`;
}

function estimateRemainingSeconds(run, nowMs = Date.now()) {
  if (!run) return null;
  const nodes = (run.nodes ?? []).filter((n) => n.id !== "ROOT");
  if (!nodes.length) return null;
  const byAgentBase = (agentName = "") => {
    const agent = agentName.toLowerCase();
    if (agent.includes("planner")) return 24;
    if (agent.includes("retriever")) return 72;
    if (agent.includes("thinker")) return 45;
    if (agent.includes("distiller")) return 32;
    if (agent.includes("formatter")) return 24;
    if (agent.includes("summarizer")) return 20;
    if (agent.includes("clarification")) return 65;
    return 35;
  };

  const queryWords = String(run.query || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean).length;
  const depth = estimateDepth(run);
  const sizeFactor = Math.max(0, nodes.length - 4) * 0.035;
  const queryFactor = Math.min(0.2, queryWords / 200);
  const depthFactor = Math.max(0, depth - 3) * 0.05;
  const complexityMultiplier = 1 + Math.min(0.55, sizeFactor + queryFactor + depthFactor);

  let completedActual = 0;
  let completedBaseline = 0;
  for (const node of nodes) {
    if (node.status !== "completed" || !Number.isFinite(Number(node.execution_time))) continue;
    const actual = Number(node.execution_time);
    if (actual <= 0) continue;
    completedActual += actual;
    completedBaseline += byAgentBase(node.agent) * complexityMultiplier;
  }
  const runtimeMultiplier =
    completedBaseline > 0
      ? Math.max(0.65, Math.min(1.9, completedActual / completedBaseline))
      : 1;

  let remaining = 0;
  for (const node of nodes) {
    const baseline = byAgentBase(node.agent) * complexityMultiplier * runtimeMultiplier;
    if (node.status === "completed" || node.status === "failed") continue;
    if (node.status === "running") {
      const startMs = Date.parse(String(node.start_time || ""));
      const elapsed = Number.isFinite(startMs) ? Math.max(0, (nowMs - startMs) / 1000) : 0;
      const expected = Math.max(baseline, elapsed * 1.25);
      remaining += Math.max(6, expected - elapsed);
      continue;
    }
    remaining += baseline;
  }
  return Math.max(5, remaining);
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

  const truncate = (value, max = 240) => {
    if (typeof value !== "string") return value;
    if (value.length <= max) return value;
    return `${value.slice(0, max)}...`;
  };

  const prettyValue = (value) => {
    if (value === null || value === undefined) return "";
    if (typeof value === "string") return truncate(normalizeString(value));
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    return "";
  };

  const objectToMarkdown = (obj) => {
    if (!obj || typeof obj !== "object") return "";
    const lines = [];
    const pushKV = (label, value) => {
      const text = prettyValue(value);
      if (text) lines.push(`- **${label}:** ${text}`);
    };

    // High-signal fields first.
    pushKV("Thought", obj.thought);
    pushKV("Reason", obj.reason);
    pushKV("Summary", obj.summary);
    pushKV("Next instruction", obj.next_instruction);
    pushKV("Execution status", obj.execution_status);
    pushKV("Execution error", obj.execution_error);
    pushKV("Executed variant", obj.executed_variant);
    pushKV("Clarification", obj.clarificationMessage);

    const tool = obj.call_tool;
    if (tool && typeof tool === "object") {
      const toolName = tool.name || "unknown_tool";
      lines.push(`- **Tool call:** \`${toolName}\``);
      if (tool.arguments && typeof tool.arguments === "object") {
        const argPreview = truncate(JSON.stringify(tool.arguments), 300);
        lines.push(`- **Tool args:** \`${argPreview}\``);
      }
    }

    // Add compact rendering for remaining primitive fields.
    const used = new Set([
      "thought",
      "reason",
      "summary",
      "next_instruction",
      "execution_status",
      "execution_error",
      "executed_variant",
      "clarificationMessage",
      "call_tool",
      "cost",
      "input_tokens",
      "output_tokens",
      "total_tokens",
      "call_self",
      "code_variants",
      "output",
    ]);
    Object.entries(obj).forEach(([key, value]) => {
      if (used.has(key)) return;
      if (Array.isArray(value)) {
        if (!value.length) return;
        const previewItems = value
          .slice(0, 3)
          .map((v) => prettyValue(v) || truncate(JSON.stringify(v), 80))
          .join(" | ");
        lines.push(`- **${key}:** ${previewItems}${value.length > 3 ? " | ..." : ""}`);
        return;
      }
      if (value && typeof value === "object") return;
      const text = prettyValue(value);
      if (text) lines.push(`- **${key}:** ${text}`);
    });

    if (!lines.length) {
      return `\`\`\`json\n${JSON.stringify(obj, null, 2)}\n\`\`\``;
    }
    return `### Agent Output\n\n${lines.join("\n")}`;
  };

  const output = node.output;
  if (typeof output === "string") return normalizeString(output);
  if (!output) return "No output captured for this node.";
  const best = pickBestString(output);
  if (best) return normalizeMarkdownTables(htmlToMarkdownIfNeeded(best));
  return objectToMarkdown(output);
}

export default function App() {
  const [theme, setTheme] = useState(() => {
    if (typeof window === "undefined") return "dark";
    const saved = window.localStorage.getItem("app_theme");
    return saved === "light" ? "light" : "dark";
  });
  const [session, setSession] = useState(null);
  const [authLoading, setAuthLoading] = useState(true);
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
  const [sampleQueriesOpen, setSampleQueriesOpen] = useState(false);
  const [clarificationCollapsed, setClarificationCollapsed] = useState(true);
  const [clarificationResponse, setClarificationResponse] = useState("");
  const [clarificationSubmitting, setClarificationSubmitting] = useState(false);
  const [clarificationPanelHeight, setClarificationPanelHeight] = useState(340);
  const [etaNowMs, setEtaNowMs] = useState(Date.now());
  const autoFocusedFormatterRunRef = useRef(new Set());
  const clarificationPanelRef = useRef(null);
  const accessToken = session?.access_token ?? "";

  const selectedNode = useMemo(
    () => runDetail?.nodes?.find((n) => n.id === selectedNodeId),
    [runDetail, selectedNodeId]
  );
  const selectedRunMeta = useMemo(
    () => runs.find((r) => r.run_id === selectedRunId) ?? null,
    [runs, selectedRunId]
  );

  const shouldBlinkFormatter = useMemo(
    () => runDetail?.status === "completed",
    [runDetail]
  );
  const flowData = useMemo(
    () => buildFlowData(runDetail, theme, shouldBlinkFormatter),
    [runDetail, theme, shouldBlinkFormatter]
  );
  const pendingClarification = useMemo(
    () => runDetail?.pending_clarification ?? null,
    [runDetail]
  );
  const failedNode = useMemo(
    () => (runDetail?.nodes ?? []).find((n) => n.id !== "ROOT" && n.status === "failed"),
    [runDetail]
  );
  const executionStatus = useMemo(() => {
    if (!runDetail) return { kind: "idle", text: "Waiting for run." };
    if (runDetail.status === "failed" || failedNode) {
      return {
        kind: "error",
        text: "Error occurred while execution, please do a new search.",
      };
    }
    if (runDetail.status === "completed") {
      return { kind: "completed", text: "Completed" };
    }
    return { kind: "working", text: "Working..." };
  }, [runDetail, failedNode]);
  const executionErrorText = useMemo(() => {
    if (failedNode?.error) return String(failedNode.error);
    if (runDetail?.error) return String(runDetail.error);
    return "";
  }, [failedNode, runDetail]);
  const showResearchHint = useMemo(() => {
    const status = (runDetail?.status || "").toLowerCase();
    return ["starting", "queued", "pending", "running"].includes(status);
  }, [runDetail]);
  const researchEta = useMemo(
    () => (showResearchHint ? estimateRemainingSeconds(runDetail, etaNowMs) : null),
    [runDetail, etaNowMs, showResearchHint]
  );
  const researchEtaLabel = useMemo(
    () => (researchEta ? `Estimated time: ${formatEta(researchEta)}` : "Estimated time: calculating..."),
    [researchEta]
  );
  const statusPanelTop = 46 + clarificationPanelHeight + 12;

  const getAuthHeaders = () =>
    accessToken ? { Authorization: `Bearer ${accessToken}` } : {};

  async function readErrorMessage(res, fallback) {
    const raw = await res.text();
    if (!raw) return fallback;
    try {
      const parsed = JSON.parse(raw);
      return parsed?.detail || parsed?.message || fallback;
    } catch {
      return raw;
    }
  }

  async function signOut() {
    setError("");
    const { error: signOutError } = await supabase.auth.signOut();
    if (signOutError) setError(signOutError.message);
  }

  function toggleTheme() {
    setTheme((prev) => (prev === "dark" ? "light" : "dark"));
  }

  async function fetchRuns() {
    if (!accessToken) {
      setRuns([]);
      setRunDetail(null);
      setSelectedRunId("");
      return;
    }
    setLoadingRuns(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/runs"), { headers: getAuthHeaders() });
      if (res.status === 401 || res.status === 403) {
        throw new Error("Authentication required. Please sign in again.");
      }
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to load runs");
        throw new Error(detail);
      }
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
    if (!runId || !accessToken) return;
    setError("");
    try {
      const res = await fetch(apiUrl(`/api/runs/${runId}`), { headers: getAuthHeaders() });
      if (!res.ok) {
        const detail = await readErrorMessage(res, `Failed to load run ${runId}`);
        throw new Error(detail);
      }
      const data = await res.json();
      setRunDetail(data);
      const firstNode = (data.nodes ?? []).find((n) => n.id !== "ROOT");
      const availableIds = new Set((data.nodes ?? []).map((n) => n.id));
      setSelectedNodeId((prev) => {
        if (prev && availableIds.has(prev)) return prev;
        return firstNode?.id ?? "ROOT";
      });
    } catch (e) {
      setError(String(e));
    }
  }

  async function createRun() {
    if (!accessToken) {
      setError("Please sign in with Google first.");
      return;
    }
    if (!query.trim()) return;
    setCreatingRun(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/runs"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ query: query.trim() }),
      });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Run creation failed");
        throw new Error(detail);
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

  async function submitClarification() {
    if (!pendingClarification || !selectedRunId || !accessToken) return;
    const response = clarificationResponse.trim();
    if (!response) return;
    setClarificationSubmitting(true);
    setError("");
    try {
      const res = await fetch(apiUrl(`/api/runs/${selectedRunId}/clarification`), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({
          clarification_id: pendingClarification.id,
          response,
        }),
      });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to submit clarification response");
        throw new Error(detail);
      }
      setClarificationResponse("");
    } catch (e) {
      setError(String(e));
    } finally {
      setClarificationSubmitting(false);
    }
  }

  useEffect(() => {
    let mounted = true;
    supabase.auth
      .getSession()
      .then(({ data }) => {
        if (!mounted) return;
        setSession(data.session ?? null);
      })
      .finally(() => {
        if (mounted) setAuthLoading(false);
      });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, newSession) => {
      setSession(newSession ?? null);
    });

    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (!authLoading) fetchRuns();
  }, [accessToken, authLoading]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem("app_theme", theme);
  }, [theme]);

  useEffect(() => {
    if (selectedRunId && accessToken) fetchRunDetail(selectedRunId);
  }, [selectedRunId, accessToken]);

  useEffect(() => {
    setClarificationResponse("");
  }, [selectedRunId, pendingClarification?.id]);

  useEffect(() => {
    if (!runDetail || runDetail.status !== "completed" || !runDetail.run_id) return;
    if (autoFocusedFormatterRunRef.current.has(runDetail.run_id)) return;
    const formatterNode = (runDetail.nodes ?? []).find((n) =>
      (n.agent || "").toLowerCase().includes("formatter")
    );
    const hasFormatterOutput =
      formatterNode &&
      formatterNode.output != null &&
      (typeof formatterNode.output !== "object" ||
        (formatterNode.output && Object.keys(formatterNode.output).length > 0));
    if (formatterNode?.id && hasFormatterOutput) {
      setSelectedNodeId(formatterNode.id);
      setViewMode("rendered");
      setInspectorExpanded(true);
      autoFocusedFormatterRunRef.current.add(runDetail.run_id);
    }
  }, [runDetail]);

  useEffect(() => {
    const panel = clarificationPanelRef.current;
    if (!panel) return;

    const updateHeight = () => {
      const measured = Math.ceil(panel.getBoundingClientRect().height);
      if (Number.isFinite(measured) && measured > 0) {
        setClarificationPanelHeight(measured);
      }
    };

    updateHeight();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(updateHeight);
    observer.observe(panel);
    return () => observer.disconnect();
  }, [clarificationCollapsed, pendingClarification, clarificationResponse, clarificationSubmitting]);

  useEffect(() => {
    if (!showResearchHint) return;
    const timer = window.setInterval(() => setEtaNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [showResearchHint]);

  useEffect(() => {
    const isActiveRun =
      selectedRunMeta?.status === "running" ||
      selectedRunMeta?.status === "queued" ||
      selectedRunMeta?.status === "pending";
    if (!selectedRunId || !selectedRunId.startsWith("run_") || !accessToken || !isActiveRun) {
      setIsStreaming(false);
      return;
    }
    let stream = null;
    let cancelled = false;

    const initStream = async () => {
      try {
        const ticketRes = await fetch(apiUrl(`/api/runs/${selectedRunId}/stream-ticket`), {
          method: "POST",
          headers: getAuthHeaders(),
        });
        if (ticketRes.status === 404) {
          // Historical/completed runs are not streamable; skip quietly.
          setIsStreaming(false);
          return;
        }
        if (!ticketRes.ok) {
          throw new Error("Failed to open stream (ticket denied)");
        }
        const ticketData = await ticketRes.json();
        if (cancelled) return;
        const ticketParam = encodeURIComponent(ticketData.ticket);
        stream = new EventSource(
          apiUrl(`/api/runs/${selectedRunId}/events?ticket=${ticketParam}`)
        );
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

        stream.addEventListener("run_clarification", (evt) => {
          try {
            const payload = JSON.parse(evt.data);
            if (payload?.snapshot) {
              setRunDetail(payload.snapshot);
            } else if (payload?.clarification) {
              setRunDetail((prev) => {
                if (!prev) return prev;
                return { ...prev, pending_clarification: payload.clarification };
              });
            }
          } catch {
            // no-op
          }
        });

        stream.addEventListener("run_clarification_resolved", (evt) => {
          try {
            const payload = JSON.parse(evt.data);
            if (payload?.snapshot) {
              setRunDetail(payload.snapshot);
            } else {
              setRunDetail((prev) => {
                if (!prev) return prev;
                return { ...prev, pending_clarification: null };
              });
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
          stream?.close();
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
          stream?.close();
        });

        stream.onerror = () => {
          setIsStreaming(false);
          stream?.close();
        };
      } catch (e) {
        setIsStreaming(false);
        setError(String(e));
      }
    };
    initStream();

    return () => {
      cancelled = true;
      stream?.close();
      setIsStreaming(false);
    };
  }, [selectedRunId, accessToken, selectedRunMeta?.status]);

  return (
    <div className={`app-page theme-${theme}`}>
      <header className="top-nav">
        <div className="brand-title">ArcReactor Agent</div>
        <div className="top-nav-actions">
          <button onClick={toggleTheme}>
            {theme === "dark" ? "Light Theme" : "Dark Theme"}
          </button>
          <button onClick={signOut} disabled={authLoading || !accessToken}>
            Sign Out
          </button>
        </div>
      </header>

      <div className={`app-shell ${inspectorExpanded ? "inspector-expanded" : ""}`}>
        <aside className="left-panel">
        <div className="left-panel-header">
          <h2>Runs</h2>
          <button
            className="sample-toggle-btn"
            onClick={() => setSampleQueriesOpen((v) => !v)}
            type="button"
            aria-expanded={sampleQueriesOpen}
          >
            <span className="sample-toggle-icon">?</span>
            <span>Sample Queries</span>
          </button>
        </div>
        {sampleQueriesOpen ? (
          <div className="sample-queries-popover">
            <div className="sample-queries-popover-header">
              <div className="sample-queries-title">Try one of these</div>
              <button
                className="sample-close-btn"
                onClick={() => setSampleQueriesOpen(false)}
                type="button"
              >
                Close
              </button>
            </div>
            {SAMPLE_QUERIES.map((sample) => (
              <button
                key={sample}
                className="sample-query-btn"
                onClick={() => {
                  setQuery(sample);
                  setSampleQueriesOpen(false);
                }}
                type="button"
              >
                {sample}
              </button>
            ))}
          </div>
        ) : null}
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
          {showResearchHint ? (
            <div className="research-hint-panel">
              <div className="research-hint-title">Agent doing research</div>
              <div className="research-hint-subtitle">{researchEtaLabel}</div>
            </div>
          ) : null}
          <div
            ref={clarificationPanelRef}
            className={`clarification-panel ${clarificationCollapsed ? "collapsed" : ""} ${
              pendingClarification ? "attention" : ""
            }`}
          >
            <div className="clarification-title">
              <span>Agent Clarification</span>
              <button
                className={`clarification-toggle-btn ${pendingClarification ? "attention" : ""}`}
                onClick={() => setClarificationCollapsed((v) => !v)}
              >
                {clarificationCollapsed ? "Expand" : "Collapse"}
              </button>
            </div>
            {!clarificationCollapsed ? (
              <>
                <div className="clarification-body">
                  {pendingClarification ? (
                    <>
                      <div className="clarification-scroll">
                        <div className="clarification-message">
                          {pendingClarification.message || pendingClarification.clarificationMessage}
                        </div>
                        {Array.isArray(pendingClarification.options) && pendingClarification.options.length ? (
                          <div className="clarification-options">
                            {pendingClarification.options.map((opt, idx) => (
                              <button
                                key={`${idx}-${opt}`}
                                onClick={() => setClarificationResponse(String(opt))}
                                disabled={clarificationSubmitting}
                              >
                                {String(opt)}
                              </button>
                            ))}
                          </div>
                        ) : null}
                      </div>
                      <div className="clarification-actions">
                        <textarea
                          rows={3}
                          value={clarificationResponse}
                          onChange={(e) => setClarificationResponse(e.target.value)}
                          placeholder="Type your response for the agent..."
                        />
                        <button
                          onClick={submitClarification}
                          disabled={clarificationSubmitting || !clarificationResponse.trim()}
                        >
                          {clarificationSubmitting ? "Submitting..." : "Submit Response"}
                        </button>
                      </div>
                    </>
                  ) : (
                    <div className="clarification-idle">
                      No question right now. If the ClarificationAgent needs input, it will appear here.
                    </div>
                  )}
                </div>
              </>
            ) : null}
          </div>
          <div
            className={`execution-status-panel status-${executionStatus.kind}`}
            style={{ top: `${statusPanelTop}px` }}
          >
            <div className="execution-status-title">Execution Status</div>
            <div className="execution-status-text">{executionStatus.text}</div>
            {executionStatus.kind === "error" && executionErrorText ? (
              <div className="execution-status-error">{executionErrorText}</div>
            ) : null}
          </div>
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
    </div>
  );
}

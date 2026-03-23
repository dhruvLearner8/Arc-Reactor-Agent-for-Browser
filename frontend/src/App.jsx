import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
} from "reactflow";
import DOMPurify from "dompurify";
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

function FormattedMailBody({ text }) {
  if (!text || typeof text !== "string") return null;
  const trimmed = text.trim();
  const looksLikeHtml = /^\s*</.test(trimmed) || /<html[\s>]/i.test(trimmed) || /<body[\s>]/i.test(trimmed) || /<div[\s>]/i.test(trimmed);
  if (looksLikeHtml) {
    const sanitized = DOMPurify.sanitize(text, {
      ALLOWED_TAGS: ["p", "br", "strong", "b", "em", "i", "u", "a", "ul", "ol", "li", "h1", "h2", "h3", "h4", "div", "span", "table", "tr", "td", "th", "tbody", "thead", "img"],
      ALLOWED_ATTR: ["href", "src", "alt", "title", "style", "class"],
    });
    return (
      <div
        className="formatted-mail-body mail-html-body"
        dangerouslySetInnerHTML={{ __html: sanitized }}
      />
    );
  }
  const normalized = text.replace(/\\n/g, "\n");
  const lines = normalized.split(/\r?\n/);
  const isQuotedLine = (ln) => /^[\s>]*>/.test(ln) || /^On .+ wrote:?\s*$/i.test(ln.trim());
  const blocks = [];
  let i = 0;
  while (i < lines.length) {
    const quoted = [];
    while (i < lines.length && isQuotedLine(lines[i])) {
      quoted.push(lines[i]);
      i++;
    }
    if (quoted.length) blocks.push({ type: "quote", lines: quoted });
    const plain = [];
    while (i < lines.length && !isQuotedLine(lines[i])) {
      plain.push(lines[i]);
      i++;
    }
    if (plain.length) blocks.push({ type: "plain", lines: plain });
  }
  return (
    <div className="formatted-mail-body">
      {blocks.map((block, idx) =>
        block.type === "quote" ? (
          <blockquote key={idx} className="mail-quoted-block">
            {block.lines.join("\n")}
          </blockquote>
        ) : (
          <div key={idx} className="mail-plain-block" style={{ whiteSpace: "pre-wrap" }}>
            {block.lines.join("\n")}
          </div>
        )
      )}
    </div>
  );
}

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
    const keys = Object.keys(obj);
    const metadataKeys = new Set([
      "final_format",
      "format",
      "output_format",
      "content_type",
      "mime_type",
      "language",
    ]);
    const contentEntries = Object.entries(obj).filter(
      ([key, value]) => typeof value === "string" && !metadataKeys.has(key)
    );
    const pickLongestByKeys = (entries) =>
      entries
        .slice()
        .sort((a, b) => (b[1]?.length ?? 0) - (a[1]?.length ?? 0))[0]?.[1] ?? "";

    // 1) Prefer rich formatter reports (often dynamic keys like formatted_report_*).
    const dynamicReportKey = keys.find(
      (k) => k.toLowerCase().startsWith("formatted_report") && typeof obj[k] === "string"
    );
    if (dynamicReportKey) return normalizeString(obj[dynamicReportKey]);

    // 2) Prefer richer final payloads (e.g. final_travel_dossier_T009) over short fallback summaries.
    const finalContentEntries = contentEntries.filter(([key]) => {
      const lower = key.toLowerCase();
      return lower.startsWith("final_") && lower !== "final_format";
    });
    const longestFinalContent = pickLongestByKeys(finalContentEntries);
    if (longestFinalContent) return normalizeString(longestFinalContent);

    // 3) Then try known high-signal content fields.
    const priority = ["formatted_report", "formatted_output", "final_answer", "fallback_markdown", "summary"];
    for (const key of priority) {
      if (typeof obj[key] === "string") return normalizeString(obj[key]);
    }

    // 4) Generic formatted keys (but still content-only).
    const dynamicFormatted = keys.find(
      (k) =>
        k.toLowerCase().includes("formatted") &&
        typeof obj[k] === "string" &&
        !metadataKeys.has(k)
    );
    if (dynamicFormatted) return normalizeString(obj[dynamicFormatted]);

    // 5) Final fallback: choose longest non-metadata text.
    const firstString = pickLongestByKeys(contentEntries);
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
  const [viewMode, setViewMode] = useState("preview");
  const [inspectorExpanded, setInspectorExpanded] = useState(false);
  const [sampleQueriesOpen, setSampleQueriesOpen] = useState(false);
  const [clarificationCollapsed, setClarificationCollapsed] = useState(true);
  const [clarificationResponse, setClarificationResponse] = useState("");
  const [clarificationSubmitting, setClarificationSubmitting] = useState(false);
  const [clarificationPanelHeight, setClarificationPanelHeight] = useState(340);
  const [formatterFollowupPrompt, setFormatterFollowupPrompt] = useState("");
  const [formatterFollowupSubmitting, setFormatterFollowupSubmitting] = useState(false);
  const [etaNowMs, setEtaNowMs] = useState(Date.now());
  const [activeSection, setActiveSection] = useState("run");
  const [notes, setNotes] = useState([]);
  const [notesLoading, setNotesLoading] = useState(false);
  const [savingNote, setSavingNote] = useState(false);
  const [creatingNodeType, setCreatingNodeType] = useState("");
  const [creatingNodeName, setCreatingNodeName] = useState("");
  const [creatingParentId, setCreatingParentId] = useState(null);
  const [selectedNoteId, setSelectedNoteId] = useState("");
  const [noteEditorContent, setNoteEditorContent] = useState("");
  const [noteSaving, setNoteSaving] = useState(false);
  const [openNoteTabs, setOpenNoteTabs] = useState([]);
  const [activeNoteTabId, setActiveNoteTabId] = useState("");
  const [expandedFolders, setExpandedFolders] = useState({});
  const [editorFont, setEditorFont] = useState("Consolas");
  const [editorFontSize, setEditorFontSize] = useState("14");
  const [mailThreads, setMailThreads] = useState([]);
  const [selectedThreadId, setSelectedThreadId] = useState("");
  const [mailReplyBody, setMailReplyBody] = useState("");
  const [mailLoading, setMailLoading] = useState(false);
  const [mailSending, setMailSending] = useState(false);
  const [mailDraftLoading, setMailDraftLoading] = useState(false);
  const [mailFilter, setMailFilter] = useState("all"); // "all" | "priority"
  const [mailError, setMailError] = useState("");
  const [mailCurrentPage, setMailCurrentPage] = useState(1);
  const [mailPageTokens, setMailPageTokens] = useState({}); // { 2: token, 3: token, ... } token to fetch that page
  const [mailNextPageToken, setMailNextPageToken] = useState(null); // from last response, for Next
  const noteEditorRef = useRef(null);
  const lastSavedContentByIdRef = useRef({});
  const inlineCreateSubmittingRef = useRef(false);
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
  const selectedNote = useMemo(
    () => notes.find((n) => n.id === activeNoteTabId) ?? null,
    [notes, activeNoteTabId]
  );
  const selectedTreeItem = useMemo(
    () => notes.find((n) => n.id === selectedNoteId) ?? null,
    [notes, selectedNoteId]
  );
  const treeChildren = useMemo(() => {
    const bucket = {};
    notes.forEach((item) => {
      const parent = item.parent_id || "__root__";
      if (!bucket[parent]) bucket[parent] = [];
      bucket[parent].push(item);
    });
    Object.keys(bucket).forEach((key) => {
      bucket[key].sort((a, b) => {
        const aCreated = String(a.created_at || "");
        const bCreated = String(b.created_at || "");
        if (aCreated && bCreated && aCreated !== bCreated) return aCreated.localeCompare(bCreated);
        return String(a.id || "").localeCompare(String(b.id || ""));
      });
    });
    return bucket;
  }, [notes]);

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

  async function submitFormatterFollowup() {
    if (!selectedRunId || !accessToken || !formatterFollowupPrompt.trim()) return;
    setFormatterFollowupSubmitting(true);
    setError("");
    try {
      const res = await fetch(apiUrl(`/api/runs/${selectedRunId}/formatter-followup`), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ prompt: formatterFollowupPrompt.trim() }),
      });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to submit formatter follow-up");
        throw new Error(detail);
      }
      const data = await res.json();
      setFormatterFollowupPrompt("");
      await fetchRuns();
      if (data?.run_id && String(data.run_id).startsWith("run_")) {
        if (data.run_id === selectedRunId) {
          await fetchRunDetail(selectedRunId);
        } else {
          setSelectedRunId(data.run_id);
        }
      } else {
        await fetchRunDetail(selectedRunId);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setFormatterFollowupSubmitting(false);
    }
  }

  async function fetchNotes() {
    if (!accessToken) {
      setNotes([]);
      return;
    }
    setNotesLoading(true);
    try {
      const res = await fetch(apiUrl("/api/notes"), { headers: getAuthHeaders() });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to load notes");
        throw new Error(detail);
      }
      const data = await res.json();
      const incoming = (Array.isArray(data) ? data : []).map((item) => {
        const rawContent = item.content || "";
        const safeId = String(item.id || "").replace(/^note_/, "").slice(0, 8) || "file";
        const rawName = String(item.name || "").trim();
        const normalizedName = isLikelyBadLegacyName(rawName)
          ? `untitled-${safeId}.md`
          : (rawName.toLowerCase().endsWith(".md") ? rawName : `${rawName}.md`);
        return {
          ...item,
          kind: item.kind === "folder" ? "folder" : "file",
          name: item.kind === "folder"
            ? (isLikelyBadLegacyName(rawName) ? `folder-${safeId}` : rawName)
            : normalizedName,
          content: typeof rawContent === "string" ? rawContent : String(rawContent || ""),
          parent_id: item.parent_id || null,
        };
      });
      setNotes(incoming);
      const snapshot = {};
      incoming.forEach((item) => {
        if (item.kind !== "folder") snapshot[item.id] = String(item.content || "");
      });
      lastSavedContentByIdRef.current = snapshot;
      if (!selectedNoteId && incoming.length) {
        setSelectedNoteId(incoming[0].id);
      }
    } catch (e) {
      setError(String(e));
    } finally {
      setNotesLoading(false);
    }
  }

  function openFileTab(noteId) {
    const note = notes.find((n) => n.id === noteId);
    if (!note || note.kind === "folder") return;
    setOpenNoteTabs((prev) => (prev.includes(noteId) ? prev : [...prev, noteId]));
    setActiveNoteTabId(noteId);
    setSelectedNoteId(noteId);
  }

  function normalizeMarkdownName(rawName) {
    const base = String(rawName || "").trim().replace(/[\\/:*?"<>|]+/g, "");
    if (!base) return "";
    return base.toLowerCase().endsWith(".md") ? base : `${base}.md`;
  }

  function isLikelyBadLegacyName(rawName) {
    const name = String(rawName || "").trim();
    if (!name) return true;
    const hasExt = /\.[a-z0-9]{1,8}$/i.test(name);
    const spaces = (name.match(/\s/g) || []).length;
    if (name.length > 64) return true;
    if (!hasExt && spaces >= 4) return true;
    return false;
  }

  async function createNote(rawName = "") {
    const name = normalizeMarkdownName(rawName);
    if (!name || !accessToken) return null;
    setSavingNote(true);
    try {
      const parentId =
        creatingParentId ??
        (selectedTreeItem?.kind === "folder" ? selectedTreeItem.id : selectedTreeItem?.parent_id || null);
      const res = await fetch(apiUrl("/api/notes"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ kind: "file", name, parent_id: parentId, content: "" }),
      });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to create file");
        throw new Error(detail);
      }
      const created = await res.json();
      setNotes((prev) => [...prev, created]);
      setSelectedNoteId(created.id);
      setOpenNoteTabs((prev) => (prev.includes(created.id) ? prev : [...prev, created.id]));
      setActiveNoteTabId(created.id);
      setNoteEditorContent(created.content || "");
      lastSavedContentByIdRef.current[created.id] = String(created.content || "");
      return created;
    } catch (e) {
      setError(String(e));
      return null;
    } finally {
      setSavingNote(false);
    }
  }

  async function createFolder(rawName = "") {
    const name = String(rawName || "").trim().replace(/[\\/:*?"<>|]+/g, "");
    if (!name || !accessToken) return null;
    setSavingNote(true);
    try {
      const parentId =
        creatingParentId ??
        (selectedTreeItem?.kind === "folder" ? selectedTreeItem.id : selectedTreeItem?.parent_id || null);
      const res = await fetch(apiUrl("/api/notes"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ kind: "folder", name, parent_id: parentId }),
      });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to create folder");
        throw new Error(detail);
      }
      const created = await res.json();
      setNotes((prev) => [...prev, created]);
      setSelectedNoteId(created.id);
      setExpandedFolders((prev) => ({ ...prev, [created.id]: true }));
      return created;
    } catch (e) {
      setError(String(e));
      return null;
    } finally {
      setSavingNote(false);
    }
  }

  function beginInlineCreate(type) {
    const parentId =
      selectedTreeItem?.kind === "folder" ? selectedTreeItem.id : selectedTreeItem?.parent_id || null;
    setCreatingNodeType(type);
    setCreatingNodeName("");
    setCreatingParentId(parentId);
    if (parentId) {
      setExpandedFolders((prev) => ({ ...prev, [parentId]: true }));
    }
  }

  async function submitInlineCreate() {
    const value = creatingNodeName.trim();
    if (!value || !creatingNodeType || inlineCreateSubmittingRef.current) return;
    inlineCreateSubmittingRef.current = true;
    const created =
      creatingNodeType === "folder" ? await createFolder(value) : await createNote(value);
    if (created) {
      setCreatingNodeType("");
      setCreatingNodeName("");
      setCreatingParentId(null);
    }
    inlineCreateSubmittingRef.current = false;
  }

  function cancelInlineCreate() {
    setCreatingNodeType("");
    setCreatingNodeName("");
    setCreatingParentId(null);
  }

  async function saveSelectedNote() {
    const noteId = activeNoteTabId;
    if (!noteId || !accessToken) return false;
    const contentToSave = String(noteEditorContent || "");
    if (lastSavedContentByIdRef.current[noteId] === contentToSave) return true;
    setNoteSaving(true);
    try {
      const res = await fetch(apiUrl(`/api/notes/${noteId}`), {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ content: contentToSave }),
      });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to update note");
        throw new Error(detail);
      }
      const updated = await res.json();
      setNotes((prev) => prev.map((n) => (n.id === noteId ? { ...n, ...updated, content: contentToSave } : n)));
      lastSavedContentByIdRef.current[noteId] = contentToSave;
      return true;
    } catch (e) {
      setError(String(e));
      return false;
    } finally {
      setNoteSaving(false);
    }
  }

  async function fetchMailMessages(pageToken = null, pageNum = 1) {
    if (!accessToken) return;
    setMailLoading(true);
    setMailError("");
    try {
      const q = mailFilter === "priority" ? "is:important" : "";
      let url = `/api/mail/messages?max_results=20${q ? `&q=${encodeURIComponent(q)}` : ""}`;
      if (pageToken) url += `&page_token=${encodeURIComponent(pageToken)}`;
      const res = await fetch(apiUrl(url), { headers: getAuthHeaders() });
      if (res.status === 503) {
        const detail = await readErrorMessage(res, "Gmail not configured");
        setMailError(detail);
        setMailMessages([]);
        return;
      }
      if (!res.ok) throw new Error(await readErrorMessage(res, "Failed to load mail"));
      const data = await res.json();
      const arr = Array.isArray(data?.threads) ? data.threads : [];
      const nextTok = data?.nextPageToken ?? null;
      setMailThreads(arr);
      setMailCurrentPage(pageNum);
      setMailNextPageToken(nextTok);
      if (nextTok) {
        setMailPageTokens((prev) => ({ ...prev, [pageNum + 1]: nextTok }));
      }
      const ids = new Set(arr.map((t) => t.id));
      setSelectedThreadId((prev) => {
        if (ids.has(prev)) return prev;
        return arr[0]?.id ?? "";
      });
    } catch (e) {
      setMailError(String(e));
      setMailThreads([]);
    } finally {
      setMailLoading(false);
    }
  }

  function goToMailPage(dir) {
    if (dir === "next" && mailNextPageToken) {
      const token = mailPageTokens[mailCurrentPage + 1] ?? mailNextPageToken;
      fetchMailMessages(token, mailCurrentPage + 1);
    } else if (dir === "prev" && mailCurrentPage > 1) {
      const token = mailCurrentPage === 2 ? null : mailPageTokens[mailCurrentPage - 1];
      fetchMailMessages(token || null, mailCurrentPage - 1);
    }
  }


  async function sendMailReply() {
    if (!replyToMessageId || !mailReplyBody.trim() || !accessToken) return;
    setMailSending(true);
    setMailError("");
    try {
      const res = await fetch(apiUrl("/api/mail/reply"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ message_id: replyToMessageId, body: mailReplyBody.trim() }),
      });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Failed to send reply"));
      setMailReplyBody("");
      await fetchMailMessages();
    } catch (e) {
      setMailError(String(e));
    } finally {
      setMailSending(false);
    }
  }

  async function draftMailWithAi() {
    if (!replyToMessageId || !accessToken) return;
    setMailDraftLoading(true);
    setMailError("");
    try {
      const res = await fetch(apiUrl("/api/mail/draft-with-ai"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeaders() },
        body: JSON.stringify({ message_id: replyToMessageId }),
      });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Failed to generate draft"));
      const data = await res.json();
      setMailReplyBody(data?.draft ?? "");
    } catch (e) {
      setMailError(String(e));
    } finally {
      setMailDraftLoading(false);
    }
  }

  async function removeNote(noteId) {
    if (!accessToken) return;
    try {
      const res = await fetch(apiUrl(`/api/notes/${noteId}`), {
        method: "DELETE",
        headers: getAuthHeaders(),
      });
      if (!res.ok) {
        const detail = await readErrorMessage(res, "Failed to delete note");
        throw new Error(detail);
      }
      setNotes((prev) => {
        const deleted = new Set([noteId]);
        let changed = true;
        while (changed) {
          changed = false;
          prev.forEach((item) => {
            if (!deleted.has(item.id) && deleted.has(item.parent_id)) {
              deleted.add(item.id);
              changed = true;
            }
          });
        }
        const remaining = prev.filter((n) => !deleted.has(n.id));
        deleted.forEach((id) => delete lastSavedContentByIdRef.current[id]);
        setOpenNoteTabs((tabs) => tabs.filter((id) => !deleted.has(id)));
        if (deleted.has(activeNoteTabId)) {
          const nextFile = remaining.find((n) => n.kind !== "folder");
          setActiveNoteTabId(nextFile?.id || "");
          setNoteEditorContent(nextFile?.content || "");
        }
        if (deleted.has(selectedNoteId)) {
          setSelectedNoteId(remaining[0]?.id || "");
        }
        return remaining;
      });
    } catch (e) {
      setError(String(e));
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
    if (!authLoading) fetchNotes();
  }, [accessToken, authLoading]);

  useEffect(() => {
    if (activeSection === "mail" && accessToken) {
      setMailPageTokens({});
      setMailCurrentPage(1);
      fetchMailMessages(null, 1);
    }
  }, [activeSection, accessToken, mailFilter]);


  const lastEditorTabIdRef = useRef("");
  useEffect(() => {
    if (!activeNoteTabId) {
      setNoteEditorContent("");
      lastEditorTabIdRef.current = "";
      return;
    }
    const note = notes.find((n) => n.id === activeNoteTabId && n.kind !== "folder");
    if (!note) return;
    if (lastEditorTabIdRef.current === activeNoteTabId) {
      return;
    }
    lastEditorTabIdRef.current = activeNoteTabId;
    const html = String(note.content || "");
    setNoteEditorContent(html);
    lastSavedContentByIdRef.current[activeNoteTabId] = html;
  }, [activeNoteTabId, notes]);

  useEffect(() => {
    if (!accessToken || !activeNoteTabId || !selectedNote || selectedNote.kind === "folder") return;
    const current = String(noteEditorContent || "");
    const saved = lastSavedContentByIdRef.current[activeNoteTabId];
    if (saved === undefined) {
      lastSavedContentByIdRef.current[activeNoteTabId] = String(selectedNote.content || "");
      return;
    }
    if (saved === current) return;
    const timer = window.setTimeout(() => {
      saveSelectedNote();
    }, 700);
    return () => window.clearTimeout(timer);
  }, [accessToken, activeNoteTabId, noteEditorContent, selectedNote]);

  useEffect(() => {
    const fileIds = new Set(notes.filter((n) => n.kind !== "folder").map((n) => n.id));
    setOpenNoteTabs((prev) => prev.filter((id) => fileIds.has(id)));
    if (activeNoteTabId && fileIds.has(activeNoteTabId)) return;
    const firstOpen = openNoteTabs.find((id) => fileIds.has(id));
    if (firstOpen) {
      setActiveNoteTabId(firstOpen);
      return;
    }
    const firstFile = notes.find((n) => n.kind !== "folder");
    setActiveNoteTabId(firstFile?.id || "");
  }, [notes, activeNoteTabId, openNoteTabs]);

  function mailFromLabel(fromStr) {
    if (!fromStr) return "(unknown)";
    const match = fromStr.match(/^([^<]+)<[^>]+>$/);
    return match ? match[1].trim() : fromStr;
  }

  function mailDateLabel(dateStr) {
    if (!dateStr) return "";
    try {
      const d = new Date(dateStr);
      const now = new Date();
      const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const diffDays = Math.floor((today - new Date(d.getFullYear(), d.getMonth(), d.getDate())) / 86400000);
      if (diffDays === 0) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      if (diffDays === 1) return "Yesterday";
      if (diffDays < 7) return d.toLocaleDateString([], { weekday: "short" });
      return d.toLocaleDateString([], { month: "short", day: "numeric" });
    } catch {
      return dateStr;
    }
  }

  const noteLabel = (note) => {
    const safeId = String(note?.id || "").replace(/^note_/, "").slice(0, 8) || "file";
    const rawName = String(note?.name || "").trim().replace(/[\\/:*?"<>|]+/g, "");
    if ((note?.kind || "file") === "folder") {
      return isLikelyBadLegacyName(rawName) ? `folder-${safeId}` : rawName;
    }
    if (!rawName || isLikelyBadLegacyName(rawName)) return `untitled-${safeId}.md`;
    return rawName.toLowerCase().endsWith(".md") ? rawName : `${rawName}.md`;
  };
  const noteStats = useMemo(() => {
    const text = String(noteEditorContent || "").replace(/<[^>]*>/g, " ");
    const words = text.trim() ? text.trim().split(/\s+/).length : 0;
    return { chars: text.length, words };
  }, [noteEditorContent]);
  const openTabNotes = useMemo(
    () => openNoteTabs.map((id) => notes.find((n) => n.id === id)).filter(Boolean),
    [openNoteTabs, notes]
  );

  const selectedThread = useMemo(
    () => mailThreads.find((t) => t.id === selectedThreadId) ?? null,
    [mailThreads, selectedThreadId]
  );
  const replyToMessageId = useMemo(
    () => {
      const msgs = selectedThread?.messages ?? [];
      return msgs.length > 0 ? msgs[msgs.length - 1].id : "";
    },
    [selectedThread]
  );

  const lastDomSyncedTabRef = useRef("");
  useLayoutEffect(() => {
    const el = noteEditorRef.current;
    if (!el) return;
    if (!activeNoteTabId) {
      el.innerHTML = "";
      lastDomSyncedTabRef.current = "";
      return;
    }
    const note = notes.find((n) => n.id === activeNoteTabId && n.kind !== "folder");
    if (!note) return;
    if (lastDomSyncedTabRef.current === activeNoteTabId) {
      return;
    }
    lastDomSyncedTabRef.current = activeNoteTabId;
    el.innerHTML = String(note.content || "");
  }, [activeNoteTabId, notes]);

  function closeTab(noteId) {
    setOpenNoteTabs((prev) => {
      const remaining = prev.filter((id) => id !== noteId);
      if (activeNoteTabId === noteId) {
        const nextId = remaining[remaining.length - 1] || "";
        setActiveNoteTabId(nextId);
        const nextNote = notes.find((n) => n.id === nextId);
        setNoteEditorContent(nextNote?.content || "");
      }
      return remaining;
    });
  }

  function toggleFolder(folderId) {
    setExpandedFolders((prev) => ({ ...prev, [folderId]: !prev[folderId] }));
  }

  function execEditorCommand(command, value = null) {
    if (!noteEditorRef.current || !activeNoteTabId) return;
    noteEditorRef.current.focus();
    document.execCommand(command, false, value);
    setNoteEditorContent(noteEditorRef.current.innerHTML);
  }

  function renderTreeNodes(parentId = null, depth = 0) {
    const list = treeChildren[parentId || "__root__"] || [];
    return list.map((item) => {
      const isFolder = item.kind === "folder";
      const isExpanded = expandedFolders[item.id] ?? true;
      return (
        <div key={item.id}>
          <button
            className={`note-tree-item ${selectedNoteId === item.id ? "active" : ""}`}
            style={{ paddingLeft: `${8 + depth * 16}px` }}
            onClick={() => {
              setSelectedNoteId(item.id);
              if (isFolder) {
                toggleFolder(item.id);
              } else {
                openFileTab(item.id);
              }
            }}
          >
            <span className="note-tree-icon">{isFolder ? (isExpanded ? "v" : ">") : "-"}</span>
            <span className="note-tree-name">{noteLabel(item)}</span>
          </button>
          {isFolder && isExpanded ? renderTreeNodes(item.id, depth + 1) : null}
        </div>
      );
    });
  }

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
      setViewMode("preview");
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
      !selectedRunMeta ||
      selectedRunMeta?.status === "starting" ||
      selectedRunMeta?.status === "running" ||
      selectedRunMeta?.status === "queued" ||
      selectedRunMeta?.status === "pending";
    if (!selectedRunId || !selectedRunId.startsWith("run_") || !accessToken || !isActiveRun) {
      setIsStreaming(false);
      return;
    }
    let stream = null;
    let cancelled = false;
    let pollTimer = null;
    let isOpeningStream = false;

    const clearPolling = () => {
      if (pollTimer != null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const startPolling = () => {
      if (pollTimer != null) return;
      pollTimer = window.setInterval(() => {
        fetchRunDetail(selectedRunId);
      }, 1500);
    };

    const sleep = (ms) => new Promise((resolve) => window.setTimeout(resolve, ms));

    const initStream = async () => {
      if (isOpeningStream) return;
      isOpeningStream = true;
      try {
        let ticketData = null;
        let ticketError = null;
        // Retry ticket acquisition for active runs to survive short race windows.
        for (let attempt = 0; attempt < 8; attempt += 1) {
          if (cancelled) return;
          const ticketRes = await fetch(apiUrl(`/api/runs/${selectedRunId}/stream-ticket`), {
            method: "POST",
            headers: getAuthHeaders(),
          });
          if (ticketRes.ok) {
            ticketData = await ticketRes.json();
            break;
          }
          if (ticketRes.status !== 404) {
            const detail = await readErrorMessage(ticketRes, "Failed to open stream (ticket denied)");
            ticketError = new Error(detail);
            break;
          }
          // 404 may be temporary while run state settles; retry briefly.
          await sleep(400);
        }
        if (!ticketData) {
          if (ticketError) throw ticketError;
          // Fall back to detail polling when SSE cannot attach.
          setIsStreaming(false);
          startPolling();
          return;
        }
        if (cancelled) return;
        clearPolling();
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
          fetchRunDetail(selectedRunId);
          startPolling();
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
          fetchRunDetail(selectedRunId);
          startPolling();
          stream?.close();
        });

        stream.onerror = () => {
          setIsStreaming(false);
          fetchRunDetail(selectedRunId);
          startPolling();
          stream?.close();
        };
      } catch (e) {
        setIsStreaming(false);
        startPolling();
        setError(String(e));
      } finally {
        isOpeningStream = false;
      }
    };
    initStream();

    return () => {
      cancelled = true;
      stream?.close();
      clearPolling();
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

      <div className={`app-shell ${inspectorExpanded ? "inspector-expanded" : ""} ${activeSection === "notes" ? "notes-mode" : ""} ${activeSection === "mail" ? "mail-mode" : ""}`}>
        <aside className="left-nav-rail">
          <button
            className={`rail-icon-btn ${activeSection === "run" ? "active" : ""}`}
            onClick={() => setActiveSection("run")}
            title="Agent Run"
          >
            ▶
          </button>
          <button
            className={`rail-icon-btn ${activeSection === "notes" ? "active" : ""}`}
            onClick={() => setActiveSection("notes")}
            title="Notepad"
          >
            📝
          </button>
          <button
            className={`rail-icon-btn ${activeSection === "mail" ? "active" : ""}`}
            onClick={() => setActiveSection("mail")}
            title="Mail"
          >
            ✉
          </button>
        </aside>
        {activeSection === "run" ? (
        <>
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
              {viewMode !== "preview" ? (
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
                </>
              ) : null}
              <div className="output-tabs">
                <button
                  className={viewMode === "preview" ? "active-tab" : ""}
                  onClick={() => setViewMode("preview")}
                >
                  Preview
                </button>
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
                {viewMode === "json" ? (
                  <pre>{JSON.stringify(selectedNode?.output ?? {}, null, 2)}</pre>
                ) : (
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>
                    {renderNodeOutput(selectedNode)}
                  </ReactMarkdown>
                )}
              </div>
              {(selectedNode?.agent || "").toLowerCase().includes("formatter") ? (
                <div className="clarification-actions">
                  <textarea
                    rows={3}
                    value={formatterFollowupPrompt}
                    onChange={(e) => setFormatterFollowupPrompt(e.target.value)}
                    placeholder="Ask more about this formatter answer..."
                  />
                  <button
                    onClick={submitFormatterFollowup}
                    disabled={formatterFollowupSubmitting || !formatterFollowupPrompt.trim()}
                  >
                    {formatterFollowupSubmitting ? "Submitting..." : "Ask More"}
                  </button>
                </div>
              ) : null}
            </>
          ) : (
            <div className="empty">Select a run to view details.</div>
          )}
          {error ? <div className="error-box">{error}</div> : null}
        </aside>
        </>
        ) : activeSection === "mail" ? (
          <main className="mail-panel-full">
            <div className="mail-panel-header-bar">
              <span className="mail-panel-title">Mail</span>
              <div className="mail-filter-tabs">
                <button
                  className={`mail-filter-tab ${mailFilter === "all" ? "active" : ""}`}
                  onClick={() => setMailFilter("all")}
                  type="button"
                >
                  All
                </button>
                <button
                  className={`mail-filter-tab ${mailFilter === "priority" ? "active" : ""}`}
                  onClick={() => setMailFilter("priority")}
                  type="button"
                >
                  Priority
                </button>
              </div>
              <div className="mail-pagination">
                <button
                  className="mail-page-btn"
                  onClick={() => goToMailPage("prev")}
                  disabled={mailLoading || mailCurrentPage <= 1}
                  type="button"
                >
                  ‹ Prev
                </button>
                <span className="mail-page-num">Page {mailCurrentPage}</span>
                <button
                  className="mail-page-btn"
                  onClick={() => goToMailPage("next")}
                  disabled={mailLoading || !mailNextPageToken}
                  type="button"
                >
                  Next ›
                </button>
              </div>
              <button
                className="mail-refresh-btn"
                onClick={() => fetchMailMessages(mailCurrentPage === 1 ? null : mailPageTokens[mailCurrentPage], mailCurrentPage)}
                disabled={mailLoading}
                type="button"
              >
                {mailLoading ? "Loading..." : "Refresh"}
              </button>
            </div>
            <div className="mail-panel-content">
              {mailError ? <div className="mail-error">{mailError}</div> : null}
              <div className="mail-layout">
                <div className="mail-list-wrapper">
                  <div className="mail-list">
                    {mailLoading ? (
                      <>
                        {[1, 2, 3, 4, 5].map((i) => (
                          <div key={i} className="mail-list-item mail-skeleton">
                            <div className="mail-skeleton-line short" />
                            <div className="mail-skeleton-line" />
                            <div className="mail-skeleton-line long" />
                          </div>
                        ))}
                      </>
                    ) : (
                    <>
                      {mailThreads.map((thread) => {
                        const latest = thread.messages?.[thread.messages.length - 1];
                        const m = latest || {};
                        return (
                          <button
                            key={thread.id}
                            className={`mail-list-item ${selectedThreadId === thread.id ? "active" : ""}`}
                            onClick={() => setSelectedThreadId(thread.id)}
                            type="button"
                          >
                            <div className="mail-item-row">
                              <span className="mail-item-from">{mailFromLabel(m.from)}</span>
                              <span className="mail-item-date">{mailDateLabel(m.date)}</span>
                            </div>
                            <span className="mail-item-subject">{m.subject || "(no subject)"}</span>
                            <span className="mail-item-snippet">{thread.snippet || m.snippet || ""}</span>
                            {thread.messages?.length > 1 ? (
                              <span className="mail-item-thread-count">{thread.messages.length} messages</span>
                            ) : null}
                          </button>
                        );
                      })}
                      {mailThreads.length === 0 && !mailError ? (
                          <div className="mail-empty">
                            {mailFilter === "priority" ? "No priority emails." : "No messages."}
                          </div>
                        ) : null}
                      </>
                    )}
                  </div>
                  {!mailLoading && (mailCurrentPage > 1 || mailNextPageToken) ? (
                    <div className="mail-list-pagination">
                      <button
                        className="mail-list-page-btn"
                        onClick={() => goToMailPage("prev")}
                        disabled={mailCurrentPage <= 1}
                        type="button"
                      >
                        ‹ Previous page
                      </button>
                      <span className="mail-list-page-info">Page {mailCurrentPage}</span>
                      <button
                        className="mail-list-page-btn"
                        onClick={() => goToMailPage("next")}
                        disabled={!mailNextPageToken}
                        type="button"
                      >
                        Next page ›
                      </button>
                    </div>
                  ) : null}
                </div>
                <div className="mail-detail-area">
                  {selectedThread ? (
                    <>
                      <div className="mail-thread-header">
                        <div className="mail-detail-subject">{selectedThread.messages?.[0]?.subject || "(no subject)"}</div>
                      </div>
                      <div className="mail-thread-messages">
                        {(selectedThread.messages || []).map((msg) => (
                          <div key={msg.id} className="mail-thread-message">
                            <div className="mail-detail-from">
                              <span className="mail-detail-avatar">{mailFromLabel(msg.from).charAt(0).toUpperCase()}</span>
                              <div>
                                <div className="mail-detail-from-name">{mailFromLabel(msg.from)}</div>
                                <div className="mail-detail-date">{msg.date}</div>
                              </div>
                            </div>
                            <div className="mail-detail-body mail-body-formatted">
                              {msg.body ? (
                                <FormattedMailBody text={msg.body} />
                              ) : (
                                <span>{msg.snippet || "(no body)"}</span>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                      <div className="mail-reply-box">
                        <textarea
                          rows={4}
                          value={mailReplyBody}
                          onChange={(e) => setMailReplyBody(e.target.value)}
                          placeholder="Type your reply..."
                          className="mail-reply-textarea"
                        />
                        <div className="mail-reply-actions">
                          <button
                            onClick={draftMailWithAi}
                            disabled={mailDraftLoading}
                            type="button"
                          >
                            {mailDraftLoading ? "Generating..." : "Write by AI"}
                          </button>
                          <button
                            onClick={sendMailReply}
                            disabled={mailSending || !mailReplyBody.trim()}
                            type="button"
                          >
                            {mailSending ? "Sending..." : "Send"}
                          </button>
                        </div>
                      </div>
                    </>
                    ) : (
                    <div className="mail-empty mail-empty-detail">Select a thread to view and reply.</div>
                  )}
                </div>
              </div>
            </div>
          </main>
        ) : (
          <main className="notes-panel">
            <div className="notes-menubar">
              <span>File</span>
              <span>Edit</span>
              <span>Selection</span>
              <span>View</span>
              <span>Help</span>
            </div>
            <div className="notes-layout">
              <aside className="notes-sidebar">
                <div className="notes-sidebar-title">
                  <span>Files</span>
                  <div className="notes-sidebar-icons">
                    <button
                      className="icon-btn"
                      title="New file"
                      onClick={() => beginInlineCreate("file")}
                      disabled={savingNote}
                    >
                      <svg className="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M8 4h6l4 4v12H8z" fill="none" stroke="currentColor" strokeWidth="1.8" />
                        <path d="M14 4v4h4" fill="none" stroke="currentColor" strokeWidth="1.8" />
                        <path d="M12 13v6M9 16h6" fill="none" stroke="currentColor" strokeWidth="1.8" />
                      </svg>
                    </button>
                    <button
                      className="icon-btn"
                      title="New folder"
                      onClick={() => beginInlineCreate("folder")}
                      disabled={savingNote}
                    >
                      <svg className="icon-svg" viewBox="0 0 24 24" aria-hidden="true">
                        <path d="M3 8h7l2 2h9v10H3z" fill="none" stroke="currentColor" strokeWidth="1.8" />
                        <path d="M12 12v6M9 15h6" fill="none" stroke="currentColor" strokeWidth="1.8" />
                      </svg>
                    </button>
                  </div>
                </div>
                <div className="notes-tree">
                  {notesLoading ? <div className="empty">Loading files...</div> : null}
                  {!notesLoading && notes.length === 0 ? <div className="empty">Create your first file.</div> : null}
                  {creatingNodeType ? (
                    <div className="inline-create-row">
                      <span className="note-tree-icon">{creatingNodeType === "folder" ? ">" : "-"}</span>
                      <input
                        autoFocus
                        value={creatingNodeName}
                        onChange={(e) => setCreatingNodeName(e.target.value)}
                        placeholder={creatingNodeType === "folder" ? "folder name" : "file name (.md auto)"}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") submitInlineCreate();
                          if (e.key === "Escape") cancelInlineCreate();
                        }}
                        onBlur={() => {
                          if (creatingNodeName.trim()) {
                            submitInlineCreate();
                          } else {
                            cancelInlineCreate();
                          }
                        }}
                      />
                    </div>
                  ) : null}
                  {!notesLoading ? renderTreeNodes() : null}
                </div>
              </aside>
              <section className="note-editor-pane">
                <div className="notes-toolbar editor-tools">
                  <select value={editorFont} onChange={(e) => setEditorFont(e.target.value)}>
                    <option value="Consolas">Consolas</option>
                    <option value="Arial">Arial</option>
                    <option value="Georgia">Georgia</option>
                  </select>
                  <select value={editorFontSize} onChange={(e) => setEditorFontSize(e.target.value)}>
                    <option value="12">12</option>
                    <option value="14">14</option>
                    <option value="16">16</option>
                    <option value="18">18</option>
                  </select>
                  <button onClick={() => execEditorCommand("bold")} disabled={!activeNoteTabId}>B</button>
                  <button onClick={() => execEditorCommand("italic")} disabled={!activeNoteTabId}>I</button>
                  <button onClick={() => execEditorCommand("underline")} disabled={!activeNoteTabId}>U</button>
                  <input
                    type="color"
                    className="color-input"
                    onChange={(e) => execEditorCommand("foreColor", e.target.value)}
                    disabled={!activeNoteTabId}
                  />
                  <button
                    onClick={() => {
                      const href = window.prompt("Enter URL");
                      if (href) execEditorCommand("createLink", href);
                    }}
                    disabled={!activeNoteTabId}
                  >
                    Link
                  </button>
                  <button onClick={() => activeNoteTabId && removeNote(activeNoteTabId)} disabled={!activeNoteTabId}>
                    Delete
                  </button>
                </div>
                <div className="notes-tabs">
                  {openTabNotes.map((tab) => (
                    <button
                      key={tab.id}
                      className={`note-tab ${activeNoteTabId === tab.id ? "active" : ""}`}
                      onClick={() => {
                        setActiveNoteTabId(tab.id);
                        setSelectedNoteId(tab.id);
                      }}
                    >
                      <span>{noteLabel(tab)}</span>
                      <span
                        className="tab-close"
                        onClick={(e) => {
                          e.stopPropagation();
                          closeTab(tab.id);
                        }}
                      >
                        x
                      </span>
                    </button>
                  ))}
                </div>
                {selectedNote ? (
                  <>
                    <div className="note-editor-header">
                      <div className="note-filename">{noteLabel(selectedNote)}</div>
                      <div className="note-filemeta">
                        {noteSaving
                          ? "Auto-saving..."
                          : new Date(selectedNote.updated_at || selectedNote.created_at).toLocaleString()}
                      </div>
                    </div>
                    <div
                      ref={noteEditorRef}
                      className="note-editor-textarea"
                      contentEditable
                      suppressContentEditableWarning
                      style={{ fontFamily: editorFont, fontSize: `${editorFontSize}px` }}
                      onInput={(e) => setNoteEditorContent(e.currentTarget.innerHTML)}
                    />
                  </>
                ) : (
                  <div className="empty">Open a file from the left tree to start editing.</div>
                )}
              </section>
            </div>
            <div className="notes-statusbar">
              <span>{selectedNote ? noteLabel(selectedNote) : "No file selected"}</span>
              <span>
                Words: {noteStats.words} | Chars: {noteStats.chars}
              </span>
            </div>
          </main>
        )}
      </div>
    </div>
  );
}

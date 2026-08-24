import { useEffect, useRef, useState } from "react";

const STATUS_LABEL = {
  processing: "Processing…",
  ready: "Ready",
  failed: "Failed",
};

export default function DocumentsPanel({ accessToken, getAuthHeaders, apiUrl, readErrorMessage, isGuest }) {
  const [documents, setDocuments] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [uploading, setUploading] = useState(false);
  const fileInputRef = useRef(null);

  async function fetchDocuments() {
    if (!accessToken || isGuest) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(apiUrl("/api/documents"), { headers: getAuthHeaders() });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Failed to load documents"));
      const data = await res.json();
      setDocuments(Array.isArray(data) ? data : []);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchDocuments();
  }, [accessToken, isGuest]);

  useEffect(() => {
    const hasProcessing = documents.some((d) => d.status === "processing");
    if (!hasProcessing) return;
    const id = setInterval(fetchDocuments, 3000);
    return () => clearInterval(id);
  }, [documents]);

  async function handleFileSelected(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    e.target.value = "";
    setUploading(true);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch(apiUrl("/api/documents"), {
        method: "POST",
        headers: getAuthHeaders(),
        body: form,
      });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Upload failed"));
      await fetchDocuments();
    } catch (e) {
      setError(String(e));
    } finally {
      setUploading(false);
    }
  }

  async function handleDelete(id) {
    setError("");
    try {
      const res = await fetch(apiUrl(`/api/documents/${id}`), {
        method: "DELETE",
        headers: getAuthHeaders(),
      });
      if (!res.ok) throw new Error(await readErrorMessage(res, "Delete failed"));
      setDocuments((prev) => prev.filter((d) => d.id !== id));
    } catch (e) {
      setError(String(e));
    }
  }

  if (isGuest) {
    return (
      <main className="documents-panel">
        <div className="empty">Sign in with Google to upload and query documents.</div>
      </main>
    );
  }

  return (
    <main className="documents-panel">
      <div className="documents-header">
        <h2>Documents</h2>
        <button
          className="documents-upload-btn"
          type="button"
          disabled={uploading}
          onClick={() => fileInputRef.current?.click()}
        >
          {uploading ? "Uploading…" : "Upload document"}
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.txt,.md"
          style={{ display: "none" }}
          onChange={handleFileSelected}
        />
      </div>
      {error ? <div className="documents-error">{error}</div> : null}
      {loading && documents.length === 0 ? (
        <div className="empty">Loading…</div>
      ) : documents.length === 0 ? (
        <div className="empty">No documents yet. Upload a PDF, TXT, or MD file to ask questions about it.</div>
      ) : (
        <ul className="documents-list">
          {documents.map((doc) => (
            <li key={doc.id} className="documents-list-item">
              <div className="documents-list-item-main">
                <span className="documents-filename">{doc.filename}</span>
                <span className={`documents-status documents-status-${doc.status}`}>
                  {STATUS_LABEL[doc.status] || doc.status}
                </span>
              </div>
              {doc.status === "failed" && doc.error_message ? (
                <div className="documents-error-message">{doc.error_message}</div>
              ) : null}
              {doc.status === "ready" ? (
                <div className="documents-meta">{doc.chunk_count} chunks indexed</div>
              ) : null}
              <button className="documents-delete-btn" type="button" onClick={() => handleDelete(doc.id)}>
                Delete
              </button>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}

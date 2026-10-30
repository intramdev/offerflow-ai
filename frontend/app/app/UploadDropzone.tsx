"use client";

import { useRef, useState } from "react";
import { ApiError, uploadRun, type Run } from "@/lib/api";

const MAX_BYTES = 10 * 1024 * 1024;

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

interface Props {
  organizationId: number | null;
  onUploaded: (run: Run) => void;
}

export default function UploadDropzone({ organizationId, onUploaded }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const disabled = organizationId === null;

  function pick(candidate: File | null) {
    setError("");
    if (!candidate) {
      setFile(null);
      return;
    }
    if (!candidate.name.toLowerCase().endsWith(".xlsx")) {
      setFile(null);
      setError("Only .xlsx files are accepted.");
      return;
    }
    if (candidate.size > MAX_BYTES) {
      setFile(null);
      setError("File exceeds the 10 MB limit.");
      return;
    }
    setFile(candidate);
  }

  async function send() {
    if (!file || disabled || busy) return;
    setError("");
    setBusy(true);
    try {
      const run = await uploadRun(organizationId, file);
      setFile(null);
      onUploaded(run);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card stack">
      <div
        className={`dropzone${dragging && !disabled ? " dragging" : ""}`}
        onDragEnter={(event) => {
          event.preventDefault();
          if (!disabled && !busy) setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (disabled || busy) return;
          pick(event.dataTransfer.files?.[0] ?? null);
        }}
      >
        <p className="dropzone-title">Drag and drop your spreadsheet here</p>
        <p className="muted">.xlsx only, up to 10 MB</p>
        <button
          className="button secondary"
          type="button"
          disabled={disabled || busy}
          onClick={() => inputRef.current?.click()}
        >
          Choose file
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".xlsx"
          className="visually-hidden"
          tabIndex={-1}
          onChange={(event) => {
            pick(event.target.files?.[0] ?? null);
            event.target.value = "";
          }}
        />
      </div>
      {disabled && (
        <p className="muted">Select an organization above before uploading.</p>
      )}
      {file && !disabled && (
        <div className="file-chip">
          <span>
            <strong>{file.name}</strong>{" "}
            <span className="muted">({formatSize(file.size)})</span>
          </span>
          <button
            className="linklike"
            type="button"
            disabled={busy}
            onClick={() => pick(null)}
          >
            Remove
          </button>
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <button
        className="button"
        type="button"
        disabled={!file || disabled || busy}
        onClick={send}
      >
        {busy ? "Uploading…" : "Upload and process"}
      </button>
    </div>
  );
}

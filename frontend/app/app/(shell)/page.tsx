"use client";

import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { MappingEditor } from "@/components/app/mapping-editor";
import { useOrg } from "@/components/app/org-context";
import { usePreviewAssist } from "@/components/app/preview-assist-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import {
  ApiError,
  cancelPreview,
  createPreview,
  createPreviewFromText,
  isMessageCandidate,
  listPendingPreviews,
  resumePreview,
  type MessageCandidate,
  type PendingPreview,
  type Preview,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { UploadDropzone } from "./UploadDropzone";

export default function UploadPage() {
  const { active, loading, memberships, prefs } = useOrg();
  const assist = usePreviewAssist();
  const [preview, setPreview] = React.useState<Preview | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [pasted, setPasted] = React.useState("");
  const [candidate, setCandidate] = React.useState<MessageCandidate | null>(null);
  const [pending, setPending] = React.useState<PendingPreview[] | null>(null);
  const [pendingRefresh, setPendingRefresh] = React.useState(0);
  const [resuming, setResuming] = React.useState<string | null>(null);

  async function onFile(file: File) {
    if (!active) return;
    setBusy(true);
    setError(null);
    setCandidate(null);
    try {
      const created = await createPreview(active.organization, file);
      if (isMessageCandidate(created)) {
        setCandidate(created);
      } else {
        setPreview(created);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed.");
    } finally {
      setBusy(false);
    }
  }

  async function onCandidateImport() {
    if (!active || !candidate) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createPreviewFromText(
        active.organization,
        candidate.text,
      );
      setCandidate(null);
      setPreview(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Preview failed.");
    } finally {
      setBusy(false);
    }
  }

  async function onPaste() {
    if (!active || pasted.trim() === "") return;
    setBusy(true);
    setError(null);
    try {
      const created = await createPreviewFromText(
        active.organization,
        pasted,
      );
      setPreview(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Preview failed.");
    } finally {
      setBusy(false);
    }
  }

  const pastedLines = pasted
    .split("\n")
    .filter((line) => line.trim() !== "").length;

  async function onResume(key: string) {
    setResuming(key);
    setError(null);
    try {
      setPreview(await resumePreview(key));
    } catch (err) {
      // The draft may have expired or been confirmed elsewhere:
      // refresh the list so the dead entry disappears.
      setPendingRefresh((count) => count + 1);
      setError(
        err instanceof ApiError ? err.message : "Could not resume the preview.",
      );
    } finally {
      setResuming(null);
    }
  }

  async function onDiscardPending(key: string) {
    setResuming(key);
    setError(null);
    try {
      await cancelPreview(key);
      setPendingRefresh((count) => count + 1);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not discard the preview.",
      );
    } finally {
      setResuming(null);
    }
  }

  // While a preview is open the floating assistant can propose changes
  // to it; applying swaps in the fresh preview, whose bumped draft
  // revision tells the editor to merge the assisted values.
  const previewKey = preview?.key ?? null;
  const previewRef = React.useRef(preview);
  previewRef.current = preview;
  React.useEffect(() => {
    if (!previewKey || !preview) return;
    assist.register({
      key: previewKey,
      messageLines: preview.message_lines,
      filename: preview.filename,
      onApplied: (next) => {
        setPreview(next);
      },
      getRevision: () => previewRef.current?.draft?.revision ?? null,
    });
    return () => assist.unregister(previewKey);
  }, [previewKey]);

  // Pasted but not yet previewed: let the assistant commit the draft
  // itself when the user sends instructions, so data talk never lands
  // in the help chat that cannot see it.
  React.useEffect(() => {
    if (previewKey || !active || pasted.trim() === "") {
      assist.setDraft(null);
      return;
    }
    const orgId = active.organization;
    const text = pasted;
    const lines = pastedLines;
    assist.setDraft({
      label: `pasted text (${lines} line${lines === 1 ? "" : "s"})`,
      commit: async () => {
        setBusy(true);
        setError(null);
        try {
          const created = await createPreviewFromText(orgId, text);
          setPreview(created);
          return created;
        } catch (err) {
          const message =
            err instanceof ApiError ? err.message : "Preview failed.";
          setError(message);
          throw new Error(message);
        } finally {
          setBusy(false);
        }
      },
    });
    return () => assist.setDraft(null);
  }, [previewKey, pasted, active]);

  // No preview open: offer resumable pending imports, so a refresh
  // never strands confirmed rules and mapping work.
  React.useEffect(() => {
    if (previewKey || !active) {
      setPending(null);
      return;
    }
    let cancelled = false;
    setPending(null);
    listPendingPreviews(active.organization)
      .then((items) => {
        if (!cancelled) setPending(items);
      })
      .catch(() => {
        if (!cancelled) setPending([]);
      });
    return () => {
      cancelled = true;
    };
  }, [previewKey, active, pendingRefresh]);

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading your workspace…</p>;
  }
  if (!active) {
    if (memberships.length > 0) {
      return <p className="text-sm text-muted-foreground">Selecting your workspace…</p>;
    }
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-bold">Upload a price list</h1>
        <p className="text-sm text-muted-foreground">
          You are not a member of any organization yet. Ask an owner to invite
          you before uploading.
        </p>
      </div>
    );
  }
  if (preview) {
    return (
      <div className="space-y-6">
        <div>
          <h1 className="text-2xl font-bold">Preview the import</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {preview.message_lines ? (
              <>
                Pasting to {active.organization_name}. Check the lines, set
                the supplier if you know it, then confirm.
              </>
            ) : (
              <>
                Uploading to {active.organization_name}. Adjust the mapping
                until the samples look right, then confirm.
              </>
            )}
          </p>
        </div>
        <MappingEditor
          preview={preview}
          onPreview={setPreview}
          onDone={() => setPreview(null)}
        />
      </div>
    );
  }
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Upload a price list</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Uploading to {active.organization_name}. You will preview and confirm
          the column mapping before anything imports.
        </p>
      </div>
      {error ? <ErrorNotice message={error} /> : null}
      {pending && pending.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Resume a pending import</CardTitle>
            <CardDescription>
              Unconfirmed previews keep their mapping, rules, and policy.
              Expired drafts disappear automatically.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ul className="space-y-3">
              {pending.map((item) => (
                <li
                  key={item.key}
                  className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-md border p-3"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{item.filename}</p>
                    <p className="text-sm text-muted-foreground">
                      {formatDateTime(item.created_at, prefs.datetime_format)}
                      {" · "}revision {item.revision}
                      {" · "}
                      {item.document_policy === "price_only"
                        ? "Price-only"
                        : "Stock"}
                      {item.message_lines ? " · pasted text" : ""}
                      {item.sheet ? ` · ${item.sheet}` : ""}
                      {item.rules_count > 0
                        ? ` · ${item.rules_count} rule${item.rules_count === 1 ? "" : "s"}`
                        : ""}
                    </p>
                  </div>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      onClick={() => void onResume(item.key)}
                      disabled={resuming !== null}
                    >
                      {resuming === item.key ? "Resuming…" : "Resume"}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => void onDiscardPending(item.key)}
                      disabled={resuming !== null}
                    >
                      Discard
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}
      {candidate ? (
        <Card>
          <CardHeader>
            <CardTitle>Import {candidate.filename} as a message?</CardTitle>
            <CardDescription>{candidate.explanation}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {candidate.total_chars.toLocaleString("en-US")} characters, no
              table headers — the message lane reads one product per line.
              {candidate.truncated
                ? " Only the first 256,000 characters are carried over."
                : ""}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button onClick={() => void onCandidateImport()} disabled={busy}>
                {busy ? "Loading preview…" : "Import as message"}
              </Button>
              <Button
                variant="outline"
                onClick={() => setCandidate(null)}
                disabled={busy}
              >
                Discard
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : null}
      <UploadDropzone disabled={busy} onFile={(file) => void onFile(file)} />
      <Card>
        <CardHeader>
          <CardTitle>Or paste a message</CardTitle>
          <CardDescription>
            Copy a price list from WhatsApp, Telegram, or anywhere else and
            paste it here — one product per line. It previews exactly like
            an uploaded file.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <label className="block">
            <span className="sr-only">Pasted price list text</span>
            <Textarea
              value={pasted}
              onChange={(event) => setPasted(event.target.value)}
              onPaste={(event) => {
                if (event.clipboardData?.getData("text")?.trim()) {
                  assist.spotlightAssistant();
                }
              }}
              disabled={busy}
              rows={6}
              placeholder={"Ex.\niPhone 15 128GB Black x5 450\nGalaxy S24 256GB Gray x3 520"}
              aria-label="Pasted price list text"
            />
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <Button
              onClick={() => void onPaste()}
              disabled={busy || pasted.trim() === ""}
            >
              {busy ? "Loading preview…" : "Preview pasted text"}
            </Button>
            {pastedLines > 0 ? (
              <span className="text-sm text-muted-foreground">
                {pastedLines} line{pastedLines === 1 ? "" : "s"}
              </span>
            ) : null}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

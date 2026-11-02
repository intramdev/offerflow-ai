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
  createPreview,
  createPreviewFromText,
  type Preview,
} from "@/lib/api";
import { UploadDropzone } from "./UploadDropzone";

export default function UploadPage() {
  const { active, loading, memberships } = useOrg();
  const assist = usePreviewAssist();
  const [preview, setPreview] = React.useState<Preview | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [pasted, setPasted] = React.useState("");
  const [assistRevision, setAssistRevision] = React.useState(0);

  async function onFile(file: File) {
    if (!active) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createPreview(active.organization, file);
      setPreview(created);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed.");
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

  // While a preview is open the floating assistant can propose changes
  // to it; applying swaps in the fresh preview and bumps the revision
  // so the editor merges the assisted values.
  const previewKey = preview?.key ?? null;
  React.useEffect(() => {
    if (!previewKey || !preview) return;
    assist.register({
      key: previewKey,
      messageLines: preview.message_lines,
      filename: preview.filename,
      onApplied: (next) => {
        setPreview(next);
        setAssistRevision((revision) => revision + 1);
      },
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
          assistRevision={assistRevision}
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

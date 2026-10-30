"use client";

import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { MappingEditor } from "@/components/app/mapping-editor";
import { useOrg } from "@/components/app/org-context";
import { ApiError, createPreview, type Preview } from "@/lib/api";
import { UploadDropzone } from "./UploadDropzone";

export default function UploadPage() {
  const { active, loading, memberships } = useOrg();
  const [preview, setPreview] = React.useState<Preview | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

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
            Uploading to {active.organization_name}. Adjust the mapping until
            the samples look right, then confirm.
          </p>
        </div>
        <MappingEditor preview={preview} onDone={() => setPreview(null)} />
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
    </div>
  );
}

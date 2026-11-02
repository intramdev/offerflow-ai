"use client";

import * as React from "react";

import { summarizeRule } from "@/components/app/draft-ops";
import { ErrorNotice } from "@/components/app/error-notice";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ApiError,
  applyPreviewAssist,
  type DraftBlock,
  type Preview,
} from "@/lib/api";

export function RulesPanel({
  previewKey,
  draft,
  onPreview,
}: {
  previewKey: string;
  draft: DraftBlock;
  onPreview: (next: Preview) => void;
}) {
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const rules = draft.rules ?? [];
  const history = draft.history ?? [];

  async function send(
    operations: { op: string; [key: string]: unknown }[],
  ) {
    setBusy(true);
    setError(null);
    try {
      const result = await applyPreviewAssist(previewKey, {
        set_constants: {},
        drop_lines: [],
        operations,
        revision: draft.revision,
      });
      onPreview(result.preview);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not update the rules.",
      );
    } finally {
      setBusy(false);
    }
  }

  const priceOnly = draft.document_policy === "price_only";

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Confirmed rules{" "}
          <span className="text-sm font-normal text-muted-foreground">
            revision {draft.revision}
          </span>
        </CardTitle>
        <CardDescription>
          These rules shape every candidate above and travel with the
          import — they are not chat suggestions. Removing a rule
          recomputes the preview immediately.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="font-medium">Policy:</span>
          <span>{priceOnly ? "Price-only (quantities unknown)" : "Stock (quantities required)"}</span>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() =>
              void send([
                { op: "policy", policy: priceOnly ? "stock" : "price_only" },
              ])
            }
          >
            {busy ? "…" : priceOnly ? "Use stock policy" : "Use price-only"}
          </Button>
        </div>
        {rules.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No rules yet. Set values above, or ask the assistant to
            interpret the lines — applied proposals land here.
          </p>
        ) : (
          <ul className="space-y-2">
            {rules.map((rule) => {
              const summary = summarizeRule(rule);
              return (
                <li
                  key={rule.id ?? `${rule.kind}-${summary.title}`}
                  className="flex items-start justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm"
                >
                  <span>
                    <span className="block font-medium">{summary.title}</span>
                    {summary.detail ? (
                      <span className="block text-xs text-muted-foreground">
                        {summary.detail}
                      </span>
                    ) : null}
                  </span>
                  {rule.id ? (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() =>
                        void send([{ op: "drop_rule", rule: rule.id }])
                      }
                      aria-label={`Remove rule: ${summary.title}`}
                    >
                      Remove
                    </Button>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
        {history.length > 0 ? (
          <div>
            <p className="text-sm font-medium">Recent changes</p>
            <ul className="mt-1 space-y-1 text-xs text-muted-foreground">
              {history
                .slice(-4)
                .reverse()
                .map((entry) => (
                  <li key={entry.revision}>
                    rev {entry.revision} · {entry.summary}
                  </li>
                ))}
            </ul>
          </div>
        ) : null}
        {error ? <ErrorNotice message={error} /> : null}
      </CardContent>
    </Card>
  );
}

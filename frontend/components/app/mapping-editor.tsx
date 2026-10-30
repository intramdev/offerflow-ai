"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import {
  FieldMapper,
  missingRequired,
  selectionFromProposed,
  shownFields,
  toPayload,
  type FieldSelection,
} from "@/components/app/field-mapper";
import { useOrg } from "@/components/app/org-context";
import { StatusBadge } from "@/components/app/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  ApiError,
  cancelPreview,
  confirmPreview,
  getOrgSettings,
  renderPreview,
  type Preview,
  type PreviewSamples,
} from "@/lib/api";

export function MappingEditor({
  preview,
  onDone,
}: {
  preview: Preview;
  onDone: () => void;
}) {
  const router = useRouter();
  const { active } = useOrg();
  const [selection, setSelection] = React.useState<FieldSelection>(() =>
    selectionFromProposed(
      preview.proposed,
      preview.proposed_constants ?? {},
      preview.proposed_ignored ?? [],
    ),
  );
  const [defaultCurrency, setDefaultCurrency] = React.useState("");
  const [samples, setSamples] = React.useState<PreviewSamples>(preview.samples);
  const [rendering, setRendering] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const timer = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  React.useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  async function refresh(next: FieldSelection) {
    setRendering(true);
    try {
      const payload = toPayload(next);
      const result = await renderPreview(
        preview.key,
        payload.mapping,
        payload.constants,
        payload.ignored,
      );
      setSamples(result.samples);
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Preview failed to update.");
    } finally {
      setRendering(false);
    }
  }

  function change(next: FieldSelection) {
    setSelection(next);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void refresh(next), 400);
  }

  React.useEffect(() => {
    if (!active) return;
    let cancelled = false;
    void getOrgSettings(active.organization).then(
      (settings) => {
        if (!cancelled) setDefaultCurrency(settings.default_currency);
      },
      () => {},
    );
    return () => {
      cancelled = true;
    };
  }, [active]);

  // Pre-fill an unmapped currency with the Settings default so every row
  // shares it without mapping a column. Anything already decided — a file
  // column, a remembered value, or the user's own edit — is left alone.
  React.useEffect(() => {
    if (!defaultCurrency || selection.currency?.kind !== "") return;
    change({
      ...selection,
      currency: { kind: "custom", position: "", value: defaultCurrency },
    });
  }, [defaultCurrency]);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const payload = toPayload(selection);
      const run = await confirmPreview(
        preview.key,
        payload.mapping,
        payload.constants,
        payload.ignored,
      );
      router.push(`/app/runs/${run.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Import failed to start.");
      setBusy(false);
    }
  }

  async function cancel() {
    setBusy(true);
    try {
      await cancelPreview(preview.key);
    } catch {
      // Expired previews cancel themselves; still leave the editor.
    }
    onDone();
  }

  const columns = shownFields(selection);
  const missing = missingRequired(selection);

  return (
    <div className="space-y-6">
      {preview.memory ? (
        <div className="rounded-md border border-border bg-muted px-4 py-3 text-sm">
          Recognized this layout — you mapped it {preview.memory.use_count}{" "}
          time{preview.memory.use_count === 1 ? "" : "s"} before
          {preview.memory.supplier_hint
            ? ` (last supplier: ${preview.memory.supplier_hint})`
            : ""}
          . Review the mapping and confirm.
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Map the columns</CardTitle>
          <CardDescription>
            {preview.filename} · {preview.headers.length} columns detected.
            Required fields are marked. Ignore fields you never want, or set
            one value for every row.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FieldMapper
            headers={preview.headers}
            selection={selection}
            onChange={change}
            autoFields={Object.keys(preview.detected)}
          />
          {missing.length > 0 ? (
            <p className="mt-3 text-sm font-medium text-destructive" role="alert">
              Still needed: {missing.map((f) => f.label).join(", ")} — pick a
              column or set a value.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>
            Sample rows{" "}
            {rendering ? (
              <span className="text-sm font-normal text-muted-foreground">
                updating…
              </span>
            ) : null}
          </CardTitle>
          <CardDescription>
            First {samples.rows.length} data rows as they will import. Row
            status is computed by OfferFlow — it is not a file column and
            cannot be mapped.
            {samples.applied_defaults && Object.keys(samples.applied_defaults).length > 0
              ? ` Defaults applied: ${Object.entries(samples.applied_defaults)
                  .map(([field, value]) => `${field} = ${value}`)
                  .join(", ")}.`
              : ""}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {samples.rows.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No rows to show for this mapping yet.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Row</TableHead>
                  {columns.map((f) => (
                    <TableHead key={f.name}>{f.label}</TableHead>
                  ))}
                  <TableHead>Row status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {samples.rows.map((row) => (
                  <TableRow key={row.index}>
                    <TableCell>{row.index}</TableCell>
                    {columns.map((f) => (
                      <TableCell key={f.name}>
                        {String(row.cells[f.name] ?? "") || (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                    ))}
                    <TableCell>
                      {row.status ? (
                        <span className="flex flex-col items-start gap-1">
                          <StatusBadge status={row.status} />
                          {(row.issues ?? []).slice(0, 2).map((issue) => (
                            <span key={issue} className="text-xs text-muted-foreground">
                              {issue}
                            </span>
                          ))}
                        </span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {error ? <ErrorNotice message={error} /> : null}

      <div className="flex flex-wrap gap-2">
        <Button onClick={() => void confirm()} disabled={busy || missing.length > 0}>
          {busy ? "Importing…" : "Confirm and import"}
        </Button>
        <Button variant="outline" onClick={() => void cancel()} disabled={busy}>
          Discard preview
        </Button>
      </div>
    </div>
  );
}

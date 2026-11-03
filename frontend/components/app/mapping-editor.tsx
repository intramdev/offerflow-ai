"use client";

import { useRouter } from "next/navigation";
import * as React from "react";

import { DraftPreview } from "@/components/app/draft-preview";
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
import { RulesPanel } from "@/components/app/rules-panel";
import { StatusBadge } from "@/components/app/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select } from "@/components/ui/select";
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
  onPreview,
  onDone,
}: {
  preview: Preview;
  onPreview: (next: Preview) => void;
  onDone: () => void;
}) {
  const router = useRouter();
  const { active } = useOrg();
  const messageLines = preview.message_lines === true;
  const [selection, setSelection] = React.useState<FieldSelection>(() => {
    const initial = selectionFromProposed(
      preview.proposed,
      preview.proposed_constants ?? {},
      preview.proposed_ignored ?? [],
    );
    if (!preview.message_lines) return initial;
    // Message imports accept constants only: the backend rejects any
    // mapping or ignored list, so start every field unset — including
    // lock, which otherwise defaults to ignored.
    const cleared: FieldSelection = {};
    for (const [field, choice] of Object.entries(initial)) {
      cleared[field] =
        choice.kind === "custom"
          ? choice
          : { kind: "", position: "", value: "" };
    }
    return cleared;
  });
  const [defaultCurrency, setDefaultCurrency] = React.useState("");
  const [currencies, setCurrencies] = React.useState<string[] | null>(null);
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
        if (cancelled) return;
        setDefaultCurrency(settings.default_currency);
        setCurrencies(settings.supported_currencies ?? []);
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

  // Fields the user edited by hand always win over assisted values; the
  // assistant may fill blank fields and refresh fields it set before.
  const touched = React.useRef<Set<string>>(new Set());
  const assisted = React.useRef<Set<string>>(new Set());
  const selectionRef = React.useRef(selection);
  selectionRef.current = selection;
  // Every confirmed change — chat proposal, rules-panel edit — arrives
  // as a new preview object with a bumped draft revision. Merge its
  // constants into untouched fields and resample quietly.
  const seenPreview = React.useRef<{ key: string; revision: number } | null>(
    null,
  );
  React.useEffect(() => {
    const revision = preview.draft?.revision ?? 0;
    const seen = seenPreview.current;
    if (seen === null) {
      // Mount: the selection was already built from this preview.
      seenPreview.current = { key: preview.key, revision };
      return;
    }
    if (seen.key !== preview.key) {
      seenPreview.current = { key: preview.key, revision };
      return;
    }
    if (seen.revision === revision) return;
    seenPreview.current = { key: preview.key, revision };
    const current = selectionRef.current;
    const next: FieldSelection = { ...current };
    const constants = preview.proposed_constants ?? {};
    for (const [field, raw] of Object.entries(constants)) {
      if (touched.current.has(field)) continue;
      const choice = current[field];
      if (choice && choice.kind !== "" && !assisted.current.has(field)) {
        continue;
      }
      next[field] = { kind: "custom", position: "", value: String(raw) };
    }
    // Assisted values follow the draft: dropping the global rule that
    // set a field clears it again, unless the user typed there since.
    for (const field of assisted.current) {
      if (touched.current.has(field)) continue;
      if (!(field in constants)) {
        next[field] = { kind: "", position: "", value: "" };
      }
    }
    assisted.current = new Set(Object.keys(constants));
    setSamples(preview.samples);
    setError(null);
    // change() would mark fields touched; refresh quietly instead.
    if (timer.current) clearTimeout(timer.current);
    setSelection(next);
    timer.current = setTimeout(() => void refresh(next), 400);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preview]);

  async function switchSheet(name: string) {
    if (messageLines || name === preview.sheets.selected) return;
    if (timer.current) clearTimeout(timer.current);
    setRendering(true);
    setError(null);
    try {
      const result = await renderPreview(preview.key, {}, {}, [], name);
      if (!result.switched) {
        setError("The sheet did not change. Try again.");
        return;
      }
      // Old-sheet positions cannot apply to the new headers: take the
      // fresh detection exactly as the backend stored it.
      const fresh = selectionFromProposed(
        result.mapping,
        result.constants,
        result.ignored,
      );
      touched.current.clear();
      assisted.current.clear();
      setSelection(fresh);
      setSamples(result.samples);
      onPreview({
        ...preview,
        headers: result.switched.headers,
        detected: result.switched.detected,
        missing: result.switched.missing,
        total_rows: result.switched.total_rows,
        proposed: result.mapping,
        proposed_constants: result.constants,
        proposed_ignored: result.ignored,
        samples: result.samples,
        sheets: result.sheets,
      });
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not switch sheets.");
    } finally {
      setRendering(false);
    }
  }

  async function confirm() {
    const revision = preview.draft?.revision ?? null;
    if (revision == null) {
      setError(
        "The preview revision is not loaded yet. Reload the preview and confirm again.",
      );
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const payload = toPayload(selection);
      const run = await confirmPreview(
        preview.key,
        payload.mapping,
        payload.constants,
        payload.ignored,
        preview.sheets.selected,
        revision,
      );
      router.push(`/app/runs/${run.id}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Our revision is stale: another tab changed the draft after
        // this page loaded it. Say so plainly — retrying blindly
        // would import a different interpretation than shown.
        setError(
          `${err.message} This page may be showing an older revision — ` +
            "if another tab changed this preview, discard it and start over.",
        );
      } else {
        setError(err instanceof ApiError ? err.message : "Import failed to start.");
      }
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
  // Message lines map nothing: interpretation plus human reviews cover
  // every gap, so nothing blocks confirmation.
  const missing = messageLines ? [] : missingRequired(selection);
  const visibleSheets = (preview.sheets?.available ?? []).filter(
    (entry) => entry.visible,
  );

  return (
    <div className="space-y-6">
      {preview.sheets?.notice ? (
        <div
          className="rounded-md border border-border bg-muted px-4 py-3 text-sm"
          role="status"
        >
          {preview.sheets.notice}
        </div>
      ) : null}
      {!messageLines && visibleSheets.length > 1 ? (
        <div className="flex flex-wrap items-center gap-3">
          <label
            htmlFor="preview-sheet"
            className="text-sm font-medium"
          >
            Sheet to import
          </label>
          <Select
            id="preview-sheet"
            value={preview.sheets.selected ?? ""}
            onChange={(event) => void switchSheet(event.target.value)}
            disabled={rendering || busy}
            aria-label="Sheet to import"
          >
            {visibleSheets.map((entry) => (
              <option key={entry.name} value={entry.name}>
                {entry.name}
                {entry.rows !== null && entry.rows !== undefined
                  ? ` (${entry.rows} rows)`
                  : ""}
              </option>
            ))}
          </Select>
        </div>
      ) : null}
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
          <CardTitle>
            {messageLines ? "Set values for every line" : "Map the columns"}
          </CardTitle>
          <CardDescription>
            {messageLines ? (
              <>
                {preview.filename} · {preview.total_rows} pasted line
                {preview.total_rows === 1 ? "" : "s"}. Each line imports as
                one row and OfferFlow reads the fields from the text — set
                the supplier when you know it.
              </>
            ) : (
              <>
                {preview.filename} · {preview.headers.length} columns detected.
                Required fields are marked. Ignore fields you never want, or
                set one value for every row.
              </>
            )}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <FieldMapper
            headers={messageLines ? [] : preview.headers}
            selection={selection}
            onChange={(next) => {
              for (const [field, choice] of Object.entries(next)) {
                const before = selection[field];
                if (
                  !before ||
                  before.kind !== choice.kind ||
                  before.position !== choice.position ||
                  before.value !== choice.value
                ) {
                  touched.current.add(field);
                  assisted.current.delete(field);
                }
              }
              change(next);
            }}
            autoFields={Object.keys(preview.detected)}
            currencies={currencies}
            valuesOnly={messageLines}
          />
          {missing.length > 0 ? (
            <p className="mt-3 text-sm font-medium text-destructive" role="alert">
              Still needed: {missing.map((f) => f.label).join(", ")} — pick a
              column or set a value.
            </p>
          ) : null}
        </CardContent>
      </Card>

      {messageLines && preview.draft ? (
        <DraftPreview draft={preview.draft} />
      ) : null}
      {messageLines && preview.draft ? (
        <RulesPanel
          previewKey={preview.key}
          draft={preview.draft}
          onPreview={onPreview}
        />
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>
            {messageLines ? "Pasted lines" : "Sample rows"}{" "}
            {rendering ? (
              <span className="text-sm font-normal text-muted-foreground">
                updating…
              </span>
            ) : null}
          </CardTitle>
          <CardDescription>
            {messageLines ? (
              <>
                First {samples.rows.length} of {preview.total_rows} lines as
                they will import. Fields are read from the text during
                import; header and note lines are set aside, and anything
                unclear becomes a question for you.
              </>
            ) : (
              <>
                First {samples.rows.length} data rows as they will import. Row
                status is computed by OfferFlow — it is not a file column and
                cannot be mapped.
              </>
            )}
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
          ) : messageLines ? (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Line</TableHead>
                  <TableHead>Message</TableHead>
                  <TableHead>Set values</TableHead>
                  <TableHead>Row status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {samples.rows.map((row) => {
                  const fixed = Object.entries(row.cells).filter(
                    ([name, value]) =>
                      name !== "message" &&
                      value !== null &&
                      value !== undefined &&
                      String(value).trim() !== "",
                  );
                  return (
                    <TableRow key={row.index}>
                      <TableCell>{row.index}</TableCell>
                      <TableCell className="max-w-md whitespace-pre-wrap break-words">
                        {String(row.cells.message ?? "")}
                      </TableCell>
                      <TableCell>
                        {fixed.length === 0 ? (
                          <span className="text-muted-foreground">—</span>
                        ) : (
                          <span className="flex flex-col items-start gap-1 text-xs">
                            {fixed.map(([name, value]) => (
                              <span key={name}>
                                {name} = {String(value)}
                              </span>
                            ))}
                          </span>
                        )}
                      </TableCell>
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
                  );
                })}
              </TableBody>
            </Table>
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
        <Button
          onClick={() => void confirm()}
          disabled={busy || rendering || missing.length > 0}
        >
          {busy ? "Importing…" : "Confirm and import"}
        </Button>
        <Button variant="outline" onClick={() => void cancel()} disabled={busy}>
          Discard preview
        </Button>
      </div>
    </div>
  );
}

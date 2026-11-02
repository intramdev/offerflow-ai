"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import {
  FieldMapper,
  missingRequired,
  selectionFromProposed,
  toPayload,
  type FieldSelection,
  type HeaderOption,
} from "@/components/app/field-mapper";
import { useOrg } from "@/components/app/org-context";
import { formatDate, type DateTimeFormat } from "@/lib/format";
import { selectFxPair, type FxPairStatus } from "@/lib/fx";
import { StatusBadge } from "@/components/app/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
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
  answerReview,
  approveRun,
  cancelRun,
  download,
  exportRun,
  getOrgSettings,
  getQuote,
  getRun,
  ignoreReview,
  listOffers,
  listReviews,
  remapRun,
  saveBlob,
  type Offer,
  type Quote,
  type Review,
  type Run,
} from "@/lib/api";

const ACTIVE = new Set(["uploaded", "parsing", "interpretation", "validation"]);

function formatRate(raw: string): string {
  const value = Number(raw);
  if (!Number.isFinite(value)) return raw;
  return value.toFixed(6).replace(/\.?0+$/, "");
}

function FxPairLine({
  fx,
  organizationId,
  datetimeFormat,
}: {
  fx: Quote["fx"];
  organizationId: number | null;
  datetimeFormat: DateTimeFormat;
}) {
  const [pair, setPair] = useState<{ from: string; to: string } | null>(null);

  useEffect(() => {
    if (organizationId === null) return;
    let cancelled = false;
    void getOrgSettings(organizationId).then(
      (settings) => {
        if (cancelled) return;
        setPair({
          from: settings.default_currency,
          to: settings.destination_currency,
        });
      },
      () => {},
    );
    return () => {
      cancelled = true;
    };
  }, [organizationId]);

  if (!pair) {
    return (
      <p className="text-sm text-muted-foreground">Loading exchange rate…</p>
    );
  }
  const status: FxPairStatus = selectFxPair(fx, pair.from, pair.to);
  const day = formatDate(fx.currency_date + "T00:00:00", datetimeFormat);
  const provenance = fx.simulated ? "simulated" : (fx.source ?? "recorded rates");
  if (status.kind === "same") {
    return (
      <p className="text-sm text-muted-foreground">
        No conversion needed ({status.currency}).
      </p>
    );
  }
  if (status.kind === "rate") {
    return (
      <div className="space-y-1 text-sm text-muted-foreground">
        <p>
          Exchange rate: {status.from} -&gt; {status.to}; 1 {status.from} ={" "}
          <span className="tabular-nums">
            {formatRate(status.rate)} {status.to}
          </span>
          ; {day} ({provenance}).
        </p>
        <p>Change the date on the Files page to reprice.</p>
      </div>
    );
  }
  return (
    <div className="space-y-1 text-sm text-muted-foreground">
      <p>
        Exchange rate {status.from || "—"} -&gt; {status.to || "—"}: unavailable
        — no recorded rate for this pair.
      </p>
      <p>
        {status.recordedDestination
          ? `Recorded rates convert to ${status.recordedDestination}; `
          : ""}
        Change the date on the Files page to reprice.
      </p>
    </div>
  );
}

function AnswerForm({ review, onAnswered }: { review: Review; onAnswered: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function send(payload: { choice?: string; value?: unknown }) {
    setError("");
    setBusy(true);
    try {
      await answerReview(review.id, payload);
      setValue("");
      onAnswered();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Answer failed.");
    } finally {
      setBusy(false);
    }
  }

  async function ignore() {
    if (
      !window.confirm(
        "Remove this record from the quote? It will be listed as excluded.",
      )
    ) {
      return;
    }
    setError("");
    setBusy(true);
    try {
      await ignoreReview(review.id);
      onAnswered();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Ignore failed.");
    } finally {
      setBusy(false);
    }
  }

  const provenance = (review.evidence as { provenance?: { sheet?: string; row?: number } } | null)
    ?.provenance;
  return (
    <Card>
      <CardContent className="space-y-3 pt-5">
        <p className="text-sm">
          <strong>
            Row {provenance?.row ?? "?"} · {review.field}
          </strong>
        </p>
        <p className="text-sm">{review.question}</p>
        {review.offer !== null ? (
          <div>
            <Button
              variant="destructive"
              className="font-bold"
              disabled={busy}
              onClick={() => void ignore()}
            >
              Ignore this record
            </Button>
          </div>
        ) : null}
        {review.kind === "choice" ? (
          <div className="flex flex-wrap gap-2">
            {review.choices.map((choice) => (
              <Button
                key={choice}
                variant="outline"
                size="sm"
                disabled={busy}
                onClick={() => void send({ choice })}
              >
                {choice}
              </Button>
            ))}
          </div>
        ) : (
          <form
            className="flex gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              void send({ value });
            }}
          >
            <Input
              value={value}
              onChange={(event) => setValue(event.target.value)}
              required
              placeholder="Type the correct value"
              aria-label="Answer value"
            />
            <Button variant="outline" type="submit" disabled={busy}>
              {busy ? "Sending…" : "Send"}
            </Button>
          </form>
        )}
        {error ? <ErrorNotice message={error} /> : null}
      </CardContent>
    </Card>
  );
}

function SheetSummary({ mapping }: { mapping: Record<string, unknown> }) {
  const sheet = typeof mapping?.sheet === "string" ? mapping.sheet : "";
  const omitted = Array.isArray(mapping?.sheets_omitted)
    ? (mapping.sheets_omitted as unknown[]).filter(
        (name): name is string => typeof name === "string",
      )
    : [];
  if (!sheet && omitted.length === 0) return null;
  return (
    <p className="mt-1 text-sm text-muted-foreground">
      {sheet ? <>Sheet {sheet}</> : null}
      {sheet && omitted.length > 0 ? " · " : null}
      {omitted.length > 0 ? <>not imported: {omitted.join(", ")}</> : null}
    </p>
  );
}

function ContextCoverageNotice({
  mapping,
}: {
  mapping: Record<string, unknown>;
}) {
  const coverage = mapping?.context_coverage as
    | {
        segments?: unknown;
        partial?: unknown;
        dropped_lines?: unknown;
      }
    | null
    | undefined;
  if (!coverage || typeof coverage !== "object") return null;
  const segments = typeof coverage.segments === "number" ? coverage.segments : 0;
  const dropped =
    typeof coverage.dropped_lines === "number" ? coverage.dropped_lines : 0;
  if (coverage.partial === true) {
    return (
      <p className="mt-1 text-sm font-medium text-amber-700 dark:text-amber-400">
        Partial document context: {dropped} line{dropped === 1 ? "" : "s"}{" "}
        omitted from model context across {segments} segment
        {segments === 1 ? "" : "s"}. All rows were still read as data.
      </p>
    );
  }
  if (segments > 1) {
    return (
      <p className="mt-1 text-sm text-muted-foreground">
        Full context coverage across {segments} segments.
      </p>
    );
  }
  return null;
}

function RemapDialog({
  run,
  open,
  onClose,
  onDone,
}: {
  run: Run;
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}) {
  const stored = (run.mapping ?? {}) as {
    headers?: string[];
    detected?: Record<string, number>;
    confirmed?: Record<string, number>;
    constants?: Record<string, string | number>;
    ignored?: string[];
  };
  const headers: HeaderOption[] = (stored.headers ?? []).map((header, position) => ({
    position,
    letter: String.fromCharCode(65 + position),
    header,
  }));
  const [selection, setSelection] = useState<FieldSelection>(() =>
    selectionFromProposed(
      { ...(stored.detected ?? {}), ...(stored.confirmed ?? {}) },
      stored.constants ?? {},
      stored.ignored ?? [],
    ),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [defaultCurrency, setDefaultCurrency] = useState("");
  const [currencies, setCurrencies] = useState<string[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getOrgSettings(run.organization).then(
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
  }, [run.organization]);

  // Pre-fill an unmapped currency with the Settings default. Anything
  // already decided — a file column, a stored value, or the user's own
  // edit — is left alone.
  useEffect(() => {
    if (!defaultCurrency || selection.currency?.kind !== "") return;
    setSelection({
      ...selection,
      currency: { kind: "custom", position: "", value: defaultCurrency },
    });
  }, [defaultCurrency]);

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const payload = toPayload(selection);
      await remapRun(run.id, payload.mapping, payload.constants, payload.ignored);
      onDone();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Remap failed.");
      setBusy(false);
    }
  }

  const missing = missingRequired(selection);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Remap this run"
      description={
        run.status === "approved" || run.status === "completed"
          ? "Reprocessing discards this run's approval and exports, then replaces every offer and review from this file."
          : "Reprocessing replaces every offer and review from this file."
      }
      wide
    >
      <div className="space-y-4">
        <FieldMapper
          headers={headers}
          selection={selection}
          onChange={setSelection}
          autoFields={Object.keys(stored.detected ?? {})}
          currencies={currencies}
        />
        {missing.length > 0 ? (
          <p className="text-sm font-medium text-destructive" role="alert">
            Still needed: {missing.map((f) => f.label).join(", ")} — pick a
            column or set a value.
          </p>
        ) : null}
        {error ? <ErrorNotice message={error} /> : null}
        <div className="flex gap-2">
          <Button onClick={() => void submit()} disabled={busy || missing.length > 0}>
            {busy ? "Reprocessing…" : "Save mapping and reprocess"}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Keep the current mapping
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

const STAGES = [
  { key: "uploaded", label: "Queued" },
  { key: "parsing", label: "Parsing" },
  { key: "interpretation", label: "Interpreting" },
  { key: "validation", label: "Validating" },
] as const;

function RunProgress({
  run,
  aiProgress,
}: {
  run: Run;
  aiProgress: { done: number; total: number } | null;
}) {
  const stageIndex = STAGES.findIndex((stage) => stage.key === run.status);
  const percent =
    run.status === "interpretation" && aiProgress && aiProgress.total > 0
      ? Math.min(100, Math.round((aiProgress.done / aiProgress.total) * 100))
      : null;
  const provider = run.provider_name || "AI";
  const detail =
    run.status === "uploaded"
      ? "Waiting for a worker to pick up the file…"
      : run.status === "parsing"
        ? "Reading the spreadsheet…"
        : run.status === "interpretation"
          ? !run.simulated && aiProgress
            ? `Enriching ${run.counts.rows} rows with ${provider}… call ${aiProgress.done} of ${aiProgress.total} done.`
            : !run.simulated
              ? `Contacting ${provider}…`
              : "Interpreting rows locally…"
          : "Running validation checks…";
  return (
    <div className="mt-4 rounded-md border border-border bg-muted/40 p-4">
      <ol
        className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs"
        aria-label="Import progress"
      >
        {STAGES.map((stage, index) => {
          const done = index < stageIndex;
          const current = index === stageIndex;
          return (
            <li
              key={stage.key}
              aria-current={current ? "step" : undefined}
              className={
                current
                  ? "font-semibold text-foreground"
                  : done
                    ? "text-muted-foreground"
                    : "text-muted-foreground/60"
              }
            >
              <span aria-hidden>
                {done ? "✓ " : current ? "● " : "○ "}
              </span>
              {stage.label}
            </li>
          );
        })}
      </ol>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted">
        {percent !== null ? (
          <div
            role="progressbar"
            aria-label="AI interpretation progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            aria-valuetext={`${aiProgress?.done} of ${aiProgress?.total} calls done`}
            className="h-full rounded-full bg-primary motion-safe:transition-[width] motion-safe:duration-500"
            style={{ width: `${percent}%` }}
          />
        ) : (
          <div
            role="progressbar"
            aria-label={detail}
            className="h-full w-1/3 rounded-full bg-primary motion-safe:animate-pulse"
          />
        )}
      </div>
      <p className="mt-2 text-sm text-muted-foreground">{detail}</p>
    </div>
  );
}

export default function RunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { canOperate, prefs } = useOrg();
  const [run, setRun] = useState<Run | null>(null);
  const [offers, setOffers] = useState<Offer[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [remapping, setRemapping] = useState(false);

  // Opened from Files → Edit: jump straight into the mapping dialog.
  useEffect(() => {
    if (!canOperate) return;
    if (window.location.hash === "#remap") {
      window.history.replaceState(
        null,
        "",
        window.location.pathname + window.location.search,
      );
      setRemapping(true);
    }
  }, [canOperate]);

  const load = useCallback(async () => {
    try {
      const runId = Number(id);
      const [detail, offerList, reviewList, quotePreview] = await Promise.all([
        getRun(runId),
        listOffers(runId),
        listReviews(runId),
        getQuote(runId),
      ]);
      setRun(detail);
      setOffers(offerList);
      setReviews(reviewList);
      setQuote(quotePreview);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Load failed.");
    }
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!run || !ACTIVE.has(run.status)) return;
    const timer = setInterval(() => void load(), 2000);
    return () => clearInterval(timer);
  }, [run, load]);

  async function act(work: () => Promise<unknown>) {
    setError("");
    setBusy(true);
    try {
      await work();
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  async function downloadQuote(format: "xlsx" | "csv") {
    setError("");
    try {
      const blob = await download(`/api/pipeline/runs/${id}/quote/download/${format}/`);
      saveBlob(blob, `offerflow-quote-${id}.${format}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Download failed.");
    }
  }

  if (!run && !error) return <p className="text-sm text-muted-foreground">Loading run…</p>;
  if (!run) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">Run not found</h1>
        {error ? <ErrorNotice message={error} /> : null}
        <p className="text-sm">
          <Link href="/app/files" className="underline">
            Back to files
          </Link>
        </p>
      </div>
    );
  }

  const progressRaw = run.mapping?.ai_progress as
    | { done?: unknown; total?: unknown }
    | undefined;
  const aiProgress =
    typeof progressRaw?.done === "number" &&
    typeof progressRaw?.total === "number"
      ? { done: progressRaw.done, total: progressRaw.total }
      : null;

  return (
    <div className="space-y-8">
      <div>
        <p className="text-sm">
          <Link href="/app/files" className="text-muted-foreground hover:underline">
            ← Files
          </Link>
        </p>
        <h1 className="mt-2 flex items-center gap-3 text-2xl font-bold">
          Run {run.id} <StatusBadge status={run.status} />
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Rules {run.rule_version} · provider {run.provider_name}
          {run.simulated ? " (simulated)" : ""} · {run.counts.offers} offers,{" "}
          {run.counts.open_reviews} open questions.
        </p>
        <SheetSummary mapping={run.mapping} />
        <ContextCoverageNotice mapping={run.mapping} />
        {run.error ? <div className="mt-3"><ErrorNotice message={run.error} /></div> : null}
        {error ? <div className="mt-3"><ErrorNotice message={error} /></div> : null}
        {ACTIVE.has(run.status) ? (
          <RunProgress run={run} aiProgress={aiProgress} />
        ) : null}
      </div>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Checks</h2>
        {(run.validations ?? []).length === 0 ? (
          <p className="text-sm text-muted-foreground">No validation results yet.</p>
        ) : (
          <ul className="space-y-1.5">
            {(run.validations ?? []).map((item) => (
              <li key={item.name} className="flex items-start gap-2 text-sm">
                <Badge variant={item.passed ? "default" : "destructive"}>
                  {item.passed ? "Passed" : "Failed"}
                </Badge>
                <span>
                  <span className="font-medium">{item.name}:</span> {item.message}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Offers</h2>
        {offers.length === 0 ? (
          <p className="text-sm text-muted-foreground">No offers produced.</p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Row</TableHead>
                <TableHead>Supplier</TableHead>
                <TableHead>Model</TableHead>
                <TableHead>GB</TableHead>
                <TableHead>Grade</TableHead>
                <TableHead>Lock</TableHead>
                <TableHead>Qty</TableHead>
                <TableHead>Price</TableHead>
                <TableHead>Cur</TableHead>
                <TableHead>Status</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {offers.map((offer) => (
                <TableRow key={offer.id}>
                  <TableCell>{offer.source_row}</TableCell>
                  <TableCell>{offer.supplier}</TableCell>
                  <TableCell>{offer.model_name}</TableCell>
                  <TableCell>{offer.capacity_gb ?? ""}</TableCell>
                  <TableCell>{offer.grade || "—"}</TableCell>
                  <TableCell>{offer.lock_state}</TableCell>
                  <TableCell>{offer.quantity ?? ""}</TableCell>
                  <TableCell>{offer.unit_price ?? ""}</TableCell>
                  <TableCell>{offer.currency}</TableCell>
                  <TableCell>
                    <span className="flex flex-col items-start gap-1">
                      <StatusBadge status={offer.status} />
                      {offer.exclusion_reason ? (
                        <span className="text-xs text-muted-foreground">
                          {offer.exclusion_reason}
                        </span>
                      ) : null}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Questions ({reviews.length} open)</h2>
        {reviews.length === 0 ? (
          <p className="text-sm text-muted-foreground">No open questions.</p>
        ) : (
          <div className="space-y-3">
            {reviews.map((review) =>
              canOperate ? (
                <AnswerForm key={review.id} review={review} onAnswered={() => void load()} />
              ) : (
                <Card key={review.id}>
                  <CardContent className="pt-5 text-sm">
                    <p className="font-medium">{review.field}</p>
                    <p className="mt-1">{review.question}</p>
                  </CardContent>
                </Card>
              ),
            )}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">Customer quote</h2>
        {quote ? (
          <FxPairLine
            fx={quote.fx}
            organizationId={run?.organization ?? null}
            datetimeFormat={prefs.datetime_format}
          />
        ) : null}
        {!quote || quote.lines.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No quotable lines yet — quotes need validated offers.
            {(quote?.notes ?? []).map((note) => (
              <span key={note} className="block">{note}</span>
            ))}
          </p>
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Brand</TableHead>
                  <TableHead>Model</TableHead>
                  <TableHead>GB</TableHead>
                  <TableHead>Grade</TableHead>
                  <TableHead>Supplier</TableHead>
                  <TableHead>Qty</TableHead>
                  <TableHead>Unit price</TableHead>
                  <TableHead>Cur</TableHead>
                  <TableHead>Rule</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {quote.lines.map((line, i) => (
                  <TableRow key={`${line.supplier}-${line.model}-${i}`}>
                    <TableCell>{line.brand}</TableCell>
                    <TableCell>{line.model}</TableCell>
                    <TableCell>{line.capacity_gb ?? ""}</TableCell>
                    <TableCell>{line.grade}</TableCell>
                    <TableCell>{line.supplier}</TableCell>
                    <TableCell>{line.quantity ?? ""}</TableCell>
                    <TableCell>{line.unit_price}</TableCell>
                    <TableCell>{line.currency}</TableCell>
                    <TableCell>{line.rule ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            {(quote.notes ?? []).map((note) => (
              <p key={note} className="text-sm text-muted-foreground">{note}</p>
            ))}
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" size="sm" onClick={() => void downloadQuote("xlsx")}>
                Download XLSX
              </Button>
              <Button variant="outline" size="sm" onClick={() => void downloadQuote("csv")}>
                Download CSV
              </Button>
            </div>
          </>
        )}
      </section>

      {canOperate ? (
        <section className="space-y-3">
          <h2 className="text-lg font-semibold">Actions</h2>
          <div className="flex flex-wrap gap-2">
            {run.status === "ready_for_review" ? (
              <Button disabled={busy} onClick={() => void act(() => approveRun(Number(id)))}>
                Approve
              </Button>
            ) : null}
            {run.status === "approved" || run.status === "completed" ? (
              <>
                <Button disabled={busy} onClick={() => void act(() => exportRun(Number(id), "xlsx"))}>
                  Export XLSX
                </Button>
                <Button
                  variant="outline"
                  disabled={busy}
                  onClick={() => void act(() => exportRun(Number(id), "csv"))}
                >
                  Export CSV
                </Button>
              </>
            ) : null}
            {["needs_input", "ready_for_review", "approved", "completed", "failed"].includes(
              run.status,
            ) ? (
              <Button variant="outline" disabled={busy} onClick={() => setRemapping(true)}>
                Remap columns
              </Button>
            ) : null}
            {ACTIVE.has(run.status) ||
            run.status === "needs_input" ||
            run.status === "ready_for_review" ? (
              <Button
                variant="destructive"
                disabled={busy}
                onClick={() => void act(() => cancelRun(Number(id)))}
              >
                Cancel run
              </Button>
            ) : null}
          </div>
        </section>
      ) : null}

      {run.artifacts.length > 0 ? (
        <section className="space-y-3">
          <h2 className="text-lg font-semibold">Downloads</h2>
          <ul className="space-y-1.5 text-sm">
            {run.artifacts.map((artifact) => (
              <li key={artifact.id}>
                <a
                  className="underline"
                  href={`/api/pipeline/artifacts/${artifact.id}/download/`}
                >
                  {artifact.format.toUpperCase()} — {artifact.size_bytes} bytes
                </a>{" "}
                <span className="text-muted-foreground">
                  {artifact.sha256.slice(0, 12)}…
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <RemapDialog
        run={run}
        open={remapping}
        onClose={() => setRemapping(false)}
        onDone={() => void load()}
      />
    </div>
  );
}

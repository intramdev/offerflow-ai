"use client";

import { ChevronDown } from "lucide-react";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { Pager } from "@/components/app/pager";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ApiError,
  getUsage,
  getUsageLog,
  releaseStuckSlots,
  type UsageImport,
  type UsageMatchCall,
  type UsageSummary,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/cn";
import { formatDate, formatDateTime, type DateTimeFormat } from "@/lib/format";

function formatCredits(n: number): string {
  return n.toLocaleString("en-US");
}

function taskLabel(task: string): string {
  switch (task) {
    case "match":
      return "Matching";
    case "categorize":
      return "Categories";
    case "assistant":
      return "Assistant";
    default:
      return task || "—";
  }
}

function RingGauge({ used, allowance }: { used: number; allowance: number }) {
  const radius = 84;
  const circumference = 2 * Math.PI * radius;
  const fraction = allowance > 0 ? Math.min(used / allowance, 1) : 0;
  const [shown, setShown] = React.useState(0);
  React.useEffect(() => {
    // Animate the sweep from empty to the real value on every load.
    setShown(0);
    const frame = requestAnimationFrame(() => setShown(fraction));
    return () => cancelAnimationFrame(frame);
  }, [fraction]);
  const danger = fraction >= 0.9;
  const warn = !danger && fraction >= 0.7;
  const stroke = danger ? "stroke-destructive" : warn ? "stroke-amber-500" : "stroke-primary";
  return (
    <div className="relative mx-auto h-52 w-52" role="img"
      aria-label={`${formatCredits(used)} of ${formatCredits(allowance)} credits used`}>
      <svg viewBox="0 0 200 200" className="h-full w-full -rotate-90">
        <circle cx="100" cy="100" r={radius} fill="none"
          className="stroke-muted" strokeWidth="16" />
        <circle cx="100" cy="100" r={radius} fill="none"
          className={cn(stroke, "transition-[stroke-dashoffset] duration-1000 ease-out")}
          strokeWidth="16" strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={circumference * (1 - shown)} />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-3xl font-bold tabular-nums text-foreground">
          {formatCredits(allowance - used > 0 ? allowance - used : 0)}
        </span>
        <span className="text-xs text-muted-foreground">credits left</span>
        <span className="mt-1 text-xs tabular-nums text-muted-foreground">
          {formatCredits(used)} / {formatCredits(allowance)} used
        </span>
      </div>
    </div>
  );
}

export default function CreditsPage() {
  const { active, prefs, canOperate } = useOrg();
  const [releasing, setReleasing] = React.useState(false);
  const [releasedNote, setReleasedNote] = React.useState<string | null>(null);
  const [usage, setUsage] = React.useState<UsageSummary | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [openRuns, setOpenRuns] = React.useState<Set<number>>(new Set());
  const [importsPage, setImportsPage] = React.useState(1);
  const [importsPages, setImportsPages] = React.useState(1);
  const [importsBusy, setImportsBusy] = React.useState(false);
  const [matchingPage, setMatchingPage] = React.useState(1);
  const [matchingPages, setMatchingPages] = React.useState(1);
  const [matchingBusy, setMatchingBusy] = React.useState(false);
  const format: DateTimeFormat = prefs.datetime_format;

  React.useEffect(() => {
    if (!active) {
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void getUsage(active.organization)
      .then((data) => {
        if (cancelled) return;
        setUsage(data);
        setImportsPage(1);
        setImportsPages(Math.max(1, Math.ceil(data.imports_count / data.log_page_size)));
        setMatchingPage(1);
        setMatchingPages(Math.max(1, Math.ceil(data.matching_count / data.log_page_size)));
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load credit usage.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  if (!active) {
    return <p className="text-sm text-muted-foreground">Select an organization to see its credits.</p>;
  }
  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading credits…</p>;
  }
  if (error || !usage) {
    return <ErrorNotice message={error ?? "Could not load credit usage."} />;
  }

  function toggle(runId: number) {
    setOpenRuns((prev) => {
      const next = new Set(prev);
      if (next.has(runId)) next.delete(runId);
      else next.add(runId);
      return next;
    });
  }

  async function releaseStuck() {
    if (!active) return;
    setReleasing(true);
    setReleasedNote(null);
    try {
      const data = await releaseStuckSlots(active.organization);
      const settled = data.swept.uncertain + data.swept.expired;
      const live = data.live_reservations.length;
      setReleasedNote(
        settled > 0
          ? `Settled ${settled} expired hold${settled === 1 ? "" : "s"} for ${active.organization_name} (${data.swept.tokens_booked} tokens booked as uncertain spend). ${live} live hold${live === 1 ? " was" : "s were"} left untouched — you can retry now.`
          : live > 0
            ? `No expired holds for ${active.organization_name} — ${live} live hold${live === 1 ? " is" : "s are"} still running and were left untouched.`
            : `No stuck slots for ${active.organization_name} — nothing needed releasing.`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not release the slot.");
    } finally {
      setReleasing(false);
    }
  }

  async function loadLogPage(section: "imports" | "matching", page: number) {
    if (!active || !usage) return;
    if (section === "imports") setImportsBusy(true);
    else setMatchingBusy(true);
    try {
      const data = await getUsageLog(active.organization, section, page);
      if (section === "imports") {
        setUsage({ ...usage, imports: data.results as UsageImport[] });
        setImportsPage(data.page);
        setImportsPages(data.pages);
      } else {
        setUsage({ ...usage, matching: data.results as UsageMatchCall[] });
        setMatchingPage(data.page);
        setMatchingPages(data.pages);
      }
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load that log page.");
    } finally {
      if (section === "imports") setImportsBusy(false);
      else setMatchingBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-bold text-foreground">AI credits</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          One credit is one AI token. Your allowance renews every billing
          cycle; the grand total below never resets.
        </p>
      </div>

      {usage.paused && (
        <ErrorNotice message="AI processing is paused for this company. Imports run without AI enrichment until an administrator resumes it." />
      )}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
        <Card>
          <CardHeader>
            <CardTitle>This cycle</CardTitle>
            <CardDescription>
              Resets {formatDate(usage.reset_date + "T00:00:00", format)}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <RingGauge used={usage.used} allowance={usage.allowance} />
          </CardContent>
        </Card>

        <div className="grid content-start gap-4 sm:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Monthly allowance
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold tabular-nums">
                {formatCredits(usage.allowance)}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Used this cycle
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-2xl font-bold tabular-nums">
                {formatCredits(usage.used)}
              </p>
            </CardContent>
          </Card>
          <Card className="sm:col-span-2">
            <CardHeader>
              <CardTitle className="text-sm font-medium text-muted-foreground">
                Grand total, all time
              </CardTitle>
              <CardDescription>Never resets</CardDescription>
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-bold tabular-nums">
                {formatCredits(usage.grand_total)}
              </p>
            </CardContent>
          </Card>
        </div>
      </div>

      {canOperate ? (
        <Card>
          <CardHeader>
            <CardTitle>Stuck AI requests?</CardTitle>
            <CardDescription>
              If requests keep failing with “No free in-flight slot” while
              nobody is working, settle {active.organization_name}’s expired
              holds here. This affects only your company, never touches
              live requests, and books expired uncertain holds as spend
              (the provider may have processed them).
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap items-center gap-3">
              <Button
                variant="outline"
                onClick={() => void releaseStuck()}
                disabled={releasing}
              >
                {releasing ? "Releasing…" : "Release stuck slot"}
              </Button>
              {releasedNote ? (
                <span className="text-sm text-muted-foreground">{releasedNote}</span>
              ) : null}
            </div>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Usage by import</CardTitle>
          <CardDescription>
            Expand an import to see every AI request it made. Newest first,
            {usage.log_page_size} per page — entries older than {usage.log_retention_days} days
            are removed automatically.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {usage.imports.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No AI requests yet. Imports processed with AI enrichment will
              appear here with their credit cost.
            </p>
          ) : (
            <>
            <ul className="divide-y divide-border">
              {usage.imports.map((entry) => {
                const open = openRuns.has(entry.run_id);
                return (
                  <li key={entry.run_id}>
                    <button
                      type="button"
                      onClick={() => toggle(entry.run_id)}
                      aria-expanded={open}
                      className="flex w-full items-center gap-3 py-3 text-left"
                    >
                      <ChevronDown
                        className={cn(
                          "h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                          !open && "-rotate-90",
                        )}
                      />
                      <span className="min-w-0 flex-1 truncate text-sm font-medium">
                        {entry.filename}
                      </span>
                      <Badge variant="secondary">{entry.run_status}</Badge>
                      <span className="hidden text-xs text-muted-foreground sm:inline">
                        {formatDateTime(entry.created_at, format)}
                      </span>
                      <span className="text-sm tabular-nums text-muted-foreground">
                        {entry.requests} {entry.requests === 1 ? "request" : "requests"}
                      </span>
                      <span className="w-28 text-right text-sm font-semibold tabular-nums">
                        {formatCredits(entry.tokens)}
                      </span>
                    </button>
                    {open && (
                      <div className="overflow-x-auto pb-4 pl-7">
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="border-b border-border text-left text-xs text-muted-foreground">
                              <th className="py-1 pr-4 font-medium">Time</th>
                              <th className="py-1 pr-4 font-medium">Model</th>
                              <th className="py-1 pr-4 text-right font-medium">In</th>
                              <th className="py-1 pr-4 text-right font-medium">Out</th>
                              <th className="py-1 text-right font-medium">Total</th>
                            </tr>
                          </thead>
                          <tbody>
                            {entry.calls.map((call, index) => (
                              <tr key={`${entry.run_id}-${index}`} className="border-b border-border/50 tabular-nums">
                                <td className="py-1 pr-4 text-xs text-muted-foreground">
                                  {formatDateTime(call.at, format)}
                                </td>
                                <td className="max-w-48 truncate py-1 pr-4 text-xs">
                                  {call.model || "—"}
                                </td>
                                <td className="py-1 pr-4 text-right">
                                  {formatCredits(call.prompt_tokens)}
                                </td>
                                <td className="py-1 pr-4 text-right">
                                  {formatCredits(call.completion_tokens)}
                                </td>
                                <td className="py-1 text-right font-medium">
                                  {formatCredits(call.total)}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
            <div className="mt-4">
              <Pager
                page={importsPage}
                total={importsPages}
                busy={importsBusy}
                label="Import log pages"
                onPage={(page) => void loadLogPage("imports", page)}
              />
            </div>
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>AI requests beyond imports</CardTitle>
          <CardDescription>
            Matching, categorization, and assistant chats, newest first.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {(usage.matching ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No requests yet. Matching, categorization, and assistant chats
              land here with their credit cost.
            </p>
          ) : (
            <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="py-1 pr-4 font-medium">Time</th>
                    <th className="py-1 pr-4 font-medium">Type</th>
                    <th className="py-1 pr-4 font-medium">Detail</th>
                    <th className="py-1 pr-4 font-medium">Model</th>
                    <th className="py-1 pr-4 text-right font-medium">In</th>
                    <th className="py-1 pr-4 text-right font-medium">Out</th>
                    <th className="py-1 text-right font-medium">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {(usage.matching ?? []).map((call, index) => (
                    <tr key={index} className="border-b border-border/50 tabular-nums">
                      <td className="py-1 pr-4 text-xs text-muted-foreground">
                        {formatDateTime(call.at, format)}
                      </td>
                      <td className="py-1 pr-4 text-xs">
                        {taskLabel(call.task)}
                      </td>
                      <td className="max-w-48 truncate py-1 pr-4 text-xs">
                        {call.label || "—"}
                      </td>
                      <td className="max-w-48 truncate py-1 pr-4 text-xs">
                        {call.model || "—"}
                      </td>
                      <td className="py-1 pr-4 text-right">
                        {formatCredits(call.prompt_tokens)}
                      </td>
                      <td className="py-1 pr-4 text-right">
                        {formatCredits(call.completion_tokens)}
                      </td>
                      <td className="py-1 text-right font-medium">
                        {formatCredits(call.total)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="mt-4">
              <Pager
                page={matchingPage}
                total={matchingPages}
                busy={matchingBusy}
                label="AI request log pages"
                onPage={(page) => void loadLogPage("matching", page)}
              />
            </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

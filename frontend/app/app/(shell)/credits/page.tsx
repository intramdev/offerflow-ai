"use client";

import { ChevronDown } from "lucide-react";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, getUsage, type UsageSummary } from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatDate, formatDateTime, type DateTimeFormat } from "@/lib/format";

function formatCredits(n: number): string {
  return n.toLocaleString("en-US");
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
  const { active, prefs } = useOrg();
  const [usage, setUsage] = React.useState<UsageSummary | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [openRuns, setOpenRuns] = React.useState<Set<number>>(new Set());
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
        if (!cancelled) setUsage(data);
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

      <Card>
        <CardHeader>
          <CardTitle>Usage by import</CardTitle>
          <CardDescription>
            Expand an import to see every AI request it made.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {usage.imports.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No AI requests yet. Imports processed with AI enrichment will
              appear here with their credit cost.
            </p>
          ) : (
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
          )}
        </CardContent>
      </Card>
    </div>
  );
}

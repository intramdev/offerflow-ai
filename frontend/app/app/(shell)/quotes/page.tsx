"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { StatusBadge } from "@/components/app/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  ApiError,
  listCustomerQuotes,
  startCustomerQuote,
  type CustomerQuote,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function QuotesPage() {
  const { active, prefs, canOperate } = useOrg();
  const router = useRouter();
  const [quotes, setQuotes] = React.useState<CustomerQuote[] | null>(null);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const reload = React.useCallback(async () => {
    if (!active) return;
    try {
      setQuotes(await listCustomerQuotes(active.organization));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load quotes.");
    }
  }, [active]);

  React.useEffect(() => {
    void reload();
  }, [reload]);

  async function generate() {
    if (!active) return;
    setError("");
    setBusy(true);
    try {
      const quote = await startCustomerQuote(active.organization);
      router.push(`/app/quotes/${quote.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not start a quote.");
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Customer quotes</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Match the latest approved run per supplier into products, reconcile
            the uncertain ones, then generate the price list and its Telegram
            message.
          </p>
        </div>
        {canOperate ? (
          <Button onClick={() => void generate()} disabled={busy || !active}>
            {busy ? "Matching…" : "Generate quote"}
          </Button>
        ) : null}
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      <Card>
        <CardHeader>
          <CardTitle>Quotes</CardTitle>
        </CardHeader>
        <CardContent>
          {quotes === null ? (
            <p className="text-sm text-muted-foreground">Loading quotes…</p>
          ) : quotes.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No quotes yet. Approve at least one supplier import, then generate
              your first customer quote.
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {quotes.map((quote) => (
                <li key={quote.id} className="flex items-center justify-between gap-4 py-3">
                  <div className="min-w-0">
                    <Link
                      href={`/app/quotes/${quote.id}`}
                      className="font-medium underline-offset-4 hover:underline"
                    >
                      Quote #{quote.id}
                    </Link>
                    <p className="mt-0.5 truncate text-xs text-muted-foreground">
                      {formatDateTime(quote.created_at, prefs.datetime_format)}
                      {" · "}
                      {quote.sources.map((s) => s.filename).join(", ") || "no sources"}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-3">
                    <span className="text-xs text-muted-foreground">
                      {quote.status === "ready"
                        ? `${quote.lines.length} lines`
                        : `${quote.groups.length} groups`}
                    </span>
                    <StatusBadge status={quote.status} />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { Pager } from "@/components/app/pager";
import { StatusBadge } from "@/components/app/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import {
  ApiError,
  deleteCustomerQuote,
  listCustomerQuotesPage,
  startCustomerQuote,
  type CustomerQuote,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";

export default function QuotesPage() {
  const { active, prefs, canOperate } = useOrg();
  const router = useRouter();
  const [quotes, setQuotes] = React.useState<CustomerQuote[] | null>(null);
  const [page, setPage] = React.useState(1);
  const [pages, setPages] = React.useState(1);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [pending, setPending] = React.useState<CustomerQuote | null>(null);
  const [deleting, setDeleting] = React.useState(false);

  const reload = React.useCallback(
    async (wanted: number) => {
      if (!active) return;
      setLoading(true);
      try {
        const paged = await listCustomerQuotesPage(
          active.organization,
          wanted,
        );
        setQuotes(paged.results);
        setPage(paged.page);
        setPages(paged.pages);
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Could not load quotes.");
      } finally {
        setLoading(false);
      }
    },
    [active],
  );

  React.useEffect(() => {
    setPage(1);
    void reload(1);
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

  async function confirmDelete() {
    if (!pending) return;
    setDeleting(true);
    setError("");
    try {
      await deleteCustomerQuote(pending.id);
      setPending(null);
      const remaining = (quotes ?? []).filter((row) => row.id !== pending.id);
      if (remaining.length === 0 && page > 1) {
        void reload(page - 1);
      } else {
        setQuotes(remaining);
      }
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete the quote.",
      );
    } finally {
      setDeleting(false);
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
            message. Newest first, 50 per page.
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
        <CardContent className="space-y-4">
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
                    {canOperate ? (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setPending(quote)}
                      >
                        Delete
                      </Button>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}
          {quotes !== null && quotes.length > 0 ? (
            <Pager
              page={page}
              total={pages}
              onPage={(wanted) => void reload(wanted)}
              label="Quotes pages"
              busy={loading}
            />
          ) : null}
        </CardContent>
      </Card>

      <Dialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title="Delete this quote?"
        description={`Quote #${pending?.id} and its groups, lines, and labels will be removed. Supplier runs and learned matches stay, so you can generate a fresh quote any time. This cannot be undone.`}
      >
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={() => setPending(null)}>
            Cancel
          </Button>
          <Button
            variant="destructive"
            onClick={() => void confirmDelete()}
            disabled={deleting}
          >
            {deleting ? "Deleting…" : "Delete"}
          </Button>
        </div>
      </Dialog>
    </div>
  );
}

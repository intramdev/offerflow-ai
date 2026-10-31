"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { StatusBadge } from "@/components/app/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
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
  confirmCustomerQuote,
  download,
  getCustomerQuote,
  getCustomerQuoteMessage,
  listCategories,
  saveBlob,
  type Category,
  type CustomerQuote,
  type QuoteGroup,
  type QuoteGroupMember,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";

function sourceBadge(source: string, confidence: number) {
  switch (source) {
    case "memory":
      return <Badge variant="default">Learned</Badge>;
    case "normalized":
      return <Badge variant="default">Auto</Badge>;
    case "llm":
      return <Badge variant="default">Model {confidence.toFixed(2)}</Badge>;
    case "manual":
      return <Badge variant="secondary">Manual</Badge>;
    default:
      return <Badge variant="warning">Needs review</Badge>;
  }
}

interface DraftGroup {
  key: string;
  canonical: string;
  offerIds: number[];
  brand: string;
  capacity: number | null;
  grade: string;
  source: string;
  confidence: number;
  needsReview: boolean;
  suggestion?: string;
  categoryId: number | null;
  categoryName: string;
  categorySource: string;
  categoryReview: boolean;
  categorySuggestion?: string;
}

function sameBlock(a: DraftGroup, b: DraftGroup): boolean {
  return (
    a.brand.trim().toLowerCase() === b.brand.trim().toLowerCase() &&
    a.capacity === b.capacity &&
    a.grade.trim().toLowerCase() === b.grade.trim().toLowerCase()
  );
}

function categoryBadge(group: DraftGroup) {
  if (group.categoryReview) {
    return <Badge variant="warning">Pick a category</Badge>;
  }
  if (!group.categoryName) return null;
  switch (group.categorySource) {
    case "file":
      return <Badge variant="default">File: {group.categoryName}</Badge>;
    case "alias_memory":
      return <Badge variant="default">Learned: {group.categoryName}</Badge>;
    case "auto":
      return <Badge variant="default">Auto: {group.categoryName}</Badge>;
    case "manual":
      return <Badge variant="secondary">{group.categoryName}</Badge>;
    default:
      return <Badge variant="secondary">{group.categoryName}</Badge>;
  }
}

function ReconcileView({
  quote,
  onConfirmed,
}: {
  quote: CustomerQuote;
  onConfirmed: (quote: CustomerQuote) => void;
}) {
  const { canOperate } = useOrg();
  const members = React.useMemo(() => {
    const map = new Map<number, QuoteGroupMember>();
    for (const group of quote.groups) {
      for (const member of group.members ?? []) {
        map.set(member.id, member);
      }
    }
    return map;
  }, [quote.groups]);
  const [draft, setDraft] = React.useState<DraftGroup[]>(() =>
    quote.groups.map((group, index) => ({
      key: `g${index}`,
      canonical: group.canonical_model,
      offerIds: [...group.offers],
      brand: group.brand,
      capacity: group.capacity_gb,
      grade: group.grade ?? "",
      source: group.source,
      confidence: group.confidence,
      needsReview: group.needs_review,
      suggestion: group.suggestion,
      categoryId: group.category_id ?? null,
      categoryName: group.category_name ?? "",
      categorySource: group.category_source ?? "none",
      categoryReview: group.category_review ?? false,
      categorySuggestion: group.category_suggestion,
    })),
  );
  const [selected, setSelected] = React.useState<string[]>([]);
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [categories, setCategories] = React.useState<Category[]>([]);
  const keyCounter = React.useRef(quote.groups.length);

  React.useEffect(() => {
    let cancelled = false;
    void listCategories(quote.organization).then(
      (rows) => {
        if (!cancelled) setCategories(rows);
      },
      () => {
        if (!cancelled) setCategories([]);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [quote.organization]);

  function toggle(key: string) {
    setSelected((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  }

  function rename(key: string, canonical: string) {
    setDraft((prev) =>
      prev.map((group) => (group.key === key ? { ...group, canonical } : group)),
    );
  }

  function split(key: string) {
    setDraft((prev) =>
      prev.flatMap((group) => {
        if (group.key !== key || group.offerIds.length < 2) return [group];
        return group.offerIds.map((offerId) => {
          keyCounter.current += 1;
          return {
            key: `g${keyCounter.current}`,
            canonical: members.get(offerId)?.model_name ?? group.canonical,
            offerIds: [offerId],
            brand: group.brand,
            capacity: group.capacity,
            grade: group.grade,
            source: "manual",
            confidence: 1,
            needsReview: false,
            categoryId: group.categoryId,
            categoryName: group.categoryName,
            categorySource: group.categorySource,
            categoryReview: group.categoryReview,
            categorySuggestion: group.categorySuggestion,
          } satisfies DraftGroup;
        });
      }),
    );
    setSelected((prev) => prev.filter((k) => k !== key));
  }

  function setCategory(key: string, value: string) {
    const id = value === "" ? null : Number(value);
    setDraft((prev) =>
      prev.map((group) => {
        if (group.key !== key) return group;
        const shelf = categories.find((row) => row.id === id);
        return {
          ...group,
          categoryId: id,
          categoryName: shelf ? shelf.name : "",
          categorySource: "manual",
          categoryReview: false,
          categorySuggestion: undefined,
        };
      }),
    );
  }

  const chosen = draft.filter((group) => selected.includes(group.key));
  const mergeable =
    chosen.length >= 2 && chosen.every((group) => sameBlock(group, chosen[0]));

  function merge() {
    if (!mergeable) return;
    keyCounter.current += 1;
    const first = chosen[0];
    const unanimousCategory = chosen.every(
      (group) => group.categoryId === first.categoryId,
    );
    const merged: DraftGroup = {
      key: `g${keyCounter.current}`,
      canonical: first.canonical,
      offerIds: chosen.flatMap((group) => group.offerIds),
      brand: first.brand,
      capacity: first.capacity,
      grade: first.grade,
      source: "manual",
      confidence: 1,
      needsReview: false,
      categoryId: unanimousCategory ? first.categoryId : null,
      categoryName: unanimousCategory ? first.categoryName : "",
      categorySource: unanimousCategory ? first.categorySource : "none",
      categoryReview:
        categories.length > 0 &&
        (!unanimousCategory ||
          chosen.some((group) => group.categoryReview)),
      categorySuggestion: undefined,
    };
    setDraft((prev) => [
      ...prev.filter((group) => !selected.includes(group.key)),
      merged,
    ]);
    setSelected([]);
  }

  async function confirm() {
    const blank = draft.find((group) => group.canonical.trim() === "");
    if (blank) {
      setError("Every group needs a product name before confirming.");
      return;
    }
    setError("");
    setBusy(true);
    try {
      const confirmed = await confirmCustomerQuote(
        quote.id,
        draft.map((group) => ({
          canonical_model: group.canonical.trim(),
          offer_ids: group.offerIds,
          category_id: group.categoryId,
        })),
      );
      onConfirmed(confirmed);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not confirm.");
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          {draft.filter((g) => g.needsReview || g.categoryReview).length === 0
            ? "Every product matched automatically. Confirm to price the quote."
            : "Check the groups flagged for review: rename, split, merge, or shelve them, then confirm."}
        </p>
        {canOperate ? (
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!mergeable}
              onClick={merge}
              title={
                chosen.length >= 2 && !mergeable
                  ? "Only groups with the same brand, capacity, and grade can merge."
                  : "Merge the selected groups"
              }
            >
              Merge selected ({selected.length})
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void confirm()}>
              {busy ? "Confirming…" : "Confirm & price"}
            </Button>
          </div>
        ) : null}
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      {quote.notes.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Matching notes</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {quote.notes.map((note, index) => (
                <li key={index}>{note}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <div className="space-y-3">
        {draft.map((group) => (
          <Card key={group.key}>
            <CardContent className="space-y-3 pt-4">
              <div className="flex flex-wrap items-center gap-3">
                {canOperate ? (
                  <input
                    type="checkbox"
                    aria-label={`Select ${group.canonical || "group"}`}
                    checked={selected.includes(group.key)}
                    onChange={() => toggle(group.key)}
                    className="h-4 w-4 accent-primary"
                  />
                ) : null}
                <div className="min-w-52 flex-1">
                  {canOperate ? (
                    <Input
                      value={group.canonical}
                      onChange={(event) => rename(group.key, event.target.value)}
                      aria-label="Product name"
                    />
                  ) : (
                    <p className="font-medium">{group.canonical}</p>
                  )}
                </div>
                {sourceBadge(group.source, group.confidence)}
                {categoryBadge(group)}
                <span className="text-xs text-muted-foreground">
                  {group.brand}
                  {group.capacity !== null ? ` · ${group.capacity}GB` : ""}
                  {group.grade ? ` · grade ${group.grade}` : ""}
                </span>
                {canOperate && categories.length > 0 ? (
                  <Select
                    value={group.categoryId === null ? "" : String(group.categoryId)}
                    onChange={(event) => setCategory(group.key, event.target.value)}
                    aria-label={`Category for ${group.canonical || "group"}`}
                    className="h-8 w-44 py-0 text-xs"
                  >
                    <option value="">No category</option>
                    {categories.map((shelf) => (
                      <option key={shelf.id} value={shelf.id}>
                        {shelf.name}
                      </option>
                    ))}
                  </Select>
                ) : null}
                {canOperate && group.offerIds.length > 1 ? (
                  <Button variant="outline" size="sm" onClick={() => split(group.key)}>
                    Split
                  </Button>
                ) : null}
              </div>
              {group.suggestion ? (
                <p className="text-xs text-muted-foreground">
                  Model suggestion: {group.suggestion}
                </p>
              ) : null}
              {group.categorySuggestion ? (
                <p className="text-xs text-muted-foreground">
                  Model suggestion: {group.categorySuggestion}
                </p>
              ) : null}
              <ul className="divide-y divide-border rounded-md border border-border">
                {group.offerIds.map((offerId) => {
                  const member = members.get(offerId);
                  if (!member) return null;
                  return (
                    <li
                      key={offerId}
                      className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 text-sm"
                    >
                      <span>
                        <span className="font-medium">{member.supplier || "—"}</span>
                        <span className="text-muted-foreground">
                          {" · "}
                          {member.model_name || "—"}
                          {member.grade ? ` (${member.grade})` : ""}
                        </span>
                      </span>
                      <span className="tabular-nums">
                        {member.currency} {member.unit_price}
                      </span>
                    </li>
                  );
                })}
              </ul>
            </CardContent>
          </Card>
        ))}
      </div>
    </div>
  );
}

function shelfSlug(name: string): string {
  const slug = name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return slug || "category";
}

function ReadyView({ quote }: { quote: CustomerQuote }) {
  const [error, setError] = React.useState("");
  const [copied, setCopied] = React.useState(false);
  const [shelf, setShelf] = React.useState("");
  const [shelfMessage, setShelfMessage] = React.useState<string | null>(null);
  const [messageLoading, setMessageLoading] = React.useState(false);
  const categorized = quote.lines.some((line) => line.category_name);
  const shelves = React.useMemo(() => {
    const seen = new Map<number, string>();
    for (const line of quote.lines) {
      if (line.category_id !== null && !seen.has(line.category_id)) {
        seen.set(line.category_id, line.category_name || "Category");
      }
    }
    return [...seen.entries()];
  }, [quote.lines]);
  const hasUncategorized = quote.lines.some(
    (line) => line.category_id === null,
  );
  const showShelfPicker =
    shelves.length > 1 || (shelves.length === 1 && hasUncategorized);
  const visibleLines =
    shelf === ""
      ? quote.lines
      : shelf === "none"
        ? quote.lines.filter((line) => line.category_id === null)
        : quote.lines.filter((line) => line.category_id === Number(shelf));
  const message = shelf === "" ? quote.message : (shelfMessage ?? "");

  React.useEffect(() => {
    if (shelf === "") {
      setShelfMessage(null);
      return;
    }
    let cancelled = false;
    setMessageLoading(true);
    setShelfMessage(null);
    const target = shelf === "none" ? "none" : Number(shelf);
    void getCustomerQuoteMessage(quote.id, target).then(
      (loaded) => {
        if (!cancelled) {
          setShelfMessage(loaded.message);
          setMessageLoading(false);
        }
      },
      (err: unknown) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError ? err.message : "Could not load message.",
          );
          setMessageLoading(false);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [shelf, quote.id]);

  function shelfSuffix(): string {
    if (shelf === "") return "";
    if (shelf === "none") return "-uncategorized";
    const name = shelves.find(([id]) => id === Number(shelf))?.[1] ?? "";
    return `-${shelfSlug(name)}`;
  }

  async function downloadFile(format: "xlsx" | "csv") {
    setError("");
    try {
      const query = shelf === "" ? "" : `?category_id=${shelf}`;
      const blob = await download(
        `/api/pipeline/quotes/${quote.id}/download/${format}/${query}`,
      );
      saveBlob(
        blob,
        `offerflow-customer-quote-${quote.id}${shelfSuffix()}.${format}`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Download failed.");
    }
  }

  async function copyMessage() {
    try {
      await navigator.clipboard.writeText(message);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("Could not copy. Select the text manually.");
    }
  }

  return (
    <div className="space-y-4">
      {error ? <ErrorNotice message={error} /> : null}
      {showShelfPicker ? (
        <Card>
          <CardContent className="flex flex-wrap items-center gap-3 pt-4">
            <label
              htmlFor="quote-shelf"
              className="text-sm font-medium text-muted-foreground"
            >
              Show, message, and download
            </label>
            <Select
              id="quote-shelf"
              value={shelf}
              onChange={(event) => setShelf(event.target.value)}
              className="w-56"
            >
              <option value="">All categories</option>
              {shelves.map(([id, name]) => (
                <option key={id} value={id}>
                  {name} only
                </option>
              ))}
              {hasUncategorized ? (
                <option value="none">Uncategorized only</option>
              ) : null}
            </Select>
          </CardContent>
        </Card>
      ) : null}
      <Card>
        <CardHeader>
          <CardTitle>Lines</CardTitle>
        </CardHeader>
        <CardContent>
          {visibleLines.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Every group was skipped — see the notes below.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Product</TableHead>
                  {categorized ? <TableHead>Category</TableHead> : null}
                  <TableHead>Grade</TableHead>
                  <TableHead>Supplier</TableHead>
                  <TableHead>Qty</TableHead>
                  <TableHead>Price</TableHead>
                  <TableHead>Rule</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleLines.map((line, index) => (
                  <TableRow key={index}>
                    <TableCell>
                      {line.brand} {line.model}
                      {line.capacity_gb !== null ? ` ${line.capacity_gb}GB` : ""}
                    </TableCell>
                    {categorized ? (
                      <TableCell>{line.category_name || "—"}</TableCell>
                    ) : null}
                    <TableCell>{line.grade || "—"}</TableCell>
                    <TableCell>
                      {line.supplier}
                      {(line.candidates ?? []).length > 1 ? (
                        <span className="block text-xs text-muted-foreground">
                          also:{" "}
                          {(line.candidates ?? [])
                            .filter((c) => c.supplier !== line.supplier)
                            .map((c) => `${c.supplier} ${c.currency} ${c.unit_price}`)
                            .join(" · ")}
                        </span>
                      ) : null}
                    </TableCell>
                    <TableCell>{line.quantity ?? "—"}</TableCell>
                    <TableCell className="tabular-nums">
                      {line.currency} {line.unit_price}
                    </TableCell>
                    <TableCell>{line.rule ?? "—"}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {quote.notes.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Notes</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {quote.notes.map((note, index) => (
                <li key={index}>{note}</li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <div className="flex items-center justify-between gap-3">
            <CardTitle>Telegram message</CardTitle>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => void copyMessage()}>
                {copied ? "Copied" : "Copy"}
              </Button>
              <Button variant="outline" size="sm" onClick={() => void downloadFile("xlsx")}>
                XLSX
              </Button>
              <Button variant="outline" size="sm" onClick={() => void downloadFile("csv")}>
                CSV
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <pre className="whitespace-pre-wrap rounded-md bg-muted p-4 font-sans text-sm">
            {messageLoading ? "Loading message…" : message}
          </pre>
        </CardContent>
      </Card>
    </div>
  );
}

export default function QuoteDetailPage() {
  const params = useParams();
  const id = Number(params.id);
  const { prefs } = useOrg();
  const [quote, setQuote] = React.useState<CustomerQuote | null>(null);
  const [error, setError] = React.useState("");

  React.useEffect(() => {
    let cancelled = false;
    void getCustomerQuote(id).then(
      (loaded) => {
        if (!cancelled) setQuote(loaded);
      },
      (err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load quote.");
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (!quote && !error) {
    return <p className="text-sm text-muted-foreground">Loading quote…</p>;
  }
  if (!quote) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">Quote not found</h1>
        {error ? <ErrorNotice message={error} /> : null}
        <p className="text-sm">
          <Link href="/app/quotes" className="underline">
            Back to quotes
          </Link>
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold">Quote #{quote.id}</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {formatDateTime(quote.created_at, prefs.datetime_format)}
            {" · from "}
            {quote.sources.map((s) => s.filename).join(", ") || "no sources"}
            {quote.simulated ? " · matching ran locally" : ""}
          </p>
        </div>
        <StatusBadge status={quote.status} />
      </div>

      {quote.status === "ready" ? (
        <ReadyView quote={quote} />
      ) : (
        <ReconcileView quote={quote} onConfirmed={setQuote} />
      )}
    </div>
  );
}

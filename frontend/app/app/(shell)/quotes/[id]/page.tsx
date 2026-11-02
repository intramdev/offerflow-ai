"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { StatusBadge } from "@/components/app/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
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
  deleteCustomerQuote,
  download,
  getCustomerQuote,
  getCustomerQuoteMessage,
  listCategories,
  saveBlob,
  updateQuoteLines,
  type Category,
  type CustomerQuote,
  type QuoteGroup,
  type QuoteGroupMember,
  type QuoteLineEdit,
} from "@/lib/api";
import { formatDateTime, formatMoney } from "@/lib/format";

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
  color: string;
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
    a.grade.trim().toLowerCase() === b.grade.trim().toLowerCase() &&
    a.color.trim().toLowerCase() === b.color.trim().toLowerCase()
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
      color: group.color ?? "",
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
            color: group.color,
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
      color: first.color,
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
                  ? "Only groups with the same brand, capacity, grade, and color can merge."
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
                  {group.color ? ` · ${group.color}` : ""}
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
                          {member.color ? ` · ${member.color}` : ""}
                          {member.grade ? ` (${member.grade})` : ""}
                        </span>
                      </span>
                      <span className="tabular-nums">
                        {member.currency} {formatMoney(member.unit_price)}
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

interface LineDraft {
  label: string;
  hidden: boolean;
  grade: string;
  color: string;
  categoryId: number | null;
  categoryName: string;
  supplier: string;
  quantity: string;
  unitPrice: string;
  manualPrice: boolean;
}

function toDrafts(quote: CustomerQuote): LineDraft[] {
  return quote.lines.map((line, index) => {
    const key = line.id ?? String(index);
    const edit = quote.line_edits?.[key] ?? quote.line_edits?.[String(index)];
    return {
      label: edit?.label ?? line.model,
      hidden: edit?.hidden ?? false,
      grade: edit?.grade ?? line.grade ?? "",
      color: edit?.color ?? line.color ?? "",
      categoryId:
        edit && "category_id" in edit ? (edit.category_id ?? null) : line.category_id,
      categoryName:
        edit && "category_id" in edit
          ? (edit.category_name ?? "")
          : (line.category_name ?? ""),
      supplier: edit?.supplier ?? line.supplier,
      quantity:
        edit?.quantity != null
          ? String(edit.quantity)
          : (line.quantity != null ? String(line.quantity) : ""),
      unitPrice: edit?.unit_price ?? line.unit_price,
      manualPrice: edit?.unit_price != null,
    };
  });
}

function ReadyView({
  quote,
  onChanged,
}: {
  quote: CustomerQuote;
  onChanged: (quote: CustomerQuote) => void;
}) {
  const { canOperate } = useOrg();
  const [error, setError] = React.useState("");
  const [copied, setCopied] = React.useState(false);
  const [shelf, setShelf] = React.useState("");
  const [liveMessage, setLiveMessage] = React.useState<string | null>(null);
  const [messageLoading, setMessageLoading] = React.useState(false);
  const [drafts, setDrafts] = React.useState<LineDraft[]>(() => toDrafts(quote));
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [categories, setCategories] = React.useState<Category[]>([]);
  // Saved-effective values (frozen lines + saved overrides): shelving,
  // filtering, and display all read from here, never from frozen lines.
  const effective = React.useMemo(() => toDrafts(quote), [quote]);
  const categorized =
    effective.some((row) => row.categoryName) ||
    (canOperate && categories.length > 0);
  const shelves = React.useMemo(() => {
    const seen = new Map<number, string>();
    for (const row of effective) {
      if (row.categoryId !== null && !seen.has(row.categoryId)) {
        seen.set(row.categoryId, row.categoryName || "Category");
      }
    }
    return [...seen.entries()];
  }, [effective]);
  const hasUncategorized = effective.some((row) => row.categoryId === null);
  const showShelfPicker =
    shelves.length > 1 || (shelves.length === 1 && hasUncategorized);
  const visibleIndexes =
    shelf === ""
      ? quote.lines.map((_, index) => index)
      : shelf === "none"
        ? effective
            .map((row, index) => (row.categoryId === null ? index : -1))
            .filter((index) => index >= 0)
        : effective
            .map((row, index) =>
              row.categoryId === Number(shelf) ? index : -1,
            )
            .filter((index) => index >= 0);
  const message = liveMessage ?? "";

  React.useEffect(() => {
    setDrafts(toDrafts(quote));
  }, [quote]);

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

  function setCell(index: number, patch: Partial<LineDraft>) {
    setSaved(false);
    setDrafts((prev) =>
      prev.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    );
  }

  React.useEffect(() => {
    let cancelled = false;
    setMessageLoading(true);
    setLiveMessage(null);
    const target =
      shelf === "" ? undefined : shelf === "none" ? "none" : Number(shelf);
    void getCustomerQuoteMessage(quote.id, target).then(
      (loaded) => {
        if (!cancelled) {
          setLiveMessage(loaded.message);
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
  }, [shelf, quote]);

  async function saveLines() {
    setError("");
    setSaved(false);
    setSaving(true);
    try {
      const edits: Record<string, QuoteLineEdit> = {};
      quote.lines.forEach((line, index) => {
        const draft = drafts[index] ?? effective[index];
        const label = draft.label.trim();
        if (label === "" && !draft.hidden) {
          throw new Error(
            `Line ${index + 1} needs words, or hide it from the exports.`,
          );
        }
        const qty = draft.quantity.trim();
        if (qty !== "" && !/^\d+$/.test(qty)) {
          throw new Error(
            `Line ${index + 1}: quantity must be a whole number.`,
          );
        }
        const price = draft.unitPrice.trim();
        if (price === "" || !Number.isFinite(Number(price))) {
          throw new Error(`Line ${index + 1} needs a numeric price.`);
        }
        // Every detail ships; the backend drops values that equal
        // the frozen line, so untouched rows store nothing.
        edits[String(index)] = {
          label: label === "" || label === line.model ? null : label,
          hidden: draft.hidden,
          grade: draft.grade.trim(),
          color: draft.color.trim(),
          category_id: draft.categoryId,
          quantity: qty === "" ? null : Number(qty),
          supplier: draft.supplier,
          unit_price: price,
        };
      });
      const updated = await updateQuoteLines(quote.id, edits);
      onChanged(updated);
      setSaved(true);
    } catch (err) {
      setError(
        err instanceof ApiError || err instanceof Error
          ? err.message
          : "Could not save lines.",
      );
    } finally {
      setSaving(false);
    }
  }

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
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle>Lines</CardTitle>
            {canOperate && visibleIndexes.length > 0 ? (
              <div className="flex items-center gap-3">
                {saved ? (
                  <span className="text-sm text-muted-foreground">Saved.</span>
                ) : null}
                <Button
                  size="sm"
                  disabled={saving}
                  onClick={() => void saveLines()}
                >
                  {saving ? "Saving…" : "Save changes"}
                </Button>
              </div>
            ) : null}
          </div>
          <p className="text-sm text-muted-foreground">
            Rewrite any customer-facing detail — product text, category,
            grade, color, supplier, quantity, or price. Manual prices are
            marked and audited; the supplier source data stays untouched.
            Hidden lines drop out of the files and the Telegram message.
          </p>
        </CardHeader>
        <CardContent>
          {visibleIndexes.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Every group was skipped — see the notes below.
            </p>
          ) : (
            <Table className="[&_td]:px-2 [&_th]:px-2">
              <TableHeader>
                <TableRow>
                  <TableHead>Product</TableHead>
                  {categorized ? <TableHead>Category</TableHead> : null}
                  <TableHead>Grade</TableHead>
                  <TableHead>Color</TableHead>
                  <TableHead>Supplier</TableHead>
                  <TableHead>Qty</TableHead>
                  <TableHead>Price</TableHead>
                  <TableHead>Rule</TableHead>
                  <TableHead>Exports</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleIndexes.map((index) => {
                  const line = quote.lines[index];
                  const draft = drafts[index] ?? effective[index];
                  const edited = draft.label.trim() !== line.model;
                  const suppliers = [
                    line.supplier,
                    ...(line.candidates ?? []).map((c) => c.supplier),
                  ].filter(
                    (name, at, all) => name !== "" && all.indexOf(name) === at,
                  );
                  return (
                    <TableRow
                      key={index}
                      className={draft.hidden ? "opacity-60" : undefined}
                    >
                      <TableCell>
                        {canOperate ? (
                          <Input
                            value={draft.label}
                            onChange={(event) =>
                              setCell(index, { label: event.target.value })
                            }
                            aria-label={`Customer label for line ${index + 1}`}
                            className="min-w-36"
                          />
                        ) : (
                          <span>
                            {line.brand} {draft.label || line.model}
                            {line.capacity_gb !== null
                              ? ` ${line.capacity_gb}GB`
                              : ""}
                          </span>
                        )}
                        {canOperate ? (
                          <span className="mt-0.5 block text-xs text-muted-foreground">
                            {line.brand}
                            {line.capacity_gb !== null
                              ? ` · ${line.capacity_gb}GB`
                              : ""}
                            {edited ? ` · was “${line.model}”` : ""}
                          </span>
                        ) : null}
                      </TableCell>
                      {categorized ? (
                        <TableCell>
                          {canOperate && categories.length > 0 ? (
                            <Select
                              value={
                                draft.categoryId === null
                                  ? ""
                                  : String(draft.categoryId)
                              }
                              onChange={(event) =>
                                setCell(index, {
                                  categoryId:
                                    event.target.value === ""
                                      ? null
                                      : Number(event.target.value),
                                })
                              }
                              aria-label={`Category for line ${index + 1}`}
                              className="h-8 w-32 py-0 text-xs"
                            >
                              <option value="">No category</option>
                              {categories.map((shelf) => (
                                <option key={shelf.id} value={shelf.id}>
                                  {shelf.name}
                                </option>
                              ))}
                            </Select>
                          ) : (
                            draft.categoryName || "—"
                          )}
                        </TableCell>
                      ) : null}
                      <TableCell>
                        {canOperate ? (
                          <Input
                            value={draft.grade}
                            onChange={(event) =>
                              setCell(index, { grade: event.target.value })
                            }
                            aria-label={`Grade for line ${index + 1}`}
                            className="h-8 w-16 py-0"
                          />
                        ) : (
                          draft.grade || "—"
                        )}
                      </TableCell>
                      <TableCell>
                        {canOperate ? (
                          <Input
                            value={draft.color}
                            onChange={(event) =>
                              setCell(index, { color: event.target.value })
                            }
                            aria-label={`Color for line ${index + 1}`}
                            className="h-8 w-20 py-0"
                          />
                        ) : (
                          draft.color || "—"
                        )}
                      </TableCell>
                      <TableCell>
                        {canOperate ? (
                          <Select
                            value={draft.supplier}
                            onChange={(event) =>
                              setCell(index, { supplier: event.target.value })
                            }
                            aria-label={`Supplier for line ${index + 1}`}
                            className="h-8 w-32 py-0 text-xs"
                          >
                            {suppliers.map((name) => (
                              <option key={name} value={name}>
                                {name}
                              </option>
                            ))}
                          </Select>
                        ) : (
                          draft.supplier
                        )}
                        {(line.candidates ?? []).length > 1 ? (
                          <span className="block text-xs text-muted-foreground">
                            also:{" "}
                            {(line.candidates ?? [])
                              .filter((c) => c.supplier !== line.supplier)
                              .map(
                                (c) =>
                                  `${c.supplier} ${c.currency} ${formatMoney(c.unit_price)}`,
                              )
                              .join(" · ")}
                          </span>
                        ) : null}
                      </TableCell>
                      <TableCell>
                        {canOperate ? (
                          <Input
                            value={draft.quantity}
                            onChange={(event) =>
                              setCell(index, { quantity: event.target.value })
                            }
                            inputMode="numeric"
                            aria-label={`Quantity for line ${index + 1}`}
                            className="h-8 w-16 py-0 tabular-nums"
                          />
                        ) : (
                          draft.quantity || "—"
                        )}
                      </TableCell>
                      <TableCell className="tabular-nums">
                        {canOperate ? (
                          <span className="flex items-center gap-1">
                            <span className="text-xs text-muted-foreground">
                              {line.currency}
                            </span>
                            <Input
                              value={draft.unitPrice}
                              onChange={(event) =>
                                setCell(index, {
                                  unitPrice: event.target.value,
                                })
                              }
                              inputMode="decimal"
                              aria-label={`Price for line ${index + 1}`}
                              className="h-8 w-20 py-0 tabular-nums"
                            />
                          </span>
                        ) : (
                          <span>
                            {line.currency} {formatMoney(draft.unitPrice)}
                          </span>
                        )}
                        {draft.manualPrice ? (
                          <Badge variant="secondary" className="ml-1">
                            Manual
                          </Badge>
                        ) : null}
                      </TableCell>
                      <TableCell>{line.rule ?? "—"}</TableCell>
                      <TableCell>
                        {canOperate ? (
                          <label className="flex cursor-pointer items-center gap-2 text-sm">
                            <input
                              type="checkbox"
                              checked={!draft.hidden}
                              onChange={() =>
                                setCell(index, { hidden: !draft.hidden })
                              }
                              aria-label={`Include line ${index + 1} in exports`}
                              className="h-4 w-4 accent-primary"
                            />
                            Include
                          </label>
                        ) : draft.hidden ? (
                          <Badge variant="warning">Hidden</Badge>
                        ) : (
                          <span className="text-sm text-muted-foreground">
                            Included
                          </span>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
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
  const router = useRouter();
  const { prefs, canOperate } = useOrg();
  const [quote, setQuote] = React.useState<CustomerQuote | null>(null);
  const [error, setError] = React.useState("");
  const [confirming, setConfirming] = React.useState(false);
  const [deleting, setDeleting] = React.useState(false);

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

  async function confirmDelete() {
    if (!quote) return;
    setDeleting(true);
    setError("");
    try {
      await deleteCustomerQuote(quote.id);
      router.push("/app/quotes");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete the quote.",
      );
      setDeleting(false);
      setConfirming(false);
    }
  }

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
        <div className="flex items-center gap-3">
          <StatusBadge status={quote.status} />
          {canOperate ? (
            <Button variant="outline" onClick={() => setConfirming(true)}>
              Delete
            </Button>
          ) : null}
        </div>
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      {quote.status === "ready" ? (
        <ReadyView quote={quote} onChanged={setQuote} />
      ) : (
        <ReconcileView quote={quote} onConfirmed={setQuote} />
      )}

      <Dialog
        open={confirming}
        onClose={() => setConfirming(false)}
        title="Delete this quote?"
        description={`Quote #${quote.id} and its groups, lines, and labels will be removed. Supplier runs and learned matches stay, so you can generate a fresh quote any time. This cannot be undone.`}
      >
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={() => setConfirming(false)}>
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

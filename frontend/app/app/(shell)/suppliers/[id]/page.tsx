"use client";

import { ArrowLeft, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { StarRating } from "@/components/app/star-rating";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  addSupplierNote,
  deleteSupplierNote,
  getSupplier,
  listSupplierNotes,
  updateSupplier,
  type Supplier,
  type SupplierNote,
} from "@/lib/api";
import { formatDateTime } from "@/lib/format";

type RatingKey =
  | "rating_shipping"
  | "rating_quality"
  | "rating_payment"
  | "rating_quantity"
  | "rating_rma";

const RATINGS: { key: RatingKey; label: string }[] = [
  { key: "rating_shipping", label: "Shipping speed" },
  { key: "rating_quality", label: "Goods quality" },
  { key: "rating_payment", label: "Payment terms" },
  { key: "rating_quantity", label: "Available quantity" },
  { key: "rating_rma", label: "RMA / DOA handling" },
];

export default function SupplierDetailPage() {
  const params = useParams();
  const id = Number(params.id);
  const { canOperate, prefs } = useOrg();
  const [supplier, setSupplier] = React.useState<Supplier | null>(null);
  const [notes, setNotes] = React.useState<SupplierNote[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [location, setLocation] = React.useState("");
  const [customs, setCustoms] = React.useState(false);
  const [fee, setFee] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [draftNote, setDraftNote] = React.useState("");
  const [noting, setNoting] = React.useState(false);

  const load = React.useCallback(async () => {
    if (!Number.isFinite(id)) return;
    setLoading(true);
    setError(null);
    try {
      const [row, history] = await Promise.all([
        getSupplier(id),
        listSupplierNotes(id),
      ]);
      setSupplier(row);
      setNotes(history);
      setLocation(row.location);
      setCustoms(row.customs_required);
      setFee(row.customs_fee_percent ?? "");
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load the supplier.",
      );
    } finally {
      setLoading(false);
    }
  }, [id]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function saveDetails() {
    if (!supplier) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await updateSupplier(supplier.id, {
        location,
        customs_required: customs,
        customs_fee_percent: fee.trim() === "" ? null : fee.trim(),
      });
      setSupplier(updated);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save the supplier.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function saveRating(key: RatingKey, stars: number | null) {
    if (!supplier) return;
    setError(null);
    try {
      const updated = await updateSupplier(supplier.id, { [key]: stars });
      setSupplier(updated);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save the rating.",
      );
    }
  }

  async function addNote() {
    if (!supplier) return;
    setNoting(true);
    setError(null);
    try {
      const note = await addSupplierNote(supplier.id, draftNote);
      setNotes((prev) => [note, ...prev]);
      setDraftNote("");
      setSupplier({ ...supplier, notes_count: supplier.notes_count + 1 });
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not add the note.",
      );
    } finally {
      setNoting(false);
    }
  }

  async function removeNote(noteId: number) {
    if (!supplier) return;
    setError(null);
    try {
      await deleteSupplierNote(supplier.id, noteId);
      setNotes((prev) => prev.filter((note) => note.id !== noteId));
      setSupplier({ ...supplier, notes_count: supplier.notes_count - 1 });
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete the note.",
      );
    }
  }

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading supplier…</p>;
  }

  if (!supplier) {
    return (
      <div className="space-y-4">
        {error ? <ErrorNotice message={error} /> : null}
        <Link href="/app/suppliers" className="text-sm hover:underline">
          ← Back to suppliers
        </Link>
      </div>
    );
  }

  const dirty =
    location !== supplier.location ||
    customs !== supplier.customs_required ||
    (fee.trim() === "" ? null : fee.trim()) !==
      (supplier.customs_fee_percent ?? null);

  return (
    <div className="space-y-6">
      <div>
        <Link
          href="/app/suppliers"
          className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:underline"
        >
          <ArrowLeft className="size-4" /> Suppliers
        </Link>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <h1 className="text-2xl font-bold">{supplier.name}</h1>
          {supplier.favorite_rank !== null ? (
            <Badge variant="default">★ Favorite {supplier.favorite_rank}</Badge>
          ) : null}
        </div>
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      <Card>
        <CardHeader>
          <CardTitle>Details</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <label className="block text-sm">
              Location
              <Input
                className="mt-1.5"
                value={location}
                disabled={!canOperate}
                onChange={(e) => setLocation(e.target.value)}
                placeholder="Ex. Shenzhen, CN"
              />
            </label>
            <label className="flex items-end gap-2 pb-2 text-sm">
              <input
                type="checkbox"
                checked={customs}
                disabled={!canOperate}
                onChange={(e) => setCustoms(e.target.checked)}
                className="size-4"
              />
              Customs clearance needed
            </label>
            <label className="block text-sm">
              Clearance fee %
              <Input
                className="mt-1.5"
                value={fee}
                disabled={!canOperate}
                onChange={(e) => setFee(e.target.value)}
                placeholder="Ex. 12.50"
                inputMode="decimal"
              />
            </label>
          </div>
          {canOperate ? (
            <Button onClick={() => void saveDetails()} disabled={saving || !dirty}>
              {saving ? "Saving…" : "Save details"}
            </Button>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Relationship ratings</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="space-y-3">
            {RATINGS.map(({ key, label }) => (
              <li
                key={key}
                className="flex flex-wrap items-center justify-between gap-3"
              >
                <span className="text-sm font-medium">{label}</span>
                <StarRating
                  label={label}
                  value={supplier[key]}
                  onChange={
                    canOperate
                      ? (stars) => void saveRating(key, stars)
                      : undefined
                  }
                />
              </li>
            ))}
          </ul>
          <p className="mt-3 text-xs text-muted-foreground">
            Click the current stars again to clear a rating.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Notes history</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {canOperate ? (
            <div className="space-y-2">
              <label htmlFor="supplier-note" className="text-sm font-medium">
                Add a note
              </label>
              <textarea
                id="supplier-note"
                value={draftNote}
                onChange={(e) => setDraftNote(e.target.value)}
                rows={3}
                placeholder="Ex. March shipment arrived late, 2 DOA units"
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm placeholder:font-normal placeholder:text-stone-400 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary"
              />
              <Button
                onClick={() => void addNote()}
                disabled={noting || draftNote.trim() === ""}
              >
                {noting ? "Adding…" : "Add note"}
              </Button>
            </div>
          ) : null}
          {notes.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No notes yet. Record calls, issues, and recoveries here so the
              history shows how the relationship evolves.
            </p>
          ) : (
            <ol className="space-y-3">
              {notes.map((note) => (
                <li
                  key={note.id}
                  className="rounded-md border border-border p-3"
                >
                  <div className="flex items-center justify-between gap-2">
                    <p className="text-xs text-muted-foreground">
                      {note.author_name || "Someone"} ·{" "}
                      {formatDateTime(note.created_at, prefs.datetime_format)}
                    </p>
                    {canOperate ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => void removeNote(note.id)}
                        aria-label="Delete note"
                      >
                        <Trash2 className="size-4" />
                      </Button>
                    ) : null}
                  </div>
                  <p className="mt-1 text-sm whitespace-pre-wrap">{note.text}</p>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

"use client";

import {
  ArrowDown,
  ArrowUp,
  GripVertical,
  Pencil,
  Plus,
  Star,
  Trash2,
  X,
} from "lucide-react";
import Link from "next/link";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { Button } from "@/components/ui/button";
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
  createSupplier,
  deleteSupplier,
  listSuppliers,
  setFavoriteOrder,
  type Supplier,
} from "@/lib/api";
import { cn } from "@/lib/cn";

export default function SuppliersPage() {
  const { active, memberships, canOperate } = useOrg();
  const [suppliers, setSuppliers] = React.useState<Supplier[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [showNew, setShowNew] = React.useState(false);
  const [draftName, setDraftName] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [pending, setPending] = React.useState<Supplier | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const [dragId, setDragId] = React.useState<number | null>(null);
  const [dropIndex, setDropIndex] = React.useState<number | null>(null);
  const [ordering, setOrdering] = React.useState(false);

  const load = React.useCallback(async () => {
    if (!active) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setSuppliers(await listSuppliers(active.organization));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load suppliers.",
      );
    } finally {
      setLoading(false);
    }
  }, [active]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const favorites = React.useMemo(
    () =>
      suppliers
        .filter((row) => row.favorite_rank !== null)
        .sort((a, b) => (a.favorite_rank ?? 0) - (b.favorite_rank ?? 0)),
    [suppliers],
  );

  async function persistOrder(ids: number[]) {
    if (!active) return;
    setOrdering(true);
    setError(null);
    try {
      await setFavoriteOrder(active.organization, ids);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not reorder favorites.",
      );
    } finally {
      setOrdering(false);
    }
  }

  function moveFavorite(id: number, delta: -1 | 1) {
    const ids = favorites.map((row) => row.id);
    const from = ids.indexOf(id);
    const to = from + delta;
    if (from < 0 || to < 0 || to >= ids.length) return;
    const [moved] = ids.splice(from, 1);
    ids.splice(to, 0, moved);
    void persistOrder(ids);
  }

  function dropFavorite(targetIndex: number) {
    if (dragId === null) return;
    const ids = favorites.map((row) => row.id).filter((id) => id !== dragId);
    ids.splice(targetIndex, 0, dragId);
    setDragId(null);
    setDropIndex(null);
    void persistOrder(ids);
  }

  function toggleFavorite(supplier: Supplier) {
    const ids = favorites.map((row) => row.id);
    if (supplier.favorite_rank === null) {
      void persistOrder([...ids, supplier.id]);
    } else {
      void persistOrder(ids.filter((id) => id !== supplier.id));
    }
  }

  async function confirmCreate() {
    if (!active) return;
    setSaving(true);
    setError(null);
    try {
      await createSupplier({
        organization_id: active.organization,
        name: draftName,
      });
      setShowNew(false);
      setDraftName("");
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not add the supplier.",
      );
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    if (!pending) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteSupplier(pending.id);
      setPending(null);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete the supplier.",
      );
    } finally {
      setDeleting(false);
    }
  }

  if (!active) {
    if (memberships.length > 0) {
      return <p className="text-sm text-muted-foreground">Selecting your workspace…</p>;
    }
    return (
      <div className="space-y-2">
        <h1 className="text-2xl font-bold">Suppliers</h1>
        <p className="text-sm text-muted-foreground">
          Join an organization to see its suppliers.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Suppliers</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Every supplier named by an import lands here. Rank your
            favorites — 1 is the favorite — and open a supplier to rate
            the relationship and keep notes.
          </p>
        </div>
        {canOperate ? (
          <Button onClick={() => setShowNew(true)}>
            <Plus className="size-4" /> New supplier
          </Button>
        ) : null}
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading suppliers…</p>
      ) : suppliers.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No suppliers yet. Import a price list and its suppliers will appear
          here automatically.
        </p>
      ) : (
        <>
          <section aria-label="Favorite suppliers" className="space-y-2">
            <h2 className="text-sm font-semibold tracking-wide text-muted-foreground uppercase">
              Favorites
            </h2>
            {favorites.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No favorites yet. Star a supplier below to shelve it here,
                then drag to order.
              </p>
            ) : (
              <ol className="space-y-1.5">
                {favorites.map((supplier, index) => (
                  <li
                    key={supplier.id}
                    draggable={canOperate && !ordering}
                    onDragStart={() => setDragId(supplier.id)}
                    onDragEnd={() => {
                      setDragId(null);
                      setDropIndex(null);
                    }}
                    onDragOver={(event) => {
                      if (dragId === null || dragId === supplier.id) return;
                      event.preventDefault();
                      setDropIndex(index);
                    }}
                    onDrop={(event) => {
                      event.preventDefault();
                      dropFavorite(index);
                    }}
                    className={cn(
                      "flex items-center gap-3 rounded-md border border-border bg-card px-3 py-2",
                      dragId === supplier.id && "opacity-40",
                      dropIndex === index &&
                        dragId !== null &&
                        "border-t-2 border-t-primary",
                    )}
                  >
                    {canOperate ? (
                      <GripVertical
                        className="size-4 shrink-0 cursor-grab text-muted-foreground"
                        aria-hidden
                      />
                    ) : null}
                    <span
                      className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-bold text-primary-foreground"
                      aria-label={`Favorite ${index + 1}`}
                    >
                      {index + 1}
                    </span>
                    <Link
                      href={`/app/suppliers/${supplier.id}`}
                      className="min-w-0 flex-1 truncate font-medium hover:underline"
                    >
                      {supplier.name}
                    </Link>
                    {supplier.location ? (
                      <span className="hidden truncate text-sm text-muted-foreground sm:block">
                        {supplier.location}
                      </span>
                    ) : null}
                    {canOperate ? (
                      <span className="flex shrink-0 items-center gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={ordering || index === 0}
                          onClick={() => moveFavorite(supplier.id, -1)}
                          aria-label={`Move ${supplier.name} up`}
                        >
                          <ArrowUp className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={ordering || index === favorites.length - 1}
                          onClick={() => moveFavorite(supplier.id, 1)}
                          aria-label={`Move ${supplier.name} down`}
                        >
                          <ArrowDown className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          disabled={ordering}
                          onClick={() => toggleFavorite(supplier)}
                          aria-label={`Remove ${supplier.name} from favorites`}
                        >
                          <X className="size-4" />
                        </Button>
                      </span>
                    ) : null}
                  </li>
                ))}
              </ol>
            )}
          </section>

          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Supplier</TableHead>
                <TableHead>Location</TableHead>
                <TableHead>Customs</TableHead>
                <TableHead>Notes</TableHead>
                {canOperate ? (
                  <TableHead>
                    <span className="sr-only">Actions</span>
                  </TableHead>
                ) : null}
              </TableRow>
            </TableHeader>
            <TableBody>
              {suppliers.map((supplier) => (
                <TableRow key={supplier.id}>
                  <TableCell className="font-medium">
                    <Link
                      href={`/app/suppliers/${supplier.id}`}
                      className="hover:underline"
                    >
                      {supplier.name}
                    </Link>
                    {supplier.favorite_rank !== null ? (
                      <span className="ml-2 text-xs text-muted-foreground">
                        ★ {supplier.favorite_rank}
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell>{supplier.location || "—"}</TableCell>
                  <TableCell>
                    {supplier.customs_required
                      ? `Yes${supplier.customs_fee_percent ? ` · ${supplier.customs_fee_percent}%` : ""}`
                      : "No"}
                  </TableCell>
                  <TableCell>{supplier.notes_count}</TableCell>
                  {canOperate ? (
                    <TableCell className="text-right whitespace-nowrap">
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={ordering}
                        onClick={() => toggleFavorite(supplier)}
                        aria-label={
                          supplier.favorite_rank === null
                            ? `Add ${supplier.name} to favorites`
                            : `Remove ${supplier.name} from favorites`
                        }
                        title={
                          supplier.favorite_rank === null
                            ? "Add to favorites"
                            : "Remove from favorites"
                        }
                      >
                        <Star
                          className={cn(
                            "size-4",
                            supplier.favorite_rank !== null &&
                              "fill-amber-400 stroke-amber-500",
                          )}
                        />
                      </Button>
                      <Link
                        href={`/app/suppliers/${supplier.id}`}
                        aria-label={`Open ${supplier.name}`}
                        className="inline-flex h-8 w-8 items-center justify-center rounded-md hover:bg-muted"
                      >
                        <Pencil className="size-4" />
                      </Link>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setPending(supplier)}
                        aria-label={`Delete ${supplier.name}`}
                      >
                        <Trash2 className="size-4" />
                      </Button>
                    </TableCell>
                  ) : null}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </>
      )}

      <Dialog
        open={showNew}
        onClose={() => setShowNew(false)}
        title="New supplier"
        description="Imports create suppliers on their own; add one by hand only when it has no price list yet."
      >
        <Input
          value={draftName}
          maxLength={255}
          onChange={(e) => setDraftName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void confirmCreate();
          }}
          aria-label="Supplier name"
          autoFocus
        />
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={() => setShowNew(false)}>
            Cancel
          </Button>
          <Button
            onClick={() => void confirmCreate()}
            disabled={saving || draftName.trim() === ""}
          >
            {saving ? "Adding…" : "Add supplier"}
          </Button>
        </div>
      </Dialog>

      <Dialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title="Delete this supplier?"
        description="Its notes go with it. The next import naming this supplier will re-create a blank record."
      >
        {pending ? (
          <div className="space-y-4">
            <p className="text-sm">
              <span className="font-medium">{pending.name}</span>
            </p>
            <div className="flex gap-2">
              <Button
                variant="destructive"
                onClick={() => void confirmDelete()}
                disabled={deleting}
              >
                {deleting ? "Deleting…" : "Delete supplier"}
              </Button>
              <Button
                variant="outline"
                onClick={() => setPending(null)}
                disabled={deleting}
              >
                Keep the supplier
              </Button>
            </div>
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}

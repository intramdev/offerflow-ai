"use client";

import { Pencil, Plus, Trash2 } from "lucide-react";
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
  createCategory,
  deleteCategory,
  listCategories,
  renameCategory,
  type Category,
} from "@/lib/api";

export default function CategoriesPage() {
  const { active, memberships, canOperate } = useOrg();
  const [categories, setCategories] = React.useState<Category[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState<Category | "new" | null>(null);
  const [draftName, setDraftName] = React.useState("");
  const [saving, setSaving] = React.useState(false);
  const [pending, setPending] = React.useState<Category | null>(null);
  const [deleting, setDeleting] = React.useState(false);

  const load = React.useCallback(async () => {
    if (!active) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setCategories(await listCategories(active.organization));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not load categories.",
      );
    } finally {
      setLoading(false);
    }
  }, [active]);

  React.useEffect(() => {
    void load();
  }, [load]);

  function openEditor(category: Category | "new") {
    setEditing(category);
    setDraftName(category === "new" ? "" : category.name);
    setError(null);
  }

  async function confirmSave() {
    if (!active || !editing) return;
    setSaving(true);
    setError(null);
    try {
      if (editing === "new") {
        await createCategory(active.organization, draftName);
      } else {
        await renameCategory(editing.id, draftName);
      }
      setEditing(null);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save the category.",
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
      await deleteCategory(pending.id);
      setPending(null);
      await load();
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not delete the category.",
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
        <h1 className="text-2xl font-bold">Categories</h1>
        <p className="text-sm text-muted-foreground">
          Join an organization to manage its product categories.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Categories</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Product shelves for {active.organization_name}. Map a whole file
            from the Files page, or let matching propose — and correct — a
            category per product when you reconcile a quote.
          </p>
        </div>
        {canOperate ? (
          <Button onClick={() => openEditor("new")}>
            <Plus className="size-4" /> New category
          </Button>
        ) : null}
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading categories…</p>
      ) : categories.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No categories yet. Without any, every quote stays one uncategorized
          list — add shelves like “Phones” or “Accessories” when you need them.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              {canOperate ? (
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              ) : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {categories.map((category) => (
              <TableRow key={category.id}>
                <TableCell className="font-medium">{category.name}</TableCell>
                {canOperate ? (
                  <TableCell className="text-right whitespace-nowrap">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => openEditor(category)}
                      aria-label={`Rename ${category.name}`}
                    >
                      <Pencil className="size-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPending(category)}
                      aria-label={`Delete ${category.name}`}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  </TableCell>
                ) : null}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Dialog
        open={editing !== null}
        onClose={() => setEditing(null)}
        title={editing === "new" ? "New category" : "Rename category"}
        description="Names are unique inside your company, up to letter case."
      >
        <Input
          value={draftName}
          maxLength={100}
          onChange={(e) => setDraftName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void confirmSave();
          }}
          aria-label="Category name"
          autoFocus
        />
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={() => setEditing(null)}>
            Cancel
          </Button>
          <Button
            onClick={() => void confirmSave()}
            disabled={saving || draftName.trim() === ""}
          >
            {saving ? "Saving…" : "Save category"}
          </Button>
        </div>
      </Dialog>

      <Dialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title="Delete this category?"
        description="Mapped files and learned matches keep working — they simply become uncategorized. Shelves named by a pricing rule cannot go until the rule is retargeted."
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
                {deleting ? "Deleting…" : "Delete category"}
              </Button>
              <Button
                variant="outline"
                onClick={() => setPending(null)}
                disabled={deleting}
              >
                Keep the category
              </Button>
            </div>
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}

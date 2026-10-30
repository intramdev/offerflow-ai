"use client";

import Link from "next/link";
import {
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Search,
  Trash2,
} from "lucide-react";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { StatusBadge } from "@/components/app/status-badge";
import { Button, buttonVariants } from "@/components/ui/button";
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
  deleteFile,
  listFiles,
  renameFile,
  updateFileFx,
  type SourceFile,
} from "@/lib/api";
import { cn } from "@/lib/cn";
import { formatBytes, formatDateTime } from "@/lib/format";

const TODAY = new Date().toISOString().slice(0, 10);
const PAGE_SIZE = 50;

function pageWindow(current: number, total: number): (number | "…")[] {
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1);
  }
  const wanted = new Set(
    [1, 2, current - 1, current, current + 1, total - 1, total].filter(
      (p) => p >= 1 && p <= total,
    ),
  );
  const sorted = [...wanted].sort((a, b) => a - b);
  const pages: (number | "…")[] = [];
  sorted.forEach((page, i) => {
    if (i > 0 && page - sorted[i - 1] > 1) pages.push("…");
    pages.push(page);
  });
  return pages;
}

function Pager({
  page,
  total,
  onPage,
}: {
  page: number;
  total: number;
  onPage: (page: number) => void;
}) {
  if (total <= 1) return null;
  const arrow = "h-8 w-8";
  const current = "h-8 min-w-8 bg-muted font-semibold";
  return (
    <nav aria-label="File pages" className="flex flex-wrap items-center gap-1">
      <Button
        variant="ghost"
        size="icon"
        className={arrow}
        disabled={page === 1}
        onClick={() => onPage(1)}
        title="First page"
        aria-label="First page"
      >
        <ChevronsLeft />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className={arrow}
        disabled={page === 1}
        onClick={() => onPage(page - 1)}
        title="Previous page"
        aria-label="Previous page"
      >
        <ChevronLeft />
      </Button>
      {pageWindow(page, total).map((entry, i) =>
        entry === "…" ? (
          <span key={`gap-${i}`} className="px-1 text-muted-foreground">
            …
          </span>
        ) : (
          <Button
            key={entry}
            variant="ghost"
            size="sm"
            className={entry === page ? current : "h-8 min-w-8"}
            disabled={entry === page}
            onClick={() => onPage(entry)}
            aria-label={`Page ${entry}`}
            aria-current={entry === page ? "page" : undefined}
          >
            {entry}
          </Button>
        ),
      )}
      <Button
        variant="ghost"
        size="icon"
        className={arrow}
        disabled={page === total}
        onClick={() => onPage(page + 1)}
        title="Next page"
        aria-label="Next page"
      >
        <ChevronRight />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className={arrow}
        disabled={page === total}
        onClick={() => onPage(total)}
        title="Last page"
        aria-label="Last page"
      >
        <ChevronsRight />
      </Button>
      <span className="ml-2 text-sm text-muted-foreground">
        Page {page} of {total}
      </span>
    </nav>
  );
}

export default function FilesPage() {
  const { active, memberships, prefs, canOperate } = useOrg();
  const [files, setFiles] = React.useState<SourceFile[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState<SourceFile | null>(null);
  const [deleting, setDeleting] = React.useState(false);
  const [fxBusy, setFxBusy] = React.useState<number | null>(null);
  const [query, setQuery] = React.useState("");
  const [page, setPage] = React.useState(1);
  const [renaming, setRenaming] = React.useState<SourceFile | null>(null);
  const [draftName, setDraftName] = React.useState("");
  const [savingName, setSavingName] = React.useState(false);

  React.useEffect(() => {
    if (!active) {
      setFiles([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void listFiles(active.organization)
      .then((rows) => {
        if (!cancelled) {
          setFiles(rows);
          setPage(1);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Could not load files.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  async function changeFxDate(file: SourceFile, value: string) {
    if (!value || value === file.currency_date) return;
    setFxBusy(file.id);
    setError(null);
    try {
      const updated = await updateFileFx(file.id, value);
      setFiles((rows) => rows.map((row) => (row.id === file.id ? updated : row)));
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not update the currency date.",
      );
    } finally {
      setFxBusy(null);
    }
  }

  async function confirmDelete() {
    if (!pending) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteFile(pending.id);
      setFiles((rows) => rows.filter((row) => row.id !== pending.id));
      setPending(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not delete the file.");
    } finally {
      setDeleting(false);
    }
  }

  function openRename(file: SourceFile) {
    setRenaming(file);
    setDraftName(file.original_name);
    setError(null);
  }

  async function confirmRename() {
    if (!renaming) return;
    setSavingName(true);
    setError(null);
    try {
      const updated = await renameFile(renaming.id, draftName);
      setFiles((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
      setRenaming(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not rename the file.");
    } finally {
      setSavingName(false);
    }
  }

  if (!active) {
    return (
      <section className="space-y-3">
        <h1 className="text-2xl font-semibold">Files</h1>
        <p className="text-sm text-muted-foreground">
          {memberships.length === 0
            ? "You are not a member of any organization yet."
            : "Select an organization to see its files."}
        </p>
      </section>
    );
  }

  const needle = query.trim().toLowerCase();
  const filtered = needle
    ? files.filter(
        (file) =>
          file.original_name.toLowerCase().includes(needle) ||
          (file.uploaded_by_name ?? "").toLowerCase().includes(needle),
      )
    : files;
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const visible = filtered.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);
  const from = filtered.length === 0 ? 0 : (safePage - 1) * PAGE_SIZE + 1;
  const to = Math.min(safePage * PAGE_SIZE, filtered.length);

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Files</h1>
          <p className="text-sm text-muted-foreground">
            {active.organization_name} · {filtered.length} of {files.length} files
            {filtered.length > 0 ? ` · showing ${from}–${to}` : ""}
          </p>
        </div>
        <label className="relative block w-full sm:w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(1);
            }}
            placeholder="Search by file name or uploader…"
            aria-label="Search files"
            className="pl-9"
          />
        </label>
      </div>
      {error ? <ErrorNotice message={error} /> : null}
      {loading ? (
        <p className="text-sm text-muted-foreground">Loading files…</p>
      ) : filtered.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          {files.length === 0 ? "No files yet." : "No files match this search."}
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="px-3">File</TableHead>
              <TableHead className="px-3">Uploaded</TableHead>
              <TableHead className="px-3">FX date</TableHead>
              <TableHead className="px-3">Run</TableHead>
              <TableHead className="px-3 text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((file) => (
              <TableRow key={file.id}>
                <TableCell className="px-3">
                  <span className="block break-words font-medium">
                    {file.original_name}
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    {formatBytes(file.size_bytes)}
                  </span>
                </TableCell>
                <TableCell className="whitespace-nowrap px-3">
                  {formatDateTime(file.created_at, prefs.datetime_format)}
                  <span className="block text-xs font-normal text-muted-foreground">
                    by {file.uploaded_by_name ?? "—"}
                  </span>
                </TableCell>
                <TableCell className="px-3">
                  {canOperate ? (
                    <input
                      type="date"
                      value={file.currency_date}
                      max={TODAY}
                      disabled={fxBusy === file.id}
                      onChange={(e) => void changeFxDate(file, e.target.value)}
                      aria-label={`Currency date for ${file.original_name}`}
                      className="w-full max-w-40 rounded-md border border-border bg-background px-2 py-1 text-sm"
                    />
                  ) : (
                    <span className="whitespace-nowrap">{file.currency_date}</span>
                  )}
                  <span className="block text-xs text-muted-foreground">
                    {file.fx_destination
                      ? `→ ${file.fx_destination}${file.fx_simulated ? " · simulated" : ""}`
                      : "no destination"}
                  </span>
                </TableCell>
                <TableCell className="px-3">
                  {file.run_id ? (
                    <span className="flex flex-wrap items-center gap-2">
                      <Link
                        href={`/app/runs/${file.run_id}`}
                        className="text-primary underline-offset-4 hover:underline"
                      >
                        Run {file.run_id}
                      </Link>
                      {file.run_status ? <StatusBadge status={file.run_status} /> : null}
                    </span>
                  ) : (
                    "—"
                  )}
                </TableCell>
                <TableCell className="px-3">
                  <span className="flex flex-wrap items-center justify-end gap-1">
                    {file.run_id ? (
                      <Link
                        href={`/app/runs/${file.run_id}#remap`}
                        title="Edit the column mapping"
                        aria-label={`Edit mapping for ${file.original_name}`}
                        className={cn(buttonVariants({ variant: "ghost", size: "sm" }))}
                      >
                        Edit
                      </Link>
                    ) : null}
                    {canOperate ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => openRename(file)}
                        title="Rename file"
                        aria-label={`Rename ${file.original_name}`}
                      >
                        Rename
                      </Button>
                    ) : null}
                    {canOperate ? (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-destructive"
                        onClick={() => setPending(file)}
                        title="Delete file"
                        aria-label={`Delete ${file.original_name}`}
                      >
                        <Trash2 />
                      </Button>
                    ) : null}
                  </span>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      <Pager page={safePage} total={totalPages} onPage={setPage} />

      <Dialog
        open={renaming !== null}
        onClose={() => setRenaming(null)}
        title="Rename file"
        description="Only the display name changes; the stored upload, history, and exports keep working."
      >
        <Input
          value={draftName}
          maxLength={255}
          onChange={(e) => setDraftName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void confirmRename();
          }}
          aria-label="File name"
          autoFocus
        />
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" onClick={() => setRenaming(null)}>
            Cancel
          </Button>
          <Button
            onClick={() => void confirmRename()}
            disabled={savingName || draftName.trim() === ""}
          >
            {savingName ? "Saving…" : "Save name"}
          </Button>
        </div>
      </Dialog>

      <Dialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title="Delete this file?"
        description={`“${pending?.original_name}” and its run, questions, and answers will be removed. This cannot be undone.`}
      >
        <p className="text-sm text-muted-foreground">
          {pending ? `${formatBytes(pending.size_bytes)} · uploaded ${formatDateTime(pending.created_at, prefs.datetime_format)}` : ""}
        </p>
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
    </section>
  );
}

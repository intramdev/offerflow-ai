import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from "lucide-react";

import { Button } from "@/components/ui/button";

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

/** First / previous / numbered / next / last pager for AJAX log lists. */
export function Pager({
  page,
  total,
  onPage,
  label,
  busy,
}: {
  page: number;
  total: number;
  onPage: (page: number) => void;
  label: string;
  busy?: boolean;
}) {
  if (total <= 1) return null;
  const arrow = "h-8 w-8";
  const current = "h-8 min-w-8 bg-muted font-semibold";
  const disabled = busy === true;
  return (
    <nav aria-label={label} className="flex flex-wrap items-center gap-1">
      <Button
        variant="ghost"
        size="icon"
        className={arrow}
        disabled={disabled || page === 1}
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
        disabled={disabled || page === 1}
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
            disabled={disabled || entry === page}
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
        disabled={disabled || page === total}
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
        disabled={disabled || page === total}
        onClick={() => onPage(total)}
        title="Last page"
        aria-label="Last page"
      >
        <ChevronsRight />
      </Button>
      <span className="ml-2 text-sm text-muted-foreground">
        Page {page} of {total}
        {busy ? " · Loading…" : ""}
      </span>
    </nav>
  );
}

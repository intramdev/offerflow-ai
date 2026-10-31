"use client";

import * as React from "react";

import { cn } from "@/lib/cn";

interface ComboboxProps {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
  allowEmpty?: boolean;
  emptyLabel?: string;
  id?: string;
}

/** Searchable dropdown. Typing filters by prefix; only a listed value
 *  (or the empty choice when allowed) can ever be committed — anything
 *  else reverts on blur or Escape. */
export function Combobox({
  value,
  options,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
  className,
  allowEmpty,
  emptyLabel = "None",
  id,
}: ComboboxProps) {
  const sorted = React.useMemo(
    () => [...options].sort((a, b) => a.localeCompare(b)),
    [options],
  );
  const [open, setOpen] = React.useState(false);
  const [text, setText] = React.useState(value);
  const [active, setActive] = React.useState(0);
  const listId = React.useId();
  const rootRef = React.useRef<HTMLDivElement>(null);

  // Outside edits (server load, reset) sync the text while closed;
  // while open the user's typing owns it.
  React.useEffect(() => {
    if (!open) setText(value);
  }, [value, open]);

  const query = text.trim().toUpperCase();
  const matches = sorted.filter((option) => option.startsWith(query));
  const showEmptyChoice = allowEmpty && query === "";
  const rows: string[] = showEmptyChoice ? ["", ...matches] : matches;
  const clampedActive = rows.length === 0 ? 0 : Math.min(active, rows.length - 1);

  function commit(next: string) {
    setText(next);
    setOpen(false);
    if (next !== value) onChange(next);
  }

  function revert() {
    setText(value);
    setOpen(false);
  }

  function openList() {
    if (disabled) return;
    const current = rows.indexOf(value);
    setActive(current >= 0 ? current : 0);
    setOpen(true);
  }

  function onBlur(event: React.FocusEvent) {
    if (rootRef.current?.contains(event.relatedTarget as Node)) return;
    const exact = sorted.find((option) => option === text.trim().toUpperCase());
    if (exact !== undefined) commit(exact);
    else if (allowEmpty && text.trim() === "") commit("");
    else revert();
  }

  return (
    <div ref={rootRef} onBlur={onBlur} className={cn("relative", className)}>
      <input
        id={id}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-activedescendant={
          open && rows.length > 0 ? `${listId}-${clampedActive}` : undefined
        }
        aria-autocomplete="list"
        aria-label={ariaLabel}
        autoComplete="off"
        spellCheck={false}
        disabled={disabled}
        placeholder={placeholder}
        value={text}
        onFocus={openList}
        onClick={openList}
        onChange={(event) => {
          setText(event.target.value.toUpperCase());
          setActive(0);
          if (!open && !disabled) setOpen(true);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            if (!open) {
              openList();
              return;
            }
            if (rows.length === 0) return;
            const step = event.key === "ArrowDown" ? 1 : -1;
            setActive((clampedActive + step + rows.length) % rows.length);
            document
              .getElementById(
                `${listId}-${(clampedActive + step + rows.length) % rows.length}`,
              )
              ?.scrollIntoView({ block: "nearest" });
          } else if (event.key === "Enter") {
            if (!open) {
              openList();
            } else if (rows.length > 0) {
              event.preventDefault();
              commit(rows[clampedActive]);
            }
          } else if (event.key === "Escape") {
            revert();
          }
        }}
        className={cn(
          "flex h-9 w-full rounded-md border border-border bg-background px-3 py-1 text-sm",
          "placeholder:font-normal placeholder:text-stone-400 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary",
          "disabled:cursor-not-allowed disabled:opacity-50",
        )}
      />
      {open && !disabled ? (
        <ul
          id={listId}
          role="listbox"
          aria-label={ariaLabel ? `${ariaLabel} options` : "Options"}
          className="absolute z-50 mt-1 max-h-60 w-full overflow-auto rounded-md border border-border bg-background py-1 shadow-lg"
        >
          {rows.length === 0 ? (
            <li className="px-3 py-1.5 text-sm text-muted-foreground">
              No matching currency.
            </li>
          ) : (
            rows.map((row, index) => (
              <li
                key={row === "" ? "__empty__" : row}
                id={`${listId}-${index}`}
                role="option"
                aria-selected={row === value}
                onMouseDown={(event) => {
                  event.preventDefault();
                  commit(row);
                }}
                className={cn(
                  "cursor-pointer px-3 py-1.5 text-sm",
                  index === clampedActive && "bg-muted",
                  row === value ? "font-medium" : "font-normal",
                  row === "" && "text-muted-foreground",
                )}
              >
                {row === "" ? emptyLabel : row}
              </li>
            ))
          )}
        </ul>
      ) : null}
    </div>
  );
}

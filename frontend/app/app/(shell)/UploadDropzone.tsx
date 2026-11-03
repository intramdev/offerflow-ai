"use client";

import { UploadCloud } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/cn";

// Single source for the chooser hint and the pre-upload check below:
// the accept attribute only filters the dialog, so picked files are
// re-checked before anything is sent.
export const ACCEPTED_EXTENSIONS = ["xlsx", "xls", "csv", "txt", "tsv", "docx"];

const ACCEPTED = ACCEPTED_EXTENSIONS.map((ext) => `.${ext}`).join(",");

export function UploadDropzone({
  disabled,
  onFile,
}: {
  disabled?: boolean;
  onFile: (file: File) => void;
}) {
  const [dragging, setDragging] = React.useState(false);
  const inputRef = React.useRef<HTMLInputElement>(null);

  function take(files: FileList | null) {
    const file = files?.[0];
    if (file && !disabled) onFile(file);
  }

  return (
    <div
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled}
      aria-label="Upload a supplier file"
      onClick={() => inputRef.current?.click()}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
      }}
      onDragOver={(event) => {
        event.preventDefault();
        if (!disabled) setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragging(false);
        take(event.dataTransfer.files);
      }}
      className={cn(
        "cursor-pointer rounded-lg border-2 border-dashed border-border bg-muted px-5 py-10 text-center",
        dragging && "border-primary bg-background",
        disabled && "cursor-wait opacity-60",
      )}
    >
      <UploadCloud className="mx-auto size-8 text-muted-foreground" />
      <p className="mt-3 font-semibold">
        {dragging ? "Drop the file to preview it" : "Drag a supplier file here, or click to choose"}
      </p>
      <p className="mt-1 text-sm text-muted-foreground">
        xlsx, xls, csv, txt, tsv, or docx — up to 10 MB. Nothing imports until
        you confirm the preview.
      </p>
      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED}
        disabled={disabled}
        className="sr-only"
        tabIndex={-1}
        onChange={(event) => {
          take(event.target.files);
          event.target.value = "";
        }}
      />
    </div>
  );
}

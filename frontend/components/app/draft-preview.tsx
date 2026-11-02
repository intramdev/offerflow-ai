"use client";

import * as React from "react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { DraftBlock, DraftRecord, DraftRecordField } from "@/lib/api";
import { cn } from "@/lib/cn";

function FieldValue({ field, label }: { field: DraftRecordField | undefined; label: string }) {
  if (!field || field.value === null || field.value === undefined || String(field.value).trim() === "") {
    return (
      <span className="text-muted-foreground" title={`${label}: unknown`}>
        Unknown
      </span>
    );
  }
  const confirmed = field.state === "user_confirmed";
  return (
    <span
      className={cn(!confirmed && "text-muted-foreground")}
      title={confirmed ? `${label}: confirmed${field.by ? ` (${field.by})` : ""}` : `${label}: ${field.state}`}
    >
      {String(field.value)}
    </span>
  );
}

function RecordRow({ record, issues }: { record: DraftRecord; issues: number }) {
  const fields = record.fields ?? {};
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap">
        <span className="font-medium">{record.id}</span>
        <span className="block text-xs text-muted-foreground">
          {record.lines.length > 1
            ? `lines ${record.lines[0]}–${record.lines[record.lines.length - 1]}`
            : `line ${record.lines[0]}`}
          {record.class && record.class !== "unknown" ? ` · ${record.class}` : ""}
          {record.section !== null && record.section !== undefined
            ? ` · section L${record.section}`
            : ""}
        </span>
      </TableCell>
      <TableCell>
        <span className="block">
          <FieldValue field={fields.supplier} label="supplier" />
          {" · "}
          <FieldValue field={fields.brand} label="brand" />
        </span>
        <span className="block font-medium">
          <FieldValue field={fields.model} label="model" />
        </span>
        <span className="block text-xs text-muted-foreground">
          <FieldValue field={fields.capacity} label="capacity" />
          {" · "}
          <FieldValue field={fields.color} label="color" />
          {" · "}
          <FieldValue field={fields.grade} label="grade" />
        </span>
      </TableCell>
      <TableCell className="whitespace-nowrap">
        <span className="block font-medium">
          <FieldValue field={fields.price} label="price" />
          {" "}
          <FieldValue field={fields.currency} label="currency" />
        </span>
        <span className="block text-xs text-muted-foreground">
          qty <FieldValue field={fields.quantity} label="quantity" />
        </span>
      </TableCell>
      <TableCell>
        {issues === 0 ? (
          <span className="text-xs text-muted-foreground">—</span>
        ) : (
          <span className="text-xs font-medium text-amber-700 dark:text-amber-400">
            {issues} open
          </span>
        )}
      </TableCell>
    </TableRow>
  );
}

export function DraftPreview({ draft }: { draft: DraftBlock }) {
  const records = draft.records ?? [];
  const unresolved = draft.unresolved ?? [];
  const excluded = draft.excluded_lines ?? [];
  const openByRecord = React.useMemo(() => {
    const counts: Record<string, number> = {};
    for (const issue of unresolved) {
      if (issue.scope === "record" && issue.record) {
        counts[issue.record] = (counts[issue.record] ?? 0) + 1;
      }
    }
    return counts;
  }, [unresolved]);
  const documentIssues = unresolved.filter((issue) => issue.scope !== "record");

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Candidate products{" "}
          <span className="text-sm font-normal text-muted-foreground">
            revision {draft.revision}
          </span>
        </CardTitle>
        <CardDescription>
          {records.length} candidate{records.length === 1 ? "" : "s"}
          {excluded.length > 0
            ? ` · ${excluded.length} excluded line${excluded.length === 1 ? "" : "s"}`
            : ""}
          {unresolved.length > 0
            ? ` · ${unresolved.length} unresolved`
            : " · nothing unresolved"}
          {draft.document_policy === "price_only"
            ? " · price-only: quantities stay unknown"
            : ""}
          . Confirmed values are yours or the chat&apos;s applied proposals;
          unknowns need a rule or a correction before import reads them.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {records.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No candidate products yet — every line is excluded or classified
            as a header or note.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Record</TableHead>
                <TableHead>Product</TableHead>
                <TableHead>Price</TableHead>
                <TableHead>Open</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {records.map((record) => (
                <RecordRow
                  key={record.id}
                  record={record}
                  issues={openByRecord[record.id] ?? 0}
                />
              ))}
            </TableBody>
          </Table>
        )}
        {documentIssues.length > 0 ? (
          <div>
            <p className="text-sm font-medium">Needs a decision</p>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {documentIssues.map((issue, index) => (
                <li key={`${issue.field}-${index}`}>{issue.reason}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {excluded.length > 0 ? (
          <p className="text-xs text-muted-foreground">
            Excluded lines: {[...excluded].sort((a, b) => a - b).join(", ")}.
            The source text is kept; exclusions only skip these lines.
          </p>
        ) : null}
      </CardContent>
    </Card>
  );
}

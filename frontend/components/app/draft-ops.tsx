"use client";

import type { AssistOperation, DraftRule } from "@/lib/api";

function text(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return String(value);
}

function linesLabel(lines: unknown): string {
  if (!Array.isArray(lines)) return "—";
  const numbers = lines.filter((n): n is number => typeof n === "number");
  if (numbers.length === 0) return "—";
  if (numbers.length === 1) return `line ${numbers[0]}`;
  const sorted = [...numbers].sort((a, b) => a - b);
  const contiguous =
    sorted[sorted.length - 1] - sorted[0] === sorted.length - 1;
  if (contiguous && sorted.length > 2) {
    return `lines ${sorted[0]}–${sorted[sorted.length - 1]}`;
  }
  return `lines ${sorted.join(", ")}`;
}

export interface OpSummary {
  title: string;
  detail: string | null;
}

/** Plain-English summary of one proposed typed operation. */
export function summarizeOperation(op: AssistOperation): OpSummary {
  switch (op.op) {
    case "global":
      return {
        title: `Set ${text(op.field)} = ${text(op.value)}`,
        detail: "Applies to every row.",
      };
    case "exclude_lines":
      return {
        title: `Drop ${linesLabel(op.lines)}`,
        detail: op.reason ? text(op.reason) : "Header, note, or date line.",
      };
    case "classify":
      return {
        title: `Mark ${linesLabel(op.lines)} as ${text(op.class)}`,
        detail: null,
      };
    case "terminal_price": {
      const scope = op.section_header_line;
      return {
        title: `Final number is the price in ${text(op.currency)}`,
        detail:
          typeof scope === "number"
            ? `Only for the section under line ${scope}.`
            : "Whole document; no quantities are supplied.",
      };
    }
    case "section_value":
      return {
        title: `Under line ${text(op.header_line)}: ${text(op.field)} = ${text(op.value)}`,
        detail: "Inherited by every product line in that section.",
      };
    case "join_lines":
      return {
        title: `Join ${linesLabel(op.lines)} into one product`,
        detail: null,
      };
    case "correct":
      return {
        title: `Fix ${text(op.record)}: ${text(op.field)} → ${text(op.value)}`,
        detail: null,
      };
    case "absent":
      return {
        title: `${text(op.field)} is absent document-wide`,
        detail:
          op.field === "quantity"
            ? "Price-only list: no stock is claimed."
            : null,
      };
    case "policy":
      return {
        title:
          op.policy === "price_only"
            ? "Switch to the price-only policy"
            : "Switch to the stock policy",
        detail:
          op.policy === "price_only"
            ? "Quantities stay unknown and are omitted from output."
            : "Every product needs a stock quantity.",
      };
    case "drop_rule":
      return {
        title: `Remove rule ${text(op.rule)}`,
        detail: null,
      };
    default:
      return { title: `Unknown operation ${text(op.op)}`, detail: null };
  }
}

/** Plain-English summary of one confirmed draft rule. */
export function summarizeRule(rule: DraftRule): OpSummary {
  switch (rule.kind) {
    case "global":
      return {
        title: `${text(rule.field)} = ${text(rule.value)}`,
        detail: "Set for every row.",
      };
    case "exclude_lines":
      return {
        title: `Dropped ${linesLabel(rule.lines)}`,
        detail: rule.reason ? text(rule.reason) : null,
      };
    case "classify":
      return {
        title: `${linesLabel(rule.lines)} are ${text(rule.class)}`,
        detail: null,
      };
    case "terminal_price": {
      const scope = rule.scope;
      const section =
        typeof scope === "object" && scope !== null
          ? scope.section_header_line
          : null;
      return {
        title: `Terminal price in ${text(rule.currency)}`,
        detail:
          typeof section === "number"
            ? `Section under line ${section}.`
            : "Whole document; no quantities are supplied.",
      };
    }
    case "section_value":
      return {
        title: `${text(rule.header_text ?? `line ${rule.header_line}`)}: ${text(rule.field)} = ${text(rule.value)}`,
        detail: `Line ${text(rule.header_line)} · inherited by its section.`,
      };
    case "join_lines":
      return {
        title: `Joined ${linesLabel(rule.lines)}`,
        detail: "One multi-line product.",
      };
    case "correct":
      return {
        title: `${text(rule.record)}: ${text(rule.field)} → ${text(rule.value)}`,
        detail: "Manual correction.",
      };
    case "absent":
      return {
        title: `${text(rule.field)} absent document-wide`,
        detail: null,
      };
    default:
      return { title: `Rule ${text(rule.id)} (${text(rule.kind)})`, detail: null };
  }
}

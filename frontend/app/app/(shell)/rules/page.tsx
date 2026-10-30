"use client";

import { Pencil, Plus, Trash2 } from "lucide-react";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { RuleDialog } from "@/components/app/rule-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ApiError, deleteRule, listRules, type PricingRule } from "@/lib/api";

function matcherSummary(rule: PricingRule): string {
  const parts = Object.entries(rule.matcher).map(([k, v]) => `${k} = ${v}`);
  return parts.length > 0 ? parts.join(", ") : "All products";
}

function strategySummary(rule: PricingRule): string {
  if (rule.supplier_strategy === "preferred") {
    return `Preferred: ${rule.preferred_supplier || "(unset)"}`;
  }
  return rule.supplier_strategy === "highest" ? "Highest price" : "Lowest price";
}

function opsSummary(rule: PricingRule): string {
  if (rule.ops.length === 0) return "No changes";
  return rule.ops
    .map((op) => {
      if (op.op === "convert") return `→ ${op.to} @ ${op.rate}`;
      if (op.op === "convert_to_destination") return "→ destination @ file rate";
      if (op.op === "round") return `round ${op.step}`;
      return `${Number(op.value) >= 0 ? "+" : ""}${op.value}%`;
    })
    .join(", ");
}

export default function RulesPage() {
  const { active, memberships, canOperate } = useOrg();
  const [rules, setRules] = React.useState<PricingRule[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [editing, setEditing] = React.useState<PricingRule | "new" | null>(null);
  const [pending, setPending] = React.useState<PricingRule | null>(null);
  const [deleting, setDeleting] = React.useState(false);

  const load = React.useCallback(async () => {
    if (!active) {
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setRules(await listRules(active.organization));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not load rules.");
    } finally {
      setLoading(false);
    }
  }, [active]);

  React.useEffect(() => {
    void load();
  }, [load]);

  async function confirmDelete() {
    if (!pending) return;
    setDeleting(true);
    try {
      await deleteRule(pending.id);
      setPending(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not delete the rule.");
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
        <h1 className="text-2xl font-bold">Pricing rules</h1>
        <p className="text-sm text-muted-foreground">
          Join an organization to manage its pricing rules.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">Pricing rules</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Rules for {active.organization_name}. Each quote line names the rule
            that priced it.
          </p>
        </div>
        {canOperate ? (
          <Button onClick={() => setEditing("new")}>
            <Plus className="size-4" /> New rule
          </Button>
        ) : null}
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      {loading ? (
        <p className="text-sm text-muted-foreground">Loading rules…</p>
      ) : rules.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No rules yet. Without rules, quotes pick the lowest price per product
          unchanged.
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead>Applies to</TableHead>
              <TableHead>Supplier</TableHead>
              <TableHead>Repricing</TableHead>
              <TableHead>Priority</TableHead>
              <TableHead>Status</TableHead>
              {canOperate ? (
                <TableHead>
                  <span className="sr-only">Actions</span>
                </TableHead>
              ) : null}
            </TableRow>
          </TableHeader>
          <TableBody>
            {rules.map((rule) => (
              <TableRow key={rule.id}>
                <TableCell className="font-medium">{rule.name}</TableCell>
                <TableCell>{matcherSummary(rule)}</TableCell>
                <TableCell>{strategySummary(rule)}</TableCell>
                <TableCell>{opsSummary(rule)}</TableCell>
                <TableCell>{rule.priority}</TableCell>
                <TableCell>
                  <Badge variant={rule.active ? "default" : "secondary"}>
                    {rule.active ? "Active" : "Off"}
                  </Badge>
                </TableCell>
                {canOperate ? (
                  <TableCell className="text-right whitespace-nowrap">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setEditing(rule)}
                      aria-label={`Edit ${rule.name}`}
                    >
                      <Pencil className="size-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setPending(rule)}
                      aria-label={`Delete ${rule.name}`}
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

      {active ? (
        <RuleDialog
          rule={editing === "new" ? null : editing}
          organizationId={active.organization}
          open={editing !== null}
          onClose={() => setEditing(null)}
          onSaved={() => void load()}
        />
      ) : null}

      <Dialog
        open={pending !== null}
        onClose={() => setPending(null)}
        title="Delete this rule?"
        description="Quotes built afterwards will no longer use it."
      >
        {pending ? (
          <div className="space-y-4">
            <p className="text-sm">
              <span className="font-medium">{pending.name}</span> —{" "}
              {matcherSummary(pending)}.
            </p>
            <div className="flex gap-2">
              <Button variant="destructive" onClick={() => void confirmDelete()} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete rule"}
              </Button>
              <Button variant="outline" onClick={() => setPending(null)} disabled={deleting}>
                Keep the rule
              </Button>
            </div>
          </div>
        ) : null}
      </Dialog>
    </div>
  );
}

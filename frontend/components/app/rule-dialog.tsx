"use client";

import { Plus, Trash2 } from "lucide-react";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  ApiError,
  createRule,
  updateRule,
  type PricingRule,
  type RuleInput,
} from "@/lib/api";

interface OpDraft {
  op: "convert" | "convert_to_destination" | "percent" | "round";
  rate: string;
  to: string;
  value: string;
  step: string;
}

function toDrafts(ops: Record<string, string>[]): OpDraft[] {
  return ops.map((op) => ({
    op: (op.op as OpDraft["op"]) ?? "percent",
    rate: op.rate ?? "",
    to: op.to ?? "",
    value: op.value ?? "",
    step: op.step ?? "",
  }));
}

function toOps(drafts: OpDraft[]): Record<string, string>[] {
  return drafts.map((d): Record<string, string> => {
    if (d.op === "convert") return { op: "convert", rate: d.rate, to: d.to.toUpperCase() };
    if (d.op === "convert_to_destination") return { op: "convert_to_destination" };
    if (d.op === "round") return { op: "round", step: d.step };
    return { op: "percent", value: d.value };
  });
}

export function RuleDialog({
  rule,
  organizationId,
  open,
  onClose,
  onSaved,
}: {
  rule: PricingRule | null;
  organizationId: number;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = React.useState(rule?.name ?? "");
  const [active, setActive] = React.useState(rule?.active ?? true);
  const [priority, setPriority] = React.useState(String(rule?.priority ?? 100));
  const [strategy, setStrategy] = React.useState<string>(
    rule?.supplier_strategy ?? "lowest",
  );
  const [preferred, setPreferred] = React.useState(rule?.preferred_supplier ?? "");
  const [brand, setBrand] = React.useState(rule?.matcher.brand ?? "");
  const [model, setModel] = React.useState(rule?.matcher.model ?? "");
  const [grade, setGrade] = React.useState(rule?.matcher.grade ?? "");
  const [drafts, setDrafts] = React.useState<OpDraft[]>(() => toDrafts(rule?.ops ?? []));
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    setName(rule?.name ?? "");
    setActive(rule?.active ?? true);
    setPriority(String(rule?.priority ?? 100));
    setStrategy(rule?.supplier_strategy ?? "lowest");
    setPreferred(rule?.preferred_supplier ?? "");
    setBrand(rule?.matcher.brand ?? "");
    setModel(rule?.matcher.model ?? "");
    setGrade(rule?.matcher.grade ?? "");
    setDrafts(toDrafts(rule?.ops ?? []));
    setError(null);
  }, [open, rule]);

  function setDraft(index: number, patch: Partial<OpDraft>) {
    setDrafts(drafts.map((d, i) => (i === index ? { ...d, ...patch } : d)));
  }

  async function save() {
    setBusy(true);
    setError(null);
    const matcher: Record<string, string> = {};
    if (brand.trim()) matcher.brand = brand.trim();
    if (model.trim()) matcher.model = model.trim();
    if (grade.trim()) matcher.grade = grade.trim();
    const input: RuleInput = {
      organization_id: organizationId,
      name: name.trim(),
      matcher,
      supplier_strategy: strategy as RuleInput["supplier_strategy"],
      preferred_supplier: preferred.trim(),
      ops: toOps(drafts),
      priority: Number(priority) || 0,
      active,
    };
    try {
      if (rule) await updateRule(rule.id, input);
      else await createRule(input);
      onSaved();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the rule.");
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={rule ? `Edit rule “${rule.name}”` : "New pricing rule"}
      description="Rules pick one supplier per product, then reprice. More specific matchers win over general ones."
      wide
    >
      <div className="space-y-5">
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-sm font-medium">
            Name
            <Input className="mt-1.5" value={name} onChange={(e) => setName(e.target.value)} required />
          </label>
          <label className="block text-sm font-medium">
            Priority (lower runs first on ties)
            <Input
              className="mt-1.5"
              type="number"
              min={0}
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            />
          </label>
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block text-sm font-medium">
            Supplier strategy
            <Select className="mt-1.5" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
              <option value="lowest">Lowest price</option>
              <option value="highest">Highest price</option>
              <option value="preferred">Preferred supplier</option>
            </Select>
          </label>
          {strategy === "preferred" ? (
            <label className="block text-sm font-medium">
              Preferred supplier
              <Input
                className="mt-1.5"
                value={preferred}
                onChange={(e) => setPreferred(e.target.value)}
                placeholder="Exact supplier name"
              />
            </label>
          ) : null}
        </div>

        <fieldset>
          <legend className="text-sm font-medium">
            Applies to <span className="font-normal text-muted-foreground">(blank = all products)</span>
          </legend>
          <div className="mt-1.5 grid gap-3 sm:grid-cols-3">
            <label className="block text-sm">
              Brand
              <Input className="mt-1.5" value={brand} onChange={(e) => setBrand(e.target.value)} placeholder="Apple" />
            </label>
            <label className="block text-sm">
              Model
              <Input className="mt-1.5" value={model} onChange={(e) => setModel(e.target.value)} placeholder="iPhone 15" />
            </label>
            <label className="block text-sm">
              Grade
              <Input className="mt-1.5" value={grade} onChange={(e) => setGrade(e.target.value)} placeholder="A" />
            </label>
          </div>
        </fieldset>

        <fieldset>
          <legend className="text-sm font-medium">
            Repricing steps <span className="font-normal text-muted-foreground">(applied in order)</span>
          </legend>
          <div className="mt-1.5 space-y-2">
            {drafts.map((draft, index) => (
              <div key={index} className="flex flex-wrap items-center gap-2 rounded-md border border-border p-2">
                <span className="w-6 text-center text-sm font-semibold text-muted-foreground">
                  {index + 1}
                </span>
                <Select
                  className="w-44"
                  value={draft.op}
                  onChange={(e) => setDraft(index, { op: e.target.value as OpDraft["op"] })}
                  aria-label={`Step ${index + 1} type`}
                >
                  <option value="convert">Convert</option>
                  <option value="convert_to_destination">Convert to destination</option>
                  <option value="percent">Percent</option>
                  <option value="round">Round</option>
                </Select>
                {draft.op === "convert" ? (
                  <>
                    <Input
                      className="w-28"
                      placeholder="Rate"
                      value={draft.rate}
                      onChange={(e) => setDraft(index, { rate: e.target.value })}
                      aria-label={`Step ${index + 1} rate`}
                    />
                    <Input
                      className="w-20"
                      placeholder="EUR"
                      maxLength={3}
                      value={draft.to}
                      onChange={(e) => setDraft(index, { to: e.target.value })}
                      aria-label={`Step ${index + 1} currency`}
                    />
                  </>
                ) : null}
                {draft.op === "convert_to_destination" ? (
                  <span className="text-xs text-muted-foreground">
                    Uses the file&apos;s recorded rate for its currency date.
                  </span>
                ) : null}
                {draft.op === "percent" ? (
                  <Input
                    className="w-28"
                    placeholder="+10"
                    value={draft.value}
                    onChange={(e) => setDraft(index, { value: e.target.value })}
                    aria-label={`Step ${index + 1} percent`}
                  />
                ) : null}
                {draft.op === "round" ? (
                  <Input
                    className="w-28"
                    placeholder="Step, e.g. 1"
                    value={draft.step}
                    onChange={(e) => setDraft(index, { step: e.target.value })}
                    aria-label={`Step ${index + 1} step`}
                  />
                ) : null}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setDrafts(drafts.filter((_, i) => i !== index))}
                  aria-label={`Remove step ${index + 1}`}
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>
            ))}
            <Button
              variant="outline"
              size="sm"
              onClick={() =>
                setDrafts([...drafts, { op: "percent", rate: "", to: "", value: "", step: "" }])
              }
            >
              <Plus className="size-4" /> Add step
            </Button>
          </div>
        </fieldset>

        <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
          <input
            type="checkbox"
            checked={active}
            onChange={(e) => setActive(e.target.checked)}
            className="size-4 accent-primary"
          />
          Rule is active
        </label>

        {error ? <ErrorNotice message={error} /> : null}

        <div className="flex gap-2">
          <Button onClick={() => void save()} disabled={busy || !name.trim()}>
            {busy ? "Saving…" : rule ? "Save changes" : "Create rule"}
          </Button>
          <Button variant="outline" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

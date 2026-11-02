"use client";

import { Badge } from "@/components/ui/badge";
import { Combobox } from "@/components/ui/combobox";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/cn";
import { MAPPING_FIELDS, type MappingField } from "@/lib/columns";

export type FieldKind = "column" | "ignore" | "custom";

export interface FieldChoice {
  kind: FieldKind | "";
  position: number | "";
  value: string;
}

export type FieldSelection = Record<string, FieldChoice>;

export interface HeaderOption {
  position: number;
  letter: string;
  header: string;
}

const EMPTY: FieldChoice = { kind: "", position: "", value: "" };

export function selectionFromProposed(
  proposed: Record<string, number>,
  constants: Record<string, string | number>,
  ignored: string[],
): FieldSelection {
  return Object.fromEntries(
    MAPPING_FIELDS.map((field) => {
      if (field.name in constants) {
        return [
          field.name,
          { kind: "custom", position: "", value: String(constants[field.name]) },
        ];
      }
      if (ignored.includes(field.name)) {
        return [field.name, { kind: "ignore", position: "", value: "" }];
      }
      if (field.name in proposed) {
        return [
          field.name,
          { kind: "column", position: proposed[field.name], value: "" },
        ];
      }
      if (field.name === "lock") {
        // Files rarely carry lock info; default to ignored so the preview
        // never nags. Mapping a column or setting a value still works.
        return [field.name, { kind: "ignore", position: "", value: "" }];
      }
      return [field.name, { ...EMPTY }];
    }),
  );
}

export function toPayload(selection: FieldSelection): {
  mapping: Record<string, number>;
  constants: Record<string, string>;
  ignored: string[];
} {
  const mapping: Record<string, number> = {};
  const constants: Record<string, string> = {};
  const ignored: string[] = [];
  for (const [field, choice] of Object.entries(selection)) {
    if (choice.kind === "column" && choice.position !== "") {
      mapping[field] = choice.position;
    } else if (choice.kind === "custom" && choice.value.trim() !== "") {
      constants[field] = choice.value.trim();
    } else if (choice.kind === "ignore") {
      ignored.push(field);
    }
  }
  return { mapping, constants, ignored };
}

export function missingRequired(selection: FieldSelection): MappingField[] {
  return MAPPING_FIELDS.filter((field) => {
    if (!field.required) return false;
    const choice = selection[field.name];
    if (!choice || choice.kind === "" || choice.kind === "ignore") return true;
    if (choice.kind === "custom") return choice.value.trim() === "";
    return choice.position === "";
  });
}

export function shownFields(selection: FieldSelection): MappingField[] {
  return MAPPING_FIELDS.filter((field) => {
    const choice = selection[field.name];
    if (!choice) return false;
    if (choice.kind === "column") return choice.position !== "";
    if (choice.kind === "custom") return choice.value.trim() !== "";
    return false;
  });
}

export function FieldMapper({
  headers,
  selection,
  onChange,
  autoFields,
  currencies,
  valuesOnly,
}: {
  headers: HeaderOption[];
  selection: FieldSelection;
  onChange: (next: FieldSelection) => void;
  autoFields?: string[];
  /** Supported currency codes; null while loading (falls back to text). */
  currencies?: string[] | null;
  /**
   * Message imports have no columns: offer only "not set" or a fixed
   * value per field, and never nag about required fields — the
   * interpretation lane and human reviews cover the gaps.
   */
  valuesOnly?: boolean;
}) {
  function choose(field: string, selectValue: string) {
    const current = selection[field];
    if (selectValue === "ignore") {
      onChange({ ...selection, [field]: { kind: "ignore", position: "", value: "" } });
    } else if (selectValue === "custom") {
      onChange({ ...selection, [field]: { kind: "custom", position: "", value: current.value } });
    } else if (selectValue === "") {
      onChange({ ...selection, [field]: { ...EMPTY } });
    } else {
      onChange({
        ...selection,
        [field]: { kind: "column", position: Number(selectValue), value: "" },
      });
    }
  }

  function setValue(field: string, value: string) {
    onChange({ ...selection, [field]: { ...selection[field], value } });
  }

  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {MAPPING_FIELDS.map((field) => {
        const choice = selection[field.name] ?? { ...EMPTY };
        const selectValue =
          choice.kind === "column"
            ? choice.position === ""
              ? ""
              : String(choice.position)
            : choice.kind === ""
              ? ""
              : choice.kind;
        const unresolved =
          !valuesOnly &&
          field.required &&
          (choice.kind === "" ||
            choice.kind === "ignore" ||
            (choice.kind === "column" && choice.position === "") ||
            (choice.kind === "custom" && choice.value.trim() === ""));
        return (
          <div key={field.name}>
            <label className="block text-sm font-medium">
              <span className="mb-1.5 flex items-center gap-2">
                {field.label}
                {field.required && !valuesOnly ? (
                  <Badge variant="outline">required</Badge>
                ) : null}
                {autoFields?.includes(field.name) ? (
                  <span className="text-xs font-normal text-muted-foreground">auto</span>
                ) : null}
                {choice.kind === "ignore" ? (
                  <Badge variant="secondary">ignored</Badge>
                ) : null}
              </span>
              <Select
                value={selectValue}
                onChange={(event) => choose(field.name, event.target.value)}
                className={cn(unresolved && "border-destructive")}
                aria-label={`${field.label} column`}
              >
                <option value="">{valuesOnly ? "Not set" : "Not mapped"}</option>
                {headers.map((h) => (
                  <option key={h.position} value={h.position}>
                    {h.letter} — {h.header || "(blank header)"}
                  </option>
                ))}
                {valuesOnly ? null : (
                  <option value="ignore">Ignore this field</option>
                )}
                <option value="custom">Set a value…</option>
              </Select>
            </label>
            {choice.kind === "custom" ? (
              field.name === "currency" && Array.isArray(currencies) ? (
                <Combobox
                  className="mt-1.5"
                  value={choice.value}
                  options={currencies}
                  onChange={(code) => setValue(field.name, code)}
                  ariaLabel={`${field.label} value`}
                  placeholder="Type to search…"
                />
              ) : (
                <Input
                  className="mt-1.5"
                  value={choice.value}
                  onChange={(event) => setValue(field.name, event.target.value)}
                  placeholder={`Value for every row, e.g. ${field.name === "supplier" ? "Acme" : field.name === "currency" ? "EUR" : "…"}`}
                  aria-label={`${field.label} value`}
                />
              )
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

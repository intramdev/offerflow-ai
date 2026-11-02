"use client";

import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Combobox } from "@/components/ui/combobox";
import {
  ApiError,
  deleteOrganizationData,
  downloadGdprExport,
  getOrgSettings,
  logout,
  saveBlob,
  updateOrgSettings,
  updatePreferences,
  type QuoteExportTemplate,
} from "@/lib/api";
import { formatDateTime, type DateTimeFormat } from "@/lib/format";

const EXAMPLE = "2026-10-30T16:54:00+00:00";

const FILE_COLUMNS: { key: string; label: string; locked?: boolean; hint?: string }[] = [
  { key: "brand", label: "Brand" },
  { key: "model", label: "Model", locked: true },
  { key: "capacity", label: "Capacity GB" },
  { key: "grade", label: "Grade" },
  { key: "color", label: "Color" },
  { key: "conditions", label: "Conditions" },
  { key: "supplier", label: "Supplier", hint: "Sourcing intelligence — hidden by default" },
  { key: "quantity", label: "Quantity" },
  { key: "price", label: "Unit Price", locked: true },
  { key: "currency", label: "Currency" },
  { key: "rule", label: "Rule" },
  { key: "category", label: "Category", hint: "Only in files that span categories" },
];

type MessageToggleKey =
  | "message_show_capacity"
  | "message_show_grade"
  | "message_show_color"
  | "message_show_quantity"
  | "message_show_categories";

const MESSAGE_TOGGLES: { key: MessageToggleKey; label: string; hint: string }[] = [
  { key: "message_show_capacity", label: "Capacity in rows", hint: "“128GB” after the product name" },
  { key: "message_show_grade", label: "Grade in rows", hint: "“(A)” after the product name" },
  { key: "message_show_color", label: "Color in rows", hint: "“(Black)” after the product name" },
  { key: "message_show_quantity", label: "Quantity in rows", hint: "“×5” after the price" },
  { key: "message_show_categories", label: "Category sections", hint: "Group rows under one header per category" },
];

function OrgCurrencyCard() {
  const { active, canOperate } = useOrg();
  const [fallback, setFallback] = React.useState("");
  const [destination, setDestination] = React.useState("");
  const [currencies, setCurrencies] = React.useState<string[]>([]);
  const [loaded, setLoaded] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!active) {
      setLoaded(false);
      return;
    }
    let cancelled = false;
    setLoaded(false);
    setError(null);
    setSaved(false);
    void getOrgSettings(active.organization)
      .then((settings) => {
        if (cancelled) return;
        setFallback(settings.default_currency);
        setDestination(settings.destination_currency);
        setCurrencies(settings.supported_currencies ?? []);
        setLoaded(true);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not load currencies.");
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  if (!active) return null;

  async function save() {
    if (!active) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const settings = await updateOrgSettings(active.organization, {
        default_currency: fallback.trim().toUpperCase(),
        destination_currency: destination.trim().toUpperCase(),
      });
      setFallback(settings.default_currency);
      setDestination(settings.destination_currency);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save currencies.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Organization currency</CardTitle>
        <CardDescription>
          Shared by {active.organization_name}. Type to search the supported
          currencies.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {!loaded && !error ? (
          <p className="text-sm text-muted-foreground">Loading currencies…</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="block text-sm font-medium">
              <label htmlFor="default-currency">Default currency</label>
              <Combobox
                id="default-currency"
                className="mt-1.5"
                value={fallback}
                options={currencies}
                onChange={(code) => {
                  setFallback(code);
                  setSaved(false);
                }}
                disabled={!canOperate}
                ariaLabel="Default currency"
                placeholder="Type to search…"
                allowEmpty
                emptyLabel="None — leave rows without currency"
              />
              <span className="mt-1 block text-xs font-normal text-muted-foreground">
                Fills in rows whose file has no currency.
              </span>
            </div>
            <div className="block text-sm font-medium">
              <label htmlFor="destination-currency">Destination currency</label>
              <Combobox
                id="destination-currency"
                className="mt-1.5"
                value={destination}
                options={currencies}
                onChange={(code) => {
                  setDestination(code);
                  setSaved(false);
                }}
                disabled={!canOperate}
                ariaLabel="Destination currency"
                placeholder="Type to search…"
                allowEmpty
                emptyLabel="None — no conversion"
              />
              <span className="mt-1 block text-xs font-normal text-muted-foreground">
                Quotes convert to this using each file&apos;s recorded rates.
              </span>
            </div>
          </div>
        )}
        {error ? <ErrorNotice message={error} /> : null}
        <div className="flex items-center gap-3">
          {canOperate ? (
            <Button onClick={() => void save()} disabled={saving || !loaded}>
              {saving ? "Saving…" : "Save currencies"}
            </Button>
          ) : (
            <span className="text-sm text-muted-foreground">
              Only operators can change organization currency.
            </span>
          )}
          {saved ? <span className="text-sm text-muted-foreground">Saved.</span> : null}
        </div>
      </CardContent>
    </Card>
  );
}

function QuoteExportCard() {
  const { active, canOperate } = useOrg();
  const [template, setTemplate] = React.useState<QuoteExportTemplate | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!active) {
      setTemplate(null);
      return;
    }
    let cancelled = false;
    setTemplate(null);
    setError(null);
    setSaved(false);
    void getOrgSettings(active.organization)
      .then((settings) => {
        if (!cancelled) setTemplate(settings.quote_export);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(
            err instanceof ApiError
              ? err.message
              : "Could not load export settings.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [active]);

  if (!active) return null;

  function toggleColumn(key: string) {
    setTemplate((prev) => {
      if (!prev) return prev;
      const has = prev.file_columns.includes(key);
      const columns = has
        ? prev.file_columns.filter((column) => column !== key)
        : [...FILE_COLUMNS.map((c) => c.key).filter((c) => c === key || prev.file_columns.includes(c))];
      return { ...prev, file_columns: columns };
    });
    setSaved(false);
  }

  function toggleMessage(key: MessageToggleKey) {
    setTemplate((prev) =>
      prev ? { ...prev, [key]: !prev[key] } : prev,
    );
    setSaved(false);
  }

  async function save() {
    if (!active || !template) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      const settings = await updateOrgSettings(active.organization, {
        quote_export: template,
      });
      setTemplate(settings.quote_export);
      setSaved(true);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not save exports.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Quote exports</CardTitle>
        <CardDescription>
          Shared by {active.organization_name}. Chooses what customers see in
          the XLSX/CSV files and the Telegram message — for new quotes and
          existing ones alike.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        {template === null && !error ? (
          <p className="text-sm text-muted-foreground">
            Loading export settings…
          </p>
        ) : template === null ? null : (
          <>
            <fieldset disabled={!canOperate}>
              <legend className="text-sm font-medium">File columns</legend>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Model and Unit Price are always included — without them the
                file is not a quote.
              </p>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                {FILE_COLUMNS.map((column) => (
                  <label
                    key={column.key}
                    className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2 text-sm"
                  >
                    <input
                      type="checkbox"
                      checked={template.file_columns.includes(column.key)}
                      onChange={() => toggleColumn(column.key)}
                      disabled={!canOperate || column.locked === true}
                      className="mt-0.5 h-4 w-4 accent-primary"
                    />
                    <span>
                      <span className="font-medium">{column.label}</span>
                      {column.locked === true ? (
                        <span className="text-muted-foreground"> · required</span>
                      ) : null}
                      {column.hint ? (
                        <span className="block text-xs text-muted-foreground">
                          {column.hint}
                        </span>
                      ) : null}
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>
            <fieldset disabled={!canOperate}>
              <legend className="text-sm font-medium">Telegram message</legend>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                {MESSAGE_TOGGLES.map((toggle) => (
                  <label
                    key={toggle.key}
                    className="flex cursor-pointer items-start gap-2 rounded-md border border-border px-3 py-2 text-sm"
                  >
                    <input
                      type="checkbox"
                      checked={template[toggle.key]}
                      onChange={() => toggleMessage(toggle.key)}
                      disabled={!canOperate}
                      className="mt-0.5 h-4 w-4 accent-primary"
                    />
                    <span>
                      <span className="font-medium">{toggle.label}</span>
                      <span className="block text-xs text-muted-foreground">
                        {toggle.hint}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <label className="block text-sm font-medium">
                  Header
                  <input
                    value={template.message_header}
                    onChange={(event) => {
                      setSaved(false);
                      const value = event.target.value;
                      setTemplate((prev) =>
                        prev ? { ...prev, message_header: value } : prev,
                      );
                    }}
                    disabled={!canOperate}
                    placeholder="Ex. Fresh stock — October"
                    maxLength={200}
                    className="mt-1.5 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-normal text-foreground placeholder:text-muted-foreground/60"
                  />
                  <span className="mt-1 block text-xs font-normal text-muted-foreground">
                    Blank keeps “Quote — company — date”.
                  </span>
                </label>
                <label className="block text-sm font-medium">
                  Footer
                  <input
                    value={template.message_footer}
                    onChange={(event) => {
                      setSaved(false);
                      const value = event.target.value;
                      setTemplate((prev) =>
                        prev ? { ...prev, message_footer: value } : prev,
                      );
                    }}
                    disabled={!canOperate}
                    placeholder="Ex. Reply to order"
                    maxLength={200}
                    className="mt-1.5 w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-normal text-foreground placeholder:text-muted-foreground/60"
                  />
                  <span className="mt-1 block text-xs font-normal text-muted-foreground">
                    Blank adds no closing line.
                  </span>
                </label>
              </div>
            </fieldset>
          </>
        )}
        {error ? <ErrorNotice message={error} /> : null}
        <div className="flex items-center gap-3">
          {canOperate ? (
            <Button
              onClick={() => void save()}
              disabled={saving || template === null}
            >
              {saving ? "Saving…" : "Save exports"}
            </Button>
          ) : (
            <span className="text-sm text-muted-foreground">
              Only operators can change export settings.
            </span>
          )}
          {saved ? <span className="text-sm text-muted-foreground">Saved.</span> : null}
        </div>
      </CardContent>
    </Card>
  );
}

function GdprCard() {
  const { active } = useOrg();
  const [confirm, setConfirm] = React.useState("");
  const [busy, setBusy] = React.useState<"export" | "delete" | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [deleted, setDeleted] = React.useState<string | null>(null);

  if (!active) return null;
  const isOwner = active.role === "owner";

  async function exportData() {
    if (!active) return;
    setBusy("export");
    setError(null);
    try {
      const blob = await downloadGdprExport(active.organization);
      saveBlob(
        blob,
        `offerflow-export-${active.organization_name}-${active.organization}.zip`,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Export failed.");
    } finally {
      setBusy(null);
    }
  }

  async function erase() {
    if (!active) return;
    setBusy("delete");
    setError(null);
    try {
      const result = await deleteOrganizationData(
        active.organization,
        confirm,
      );
      setDeleted(
        `Deleted ${result.organization}: ${result.stored_files_removed} files, ` +
          `${result.member_logins_deleted} logins. Signing out…`,
      );
      try {
        await logout();
      } catch {
        // Memberships are gone; leave regardless.
      }
      window.location.assign("/app/login");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Deletion failed.");
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Privacy &amp; data</CardTitle>
        <CardDescription>
          Your rights under the GDPR: take your data or erase it. Both
          actions cover {active.organization_name} only.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {error ? <ErrorNotice message={error} /> : null}
        {deleted ? (
          <p className="text-sm text-muted-foreground">{deleted}</p>
        ) : (
          <>
            <div className="flex flex-wrap items-center gap-3">
              {isOwner ? (
                <Button
                  variant="outline"
                  onClick={() => void exportData()}
                  disabled={busy !== null}
                >
                  {busy === "export"
                    ? "Preparing…"
                    : "Download all my data"}
                </Button>
              ) : (
                <span className="text-sm text-muted-foreground">
                  Only the organization owner can export or erase data.
                </span>
              )}
            </div>
            {isOwner ? (
              <div className="space-y-2 rounded-md border border-red-200 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950/50">
                <p className="text-sm font-semibold text-red-800 dark:text-red-200">
                  Delete everything
                </p>
                <p className="text-sm text-red-700 dark:text-red-300">
                  Removes the organization, every file, run, quote,
                  supplier, and usage record, plus logins left without an
                  organization. This cannot be undone.
                </p>
                <label
                  className="block text-sm font-medium text-red-800 dark:text-red-200"
                  htmlFor="gdpr-confirm"
                >
                  Type {active.organization_name} to confirm
                </label>
                <input
                  id="gdpr-confirm"
                  className="w-full rounded-md border border-red-300 bg-white px-3 py-2 text-sm text-foreground dark:border-red-800 dark:bg-muted"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                  placeholder={active.organization_name}
                />
                <Button
                  variant="destructive"
                  onClick={() => void erase()}
                  disabled={
                    busy !== null || confirm !== active.organization_name
                  }
                >
                  {busy === "delete" ? "Deleting…" : "Delete everything"}
                </Button>
              </div>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  );
}

export default function SettingsPage() {
  const { memberships, prefs, loading, refreshPrefs } = useOrg();
  const [choice, setChoice] = React.useState<DateTimeFormat>("us");
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    setChoice(prefs.datetime_format);
  }, [prefs.datetime_format]);

  async function save() {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await updatePreferences({ datetime_format: choice });
      await refreshPrefs();
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save settings.");
    } finally {
      setSaving(false);
    }
  }

  if (loading) {
    return <p className="text-sm text-muted-foreground">Loading settings…</p>;
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Display preferences apply to you everywhere; organization data is
          shared.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Date and time format</CardTitle>
          <CardDescription>
            Used for upload times, file history, and audit dates.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2" role="radiogroup" aria-label="Date format">
            {(["us", "eu"] as const).map((format) => (
              <label
                key={format}
                className="flex cursor-pointer items-center gap-3 rounded-md border border-border px-4 py-3 text-sm"
              >
                <input
                  type="radio"
                  name="datetime-format"
                  value={format}
                  checked={choice === format}
                  onChange={() => {
                    setChoice(format);
                    setSaved(false);
                  }}
                  className="size-4 accent-primary"
                />
                <span>
                  <span className="font-medium">
                    {format === "us" ? "US (MM/DD/YYYY)" : "European (DD/MM/YYYY)"}
                  </span>
                  <span className="ml-3 text-muted-foreground">
                    {formatDateTime(EXAMPLE, format)}
                  </span>
                </span>
              </label>
            ))}
          </div>
          {error ? <ErrorNotice message={error} /> : null}
          <div className="flex items-center gap-3">
            <Button onClick={() => void save()} disabled={saving || choice === prefs.datetime_format}>
              {saving ? "Saving…" : "Save format"}
            </Button>
            {saved ? <span className="text-sm text-muted-foreground">Saved.</span> : null}
          </div>
        </CardContent>
      </Card>

      <OrgCurrencyCard />

      <QuoteExportCard />

      <GdprCard />

      <Card>
        <CardHeader>
          <CardTitle>Your organizations</CardTitle>
          <CardDescription>Roles are managed by organization owners.</CardDescription>
        </CardHeader>
        <CardContent>
          {memberships.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              You are not a member of any organization yet.
            </p>
          ) : (
            <ul className="divide-y divide-border text-sm">
              {memberships.map((m) => (
                <li key={m.id} className="flex items-center justify-between py-2">
                  <span className="font-medium">{m.organization_name}</span>
                  <span className="text-muted-foreground">{m.role}</span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

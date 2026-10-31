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
} from "@/lib/api";
import { formatDateTime, type DateTimeFormat } from "@/lib/format";

const EXAMPLE = "2026-10-30T16:54:00+00:00";

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
              <div className="space-y-2 rounded-md border border-red-200 bg-red-50 p-4">
                <p className="text-sm font-semibold text-red-800">
                  Delete everything
                </p>
                <p className="text-sm text-red-700">
                  Removes the organization, every file, run, quote,
                  supplier, and usage record, plus logins left without an
                  organization. This cannot be undone.
                </p>
                <label
                  className="block text-sm font-medium text-red-800"
                  htmlFor="gdpr-confirm"
                >
                  Type {active.organization_name} to confirm
                </label>
                <input
                  id="gdpr-confirm"
                  className="w-full rounded-md border border-red-300 bg-white px-3 py-2 text-sm"
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

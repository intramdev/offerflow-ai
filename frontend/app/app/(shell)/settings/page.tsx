"use client";

import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  getOrgSettings,
  updateOrgSettings,
  updatePreferences,
} from "@/lib/api";
import { formatDateTime, type DateTimeFormat } from "@/lib/format";

const EXAMPLE = "2026-10-30T16:54:00+00:00";

function OrgCurrencyCard() {
  const { active, canOperate } = useOrg();
  const [fallback, setFallback] = React.useState("");
  const [destination, setDestination] = React.useState("");
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
          Shared by {active.organization_name}. Codes are three letters, for
          example USD, EUR, or CZK.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {!loaded && !error ? (
          <p className="text-sm text-muted-foreground">Loading currencies…</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="block text-sm font-medium">
              Default currency
              <Input
                className="mt-1.5 uppercase"
                value={fallback}
                maxLength={3}
                placeholder="—"
                disabled={!canOperate}
                onChange={(e) => {
                  setFallback(e.target.value.toUpperCase());
                  setSaved(false);
                }}
              />
              <span className="mt-1 block text-xs font-normal text-muted-foreground">
                Fills in rows whose file has no currency.
              </span>
            </label>
            <label className="block text-sm font-medium">
              Destination currency
              <Input
                className="mt-1.5 uppercase"
                value={destination}
                maxLength={3}
                placeholder="EUR"
                disabled={!canOperate}
                onChange={(e) => {
                  setDestination(e.target.value.toUpperCase());
                  setSaved(false);
                }}
              />
              <span className="mt-1 block text-xs font-normal text-muted-foreground">
                Quotes convert to this using each file&apos;s recorded rates.
              </span>
            </label>
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

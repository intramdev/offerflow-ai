"use client";

import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { useOrg } from "@/components/app/org-context";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import {
  ApiError,
  updatePreferences,
  updateProfile,
  type Preferences,
} from "@/lib/api";

const THEME_OPTIONS = [
  { value: "system", label: "Follow system", hint: "Matches your device setting automatically." },
  { value: "light", label: "Light", hint: "Dark text on a light background." },
  { value: "dark", label: "Dark", hint: "Light text on a dark background." },
] as const;

const FONT_OPTIONS = [
  { value: "system", label: "System default" },
  { value: "readable", label: "Highly legible (Verdana-based)" },
  { value: "serif", label: "Serif" },
  { value: "mono", label: "Monospace" },
] as const;

const SIZE_OPTIONS = [
  { value: "sm", label: "Compact" },
  { value: "md", label: "Default" },
  { value: "lg", label: "Large" },
  { value: "xl", label: "Extra large" },
] as const;

const CONTRAST_OPTIONS = [
  { value: "standard", label: "Standard", hint: "The default palette (WCAG AA)." },
  { value: "high", label: "High contrast", hint: "Stronger text and border contrast." },
] as const;

const MOTION_OPTIONS = [
  { value: "system", label: "Follow system" },
  { value: "reduce", label: "Reduce motion" },
  { value: "full", label: "Full motion" },
] as const;

function ChoiceGroup<T extends string>({
  legend,
  description,
  name,
  value,
  options,
  onChange,
}: {
  legend: string;
  description?: string;
  name: string;
  value: T;
  options: ReadonlyArray<{ value: T; label: string; hint?: string }>;
  onChange: (value: T) => void;
}) {
  return (
    <fieldset>
      <legend className="text-sm font-medium">{legend}</legend>
      {description ? (
        <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>
      ) : null}
      <div className="mt-2 grid gap-2 sm:grid-cols-3">
        {options.map((option) => (
          <label
            key={option.value}
            className="flex cursor-pointer items-start gap-2.5 rounded-md border border-border px-3 py-2.5 text-sm has-checked:border-primary has-checked:bg-primary/5"
          >
            <input
              type="radio"
              name={name}
              value={option.value}
              checked={value === option.value}
              onChange={() => onChange(option.value)}
              className="mt-0.5 size-4 shrink-0 accent-primary"
            />
            <span>
              <span className="font-medium">{option.label}</span>
              {option.hint ? (
                <span className="block text-xs font-normal text-muted-foreground">
                  {option.hint}
                </span>
              ) : null}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export default function ProfilePage() {
  const { user, prefs, loading, refreshPrefs, refreshUser } = useOrg();
  const [firstName, setFirstName] = React.useState("");
  const [lastName, setLastName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [choices, setChoices] = React.useState<Preferences | null>(null);
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (user) {
      setFirstName(user.first_name);
      setLastName(user.last_name);
      setEmail(user.email);
    }
  }, [user]);

  React.useEffect(() => {
    setChoices(prefs);
  }, [prefs]);

  if (loading || !choices) {
    return <p className="text-sm text-muted-foreground">Loading profile…</p>;
  }

  function setChoice<K extends keyof Preferences>(key: K, value: Preferences[K]) {
    setChoices((prev) => (prev ? { ...prev, [key]: value } : prev));
    setSaved(false);
  }

  async function save() {
    if (!choices) return;
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await updateProfile({
        first_name: firstName.trim(),
        last_name: lastName.trim(),
        email: email.trim(),
      });
      await updatePreferences(choices);
      await refreshUser();
      await refreshPrefs();
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save profile.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Profile</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Your details and reading preferences. Everything on this page is
          stored with your account and applies on every device.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Personal details</CardTitle>
          <CardDescription>
            Shown to organization members alongside your work.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm font-medium">
              Username
              <Input className="mt-1.5" value={user?.username ?? ""} disabled aria-describedby="username-note" />
              <span id="username-note" className="mt-1 block text-xs font-normal text-muted-foreground">
                Usernames cannot be changed.
              </span>
            </label>
            <label className="block text-sm font-medium">
              Email
              <Input
                className="mt-1.5"
                type="email"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  setSaved(false);
                }}
                autoComplete="email"
              />
            </label>
            <label className="block text-sm font-medium">
              First name
              <Input
                className="mt-1.5"
                value={firstName}
                onChange={(event) => {
                  setFirstName(event.target.value);
                  setSaved(false);
                }}
                autoComplete="given-name"
              />
            </label>
            <label className="block text-sm font-medium">
              Last name
              <Input
                className="mt-1.5"
                value={lastName}
                onChange={(event) => {
                  setLastName(event.target.value);
                  setSaved(false);
                }}
                autoComplete="family-name"
              />
            </label>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Appearance</CardTitle>
          <CardDescription>
            Theme, typeface, and text size. Changes apply as soon as you save.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <ChoiceGroup
            legend="Color theme"
            name="theme-mode"
            value={choices.theme_mode}
            options={THEME_OPTIONS}
            onChange={(value) => setChoice("theme_mode", value)}
          />
          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block text-sm font-medium">
              Typeface
              <Select
                className="mt-1.5"
                value={choices.reading_font}
                onChange={(event) =>
                  setChoice("reading_font", event.target.value as Preferences["reading_font"])
                }
              >
                {FONT_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </label>
            <label className="block text-sm font-medium">
              Text size
              <Select
                className="mt-1.5"
                value={choices.reading_size}
                onChange={(event) =>
                  setChoice("reading_size", event.target.value as Preferences["reading_size"])
                }
              >
                {SIZE_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </label>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Accessibility</CardTitle>
          <CardDescription>
            Extra support for contrast and motion, following EN 301 549 /
            WCAG 2.1 AA guidance.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <ChoiceGroup
            legend="Contrast"
            name="contrast-mode"
            value={choices.contrast_mode}
            options={CONTRAST_OPTIONS}
            onChange={(value) => setChoice("contrast_mode", value)}
          />
          <div className="max-w-sm">
            <label className="block text-sm font-medium">
              Motion
              <Select
                className="mt-1.5"
                value={choices.motion_mode}
                onChange={(event) =>
                  setChoice("motion_mode", event.target.value as Preferences["motion_mode"])
                }
              >
                {MOTION_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </label>
            <p className="mt-1 text-xs text-muted-foreground">
              “Reduce motion” disables animation throughout the app.
            </p>
          </div>
        </CardContent>
      </Card>

      {error ? <ErrorNotice message={error} /> : null}
      <div className="flex items-center gap-3">
        <Button onClick={() => void save()} disabled={saving}>
          {saving ? "Saving…" : "Save profile"}
        </Button>
        {saved ? <span className="text-sm text-muted-foreground">Saved.</span> : null}
      </div>
    </div>
  );
}

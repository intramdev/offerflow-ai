"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, login } from "@/lib/api";

function LoginForm() {
  const router = useRouter();
  const next = useSearchParams().get("next") || "/app";
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      router.push(next);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed.");
      setBusy(false);
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>OfferFlow AI</CardTitle>
        <CardDescription>Sign in to your workspace.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={(event) => void submit(event)} className="space-y-4">
          <label className="block text-sm font-medium">
            Username
            <Input
              className="mt-1.5"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          </label>
          <label className="block text-sm font-medium">
            Password
            <Input
              className="mt-1.5"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {error ? <ErrorNotice message={error} /> : null}
          <Button className="w-full" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted px-4">
      <React.Suspense>
        <LoginForm />
      </React.Suspense>
    </div>
  );
}

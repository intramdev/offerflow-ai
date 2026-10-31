"use client";

import Image from "next/image";
import Link from "next/link";
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
        <div className="flex items-center gap-3">
          <Image
            src="/offerflow-logo.svg"
            alt=""
            width={48}
            height={48}
            aria-hidden
          />
          <div>
            <CardTitle className="text-2xl">OfferFlow AI</CardTitle>
            <CardDescription>Sign in to your workspace.</CardDescription>
          </div>
        </div>
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
          <p className="text-center text-sm">
            <Link href="/app/reset-password" className="font-medium text-primary underline">
              Forgot your password?
            </Link>
          </p>
        </form>
      </CardContent>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-muted px-4 py-8">
      <React.Suspense>
        <LoginForm />
      </React.Suspense>
      <p className="text-xs text-muted-foreground">
        By signing in you accept the{" "}
        <Link href="/terms" className="underline">
          Terms of service
        </Link>{" "}
        and{" "}
        <Link href="/privacy" className="underline">
          Privacy policy
        </Link>
        .
      </p>
    </div>
  );
}

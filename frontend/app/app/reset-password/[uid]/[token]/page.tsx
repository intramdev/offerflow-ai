"use client";

import Image from "next/image";
import Link from "next/link";
import { useParams } from "next/navigation";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, confirmPasswordReset } from "@/lib/api";

export default function ResetConfirmPage() {
  const params = useParams<{ uid: string; token: string }>();
  const [password, setPassword] = React.useState("");
  const [repeat, setRepeat] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [done, setDone] = React.useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (password !== repeat) {
      setError("The two passwords do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setDone(await confirmPasswordReset(params.uid, params.token, password));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-muted px-4 py-8">
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
              <CardTitle className="text-2xl">New password</CardTitle>
              <CardDescription>
                Choose the password for your account.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {done ? (
            <div className="space-y-4">
              <p className="text-sm text-foreground">{done}</p>
              <Link href="/app/login" className="text-sm font-medium text-primary underline">
                Sign in with the new password
              </Link>
            </div>
          ) : (
            <form onSubmit={(event) => void submit(event)} className="space-y-4">
              <label className="block text-sm font-medium">
                New password
                <Input
                  className="mt-1.5"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
              </label>
              <label className="block text-sm font-medium">
                Repeat new password
                <Input
                  className="mt-1.5"
                  type="password"
                  autoComplete="new-password"
                  value={repeat}
                  onChange={(event) => setRepeat(event.target.value)}
                  required
                />
              </label>
              {error ? <ErrorNotice message={error} /> : null}
              <Button className="w-full" type="submit" disabled={busy}>
                {busy ? "Changing…" : "Change password"}
              </Button>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

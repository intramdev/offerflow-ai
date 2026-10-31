"use client";

import Image from "next/image";
import Link from "next/link";
import * as React from "react";

import { ErrorNotice } from "@/components/app/error-notice";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ApiError, requestPasswordReset } from "@/lib/api";

export default function ResetRequestPage() {
  const [email, setEmail] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [sent, setSent] = React.useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setSent(await requestPasswordReset(email.trim()));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not send the email.");
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
              <CardTitle className="text-2xl">Reset password</CardTitle>
              <CardDescription>
                We email you a link valid for 30 minutes.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {sent ? (
            <div className="space-y-4">
              <p className="text-sm text-foreground">{sent}</p>
              <p className="text-sm text-muted-foreground">
                The link is single-use: opening it after a password change, or
                after 30 minutes, shows an error instead.
              </p>
              <Link href="/app/login" className="text-sm font-medium text-primary underline">
                Back to sign in
              </Link>
            </div>
          ) : (
            <form onSubmit={(event) => void submit(event)} className="space-y-4">
              <label className="block text-sm font-medium">
                Account email
                <Input
                  className="mt-1.5"
                  type="email"
                  autoComplete="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  required
                />
              </label>
              {error ? <ErrorNotice message={error} /> : null}
              <Button className="w-full" type="submit" disabled={busy}>
                {busy ? "Sending…" : "Send reset link"}
              </Button>
              <p className="text-center text-sm">
                <Link href="/app/login" className="font-medium text-primary underline">
                  Back to sign in
                </Link>
              </p>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

"use client";

import Image from "next/image";
import * as React from "react";

import { useOrg } from "@/components/app/org-context";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  askAssistant,
  getAssistantStatus,
  type AssistantTurn,
} from "@/lib/api";
import { cn } from "@/lib/cn";

interface Bubble extends AssistantTurn {
  key: number;
  tokens?: number;
}

export function AssistantWidget() {
  const { active } = useOrg();
  const [available, setAvailable] = React.useState(false);
  const [open, setOpen] = React.useState(false);
  const [turns, setTurns] = React.useState<Bubble[]>([]);
  const [draft, setDraft] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const keyCounter = React.useRef(0);
  const bottomRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    setAvailable(false);
    setOpen(false);
    setTurns([]);
    setError("");
    if (!active) return;
    let cancelled = false;
    void getAssistantStatus(active.organization).then(
      (status) => {
        if (!cancelled) setAvailable(status.available);
      },
      () => {
        if (!cancelled) setAvailable(false);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [active]);

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, open]);

  async function send() {
    const text = draft.trim();
    if (!active || !text || busy) return;
    setError("");
    setDraft("");
    keyCounter.current += 1;
    const mine: Bubble = {
      key: keyCounter.current,
      role: "user",
      content: text.slice(0, 2000),
    };
    const history: AssistantTurn[] = [
      ...turns.map(({ role, content }) => ({ role, content })),
      { role: mine.role, content: mine.content },
    ];
    setTurns((prev) => [...prev, mine]);
    setBusy(true);
    try {
      const answer = await askAssistant(active.organization, history);
      keyCounter.current += 1;
      setTurns((prev) => [
        ...prev,
        {
          key: keyCounter.current,
          role: "assistant",
          content: answer.reply,
          tokens: answer.tokens,
        },
      ]);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Could not reach the assistant.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (!active || !available) return null;

  return (
    <div className="fixed right-5 bottom-5 z-50 flex flex-col items-end gap-3">
      {open ? (
        <Card className="flex h-[480px] w-[min(92vw,380px)] flex-col overflow-hidden shadow-xl">
          <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
            <Image
              src="/muse-icon.svg"
              alt=""
              width={22}
              height={22}
              aria-hidden
            />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold">OfferFlow assistant</p>
              <p className="truncate text-xs text-muted-foreground">
                Help with app features · answers spend credits
              </p>
            </div>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close assistant"
              className="rounded-md px-2 py-1 text-lg leading-none text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              ×
            </button>
          </div>
          <div
            className="flex-1 space-y-3 overflow-y-auto px-4 py-3"
            aria-live="polite"
          >
            {turns.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                Ask how to use OfferFlow — imports, rules, matching, quotes,
                suppliers, credits. I can&apos;t see your data or anyone
                else&apos;s.
              </p>
            ) : (
              turns.map((turn) => (
                <div
                  key={turn.key}
                  className={cn(
                    "max-w-[85%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap",
                    turn.role === "user"
                      ? "ml-auto bg-primary text-primary-foreground"
                      : "bg-muted text-foreground",
                  )}
                >
                  {turn.content}
                  {turn.role === "assistant" &&
                  typeof turn.tokens === "number" ? (
                    <span className="mt-1 block text-xs opacity-70">
                      {turn.tokens} credits
                    </span>
                  ) : null}
                </div>
              ))
            )}
            {busy ? (
              <p className="text-sm text-muted-foreground">Thinking…</p>
            ) : null}
            <div ref={bottomRef} />
          </div>
          {error ? (
            <p role="alert" className="border-t border-border px-4 py-2 text-xs text-red-600 dark:text-red-400">
              {error}
            </p>
          ) : null}
          <form
            className="flex gap-2 border-t border-border p-3"
            onSubmit={(event) => {
              event.preventDefault();
              void send();
            }}
          >
            <Input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Ex. How do I remap a file?"
              aria-label="Ask the assistant"
              maxLength={2000}
              disabled={busy}
              className="flex-1"
            />
            <Button type="submit" disabled={busy || draft.trim() === ""}>
              {busy ? "…" : "Send"}
            </Button>
          </form>
        </Card>
      ) : null}
      <button
        type="button"
        onClick={() => setOpen((was) => !was)}
        aria-label={open ? "Hide assistant" : "Ask the assistant"}
        title="Ask the assistant"
        className="flex size-14 items-center justify-center rounded-full border border-border bg-card shadow-lg transition hover:scale-105"
      >
        <Image src="/muse-icon.svg" alt="Muse" width={30} height={30} />
      </button>
    </div>
  );
}

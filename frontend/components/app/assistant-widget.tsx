"use client";

import Image from "next/image";
import * as React from "react";

import { summarizeOperation } from "@/components/app/draft-ops";
import { useOrg } from "@/components/app/org-context";
import { usePreviewAssist } from "@/components/app/preview-assist-context";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ApiError,
  applyPreviewAssist,
  askAssistant,
  assistPreview,
  getAssistantStatus,
  type AssistantTurn,
  type PreviewAssistProposal,
} from "@/lib/api";
import { cn } from "@/lib/cn";

interface Bubble extends AssistantTurn {
  key: number;
  tokens?: number;
}

interface PendingProposal {
  bubbleKey: number;
  proposal: PreviewAssistProposal;
  /** Draft revision the proposal was computed against. */
  baseRevision: number;
  /** Checked operation cards, by index into proposal.operations. */
  selected: boolean[];
}

function isEmptyProposal(proposal: PreviewAssistProposal): boolean {
  return (
    Object.keys(proposal.set_constants).length === 0 &&
    proposal.drop_lines.length === 0 &&
    (proposal.operations ?? []).length === 0
  );
}

export function AssistantWidget() {
  const { active } = useOrg();
  const { session, draft: assistDraft, spotlight } = usePreviewAssist();
  const sessionKey = session?.key ?? null;
  const [available, setAvailable] = React.useState(false);
  const [open, setOpen] = React.useState(false);
  const [turns, setTurns] = React.useState<Bubble[]>([]);
  const [draft, setDraft] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [applying, setApplying] = React.useState(false);
  const [pending, setPending] = React.useState<PendingProposal | null>(null);
  const [notice, setNotice] = React.useState("");
  const [error, setError] = React.useState("");
  const keyCounter = React.useRef(0);
  const bottomRef = React.useRef<HTMLDivElement | null>(null);
  const [spotlightOn, setSpotlightOn] = React.useState(false);
  const openRef = React.useRef(open);
  openRef.current = open;
  const seenSpotlight = React.useRef(0);

  // Announce the assistant when something paste-worthy happens (e.g. a
  // price list lands in the upload box): pulse the button, then open
  // the panel so the user sees where the help lives.
  React.useEffect(() => {
    if (spotlight === seenSpotlight.current || openRef.current) return;
    seenSpotlight.current = spotlight;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setOpen(true);
      return;
    }
    setSpotlightOn(true);
    const timer = setTimeout(() => {
      setSpotlightOn(false);
      setOpen(true);
    }, 900);
    return () => clearTimeout(timer);
  }, [spotlight]);

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

  // One conversation per preview: a new pending preview starts fresh,
  // and leaving the preview returns the widget to help mode. A commit
  // the widget itself triggered keeps the turns it just sent. "pending"
  // can never collide with a real key (keys are at least 8 chars).
  const committedRef = React.useRef<string | null>(null);
  React.useEffect(() => {
    if (
      sessionKey &&
      (committedRef.current === "pending" ||
        committedRef.current === sessionKey)
    ) {
      committedRef.current = null;
      return;
    }
    setTurns([]);
    setPending(null);
    setNotice("");
    setError("");
    setDraft("");
  }, [sessionKey]);

  React.useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, open, pending, notice]);

  function pushTurn(role: "user" | "assistant", content: string, tokens?: number) {
    keyCounter.current += 1;
    const bubble: Bubble = { key: keyCounter.current, role, content, tokens };
    setTurns((prev) => [...prev, bubble]);
    return bubble;
  }

  async function assistWithRecovery(
    key: string,
    messages: AssistantTurn[],
    requestKey: string,
  ) {
    try {
      return await assistPreview(key, messages, requestKey);
    } catch (err) {
      // Only a live first attempt (409) is worth polling: the same
      // key can never dispatch twice, so polling cannot double-spend.
      if (!(err instanceof ApiError) || err.status !== 409) throw err;
    }
    setNotice("Still working — collecting the result…");
    for (let attempt = 0; attempt < 10; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, 3000));
      try {
        return await assistPreview(key, messages, requestKey);
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 409) throw err;
      }
    }
    throw new Error("The assistant is still working. Retry to collect the result.");
  }

  async function runAssist(
    key: string,
    history: AssistantTurn[],
    requestKey: string,
  ) {
    const answer = await assistWithRecovery(key, history.slice(-6), requestKey);
    if (answer.refusal) {
      pushTurn("assistant", answer.refusal);
      return;
    }
    const bubble = pushTurn("assistant", answer.message, answer.tokens);
    const notes: string[] = [];
    if (answer.replayed) {
      notes.push(
        "Recovered the completed result after the interruption — no second call was charged.",
      );
    }
    if (answer.hint) {
      notes.push(answer.hint);
    }
    const coverage = answer.context_coverage;
    if (coverage?.truncated) {
      notes.push(
        `The assistant could only read ${coverage.lines_shown} of ${coverage.total_rows} pasted lines, so suggestions may miss later content.`,
      );
    }
    if (notes.length > 0) {
      setNotice(notes.join(" "));
    }
    if (!isEmptyProposal(answer.proposal)) {
      setPending({
        bubbleKey: bubble.key,
        proposal: answer.proposal,
        baseRevision: answer.base_revision,
        selected: (answer.proposal.operations ?? []).map(() => true),
      });
    } else {
      setPending(null);
    }
  }

  const retryRef = React.useRef<{
    key: string;
    history: AssistantTurn[];
    requestKey: string;
  } | null>(null);
  const [canRetry, setCanRetry] = React.useState(false);

  async function send() {
    const text = draft.trim();
    if (!active || !text || busy || applying) return;
    setError("");
    setNotice("");
    setDraft("");
    setCanRetry(false);
    retryRef.current = null;
    const requestKey = crypto.randomUUID();
    const mine = pushTurn("user", text.slice(0, 2000));
    const history: AssistantTurn[] = [
      ...turns.map(({ role, content }) => ({ role, content })),
      { role: mine.role, content: mine.content },
    ];
    setBusy(true);
    try {
      if (session) {
        retryRef.current = { key: session.key, history, requestKey };
        await runAssist(session.key, history, requestKey);
      } else if (assistDraft) {
        committedRef.current = "pending";
        let created;
        try {
          created = await assistDraft.commit();
        } catch (err) {
          committedRef.current = null;
          throw err;
        }
        committedRef.current = created.key;
        retryRef.current = { key: created.key, history, requestKey };
        await runAssist(created.key, history, requestKey);
      } else {
        const answer = await askAssistant(active.organization, history);
        pushTurn("assistant", answer.reply, answer.tokens);
      }
      retryRef.current = null;
    } catch (err) {
      if (retryRef.current) {
        // The first attempt may have dispatched: retry reuses its key,
        // so a completed result replays without a second charge. If
        // the attempt failed instead, the retry is a new call that
        // may spend again.
        setCanRetry(true);
      } else {
        setDraft(text);
      }
      setError(
        err instanceof ApiError || err instanceof Error
          ? err.message
          : "Could not reach the assistant.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function retrySend() {
    const retry = retryRef.current;
    if (!retry || busy || applying) return;
    setError("");
    setCanRetry(false);
    setBusy(true);
    try {
      await runAssist(retry.key, retry.history, retry.requestKey);
      retryRef.current = null;
    } catch (err) {
      setCanRetry(true);
      setError(
        err instanceof ApiError || err instanceof Error
          ? err.message
          : "Could not reach the assistant.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function applyPending() {
    if (!session || !pending || busy || applying) return;
    const revision = session.getRevision();
    if (revision == null) {
      setError(
        "The preview revision is not loaded yet. Reload the preview and apply again.",
      );
      return;
    }
    if (pending.baseRevision !== revision) {
      // The draft moved since this proposal was computed (e.g. a manual
      // correction): applying it could supersede confirmed decisions.
      // The server enforces the same check; stop here with guidance.
      setError(
        `This proposal was made for revision ${pending.baseRevision}, but the draft is now at revision ${revision}. Ask the assistant again so the proposal is recomputed.`,
      );
      return;
    }
    const operations = (pending.proposal.operations ?? []).filter(
      (_, index) => pending.selected[index] !== false,
    );
    setError("");
    setNotice("");
    setApplying(true);
    try {
      const result = await applyPreviewAssist(session.key, {
        set_constants: pending.proposal.set_constants,
        drop_lines: pending.proposal.drop_lines,
        operations,
        revision,
        base_revision: pending.baseRevision,
      });
      const parts: string[] = [];
      const names = Object.keys(result.applied.set_constants);
      if (names.length > 0) {
        parts.push(
          `set ${names.map((name) => `${name} = ${result.applied.set_constants[name]}`).join(", ")}`,
        );
      }
      if (result.applied.drop_lines.length > 0) {
        const count = result.applied.drop_lines.length;
        parts.push(`dropped ${count} line${count === 1 ? "" : "s"}`);
      }
      const opCount = (result.applied.operations ?? []).length;
      if (opCount > 0) {
        parts.push(
          `applied ${opCount} interpretation change${opCount === 1 ? "" : "s"}`,
        );
      }
      session.onApplied(result.preview);
      setPending(null);
      pushTurn(
        "assistant",
        `Applied — ${parts.join("; ")}. The preview on this page is updated (revision ${result.applied.revision}).`,
      );
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // The draft moved since this proposal was computed. The
        // server message already says to ask again; keep the stale
        // proposal visible for reference but do not resend it.
        setError(err.message);
      } else {
        setError(
          err instanceof ApiError ? err.message : "Could not apply the proposal.",
        );
      }
    } finally {
      setApplying(false);
    }
  }

  if (!active || !available) return null;

  const assistMode = session !== null || assistDraft !== null;
  const pendingConstants = pending
    ? Object.entries(pending.proposal.set_constants)
    : [];

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
              <p className="text-sm font-semibold">
                {assistMode ? "Preview assistant" : "OfferFlow assistant"}
              </p>
              <p className="truncate text-xs text-muted-foreground">
                {session
                  ? "Proposes preview changes · you apply them"
                  : assistDraft
                    ? `Previews your ${assistDraft.label}, then proposes`
                    : "Help with app features · answers spend credits"}
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
                {session ? (
                  <>
                    I can see this pending preview
                    {session.messageLines
                      ? " — explain what the lines mean (supplier, grade sections, which number is the price) and I will propose typed interpretation changes for you to review and apply."
                      : " — tell me values to set for every row, and I will propose the change for you to apply."}
                  </>
                ) : assistDraft ? (
                  <>
                    Tell me what to do with your {assistDraft.label} — I will
                    preview it first, then propose the change for you to
                    apply.
                  </>
                ) : (
                  <>
                    Ask how to use OfferFlow — imports, rules, matching,
                    quotes, suppliers, credits. I can&apos;t see your data or
                    anyone else&apos;s.
                  </>
                )}
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
            {pending ? (
              <div className="rounded-lg border border-border bg-card p-3 text-sm">
                <p className="font-semibold">Proposed change</p>
                {pendingConstants.length > 0 ? (
                  <ul className="mt-2 space-y-1">
                    {pendingConstants.map(([name, value]) => (
                      <li key={name}>
                        Set <strong>{name}</strong> = {value}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {pending.proposal.drop_lines.length > 0 ? (
                  <ul className="mt-2 space-y-1">
                    {pending.proposal.dropped_previews.map((item) => (
                      <li key={item.line}>
                        Drop line <strong>{item.line}</strong>
                        {item.text ? (
                          <span className="block truncate text-xs text-muted-foreground">
                            {item.text}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                ) : null}
                {(pending.proposal.operations ?? []).length > 0 ? (
                  <ul className="mt-2 space-y-2">
                    {(pending.proposal.operations ?? []).map((op, index) => {
                      const summary = summarizeOperation(op);
                      const checked = pending.selected[index] !== false;
                      return (
                        <li
                          key={index}
                          className="flex items-start gap-2 rounded-md border border-border px-2 py-1.5"
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            disabled={applying || busy}
                            onChange={() =>
                              setPending((current) =>
                                current
                                  ? {
                                      ...current,
                                      selected: current.selected.map(
                                        (was, at) =>
                                          at === index ? !was : was,
                                      ),
                                    }
                                  : current,
                              )
                            }
                            aria-label={`Include: ${summary.title}`}
                            className="mt-1"
                          />
                          <span>
                            <span className="block font-medium">
                              {summary.title}
                            </span>
                            {summary.detail ? (
                              <span className="block text-xs text-muted-foreground">
                                {summary.detail}
                              </span>
                            ) : null}
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                ) : null}
                <div className="mt-3 flex gap-2">
                  <Button
                    size="sm"
                    onClick={() => void applyPending()}
                    disabled={applying || busy}
                  >
                    {applying
                      ? "Applying…"
                      : (pending.proposal.operations ?? []).length > 0
                        ? `Apply ${pending.selected.filter(Boolean).length} of ${(pending.proposal.operations ?? []).length} to preview`
                        : "Apply to preview"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setPending(null);
                      pushTurn(
                        "assistant",
                        "Proposal discarded — the preview is unchanged.",
                      );
                    }}
                    disabled={applying || busy}
                  >
                    Discard
                  </Button>
                </div>
              </div>
            ) : null}
            {busy ? (
              <p className="text-sm text-muted-foreground">Thinking…</p>
            ) : null}
            <div ref={bottomRef} />
          </div>
          {notice ? (
            <p className="border-t border-border px-4 py-2 text-xs text-muted-foreground">
              {notice}
            </p>
          ) : null}
          {error ? (
            <p role="alert" className="border-t border-border px-4 py-2 text-xs text-red-600 dark:text-red-400">
              {error}{" "}
              {canRetry ? (
                <button
                  type="button"
                  onClick={() => void retrySend()}
                  disabled={busy || applying}
                  className="font-semibold underline hover:no-underline disabled:opacity-50"
                >
                  Retry
                </button>
              ) : null}
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
              placeholder={
                assistMode
                  ? "Ex. Set supplier to Acme, drop lines 1 and 5"
                  : "Ex. How do I remap a file?"
              }
              aria-label="Ask the assistant"
              maxLength={2000}
              disabled={busy || applying}
              className="flex-1"
            />
            <Button type="submit" disabled={busy || applying || draft.trim() === ""}>
              {busy ? "…" : "Send"}
            </Button>
          </form>
        </Card>
      ) : null}
      <span className="relative flex">
        {spotlightOn ? (
          <span
            aria-hidden
            className="absolute inline-flex size-full animate-ping rounded-full bg-primary opacity-40 motion-reduce:hidden"
          />
        ) : null}
        <button
          type="button"
          onClick={() => setOpen((was) => !was)}
          aria-label={open ? "Hide assistant" : "Ask the assistant"}
          title="Ask the assistant"
          className="relative flex size-14 items-center justify-center rounded-full border border-border bg-card shadow-lg transition hover:scale-105"
        >
          <Image src="/muse-icon.svg" alt="Muse" width={30} height={30} />
        </button>
      </span>
    </div>
  );
}

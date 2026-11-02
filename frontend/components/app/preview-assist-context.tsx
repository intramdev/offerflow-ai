"use client";

import * as React from "react";

import type { Preview } from "@/lib/api";

export interface PreviewAssistSession {
  key: string;
  messageLines: boolean;
  filename: string;
  /** Owner applies the fresh preview after a confirmed assist. */
  onApplied: (preview: Preview) => void;
}

export interface PreviewAssistDraft {
  label: string;
  /** Create the preview from not-yet-previewed input, then return it. */
  commit: () => Promise<Preview>;
}

interface PreviewAssistState {
  session: PreviewAssistSession | null;
  draft: PreviewAssistDraft | null;
  /** Bumps each time the widget should announce itself. */
  spotlight: number;
  register: (session: PreviewAssistSession) => void;
  unregister: (key: string) => void;
  setDraft: (draft: PreviewAssistDraft | null) => void;
  spotlightAssistant: () => void;
}

const PreviewAssistContext = React.createContext<PreviewAssistState | null>(
  null,
);

export function PreviewAssistProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [session, setSession] = React.useState<PreviewAssistSession | null>(
    null,
  );
  const [draft, setDraftState] = React.useState<PreviewAssistDraft | null>(
    null,
  );
  const [spotlight, setSpotlight] = React.useState(0);
  const value = React.useMemo<PreviewAssistState>(
    () => ({
      session,
      draft,
      spotlight,
      register: (next) => setSession(next),
      unregister: (key) =>
        setSession((current) => (current?.key === key ? null : current)),
      setDraft: (next) => setDraftState(next),
      spotlightAssistant: () => setSpotlight((count) => count + 1),
    }),
    [session, draft, spotlight],
  );
  return (
    <PreviewAssistContext.Provider value={value}>
      {children}
    </PreviewAssistContext.Provider>
  );
}

export function usePreviewAssist(): PreviewAssistState {
  const state = React.useContext(PreviewAssistContext);
  if (!state) {
    throw new Error("usePreviewAssist needs PreviewAssistProvider.");
  }
  return state;
}

"use client";

import * as React from "react";

import type { Preferences } from "@/lib/api";

const CACHE_KEY = "of-display";

interface CachedDisplay {
  dark: boolean;
  font: string;
  size: string;
  contrast: string;
  motion: string;
}

/** Runs before first paint (see root layout): replays cached display. */
export const THEME_BOOT_SCRIPT = `
(function(){try{var c=JSON.parse(localStorage.getItem("${CACHE_KEY}")||"null");if(!c)return;
var r=document.documentElement;
if(c.dark){r.classList.add("dark");}
r.dataset.readingFont=c.font||"system";
r.dataset.readingSize=c.size||"md";
r.dataset.contrast=c.contrast||"standard";
r.dataset.motion=c.motion||"system";}catch(e){}})();
`;

function systemDark(): boolean {
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function resolveDark(themeMode: Preferences["theme_mode"]): boolean {
  if (themeMode === "dark") return true;
  if (themeMode === "light") return false;
  return systemDark();
}

function applyDisplay(prefs: Preferences): void {
  const root = document.documentElement;
  const dark = resolveDark(prefs.theme_mode);
  root.classList.toggle("dark", dark);
  root.dataset.readingFont = prefs.reading_font;
  root.dataset.readingSize = prefs.reading_size;
  root.dataset.contrast = prefs.contrast_mode;
  root.dataset.motion = prefs.motion_mode;
  const cached: CachedDisplay = {
    dark,
    font: prefs.reading_font,
    size: prefs.reading_size,
    contrast: prefs.contrast_mode,
    motion: prefs.motion_mode,
  };
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(cached));
  } catch {
    // Private browsing: the live DOM still has the right attributes.
  }
}

/** Applies the user's saved display prefs; follows the OS when asked. */
export function ThemeProvider({
  prefs,
  children,
}: {
  prefs: Preferences;
  children: React.ReactNode;
}) {
  React.useEffect(() => {
    applyDisplay(prefs);
    if (prefs.theme_mode !== "system") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = () => applyDisplay(prefs);
    query.addEventListener("change", follow);
    return () => query.removeEventListener("change", follow);
  }, [prefs]);

  return <>{children}</>;
}

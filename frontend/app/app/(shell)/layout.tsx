"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { CircleHelp, Coins, Files, LogOut, Menu, Receipt, Settings, Shapes, Tags, Truck, UploadCloud, UserRound, X } from "lucide-react";
import * as React from "react";

import { AssistantWidget } from "@/components/app/assistant-widget";
import { OrgProvider, useOrg } from "@/components/app/org-context";
import { PreviewAssistProvider } from "@/components/app/preview-assist-context";
import { ThemeProvider } from "@/components/app/theme-provider";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/cn";
import { logout } from "@/lib/api";

const NAV = [
  { href: "/app", label: "Upload", icon: UploadCloud, exact: true },
  { href: "/app/files", label: "Files", icon: Files, exact: false },
  { href: "/app/rules", label: "Pricing rules", icon: Tags, exact: false },
  { href: "/app/categories", label: "Categories", icon: Shapes, exact: false },
  { href: "/app/suppliers", label: "Suppliers", icon: Truck, exact: false },
  { href: "/app/quotes", label: "Quotes", icon: Receipt, exact: false },
  { href: "/app/credits", label: "Credits", icon: Coins, exact: false },
  { href: "/app/settings", label: "Settings", icon: Settings, exact: false },
  { href: "/app/profile", label: "Profile", icon: UserRound, exact: false },
  { href: "/app/help", label: "Help", icon: CircleHelp, exact: false },
];

function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, memberships, active, loading, prefs, selectOrg } = useOrg();
  const [signingOut, setSigningOut] = React.useState(false);
  const [menuOpen, setMenuOpen] = React.useState(false);

  // The drawer is navigation: close it whenever the route changes.
  React.useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  React.useEffect(() => {
    if (!menuOpen) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setMenuOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  async function signOut() {
    setSigningOut(true);
    try {
      await logout();
    } catch {
      // Cookie already gone: still leave the shell.
    }
    router.push("/app/login");
  }

  return (
    <ThemeProvider prefs={prefs}>
    <div className="flex min-h-screen bg-background">
      {menuOpen ? (
        <div
          className="fixed inset-0 z-30 bg-black/50 md:hidden"
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setMenuOpen(false);
          }}
        />
      ) : null}
      <aside
        className={cn(
          "w-60 shrink-0 flex-col border-r border-border bg-background md:flex md:bg-muted/40",
          menuOpen ? "fixed inset-y-0 left-0 z-40 flex" : "hidden",
        )}
      >
        <div className="border-b border-border px-5 py-4">
          <div className="flex items-center justify-between gap-2">
            <Link
              href="/app"
              className="flex items-center gap-3 text-xl font-bold text-foreground"
            >
              <Image
                src="/offerflow-logo.svg"
                alt=""
                width={36}
                height={36}
                aria-hidden
              />
              OfferFlow AI
            </Link>
            <button
              type="button"
              onClick={() => setMenuOpen(false)}
              aria-label="Close menu"
              className="rounded-md p-1.5 text-muted-foreground hover:bg-muted hover:text-foreground md:hidden"
            >
              <X className="size-5" />
            </button>
          </div>
          <p className="mt-3 text-sm font-medium text-muted-foreground">
            {active ? active.organization_name : "Workspace"}
          </p>
        </div>
        <nav aria-label="Workspace" className="flex-1 p-3">
          <ul className="space-y-1">
            {NAV.map((item) => {
              const selected = item.exact
                ? pathname === item.href
                : pathname.startsWith(item.href);
              const Icon = item.icon;
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={selected ? "page" : undefined}
                    className={cn(
                      "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium",
                      selected
                        ? "bg-primary text-primary-foreground"
                        : "text-foreground hover:bg-muted",
                    )}
                  >
                    <Icon className="size-4" />
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
        <div className="space-y-3 border-t border-border p-4">
          {memberships.length > 1 ? (
            <label className="block text-xs font-medium text-muted-foreground">
              Organization
              <Select
                className="mt-1"
                value={active ? String(active.organization) : ""}
                disabled={loading}
                onChange={(event) => void selectOrg(Number(event.target.value))}
              >
                {memberships.map((m) => (
                  <option key={m.organization} value={m.organization}>
                    {m.organization_name} ({m.role})
                  </option>
                ))}
              </Select>
            </label>
          ) : null}
          <div className="flex items-center justify-between gap-2 text-sm">
            <span className="truncate text-muted-foreground" title={user?.username ?? ""}>
              {user?.username ?? "…"}
            </span>
            <button
              type="button"
              onClick={() => void signOut()}
              disabled={signingOut}
              className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-sm text-muted-foreground hover:bg-muted hover:text-foreground disabled:opacity-50"
            >
              <LogOut className="size-4" />
              Sign out
            </button>
          </div>
        </div>
      </aside>
      <div className="flex min-h-screen min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-border px-4 py-2.5 md:hidden">
          <button
            type="button"
            onClick={() => setMenuOpen(true)}
            aria-label="Open menu"
            aria-expanded={menuOpen}
            className="rounded-md p-1.5 text-foreground hover:bg-muted"
          >
            <Menu className="size-5" />
          </button>
          <Image
            src="/offerflow-logo.svg"
            alt=""
            width={24}
            height={24}
            aria-hidden
          />
          <span className="truncate text-sm font-semibold">
            {active ? active.organization_name : "OfferFlow AI"}
          </span>
        </div>
        <main
          className={
            pathname.startsWith("/app/files") ||
            pathname.startsWith("/app/quotes")
              ? "mx-auto w-full flex-1 px-4 py-6 md:w-[90%] md:px-6 md:py-8"
              : "mx-auto w-full max-w-5xl flex-1 px-4 py-6 md:px-6 md:py-8"
          }
        >
          {children}
        </main>
        <footer className="border-t border-border py-4 text-xs text-muted-foreground">
          <div className="mx-auto flex w-full max-w-5xl flex-col items-center gap-y-1 px-6 text-center">
            <span>© 2026 IN.TRA.M s.r.o. · Developed by Gian Paolo Cipicchia</span>
            <span className="flex flex-wrap justify-center gap-x-4 gap-y-1">
              <Link href="/privacy" className="hover:text-foreground hover:underline">
                Privacy policy
              </Link>
              <Link href="/terms" className="hover:text-foreground hover:underline">
                Terms of service
              </Link>
              <Link href="/source" className="hover:text-foreground hover:underline">
                Source &amp; licenses
              </Link>
            </span>
            <span>
              Built for the Meta Global AI Developer Hackathon · AGPL-3.0-only
            </span>
            <span>
              Independent entry: Meta is not affiliated with this app and
              accepts no responsibility for it.
            </span>
          </div>
        </footer>
      </div>
      <AssistantWidget />
    </div>
    </ThemeProvider>
  );
}

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <OrgProvider>
      <PreviewAssistProvider>
        <Shell>{children}</Shell>
      </PreviewAssistProvider>
    </OrgProvider>
  );
}

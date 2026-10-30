"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Coins, Files, LogOut, Settings, Tags, UploadCloud } from "lucide-react";
import * as React from "react";

import { OrgProvider, useOrg } from "@/components/app/org-context";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/cn";
import { logout } from "@/lib/api";

const NAV = [
  { href: "/app", label: "Upload", icon: UploadCloud, exact: true },
  { href: "/app/files", label: "Files", icon: Files, exact: false },
  { href: "/app/rules", label: "Pricing rules", icon: Tags, exact: false },
  { href: "/app/credits", label: "Credits", icon: Coins, exact: false },
  { href: "/app/settings", label: "Settings", icon: Settings, exact: false },
];

function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, memberships, active, loading, selectOrg } = useOrg();
  const [signingOut, setSigningOut] = React.useState(false);

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
    <div className="flex min-h-screen bg-background">
      <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-muted/40">
        <div className="border-b border-border px-5 py-4">
          <Link href="/app" className="text-base font-bold text-foreground">
            OfferFlow AI
          </Link>
          <p className="mt-0.5 text-xs text-muted-foreground">
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
      <div className="min-w-0 flex-1">
        <main className="mx-auto w-full max-w-5xl px-6 py-8">{children}</main>
      </div>
    </div>
  );
}

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <OrgProvider>
      <Shell>{children}</Shell>
    </OrgProvider>
  );
}

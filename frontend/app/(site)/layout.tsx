import Link from "next/link";

const NAV = [
  { href: "/how-it-works", label: "How it works" },
  { href: "/demo", label: "Demo" },
  { href: "/self-hosting", label: "Self-hosting" },
  { href: "/docs", label: "Docs" },
  { href: "/data", label: "Data" },
  { href: "/source", label: "Source" },
  { href: "/app", label: "Open the app" },
];

export default function SiteLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <a
        href="#content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-0 focus:left-0 focus:z-100 focus:bg-foreground focus:px-4 focus:py-2 focus:text-background"
      >
        Skip to content
      </a>
      <header className="border-b border-border">
        <div className="mx-auto flex max-w-3xl flex-wrap items-baseline gap-x-8 gap-y-1 px-5 py-3.5">
          <Link href="/" className="text-lg font-bold text-foreground no-underline">
            OfferFlow AI
          </Link>
          <nav aria-label="Primary">
            <ul className="flex flex-wrap gap-x-5 gap-y-1">
              {NAV.map((item) => (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    className="text-muted-foreground hover:text-foreground hover:underline"
                  >
                    {item.label}
                  </Link>
                </li>
              ))}
            </ul>
          </nav>
        </div>
      </header>
      <main id="content" className="mx-auto max-w-3xl px-5 pt-10 pb-14">
        <div className="prose prose-stone max-w-none">{children}</div>
      </main>
      <footer className="border-t border-border py-7 text-sm text-muted-foreground">
        <div className="mx-auto max-w-3xl space-y-1 px-5">
          <p>
            OfferFlow AI turns supplier spreadsheets into structured,
            verifiable price lists. Built by IN.TRA.M s.r.o. during the Meta
            Global AI Developer Hackathon.
          </p>
          <p>
            Licensed under the GNU Affero General Public License version 3
            only. <Link href="/source">Source and licenses</Link>
          </p>
        </div>
      </footer>
    </>
  );
}

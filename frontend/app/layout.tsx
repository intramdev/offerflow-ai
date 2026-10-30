import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "OfferFlow AI — verifiable supplier price lists",
    template: "%s — OfferFlow AI",
  },
  description:
    "Turn inconsistent supplier spreadsheets into structured, verifiable price lists.",
};

const NAV = [
  { href: "/how-it-works", label: "How it works" },
  { href: "/demo", label: "Demo" },
  { href: "/self-hosting", label: "Self-hosting" },
  { href: "/docs", label: "Docs" },
  { href: "/data", label: "Data" },
  { href: "/source", label: "Source" },
  { href: "/app", label: "Open the app" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <a className="skip" href="#content">
          Skip to content
        </a>
        <header className="site-header">
          <div className="wrap bar">
            <Link className="wordmark" href="/">
              OfferFlow AI
            </Link>
            <nav aria-label="Primary">
              <ul>
                {NAV.map((item) => (
                  <li key={item.href}>
                    <Link href={item.href}>{item.label}</Link>
                  </li>
                ))}
              </ul>
            </nav>
          </div>
        </header>
        <main id="content" className="wrap">
          {children}
        </main>
        <footer className="site-footer">
          <div className="wrap">
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
      </body>
    </html>
  );
}

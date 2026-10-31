"use client";

import Link from "next/link";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const SECTIONS = [
  { id: "upload", label: "Upload & preview" },
  { id: "mapping", label: "Column mapping" },
  { id: "review", label: "Review queue" },
  { id: "rules", label: "Pricing rules" },
  { id: "categories", label: "Categories" },
  { id: "matching", label: "Matching & reconcile" },
  { id: "quotes", label: "Quotes & export" },
  { id: "suppliers", label: "Suppliers" },
  { id: "credits", label: "AI credits" },
  { id: "assistant", label: "Assistant chat" },
  { id: "profile", label: "Profile & accessibility" },
  { id: "privacy", label: "Privacy & account" },
];

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Card id={id} className="scroll-mt-6">
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm leading-relaxed">{children}</CardContent>
    </Card>
  );
}

function P({ children }: { children: React.ReactNode }) {
  return <p className="text-foreground">{children}</p>;
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="text-muted-foreground">{children}</p>;
}

export default function HelpPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold">Help center</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Everything in the workspace, in the order you use it. Setup and
          self-hosting live in the{" "}
          <Link href="/docs" className="font-medium text-primary underline">
            public docs
          </Link>
          .
        </p>
      </div>

      <nav aria-label="Help sections">
        <ul className="flex flex-wrap gap-2">
          {SECTIONS.map((section) => (
            <li key={section.id}>
              <Link
                href={`#${section.id}`}
                className="inline-block rounded-full border border-border px-3 py-1 text-sm hover:bg-muted"
              >
                {section.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>

      <Section id="upload" title="Upload & preview">
        <P>
          Drop a supplier file on the <strong>Upload</strong> page. OfferFlow
          reads XLSX, XLS, CSV, TXT, TSV, DOC, and DOCX. Nothing is stored
          until you confirm: the file is parsed and you get a preview first.
        </P>
        <Note>
          Files from the same supplier are recognized: a previous mapping is
          suggested automatically, so repeated price lists import in one click.
        </Note>
      </Section>

      <Section id="mapping" title="Column mapping">
        <P>
          In preview, every source column is matched to an OfferFlow field
          (supplier, product, price, currency, capacity, grade, …). Change any
          dropdown before confirming; columns you do not need can be set to{" "}
          <strong>Ignore</strong>, and you can type a fixed value instead of
          mapping a column.
        </P>
        <Note>
          Currency defaults to your organization setting. Rows inherit it
          unless the file carries its own currency per row.
        </Note>
      </Section>

      <Section id="review" title="Review queue">
        <P>
          After you confirm, each row is validated. Rows the system cannot
          read — a missing price, an unknown unit — land in the review queue
          on the run page. Fix the value inline, or press{" "}
          <strong>Ignore record</strong> to drop the row from the quote.
        </P>
        <Note>
          Once every question is answered or ignored, the run validates
          itself; there is nothing else to click.
        </Note>
      </Section>

      <Section id="rules" title="Pricing rules">
        <P>
          <strong>Pricing rules</strong> turn supplier prices into your
          prices: pick the cheapest or priciest offer per product, convert
          currency, then add a percentage margin. One general rule covers
          everything; a category rule replaces the general one for its
          category only.
        </P>
        <Note>
          Exchange rates are recorded per file with their date — never
          guessed — and every quote shows which rate it used.
        </Note>
      </Section>

      <Section id="categories" title="Categories">
        <P>
          Categories are optional. Map a whole file to one category, let the
          AI detect them per row, or reconcile uncertain rows by hand. Quotes
          can then be exported as one file or one file per category.
        </P>
      </Section>

      <Section id="matching" title="Matching & reconcile">
        <P>
          The <strong>Quotes</strong> page groups identical products across
          suppliers — same brand and capacity, even when spelled
          differently — so the rule picks one winner per product. Grade
          matters for used and refurbished phones.
        </P>
        <Note>
          Uncertain matches wait in a reconcile list where you merge or
          split them; the system remembers your decisions per company.
        </Note>
      </Section>

      <Section id="quotes" title="Quotes & export">
        <P>
          A quote is generated from the latest approved supplier data with
          your rules applied. Download it as XLSX or CSV, copy the
          per-category message for Telegram, or send both together.
        </P>
      </Section>

      <Section id="suppliers" title="Suppliers">
        <P>
          Suppliers are created automatically from imports. Drag favorites
          into order, then open a supplier to rate shipping, quality,
          payment terms, availability, and returns from 1 to 5 stars, keep
          timestamped notes, and record location, customs, and clearing fees.
        </P>
      </Section>

      <Section id="credits" title="AI credits">
        <P>
          One credit is one AI token. The <strong>Credits</strong> page shows
          this cycle’s allowance, what is left, and the grand total that
          never resets. The log below lists every AI request — newest first,
          50 per page — and entries older than 120 days are removed
          automatically.
        </P>
        <Note>
          When no model key is configured, imports run without AI
          enrichment and spend nothing. If requests fail with “No free
          in-flight slot” while nobody is working, the “Release stuck
          slot” button on this page frees your company’s slot; other
          companies are never affected.
        </Note>
      </Section>

      <Section id="assistant" title="Assistant chat">
        <P>
          With the paid model connected, the Muse icon in the bottom-right
          opens a chat about app features. It cannot see your data or anyone
          else’s, it refuses anything outside the app, and every answer
          spends credits from the same log above.
        </P>
      </Section>

      <Section id="profile" title="Profile & accessibility">
        <P>
          The <strong>Profile</strong> page holds your name, email, and
          reading preferences: light, dark, or system theme; typeface and
          text size; high-contrast colors; and reduced motion. All of it is
          saved with your account and follows EN 301 549 / WCAG 2.1 AA
          guidance.
        </P>
      </Section>

      <Section id="privacy" title="Privacy & account">
        <P>
          <strong>Settings → Privacy & data</strong> exports everything the
          app holds about your company as a ZIP, or erases the company
          entirely after you type its name. Owners only; erasure cannot be
          undone. Forgot your password? Use the link on the sign-in page —
          emailed links expire after 30 minutes and work once.
        </P>
      </Section>
    </div>
  );
}

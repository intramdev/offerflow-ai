import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Privacy policy",
};

export default function PrivacyPolicy() {
  return (
    <>
      <h1>Privacy policy</h1>
      <p className="lede">
        How personal data is handled when you run OfferFlow AI, and your
        rights under the EU General Data Protection Regulation (GDPR).
      </p>

      <h2>Who is who</h2>
      <p>
        Each company using OfferFlow AI is the data controller for its own
        workspace: its member accounts, supplier files, quotes, and notes.
        OfferFlow AI processes that data only to provide the service, and
        never discloses one company&apos;s data to another. The deployment
        operator (whoever runs this instance) is responsible for hosting
        security and backups.
      </p>

      <h2>What is stored</h2>
      <ul className="tight">
        <li>
          Account data: usernames, email addresses, organization roles, and
          display preferences.
        </li>
        <li>
          Workspace data: uploaded supplier files, import runs, offers,
          review decisions, categories, pricing rules, suppliers with
          ratings and notes, customer quotes, and credit-usage records.
        </li>
        <li>
          Security records: audit events and refused assistant attempts,
          kept so administrators can spot abuse.
        </li>
      </ul>

      <h2>Local-first processing</h2>
      <p>
        In simulated mode everything stays on your machine and no AI
        provider is contacted at all. When the administrator enables the
        paid model, only then do price-list contents, matching candidates,
        and assistant questions travel to the configured Meta model
        endpoint — and only for the organization that triggered the call.
      </p>
      <p>
        Note: Meta&apos;s Contributor model tier uses inputs and outputs to
        train and improve Meta&apos;s AI models. If that is unacceptable
        for your data, use the Standard tier or stay in simulated mode.
      </p>

      <h2>Your rights</h2>
      <p>
        Under the GDPR you can access, correct, export, and erase your
        personal data. Inside the app, the organization owner can download
        every row and file belonging to the company (Settings → Privacy
        &amp; data → Download all my data) and can erase the whole
        workspace, including logins left without an organization
        (Settings → Privacy &amp; data → Delete everything). Erasure
        cannot be undone.
      </p>
      <p>
        To correct your account details or raise a privacy concern,
        contact the operator of this instance. You also have the right to
        lodge a complaint with your national data protection authority.
      </p>

      <h2>Retention</h2>
      <p>
        Workspace data is kept until the organization owner deletes it.
        Security records are kept with the workspace and disappear with
        it. The AI usage log rotates faster: entries older than 120 days
        are removed automatically, while monthly totals and the grand
        total stay. No personal data is sold, shared for advertising, or
        moved outside the configured infrastructure except for paid model
        calls described above.
      </p>

      <h2>About this app</h2>
      <p>
        OfferFlow AI is free software developed by Gian Paolo Cipicchia
        for IN.TRA.M s.r.o. for the Meta Global AI Developer Hackathon
        (AGPL-3.0-only; see the source and licenses page). It is an
        independent entry: Meta is not affiliated with this app, does not
        endorse it, and accepts no responsibility for it or for data
        processed through it.
      </p>

      <p>
        <em>Last updated: October 2026.</em>
      </p>
    </>
  );
}

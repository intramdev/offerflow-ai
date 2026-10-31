import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Terms of service",
};

export default function TermsOfService() {
  return (
    <>
      <h1>Terms of service</h1>
      <p className="lede">
        The rules for using OfferFlow AI. By creating an account you
        accept these terms.
      </p>

      <h2>The service</h2>
      <p>
        OfferFlow AI turns supplier price lists into structured,
        verifiable customer quotes. Simulated features run locally at no
        cost; paid AI features (interpretation, matching, categorization,
        the help assistant) spend the company&apos;s monthly credits, and
        dispatch is refused loudly when credits run out.
      </p>

      <h2>Your responsibilities</h2>
      <ul className="tight">
        <li>
          Only upload data you have the right to process, and keep your
          login credentials secret.
        </li>
        <li>
          Keep the organization owner role with someone accountable: the
          owner can export and permanently erase the whole workspace.
        </li>
        <li>
          Respect other tenants: probing for other companies&apos; data,
          sending SQL, code, or bulk-destructive commands to the
          assistant, or otherwise abusing the service can get the account
          limited or deactivated, with attempts logged for the
          administrator.
        </li>
      </ul>

      <h2>Acceptable use of the assistant</h2>
      <p>
        The help assistant answers questions about using OfferFlow AI. It
        has no access to company data and cannot run commands or change
        anything. Questions outside the app&apos;s features are refused
        with a fixed message; abuse attempts are recorded and may lead
        to an assistant ban or account deactivation.
      </p>

      <h2>Availability and liability</h2>
      <p>
        The service is provided as is, without warranties. The operator
        is not liable for quote contents, pricing decisions, rate
        movements, or model output quality — every import preview,
        reconciliation, and confirmation step exists so a human verifies
        before anything is sent to customers.
      </p>

      <h2>Closing your account</h2>
      <p>
        The organization owner can erase the workspace at any time from
        Settings → Privacy &amp; data. Erasure deletes files, runs,
        quotes, suppliers, usage history, and orphaned logins, and
        cannot be undone. See the privacy policy for details.
      </p>

      <h2>About this app</h2>
      <p>
        OfferFlow AI is free software developed by Gian Paolo Cipicchia
        for IN.TRA.M s.r.o. for the Meta Global AI Developer Hackathon
        (AGPL-3.0-only; see the source and licenses page). It is an
        independent entry: Meta is not affiliated with this app, does not
        endorse it, and accepts no responsibility for it, for quote
        contents, or for model output.
      </p>

      <h2>Changes</h2>
      <p>
        These terms may change with new releases; continued use after a
        change means acceptance. Material changes will be noted in the
        release notes.
      </p>

      <p>
        <em>Last updated: October 2026.</em>
      </p>
    </>
  );
}

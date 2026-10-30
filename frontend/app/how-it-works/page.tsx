import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "How it works",
};

export default function HowItWorks() {
  return (
    <>
      <h1>How it works</h1>
      <p className="lede">
        One supplier file travels a short, checkable path: upload, parse,
        interpret, validate, review, approve, export. People decide the
        ambiguous parts; software enforces the checks.
      </p>

      <h2>The vocabulary</h2>
      <ul className="tight">
        <li>
          <strong>Upload</strong> — one supplier spreadsheet, stored privately
          under your organization.
        </li>
        <li>
          <strong>Run</strong> — one processing pass over one upload, with a
          visible status from upload to completion or failure.
        </li>
        <li>
          <strong>Review</strong> — one open question about one row, answered
          by a person with the original evidence on screen.
        </li>
        <li>
          <strong>Approval</strong> — a human sign-off bound to the exact
          source file, rule version, and normalized result.
        </li>
        <li>
          <strong>Export</strong> — the validated spreadsheet or CSV. The
          bytes you download are exactly the bytes that passed validation.
        </li>
      </ul>

      <h2>The checks</h2>
      <p>Before anything is releasable, the run must pass every gate:</p>
      <ul className="tight">
        <li>No open review questions and no rows still needing input.</li>
        <li>
          Quantities reconcile between source rows and normalized offers.
        </li>
        <li>Every offer carries its sheet and row references.</li>
        <li>
          Nothing is merged across suppliers, currencies, prices, or
          incompatible conditions.
        </li>
      </ul>
      <p>
        A failed check blocks release or produces an explicit, reviewed
        exclusion. There is no average score that can overrule a failed
        check, and any change after approval invalidates the approval.
      </p>

      <h2>What people decide</h2>
      <p>
        Ambiguous vendor grades, unreadable prices or currencies, and unclear
        lock states pause for a decision. Each question shows the original
        cell evidence, the proposed reading, and the consequence of each
        choice. Locked devices are excluded with a stated reason instead of
        being silently dropped.
      </p>

      <h2>Who sees what</h2>
      <p>
        Each customer organization is a separate tenant: uploads, runs,
        reviews, exports, and audit history belong to exactly one
        organization, and members only ever see their own. Cross-tenant
        access attempts are refused.
      </p>
      <p>
        <Link href="/demo">Walk through the synthetic demo</Link> to see the
        flow on fictitious data.
      </p>
    </>
  );
}

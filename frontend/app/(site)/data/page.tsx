import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Data handling",
};

export default function DataHandling() {
  return (
    <>
      <h1>How data is handled</h1>
      <p className="lede">
        Plain statements about data, matching the current implementation.
        Anything not listed here is not promised.
      </p>

      <h2>Today: synthetic data only</h2>
      <p>
        Development, tests, and demonstrations use fictitious supplier data.
        The repository contains no real supplier prices, customer records, or
        proprietary datasets, and nothing on this site asks you to upload
        real data.
      </p>

      <h2>Local processing</h2>
      <ul className="tight">
        <li>
          In simulated mode, uploads stay on your machine inside the project
          volumes, and no inference provider is contacted at all.
        </li>
        <li>
          Each organization&apos;s files, runs, reviews, exports, and audit
          history are isolated per tenant; members only see their own.
        </li>
        <li>
          Logs carry tenant and job correlation identifiers, not document
          contents or credentials.
        </li>
      </ul>

      <h2>Model use, when explicitly enabled</h2>
      <p>
        Real-model use requires your explicit activation, your own
        credential, and a separately approved spending allowance. Only rows
        that local parsing cannot read are ever sent — clean rows never
        leave the machine.
      </p>
      <p>
        Model billing tier matters for privacy: Standard billing does not
        permit training use of your content, while discounted
        Contributor-tier models do use inputs to train Meta&apos;s AI. This
        deployment can run either tier depending on the configured key, so
        check which model id is active and prefer Standard billing for
        sensitive supplier lists.
      </p>

      <h2>Retention and deletion</h2>
      <p>
        Local development data lives in the project volumes until you remove
        them with a full environment reset. Per-organization export and
        deletion routines for hosted use are planned and not yet implemented,
        which is one reason real customer onboarding is not open.
      </p>

      <h2>What this page does not claim</h2>
      <p>
        A server location alone proves nothing about where model processing
        happens or which obligations a deployment satisfies. Before any real
        data use, processors, locations, retention, and provider terms must be
        documented and agreed separately.
      </p>
    </>
  );
}

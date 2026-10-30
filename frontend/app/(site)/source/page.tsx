import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Source and licenses",
};

export default function Source() {
  return (
    <>
      <h1>Source and licenses</h1>
      <p>
        OfferFlow AI is free software, built by IN.TRA.M s.r.o. during the
        Meta Global AI Developer Hackathon. Hackathon-period work is recorded
        in dated notes in the repository.
      </p>

      <h2>License</h2>
      <p>
        Copyright © 2026 IN.TRA.M s.r.o. Licensed under the GNU Affero General
        Public License version 3 only (AGPL-3.0-only). The full license text
        ships in the repository. Network users receive the corresponding
        source for the version actually served.
      </p>
      <p>
        Repository:{" "}
        <a href="https://github.com/intramdev/offerflow-ai">
          github.com/intramdev/offerflow-ai
        </a>
      </p>

      <h2>Third-party components</h2>
      <p>
        Third-party components and models remain subject to their own licenses
        and terms. Current direct dependencies:
      </p>
      <ul className="tight">
        <li>Django (BSD) — backend web framework</li>
        <li>Django REST framework (BSD) — API toolkit</li>
        <li>Celery (BSD) — background task execution</li>
        <li>psycopg (LGPL-3.0-only) — PostgreSQL adapter</li>
        <li>openpyxl (MIT) — spreadsheet reading and writing</li>
        <li>defusedxml (PSFL) — XML attack hardening</li>
        <li>openai SDK (Apache-2.0) — paid model calls (Meta Model API)</li>
        <li>xlrd (BSD) — legacy .xls reading</li>
        <li>python-docx (MIT) — .docx table reading</li>
        <li>cryptography (Apache-2.0/BSD) — API key encryption at rest</li>
        <li>Next.js, React, React DOM (MIT) — frontend</li>
        <li>TypeScript (Apache-2.0) — frontend language tooling</li>
        <li>PostgreSQL and RabbitMQ — database and message broker</li>
      </ul>
      <p>
        This list is informational, not a legal compliance statement. Review
        the actual dependency manifests and model provider terms before any
        public deployment.
      </p>
    </>
  );
}

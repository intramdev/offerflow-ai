import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Docs",
};

export default function Docs() {
  return (
    <>
      <h1>Setup and user docs</h1>
      <p className="lede">
        Get from a clean machine to the local simulated demo. Every step
        below is verified against the current repository state.
      </p>

      <h2>Prerequisites</h2>
      <ul className="tight">
        <li>Docker Desktop, running, on a machine you administer.</li>
        <li>Git and a terminal.</li>
        <li>No model key and no cloud account for the simulated demo.</li>
      </ul>

      <h2>Clean-machine walkthrough</h2>
      <ol className="steps">
        <li>
          Clone the repository and enter it.
          <pre>
            <code>
              git clone https://github.com/intramdev/offerflow-ai.git{"\n"}cd
              offerflow-ai
            </code>
          </pre>
        </li>
        <li>
          Copy the example environment and fill in every secret. Passwords
          must be URL-safe (letters and digits).
          <pre>
            <code>
              cp .env.example .env{"\n"}python3 -c &quot;import secrets;
              print(secrets.token_urlsafe(48))&quot;
            </code>
          </pre>
          Use the generated value for the Django secret key and URL-safe
          values for both passwords. Never commit <code>.env</code>.
        </li>
        <li>
          Build and start the stack.
          <pre>
            <code>docker compose up --build</code>
          </pre>
        </li>
        <li>
          Run database migrations once, from a second terminal.
          <pre>
            <code>docker compose run --rm backend python manage.py migrate</code>
          </pre>
        </li>
        <li>
          Run the test suite.
          <pre>
            <code>docker compose run --rm backend python manage.py test</code>
          </pre>
        </li>
        <li>
          Open the site at <code>http://127.0.0.1:4344</code> and check API
          health at <code>http://127.0.0.1:8384/api/health/</code> — expect
          mode <code>simulated</code>.
        </li>
      </ol>

      <h2>First sign-in</h2>
      <p>
        Create your owner account, then open the Django admin (loopback-only,
        backend port) to create the first organization and membership:
      </p>
      <pre>
        <code>
          docker compose run --rm backend python manage.py createsuperuser
        </code>
      </pre>
      <p>
        Sign in at <code>http://127.0.0.1:8384/admin/</code>, add an
        organization, then add a membership linking your user to it as owner.
        Afterwards the application sign-in lives at{" "}
        <code>http://127.0.0.1:4344/app/login</code>. The admin sees all
        organizations by design: it is the local provisioning tool, and any
        shared use needs an explicit access review first.
      </p>

      <h2>Using the workflow</h2>
      <ul className="tight">
        <li>
          Accepted input: one file per upload — <code>.xlsx</code>,{" "}
          <code>.xls</code>, <code>.csv</code>, <code>.txt</code>,{" "}
          <code>.tsv</code>, or <code>.docx</code> — up to 10 MB and 2000
          rows. Legacy <code>.doc</code> is refused with guidance. Headers
          may sit below row one. Macros, formulas-as-code, and external
          links are never executed or followed.
        </li>
        <li>
          Every upload opens a preview first: check the mapping, ignore
          columns or set fixed values, then confirm. Repeat suppliers get
          their remembered mapping proposed automatically.
        </li>
        <li>
          Local parsing reads clean rows in seconds with no model involved.
          Only rows it cannot read go to the model (paid lane), in bounded
          chunks with live progress — or pause for your decision.
        </li>
        <li>
          Ambiguous grades, unreadable prices, and unknown lock states pause
          the run for your decision, with the original evidence shown. You
          can also drop a record from the quote; it stays listed as
          excluded with a reason.
        </li>
        <li>
          Quotes convert to your destination currency using the rate recorded
          for each file on its own date. Exports carry sheet and row
          references; exclusions are listed with reasons rather than dropped
          silently.
        </li>
      </ul>

      <h2>Enabling the paid model lane</h2>
      <p>
        Without a key, everything above runs locally and gaps always ask the
        reviewer. To let a Muse model read gap rows, set the paid variables
        in <code>.env</code> (provider, switch, key, exact chat-model id —
        see <code>.env.example</code>), restart with the integration overlay
        so the worker can reach the API, then store the shared key and each
        company&apos;s monthly credits in the Django admin. Full runbook,
        budgeting math, and kill switches live in{" "}
        <code>backend/README.md</code>.
      </p>

      <h2>Reset and uninstall</h2>
      <p>
        <code>docker compose down -v</code> deletes the database, broker, and
        file volumes — all local development data. Uninstalling means
        stopping the stack, removing those volumes, and deleting the checkout.
      </p>

      <h2>Troubleshooting</h2>
      <h3>Cannot connect to the Docker daemon</h3>
      <p>
        Start Docker Desktop and re-run the command from your own terminal.
        Containers are started by you, never by a remote party.
      </p>
      <h3>Missing required environment variable</h3>
      <p>
        <code>.env</code> is absent or incomplete. Re-copy{" "}
        <code>.env.example</code> and fill every value.
      </p>
      <h3>Port is already allocated</h3>
      <p>
        Something already listens on 4344 or 8384. Stop it, or change the
        published port in <code>compose.yaml</code> while keeping it
        loopback-only.
      </p>
    </>
  );
}

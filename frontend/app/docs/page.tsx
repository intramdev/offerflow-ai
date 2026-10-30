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
          Accepted input: one <code>.xlsx</code> file per upload, up to 10 MB,
          with supplier, model, and price columns. Headers may sit below row
          one. Macros, formulas-as-code, and external links are never
          executed or followed.
        </li>
        <li>
          Ambiguous grades, unreadable prices, and unknown lock states pause
          the run for your decision, with the original evidence shown.
        </li>
        <li>
          Exports carry sheet and row references; exclusions are listed with
          reasons rather than dropped silently.
        </li>
      </ul>

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

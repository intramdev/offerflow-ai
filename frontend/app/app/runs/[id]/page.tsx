"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ApiError, api } from "@/lib/api";

interface Validation {
  name: string;
  passed: boolean;
  message: string;
}

interface Offer {
  id: number;
  source_row: number;
  supplier: string;
  brand: string;
  model_name: string;
  capacity_gb: number | null;
  color: string;
  grade: string;
  lock_state: string;
  quantity: number | null;
  unit_price: string | null;
  currency: string;
  conditions: string;
  status: string;
  exclusion_reason: string;
  provenance: { sheet?: string; row?: number };
}

interface Review {
  id: number;
  offer: number;
  kind: string;
  field: string;
  question: string;
  evidence: { provenance?: { sheet?: string; row?: number } };
  choices: string[];
  status: string;
}

interface Artifact {
  id: number;
  format: string;
  sha256: string;
  size_bytes: number;
  created_at: string;
}

interface RunDetail {
  id: number;
  status: string;
  provider_name: string;
  simulated: boolean;
  error: string;
  rule_version: string;
  validations: Validation[];
  artifacts: Artifact[];
}

const ACTIVE = new Set([
  "uploaded",
  "parsing",
  "interpretation",
  "validation",
]);

function AnswerForm({
  review,
  onAnswered,
}: {
  review: Review;
  onAnswered: () => void;
}) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function send(payload: object) {
    setError("");
    setBusy(true);
    try {
      await api(`/api/pipeline/reviews/${review.id}/answer/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      setValue("");
      onAnswered();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Answer failed.");
    } finally {
      setBusy(false);
    }
  }

  const provenance = review.evidence?.provenance;
  return (
    <div className="card stack">
      <p>
        <strong>
          Row {provenance?.row ?? "?"} · {review.field}
        </strong>
      </p>
      <p>{review.question}</p>
      {review.kind === "choice" ? (
        <div className="cta-row">
          {review.choices.map((choice) => (
            <button
              key={choice}
              className="button secondary"
              type="button"
              disabled={busy}
              onClick={() => send({ choice })}
            >
              {choice}
            </button>
          ))}
        </div>
      ) : (
        <form
          className="stack"
          onSubmit={(event) => {
            event.preventDefault();
            send({ value });
          }}
        >
          <label className="field">
            Value
            <input
              type="text"
              value={value}
              onChange={(event) => setValue(event.target.value)}
              required
            />
          </label>
          <div>
            <button className="button secondary" type="submit" disabled={busy}>
              {busy ? "Sending…" : "Send answer"}
            </button>
          </div>
        </form>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export default function RunPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [run, setRun] = useState<RunDetail | null>(null);
  const [offers, setOffers] = useState<Offer[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const detail = await api<RunDetail>(`/api/pipeline/runs/${id}/`);
      setRun(detail);
      setOffers(await api<Offer[]>(`/api/pipeline/runs/${id}/offers/`));
      setReviews(await api<Review[]>(`/api/pipeline/runs/${id}/reviews/`));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Load failed.");
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!run || !ACTIVE.has(run.status)) return;
    const timer = setInterval(load, 2000);
    return () => clearInterval(timer);
  }, [run, load]);

  async function act(
    path: string,
    body?: object,
    expectJson = true,
  ): Promise<void> {
    setError("");
    setBusy(true);
    try {
      await api(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body ?? {}),
      });
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Action failed.");
    } finally {
      setBusy(false);
    }
  }

  if (!run && !error) return <p>Loading run…</p>;
  if (!run)
    return (
      <>
        <h1>Run not found</h1>
        <p className="error">{error}</p>
        <p>
          <Link href="/app">Back to workspace</Link>
        </p>
      </>
    );

  const terminal = ["completed", "failed", "cancelled"].includes(run.status);

  return (
    <>
      <p>
        <Link href="/app">← Workspace</Link>
      </p>
      <h1>
        Run {run.id} <span className="pill">{run.status.replace(/_/g, " ")}</span>
      </h1>
      <p className="lede">
        Rules {run.rule_version} · provider {run.provider_name}
        {run.simulated ? " (simulated)" : ""}.
      </p>
      {run.error && (
        <p className="error" role="alert">
          {run.error}
        </p>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {ACTIVE.has(run.status) && <p>Processing… this page refreshes itself.</p>}

      <h2>Checks</h2>
      {run.validations.length === 0 ? (
        <p>No validation results yet.</p>
      ) : (
        <ul className="tight">
          {run.validations.map((item) => (
            <li key={item.name}>
              {item.passed ? "Passed" : "Failed"} — {item.name}: {item.message}
            </li>
          ))}
        </ul>
      )}

      <h2>Offers</h2>
      {offers.length === 0 ? (
        <p>No offers produced.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Row</th>
              <th>Supplier</th>
              <th>Model</th>
              <th>GB</th>
              <th>Grade</th>
              <th>Lock</th>
              <th>Qty</th>
              <th>Price</th>
              <th>Cur</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {offers.map((offer) => (
              <tr key={offer.id}>
                <td>{offer.provenance?.row ?? ""}</td>
                <td>{offer.supplier}</td>
                <td>{offer.model_name}</td>
                <td>{offer.capacity_gb ?? ""}</td>
                <td>{offer.grade || offer.status}</td>
                <td>{offer.lock_state}</td>
                <td>{offer.quantity ?? ""}</td>
                <td>{offer.unit_price ?? ""}</td>
                <td>{offer.currency}</td>
                <td>
                  {offer.status}
                  {offer.exclusion_reason
                    ? ` (${offer.exclusion_reason})`
                    : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Questions ({reviews.length} open)</h2>
      {reviews.length === 0 ? (
        <p>No open questions.</p>
      ) : (
        <div className="stack">
          {reviews.map((review) => (
            <AnswerForm key={review.id} review={review} onAnswered={load} />
          ))}
        </div>
      )}

      <h2>Actions</h2>
      <div className="cta-row">
        {run.status === "ready_for_review" && (
          <button
            className="button"
            type="button"
            disabled={busy}
            onClick={() => act(`/api/pipeline/runs/${id}/approve/`)}
          >
            Approve
          </button>
        )}
        {(run.status === "approved" || run.status === "completed") && (
          <>
            <button
              className="button"
              type="button"
              disabled={busy}
              onClick={() =>
                act(`/api/pipeline/runs/${id}/export/`, { format: "xlsx" })
              }
            >
              Export XLSX
            </button>
            <button
              className="button secondary"
              type="button"
              disabled={busy}
              onClick={() =>
                act(`/api/pipeline/runs/${id}/export/`, { format: "csv" })
              }
            >
              Export CSV
            </button>
          </>
        )}
        {!terminal && (
          <button
            className="button secondary"
            type="button"
            disabled={busy}
            onClick={() => act(`/api/pipeline/runs/${id}/cancel/`)}
          >
            Cancel run
          </button>
        )}
      </div>

      {run.artifacts.length > 0 && (
        <>
          <h2>Downloads</h2>
          <ul className="tight">
            {run.artifacts.map((artifact) => (
              <li key={artifact.id}>
                <a href={`/api/pipeline/artifacts/${artifact.id}/download/`}>
                  {artifact.format.toUpperCase()} — {artifact.size_bytes} bytes
                </a>{" "}
                <span className="muted">{artifact.sha256.slice(0, 12)}…</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Self-hosting",
};

export default function SelfHosting() {
  return (
    <>
      <h1>Self-hosting</h1>
      <p className="lede">
        OfferFlow AI runs from containers you control, in three stages. Each
        stage must pass its gates before the next one is considered.
      </p>

      <h2>Stage 1 — Local simulated development</h2>
      <p>
        Runs on your own machine with fictitious data and deterministic
        fixture responses. No model key is required, no inference provider is
        contacted, and the interface carries a persistent simulated-mode
        label. This is the default: a clean checkout always starts here.
      </p>

      <h2>Stage 2 — Local real-model testing</h2>
      <p>
        Enabled explicitly, and only with a dedicated development credential
        plus a separately approved spending allowance. Calls, tokens,
        concurrency, retries, and total spend are capped in advance, starting
        from a single running request with automatic retry disabled. Simulated
        results are never presented as real model output.
      </p>

      <h2>Stage 3 — Single-server deployment</h2>
      <p>
        Considered only after local acceptance gates pass: tenant isolation,
        the synthetic end-to-end flow with independent expected outputs, zero
        provider traffic in simulated mode, working budget and cancellation
        accounting, no secrets in images or logs, and a clean-machine
        quickstart. The demo host serves synthetic data to authenticated demo
        users; marketing pages never expose an anonymous inference endpoint.
      </p>

      <h2>What you will need</h2>
      <ul className="tight">
        <li>Docker on a machine you administer.</li>
        <li>
          For stage 2: a model API account you own, with understood credit
          expiry and post-credit billing.
        </li>
        <li>
          For stage 3: a server, a domain with HTTPS, and separate staging
          credentials — nothing copied from development.
        </li>
      </ul>
      <p>
        Start with the <Link href="/docs">setup docs</Link>, and read{" "}
        <Link href="/data">how data is handled</Link> before processing
        anything real.
      </p>
    </>
  );
}

import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "OfferFlow AI — verifiable supplier price lists",
};

export default function Home() {
  return (
    <>
      <h1>Supplier spreadsheets in, verifiable price lists out</h1>
      <p className="lede">
        Suppliers send inconsistent files: mismatched descriptions, missing
        capacities, prices without currencies. OfferFlow AI interprets each
        file, checks every row against deterministic rules, asks a person
        about the ambiguous parts, and exports a price list that cites its
        sources cell by cell.
      </p>
      <div className="cta-row">
        <Link className="button" href="/demo">
          See the synthetic demo
        </Link>
        <Link className="button secondary" href="/how-it-works">
          How it works
        </Link>
      </div>

      <h2>What it does</h2>
      <ol className="steps">
        <li>
          <strong>Upload.</strong> Bring one supplier spreadsheet. The file is
          stored privately and never executed, and formulas inside it are
          never evaluated.
        </li>
        <li>
          <strong>Interpret.</strong> Each row is read into structured fields —
          supplier, model, capacity, grade, lock state, quantity, price, and
          currency. Anything unreadable is flagged, never guessed.
        </li>
        <li>
          <strong>Review.</strong> Ambiguous rows pause for a human decision
          with the original cell evidence on screen. Locked devices are
          excluded with reasons, never silently dropped.
        </li>
        <li>
          <strong>Export.</strong> The validated list downloads with source
          references, and the bytes you download are exactly the bytes that
          passed validation.
        </li>
      </ol>

      <h2>Project status</h2>
      <p>
        OfferFlow AI is under active development for the Meta Global AI
        Developer Hackathon. The demo on this site uses fictitious data and is
        clearly labelled as synthetic. Setup instructions, source code, and
        data-handling notes are published alongside the work as it lands.
      </p>
      <div className="cta-row">
        <Link className="button secondary" href="/docs">
          Read the setup docs
        </Link>
        <Link className="button secondary" href="/source">
          Source and licenses
        </Link>
      </div>
    </>
  );
}

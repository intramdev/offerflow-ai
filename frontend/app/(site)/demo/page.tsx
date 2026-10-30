import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Demo",
};

export default function Demo() {
  return (
    <>
      <h1>Demo</h1>
      <div className="banner" role="note">
        <p>
          <strong>Synthetic demonstration.</strong> Every name, price, and
          quantity below is fictitious fixture data. This page is a static
          illustrated walkthrough, not a live product session.
        </p>
      </div>

      <h2>1. The supplier file</h2>
      <p>
        A four-row spreadsheet arrives with the usual inconsistencies: one
        ambiguous grade, one locked device, one missing price.
      </p>
      <table>
        <thead>
          <tr>
            <th>Supplier</th>
            <th>Model</th>
            <th>Capacity</th>
            <th>Grade</th>
            <th>Lock</th>
            <th>Qty</th>
            <th>Price</th>
            <th>Currency</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Acme</td>
            <td>P1</td>
            <td>128GB</td>
            <td>A</td>
            <td>Unlocked</td>
            <td>5</td>
            <td>299.99</td>
            <td>USD</td>
          </tr>
          <tr>
            <td>Acme</td>
            <td>P1</td>
            <td>128GB</td>
            <td>A/B</td>
            <td>Unlocked</td>
            <td>5</td>
            <td>299.99</td>
            <td>USD</td>
          </tr>
          <tr>
            <td>Acme</td>
            <td>P2</td>
            <td>256GB</td>
            <td>B</td>
            <td>iCloud locked</td>
            <td>2</td>
            <td>199.99</td>
            <td>USD</td>
          </tr>
          <tr>
            <td>Acme</td>
            <td>P3</td>
            <td>64GB</td>
            <td>A</td>
            <td>Unlocked</td>
            <td>3</td>
            <td></td>
            <td>USD</td>
          </tr>
        </tbody>
      </table>

      <h2>2. What the checks found</h2>
      <ul className="tight">
        <li>Row 1 reads cleanly and is releasable.</li>
        <li>
          Row 2 pauses: grade “A/B” could be one part or the raw term as a
          whole. The reviewer picks with the original cell on screen.
        </li>
        <li>Row 3 is excluded with the reason “locked device”, listed openly.</li>
        <li>
          Row 4 pauses: the missing price is never guessed. The reviewer
          supplies it, lets the model try to read it (paid lane), or drops
          the row from the quote.
        </li>
      </ul>

      <h2>3. The export</h2>
      <p>
        After the two answers and a human approval, the run exports three
        releasable offers with sheet and row references, plus the one stated
        exclusion. The download is byte-identical to the validated artifact.
      </p>
      <p>
        To run this flow yourself with the same fixture data, follow the{" "}
        <Link href="/docs">setup docs</Link>.
      </p>
    </>
  );
}

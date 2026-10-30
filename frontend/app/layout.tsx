import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: {
    default: "OfferFlow AI — verifiable supplier price lists",
    template: "%s — OfferFlow AI",
  },
  description:
    "Turn inconsistent supplier spreadsheets into structured, verifiable price lists.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

import type { Metadata } from "next";
import "./globals.css";

import { THEME_BOOT_SCRIPT } from "@/components/app/theme-provider";

export const metadata: Metadata = {
  title: {
    default: "OfferFlow AI — verifiable supplier price lists",
    template: "%s — OfferFlow AI",
  },
  description:
    "Turn supplier spreadsheets and WhatsApp and Telegram messages into structured, verifiable price lists.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
        {children}
      </body>
    </html>
  );
}

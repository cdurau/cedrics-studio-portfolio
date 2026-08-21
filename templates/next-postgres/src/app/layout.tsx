import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Next Postgres App",
  description: "Phase 10 Next.js + PostgreSQL Template",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased font-sans">
        {children}
      </body>
    </html>
  );
}

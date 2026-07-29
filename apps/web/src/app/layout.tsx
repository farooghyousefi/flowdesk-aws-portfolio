import type { Metadata } from "next";
import "@/app/globals.css";

export const metadata: Metadata = {
  title: "Flowdesk · Local Orderflow Assistant",
  description: "Lokaler Databento-MBO-Replay-, Orderflow- und Risk-Arbeitsbereich"
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>): React.ReactElement {
  return (
    <html lang="de" className="dark">
      <body>{children}</body>
    </html>
  );
}

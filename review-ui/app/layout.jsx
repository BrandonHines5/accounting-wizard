import "./globals.css";

export const metadata = {
  title: "Forensics Review",
  description: "Review and disposition financial-forensics findings",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

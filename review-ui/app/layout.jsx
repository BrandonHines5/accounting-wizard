import "./globals.css";

// Favicon + home-screen bookmark icons are wired up by Next's file conventions:
//   app/icon.svg        -> scalable favicon
//   app/favicon.ico     -> legacy favicon
//   app/apple-icon.png  -> iOS "Add to Home Screen" bookmark image
//   app/manifest.js     -> Android / PWA install icons
export const metadata = {
  title: "Forensics Review",
  description: "Review and disposition financial-forensics findings",
  applicationName: "Acct-Wizd",
  appleWebApp: { capable: true, title: "Acct-Wizd", statusBarStyle: "default" },
};

export const viewport = {
  themeColor: "#16233f",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}

// Web app manifest — drives the Android / PWA "Add to Home Screen" icon and name,
// mirroring the apple-touch-icon so the bookmark looks the same on every device.
export default function manifest() {
  return {
    name: "Forensics Review",
    short_name: "Acct-Wizd",
    description: "Review and disposition financial-forensics findings",
    start_url: "/",
    display: "standalone",
    background_color: "#ffffff",
    theme_color: "#16233f",
    icons: [
      { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
      { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
    ],
  };
}

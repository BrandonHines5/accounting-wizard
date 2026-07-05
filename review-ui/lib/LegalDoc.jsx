// Shared shell for the public legal pages (/eula, /privacy). These routes are
// intentionally OUTSIDE the auth-gated dashboard (app/page.jsx): they import no
// Supabase client, so they render for anyone — Intuit's app profile links to them.
//
// The dashboard's globals.css forces a dark theme on <body>; this component paints
// its own full-viewport, theme-aware surface (light default, dark via
// prefers-color-scheme) so the legal pages read as clean documents regardless. All
// selectors are scoped under `.legal-page` so nothing here touches the dashboard.
const CSS = `
.legal-page{
  --l-ground:#FAFBFC;--l-surface:#FFFFFF;--l-ink:#17212E;--l-body:#3B4650;
  --l-muted:#6E7A86;--l-hairline:#E4E8ED;--l-accent:#1E5A86;--l-accent-soft:rgba(30,90,134,.09);
  --l-serif:ui-serif,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,"Times New Roman",serif;
  --l-sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,Roboto,"Helvetica Neue",Arial,sans-serif;
  min-height:100vh;background:var(--l-ground);color:var(--l-body);
  font-family:var(--l-sans);font-size:16px;line-height:1.65;-webkit-font-smoothing:antialiased;
  display:flex;justify-content:center;padding:clamp(1.5rem,5vw,5rem) 1.25rem;
}
@media (prefers-color-scheme:dark){
  .legal-page{
    --l-ground:#10161D;--l-surface:#151D26;--l-ink:#EAF0F5;--l-body:#B7C2CD;
    --l-muted:#7F8C99;--l-hairline:#26313C;--l-accent:#6FB2DE;--l-accent-soft:rgba(111,178,222,.12);
  }
}
.legal-doc{width:100%;max-width:44rem;}
.legal-topline{height:3px;width:56px;background:var(--l-accent);border-radius:2px;margin-bottom:2rem;}
.legal-eyebrow{font-size:.72rem;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--l-accent);margin:0 0 .9rem;}
.legal-h1{font-family:var(--l-serif);font-weight:600;color:var(--l-ink);font-size:clamp(2rem,5vw,2.7rem);line-height:1.08;letter-spacing:-.01em;margin:0 0 .85rem;text-wrap:balance;}
.legal-lede{font-size:1.05rem;color:var(--l-body);margin:0 0 1.5rem;max-width:40rem;}
.legal-meta{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;align-items:baseline;font-size:.85rem;color:var(--l-muted);padding-bottom:1.6rem;border-bottom:1px solid var(--l-hairline);}
.legal-meta b{color:var(--l-ink);font-weight:600;}
.legal-meta code{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:.8rem;color:var(--l-accent);background:var(--l-accent-soft);padding:.1rem .4rem;border-radius:4px;}
.legal-main{counter-reset:clause;margin-top:2.2rem;}
.legal-main section{margin-bottom:2rem;}
.legal-main h2{font-family:var(--l-serif);color:var(--l-ink);font-weight:600;font-size:1.22rem;line-height:1.25;margin:0 0 .6rem;text-wrap:balance;}
.legal-main h2::before{counter-increment:clause;content:counter(clause) ".";color:var(--l-accent);font-family:var(--l-sans);font-weight:600;font-variant-numeric:tabular-nums;margin-right:.55rem;}
.legal-main p{margin:0 0 .85rem;}
.legal-main a{color:var(--l-accent);text-decoration-thickness:1px;text-underline-offset:2px;}
.legal-main ul{margin:0 0 .85rem;padding-left:1.2rem;}
.legal-main li{margin-bottom:.35rem;}
.legal-main strong{color:var(--l-ink);font-weight:600;}
.legal-footer{margin-top:2.5rem;padding-top:1.5rem;border-top:1px solid var(--l-hairline);font-size:.86rem;color:var(--l-muted);}
.legal-footer a{color:var(--l-accent);font-weight:500;}
.legal-signoff{font-size:.8rem;color:var(--l-muted);margin-top:1rem;}
`;

export default function LegalDoc({ eyebrow, title, lede, children }) {
  return (
    <div className="legal-page">
      <style dangerouslySetInnerHTML={{ __html: CSS }} />
      <article className="legal-doc">
        <div className="legal-topline" />
        <p className="legal-eyebrow">{eyebrow}</p>
        <h1 className="legal-h1">{title}</h1>
        <p className="legal-lede">{lede}</p>
        <div className="legal-meta">
          <span><b>Provider</b>&nbsp;&nbsp;Hines Homes&nbsp;LLC and affiliated entities</span>
          <span><b>Effective</b>&nbsp;&nbsp;July&nbsp;5,&nbsp;2026</span>
          <span><code>com.intuit.quickbooks.accounting</code></span>
        </div>
        <main className="legal-main">{children}</main>
        <footer className="legal-footer">
          © 2026 Hines Homes&nbsp;LLC. All rights reserved.
          <div className="legal-signoff">
            Accounting Wizard is an internal application and is not distributed to the public.
          </div>
        </footer>
      </article>
    </div>
  );
}

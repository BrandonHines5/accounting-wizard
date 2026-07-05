import LegalDoc from "../../lib/LegalDoc";

export const metadata = {
  title: "End-User License Agreement — Accounting Wizard",
  description:
    "End-User License Agreement for the Accounting Wizard QuickBooks Online integration, operated by Hines Homes LLC.",
};

export default function EulaPage() {
  return (
    <LegalDoc
      eyebrow="End-User License Agreement"
      title="Accounting Wizard"
      lede={
        <>
          An internal financial-review application operated by Hines Homes&nbsp;LLC. It connects to
          QuickBooks&nbsp;Online through Intuit’s API to read accounting data for the company’s own
          bookkeeping review, error detection, and fraud-prevention controls.
        </>
      }
    >
      <section>
        <h2>Acceptance of this agreement</h2>
        <p>
          This End-User License Agreement (the “Agreement”) governs your access to and use of the
          Accounting&nbsp;Wizard application (the “Application”), operated by Hines Homes&nbsp;LLC and its
          affiliated entities (collectively, “we,” “us,” or the “Company”). By authorizing, installing, or
          using the Application, you agree to be bound by this Agreement. If you do not agree, do not use the
          Application.
        </p>
      </section>

      <section>
        <h2>License grant</h2>
        <p>
          Subject to your continued compliance with this Agreement, the Company grants you a limited,
          non-exclusive, non-transferable, revocable license to use the Application solely for the Company’s
          internal business purposes. The Application is an internal tool; it is not offered, sold, or licensed
          to the general public.
        </p>
      </section>

      <section>
        <h2>Authorized users and permitted use</h2>
        <p>
          Access is limited to owners, officers, and personnel of the Company and its affiliated entities who
          are authorized to review the Company’s financial records. You may use the Application only to access
          accounting data belonging to entities the Company owns or operates, and only for legitimate internal
          purposes, including bookkeeping review, reconciliation, error detection, and fraud prevention.
        </p>
      </section>

      <section>
        <h2>Restrictions</h2>
        <p>You agree that you will not, and will not permit any third party to:</p>
        <ul>
          <li>use the Application to access financial data of any entity the Company does not own or operate;</li>
          <li>copy, modify, distribute, sell, sublicense, or transfer the Application or access to it;</li>
          <li>reverse engineer, decompile, or attempt to derive source code except as permitted by law;</li>
          <li>use the Application in violation of any applicable law, regulation, or the terms of Intuit&nbsp;Inc.;</li>
          <li>attempt to gain unauthorized access to any account, system, or data.</li>
        </ul>
      </section>

      <section>
        <h2>QuickBooks Online and Intuit services</h2>
        <p>
          The Application connects to QuickBooks&nbsp;Online using Intuit’s authorized API and the OAuth&nbsp;2.0
          protocol under the <strong>com.intuit.quickbooks.accounting</strong> scope. It reads accounting records
          — such as transactions, general-ledger detail, and vendor records — for internal review. Your use of
          QuickBooks&nbsp;Online and other Intuit services remains governed by your separate agreements with
          Intuit&nbsp;Inc. You may revoke the Application’s access at any time from within QuickBooks&nbsp;Online
          (Settings → Apps → Disconnect). We are not affiliated with, endorsed by, or sponsored by Intuit&nbsp;Inc.
        </p>
      </section>

      <section>
        <h2>Intellectual property</h2>
        <p>
          The Application, including its software, structure, and content, is and remains the property of the
          Company. Except for the limited license granted above, no right, title, or interest in the Application
          is transferred to you.
        </p>
      </section>

      <section>
        <h2>Disclaimer of warranties</h2>
        <p>
          The Application is provided <strong>“as is”</strong> and <strong>“as available,”</strong> without
          warranties of any kind, whether express, implied, or statutory, including any implied warranties of
          merchantability, fitness for a particular purpose, accuracy, or non-infringement. The Company does not
          warrant that the Application will be uninterrupted, error-free, or that findings it produces are
          complete or accurate. Output is for internal review and does not constitute accounting, tax, or legal
          advice.
        </p>
      </section>

      <section>
        <h2>Limitation of liability</h2>
        <p>
          To the fullest extent permitted by law, the Company will not be liable for any indirect, incidental,
          special, consequential, or punitive damages, or any loss of data, profits, or business, arising out of
          or related to your use of or inability to use the Application, even if advised of the possibility of
          such damages.
        </p>
      </section>

      <section>
        <h2>Term and termination</h2>
        <p>
          This Agreement remains in effect while you use the Application. The Company may suspend or terminate
          your access at any time, with or without cause. Upon termination, the license granted to you ends and
          you must cease all use of the Application. Sections that by their nature should survive termination will
          survive.
        </p>
      </section>

      <section>
        <h2>Governing law</h2>
        <p>
          This Agreement is governed by the laws of the State of Arkansas, without regard to its
          conflict-of-laws principles. Any dispute arising under it is subject to the exclusive jurisdiction of
          the state and federal courts located in Arkansas.
        </p>
      </section>

      <section>
        <h2>Changes to this agreement</h2>
        <p>
          The Company may update this Agreement from time to time. The current version is identified by the
          effective date above; continued use of the Application after an update constitutes acceptance of the
          revised terms.
        </p>
      </section>

      <section>
        <h2>Contact</h2>
        <p>
          Questions about this Agreement may be directed to Hines Homes&nbsp;LLC at{" "}
          <a href="mailto:brandon@hineshomes.com">brandon@hineshomes.com</a>.
        </p>
      </section>
    </LegalDoc>
  );
}

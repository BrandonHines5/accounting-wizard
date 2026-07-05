import LegalDoc from "../../lib/LegalDoc";

export const metadata = {
  title: "Privacy Policy — Accounting Wizard",
  description:
    "Privacy Policy for the Accounting Wizard QuickBooks Online integration, operated by Hines Homes LLC.",
};

export default function PrivacyPage() {
  return (
    <LegalDoc
      eyebrow="Privacy Policy"
      title="Accounting Wizard"
      lede={
        <>
          How Hines Homes&nbsp;LLC handles the accounting data accessed by the Accounting&nbsp;Wizard
          application. This is an internal business tool; it is not a consumer product and does not collect
          personal information from the public.
        </>
      }
    >
      <section>
        <h2>Overview</h2>
        <p>
          The Accounting&nbsp;Wizard application (the “Application”) is operated by Hines Homes&nbsp;LLC and its
          affiliated entities (“we,” “us,” or the “Company”) solely for the Company’s internal financial review.
          This policy explains what data the Application accesses, how it is used, and how it is protected. The
          Application accesses only accounting data belonging to entities the Company owns or operates.
        </p>
      </section>

      <section>
        <h2>Information the application accesses</h2>
        <p>
          Through Intuit’s authorized API and the OAuth&nbsp;2.0 protocol
          (<strong>com.intuit.quickbooks.accounting</strong> scope), the Application reads accounting records
          from the Company’s own QuickBooks&nbsp;Online companies, which may include:
        </p>
        <ul>
          <li>transaction records (bills, payments, expenses, journal entries, and credits);</li>
          <li>general-ledger detail and account information;</li>
          <li>vendor records, such as name, address, contact details, and tax identifiers;</li>
          <li>an OAuth access token and refresh token used to maintain the connection.</li>
        </ul>
        <p>
          The Application does not access payroll data, does not process payments, and does not collect
          information from members of the public.
        </p>
      </section>

      <section>
        <h2>How we use the information</h2>
        <p>
          Accounting data is used exclusively for the Company’s internal purposes: bookkeeping review,
          reconciliation, detection of errors and duplicate or anomalous transactions, and vendor-fraud
          prevention. The data is not used for advertising, profiling, or any purpose unrelated to reviewing the
          Company’s own financial records.
        </p>
      </section>

      <section>
        <h2>How we share information</h2>
        <p>
          <strong>We do not sell, rent, or trade this data, and we do not share it with third parties</strong>{" "}
          for their own use. Access is limited to the Company’s authorized owners, officers, and personnel. Data
          may be processed by infrastructure providers acting solely on our behalf (for example, our database
          host) under confidentiality and security obligations, and may be disclosed if required by law.
        </p>
      </section>

      <section>
        <h2>Data storage and security</h2>
        <p>
          Data is stored in access-controlled internal systems with row-level security restricting access to
          authorized service credentials. We apply data-minimization and safeguards consistent with the
          sensitivity of financial records, including:
        </p>
        <ul>
          <li>OAuth tokens are held as secrets and are never exposed in logs or client-side code;</li>
          <li>raw bank-account numbers are never stored — only hashed fingerprints are kept;</li>
          <li>bank statements and check images are not stored in our database; only references and read results are retained.</li>
        </ul>
      </section>

      <section>
        <h2>Data retention</h2>
        <p>
          Accounting data is retained only as long as needed for ongoing financial review and recordkeeping, or
          as required by law, after which it is deleted or de-identified. If the Application is disconnected, no
          further data is retrieved.
        </p>
      </section>

      <section>
        <h2>Your choices and disconnecting</h2>
        <p>
          The connection can be revoked at any time from within QuickBooks&nbsp;Online
          (Settings → Apps → Disconnect). Once disconnected, the Application can no longer access the company’s
          data. Requests regarding stored data may be directed to the contact below.
        </p>
      </section>

      <section>
        <h2>Intuit and QuickBooks Online</h2>
        <p>
          The Application integrates with QuickBooks&nbsp;Online under Intuit’s API terms. Your use of
          QuickBooks&nbsp;Online remains governed by Intuit’s own agreements and privacy policy. We are not
          affiliated with, endorsed by, or sponsored by Intuit&nbsp;Inc.
        </p>
      </section>

      <section>
        <h2>Changes to this policy</h2>
        <p>
          We may update this policy from time to time. The current version is identified by the effective date
          above; material changes will be reflected here.
        </p>
      </section>

      <section>
        <h2>Contact</h2>
        <p>
          Questions about this policy or the data the Application handles may be directed to Hines Homes&nbsp;LLC
          at <a href="mailto:brandon@hineshomes.com">brandon@hineshomes.com</a>.
        </p>
      </section>
    </LegalDoc>
  );
}

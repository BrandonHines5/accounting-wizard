// feedback-review — when a reviewer dispositions a finding with a reason, re-review
// every still-OPEN finding in light of the accumulated human feedback and update it
// as needed. Invoked by the review UI after each dispositioned-with-note action.
//
// financial_forensics is not exposed to PostgREST, so this function reaches it only
// through public SECURITY DEFINER RPCs (feedback_corpus / open_findings_for_review /
// apply_feedback_update), all service_role-only.
//
// Guardrails (CLAUDE.md hard rules):
//   - Never sets `disposition` (only `suggested_disposition`) — the human decides.
//   - Never mutates `severity`. Severity floors (vendor bank-detail = CRITICAL until
//     callback-verified; nonprofit misallocation >= HIGH) are deterministic and owned
//     by the rules / offline Tier 3 layer. apply_feedback_update only writes
//     ai_assessment + suggested_disposition, so it can't downgrade past a floor.
//   - Only updates OPEN findings, and only those the feedback genuinely bears on.
//   - A missing ANTHROPIC_API_KEY is a misconfiguration: returns 503, not a silent
//     no-op. The disposition + reason are already saved by set_finding_disposition
//     before this runs, so only the AI re-review is affected.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const MODEL = Deno.env.get("ANTHROPIC_MODEL") || "claude-sonnet-4-6";
const TIMEOUT_MS = 60_000;

// Defense-in-depth egress guard for the no-raw-bank-numbers hard rule: the RPC
// redacts disposition_note at write time, but this is the external egress to
// Anthropic, so scrub long digit runs (incl. space/hyphen-separated) again here —
// covering legacy rows or any out-of-band writes that bypassed the RPC.
const redactDigits = (s: string | null) =>
  s ? s.replace(/\d([ -]?\d){6,}/g, "[redacted]") : s;

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { ...cors, "Content-Type": "application/json" },
  });

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: cors });
  try {
    const url = Deno.env.get("SUPABASE_URL")!;
    const serviceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
    const admin = createClient(url, serviceKey);

    // Authz: the caller must be an allowlisted reviewer (same gate as the RPCs).
    const token = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    const { data: userData, error: userErr } = await admin.auth.getUser(token);
    const email = userData?.user?.email?.toLowerCase();
    if (userErr || !email) return json({ error: "unauthenticated" }, 401);
    const { data: allow, error: allowErr } = await admin
      .from("review_allowlist").select("email").eq("email", email).maybeSingle();
    if (allowErr) return json({ error: "allowlist_lookup_failed", detail: allowErr.message }, 500);
    if (!allow) return json({ error: "not authorized" }, 403);

    const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
    // A missing key is a server misconfiguration, not a successful no-op — surface it.
    if (!apiKey) return json({ error: "anthropic_api_key_not_configured" }, 503);

    // The human feedback corpus (dispositioned findings that carry a reason) and the
    // still-open findings to re-review against it, via the public RPCs. Propagate
    // errors rather than masking a DB/permission failure as "nothing to do".
    const { data: feedback, error: fbErr } = await admin.rpc("feedback_corpus");
    if (fbErr) return json({ error: "feedback_query_failed", detail: fbErr.message }, 500);
    const { data: open, error: openErr } = await admin.rpc("open_findings_for_review");
    if (openErr) return json({ error: "open_query_failed", detail: openErr.message }, 500);

    if (!open?.length) return json({ updated: 0, open: 0 });
    if (!feedback?.length) return json({ updated: 0, note: "no reasoned feedback yet" });

    const system = [
      "You are the Tier 3 reviewer for a forensic accounting tool.",
      "A human reviewer dispositioned some findings and wrote WHY (their reasons).",
      "Re-review each still-OPEN finding in light of that feedback.",
      "Findings are verification questions, not accusations; errors outnumber fraud ~100:1.",
      "Rules you MUST follow:",
      "- You may SUGGEST a disposition (legit | error_corrected | escalated | open) but NEVER decide it; the human does.",
      "- You do NOT set severity. If you believe a severity is wrong, say so in ai_assessment; the badge is governed elsewhere.",
      "- Only return a finding if the human feedback genuinely bears on it (same vendor, same rule pattern, same root cause). Leave unrelated findings out.",
      "Return ONLY the findings you are updating, via the update_findings tool.",
    ].join("\n");

    const tool = {
      name: "update_findings",
      description: "Apply feedback-informed updates to specific open findings.",
      input_schema: {
        type: "object",
        properties: {
          updates: {
            type: "array",
            items: {
              type: "object",
              properties: {
                fingerprint: { type: "string" },
                ai_assessment: {
                  type: "string",
                  description: "Updated plain-English assessment that cites the reviewer's reason.",
                },
                suggested_disposition: {
                  type: "string",
                  enum: ["legit", "error_corrected", "escalated", "open"],
                },
              },
              required: ["fingerprint", "ai_assessment"],
            },
          },
        },
        required: ["updates"],
      },
    };

    const payload = {
      reviewer_feedback: feedback.map((f: any) => ({
        ...f, disposition_note: redactDigits(f.disposition_note),
      })),
      open_findings: open.map((f: any) => ({
        fingerprint: f.fingerprint, rule_id: f.rule_id, severity: f.severity,
        question: f.question, details: f.details, entity_ids: f.entity_ids,
      })),
    };

    // Bound the upstream call so a slow Anthropic response can't hang the edge
    // runtime until its own hard timeout.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    let resp: Response;
    try {
      resp = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        signal: controller.signal,
        headers: {
          "x-api-key": apiKey,
          "anthropic-version": "2023-06-01",
          "content-type": "application/json",
        },
        body: JSON.stringify({
          model: MODEL,
          max_tokens: 4096,
          system,
          tools: [tool],
          tool_choice: { type: "tool", name: "update_findings" },
          messages: [{ role: "user", content: JSON.stringify(payload) }],
        }),
      });
    } catch (e) {
      const aborted = e instanceof DOMException && e.name === "AbortError";
      return json({ error: aborted ? "anthropic_timeout" : "anthropic_unreachable",
                    detail: String(e) }, 504);
    } finally {
      clearTimeout(timer);
    }
    if (!resp.ok) {
      return json({ error: "anthropic_error", status: resp.status,
                    detail: (await resp.text()).slice(0, 500) }, 502);
    }
    const result = await resp.json();
    const toolUse = (result.content || []).find((c: any) => c.type === "tool_use");
    const updates: any[] = toolUse?.input?.updates ?? [];

    const openByFp = new Map(open.map((f: any) => [f.fingerprint, f]));
    let applied = 0;
    const seen = new Set<string>();
    const failures: { fingerprint: string; error: string }[] = [];
    for (const u of updates) {
      if (!openByFp.get(u?.fingerprint) || !u.ai_assessment?.trim() || seen.has(u.fingerprint)) {
        continue;                                            // skip unknown / empty / dupes
      }
      seen.add(u.fingerprint);
      const { data: updatedFp, error } = await admin.rpc("apply_feedback_update", {
        p_fingerprint: u.fingerprint,
        p_ai_assessment: u.ai_assessment,
        p_suggested_disposition: u.suggested_disposition ?? null,
      });
      if (error) failures.push({ fingerprint: u.fingerprint, error: error.message });
      else if (updatedFp) applied++;                         // rpc returns fp, or null if no open row
    }
    return json({ updated: applied, considered: open.length,
                  ...(failures.length ? { failures } : {}) });
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});

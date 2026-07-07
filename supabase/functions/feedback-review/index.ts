// feedback-review — when a reviewer dispositions a finding with a reason, re-review
// the still-OPEN findings in light of the accumulated human feedback and update them
// as needed. Invoked by the review UI after each dispositioned-with-note action.
//
// The model call runs as a BACKGROUND task (EdgeRuntime.waitUntil): the reviewer's
// disposition is already saved before this function is even invoked, so there is
// nothing for them to wait on — the function validates, snapshots the candidates,
// responds 202 {started}, and applies the model's updates after the response. This
// is what fixed the reviewer-facing "anthropic_timeout" notices: the UI used to
// block (buttons locked) on a model call that could outrun the old 60s ceiling.
// Background failures land in the edge-function logs, not the UI.
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
//   - Only updates OPEN findings, and only those the feedback genuinely bears on:
//     the candidate set is pre-filtered to open findings sharing a rule_id with the
//     feedback (capped, surfaced in the response — never silent).
//   - A missing ANTHROPIC_API_KEY is a clean no-op (HTTP 200, {skipped}): the
//     optional AI re-review must never fail the reviewer's action. Pre-flight
//     failures (auth, DB, config) still surface synchronously as non-2xx and the
//     UI shows them as a soft, non-blocking notice.
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const MODEL = Deno.env.get("ANTHROPIC_MODEL") || "claude-sonnet-4-6";
// Abort guard on the background model call. Nobody is waiting on it anymore, so
// this only needs to stay inside the edge runtime's 400s wall-clock budget —
// generous beats the observed ~60-70s a full 30-candidate review can take.
const TIMEOUT_MS = 150_000;
// Bound the candidate count and the output budget together: MAX_OUTPUT_TOKENS
// comfortably covers MAX_REVIEW concise assessments (~70 tokens each) without
// letting the model run to a 4k-token generation.
const MAX_REVIEW = 30;
const MAX_OUTPUT_TOKENS = 2600;

// Defense-in-depth egress guard for the no-raw-bank-numbers hard rule: the RPC
// redacts disposition_note at write time, but this is the external egress to
// Anthropic, so scrub long digit runs (incl. space/hyphen-separated) again here —
// covering legacy rows or any out-of-band writes that bypassed the RPC.
const redactDigits = (s: string | null) =>
  s ? s.replace(/\d([ -]?\d){6,}/g, "[redacted]") : s;

// Auth is a Bearer JWT (not a cookie), so a hostile origin can't ride a victim's
// session — but restrict browsers to the review UI's origin anyway when the
// REVIEW_UI_ORIGIN secret is set (unset keeps the open default).
const cors = {
  "Access-Control-Allow-Origin": Deno.env.get("REVIEW_UI_ORIGIN") ?? "*",
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
    // No key configured → skip the AI re-review cleanly (HTTP 200). The disposition
    // and reason were already saved by set_finding_disposition before this ran, so a
    // missing key must not fail the reviewer's action — the live re-review is an
    // optional enhancement (see review-ui/README.md). Set ANTHROPIC_API_KEY as an
    // edge-function secret to enable it.
    if (!apiKey) return json({ updated: 0, skipped: "anthropic_api_key_not_configured" });

    // The human feedback corpus (most recent dispositioned-with-reason findings —
    // the RPC orders newest-first so the note that triggered this call is always
    // included) and the still-open findings to re-review against it. Propagate
    // errors rather than masking a DB/permission failure as "nothing to do".
    const { data: feedback, error: fbErr } = await admin.rpc("feedback_corpus");
    if (fbErr) return json({ error: "feedback_query_failed", detail: fbErr.message }, 500);
    if (!feedback?.length) return json({ updated: 0, note: "no reasoned feedback yet" });

    const { data: openAll, error: openErr } = await admin.rpc("open_findings_for_review");
    if (openErr) return json({ error: "open_query_failed", detail: openErr.message }, 500);
    if (!openAll?.length) return json({ updated: 0, open: 0 });

    // Bulk dispositions write the SAME shared note onto every selected finding, so
    // the corpus can carry hundreds of identical (rule, disposition, note) rows —
    // pure payload bloat that once pushed the model call past its timeout. Collapse
    // each group to one exemplar plus a count the model can weigh.
    const grouped = new Map<string, any>();
    for (const f of feedback) {
      const k = `${f.rule_id}|${f.disposition}|${f.disposition_note}`;
      const g = grouped.get(k);
      if (g) g.times_applied++;
      else grouped.set(k, { ...f, times_applied: 1 });
    }
    const corpus = [...grouped.values()];

    // Scope to the open findings the feedback can plausibly bear on: those sharing a
    // rule_id with a dispositioned-with-reason finding — matching the prompt's own
    // relevance criteria (same rule pattern / root cause). The RPC returns them
    // severity-ranked and newest-first, so the cap keeps the most review-worthy;
    // the cap is surfaced in the response, never silent.
    const feedbackRules = new Set(feedback.map((f: any) => f.rule_id));
    const related = openAll.filter((f: any) => feedbackRules.has(f.rule_id));
    if (!related.length) {
      return json({ updated: 0, considered: 0, open: openAll.length,
                    note: "no open findings related to the feedback" });
    }
    const truncated = related.length > MAX_REVIEW;
    const open = truncated ? related.slice(0, MAX_REVIEW) : related;
    const scope = { considered: open.length,
                    ...(truncated ? { capped_at: MAX_REVIEW, related: related.length } : {}) };

    const system = [
      "You are the Tier 3 reviewer for a forensic accounting tool.",
      "A human reviewer dispositioned some findings and wrote WHY (their reasons).",
      "Re-review each still-OPEN finding in light of that feedback.",
      "Findings are verification questions, not accusations; errors outnumber fraud ~100:1.",
      "Rules you MUST follow:",
      "- You may SUGGEST a disposition (legit | error_corrected | escalated | open) but NEVER decide it; the human does.",
      "- You do NOT set severity. If you believe a severity is wrong, say so in ai_assessment; the badge is governed elsewhere.",
      "- Only return a finding if the human feedback genuinely bears on it (same vendor, same rule pattern, same root cause). Leave unrelated findings out.",
      "- Keep each ai_assessment to one or two sentences; do not restate the finding.",
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
      reviewer_feedback: corpus.map((f: any) => ({
        ...f, disposition_note: redactDigits(f.disposition_note),
      })),
      open_findings: open.map((f: any) => ({
        fingerprint: f.fingerprint, rule_id: f.rule_id, severity: f.severity,
        question: f.question, details: f.details, entity_ids: f.entity_ids,
      })),
    };

    // Everything past this point runs AFTER the response when the runtime supports
    // background tasks — the model call and the writes it drives.
    const runReview = async (): Promise<{ status: number; body: Record<string, unknown> }> => {
      // Abort guard so a hung upstream can't burn the whole wall-clock budget.
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
            max_tokens: MAX_OUTPUT_TOKENS,
            system,
            tools: [tool],
            tool_choice: { type: "tool", name: "update_findings" },
            messages: [{ role: "user", content: JSON.stringify(payload) }],
          }),
        });
      } catch (e) {
        const aborted = e instanceof DOMException && e.name === "AbortError";
        return { status: 504, body: { error: aborted ? "anthropic_timeout" : "anthropic_unreachable",
                                      detail: String(e) } };
      } finally {
        clearTimeout(timer);
      }
      if (!resp.ok) {
        return { status: 502, body: { error: "anthropic_error", status: resp.status,
                                      detail: (await resp.text()).slice(0, 500) } };
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
        // apply_feedback_update only touches rows still open, so a finding the
        // reviewer dispositioned while the model was thinking is left alone.
        const { data: updatedFp, error } = await admin.rpc("apply_feedback_update", {
          p_fingerprint: u.fingerprint,
          p_ai_assessment: u.ai_assessment,
          p_suggested_disposition: u.suggested_disposition ?? null,
        });
        if (error) failures.push({ fingerprint: u.fingerprint, error: error.message });
        else if (updatedFp) applied++;                         // rpc returns fp, or null if no open row
      }
      return { status: 200, body: { updated: applied, ...scope,
                                    ...(failures.length ? { failures } : {}) } };
    };

    const edgeRuntime = (globalThis as any).EdgeRuntime;
    if (typeof edgeRuntime?.waitUntil === "function") {
      // The logs are the only place a background outcome can surface, so always
      // record it — success and failure alike.
      edgeRuntime.waitUntil(runReview().then(
        (r) => console.log("feedback-review background result:", JSON.stringify(r.body)),
        (e) => console.error("feedback-review background crash:", e),
      ));
      return json({ started: true, ...scope }, 202);
    }
    // Runtime without background tasks (e.g. some local dev setups): run inline.
    const r = await runReview();
    return json(r.body, r.status);
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});

// feedback-review — when a reviewer dispositions a finding with a reason, re-review
// every still-OPEN finding in light of the accumulated human feedback and update it
// as needed. Invoked by the review UI after each dispositioned-with-note action.
//
// Guardrails (CLAUDE.md hard rules):
//   - Never sets `disposition` (only `suggested_disposition`) — the human decides.
//   - Never mutates `severity`. Severity floors (vendor bank-detail = CRITICAL until
//     callback-verified; nonprofit misallocation >= HIGH) are deterministic and owned
//     by the offline Tier 3 / rules layer, which has the entity registry. This
//     interactive nudge only updates the assessment + a suggested disposition, so it
//     can never downgrade a finding past a floor. It may note a severity concern in
//     ai_assessment, but the badge only changes through the governed pipeline.
//   - Only updates OPEN findings, and only those the feedback genuinely bears on.
//   - Degrades gracefully: with no ANTHROPIC_API_KEY set it no-ops (the reason is
//     still saved by set_finding_disposition; only the AI re-review is skipped).
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const SCHEMA = "financial_forensics";
const MODEL = Deno.env.get("ANTHROPIC_MODEL") || "claude-sonnet-4-6";
const TIMEOUT_MS = 60_000;

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
    const ff = createClient(url, serviceKey, { db: { schema: SCHEMA } });
    const pub = createClient(url, serviceKey, { db: { schema: "public" } });

    // Authz: the caller must be an allowlisted reviewer (same gate as the RPCs).
    const token = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
    const { data: userData, error: userErr } = await ff.auth.getUser(token);
    const email = userData?.user?.email?.toLowerCase();
    if (userErr || !email) return json({ error: "unauthenticated" }, 401);
    const { data: allow, error: allowErr } = await pub
      .from("review_allowlist").select("email").eq("email", email).maybeSingle();
    if (allowErr) return json({ error: "allowlist_lookup_failed", detail: allowErr.message }, 500);
    if (!allow) return json({ error: "not authorized" }, 403);

    const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
    if (!apiKey) return json({ updated: 0, skipped: "ANTHROPIC_API_KEY not set" });

    // The human feedback corpus (dispositioned findings that carry a reason) and
    // the still-open findings to re-review against it. Propagate query errors
    // rather than masking a DB/permission failure as "nothing to do".
    const { data: feedback, error: fbErr } = await ff.from("findings")
      .select("rule_id,severity,question,disposition,disposition_note,entity_ids")
      .neq("disposition", "open").not("disposition_note", "is", null);
    if (fbErr) return json({ error: "feedback_query_failed", detail: fbErr.message }, 500);
    const { data: open, error: openErr } = await ff.from("findings")
      .select("fingerprint,rule_id,severity,question,details,entity_ids,ai_assessment")
      .eq("disposition", "open");
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
      reviewer_feedback: feedback,
      open_findings: open.map((f) => ({
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

    const openByFp = new Map(open.map((f) => [f.fingerprint, f]));
    const now = new Date().toISOString();
    let applied = 0;
    const failures: { fingerprint: string; error: string }[] = [];
    for (const u of updates) {
      const cur = openByFp.get(u.fingerprint);
      if (!cur || !u.ai_assessment?.trim()) continue;       // only touch loaded open rows
      const { error } = await ff.from("findings").update({
        ai_assessment: u.ai_assessment,
        suggested_disposition: u.suggested_disposition ?? null,
        ai_updated_at: now,
      }).eq("fingerprint", u.fingerprint).eq("disposition", "open");
      if (error) failures.push({ fingerprint: u.fingerprint, error: error.message });
      else applied++;
    }
    return json({ updated: applied, considered: open.length,
                  ...(failures.length ? { failures } : {}) });
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});

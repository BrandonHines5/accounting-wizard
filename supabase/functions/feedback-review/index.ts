// feedback-review — when a reviewer dispositions a finding with a reason, re-review
// every still-OPEN finding in light of the accumulated human feedback and update it
// as needed. Invoked by the review UI after each dispositioned-with-note action.
//
// Guardrails (CLAUDE.md hard rules):
//   - Never sets `disposition` (only `suggested_disposition`) — the human decides.
//   - May lower severity only with a stated reason; never silently drops a CRITICAL.
//   - Only updates OPEN findings, and only those the feedback genuinely bears on.
//   - Degrades gracefully: with no ANTHROPIC_API_KEY set it no-ops (the reason is
//     still saved by set_finding_disposition; only the AI re-review is skipped).
import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import { createClient } from "jsr:@supabase/supabase-js@2";

const SCHEMA = "financial_forensics";
const MODEL = Deno.env.get("ANTHROPIC_MODEL") || "claude-sonnet-4-6";
const SEV_RANK: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, INFO: 3 };

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
    const { data: allow } = await pub
      .from("review_allowlist").select("email").eq("email", email).maybeSingle();
    if (!allow) return json({ error: "not authorized" }, 403);

    const apiKey = Deno.env.get("ANTHROPIC_API_KEY");
    if (!apiKey) return json({ updated: 0, skipped: "ANTHROPIC_API_KEY not set" });

    // The human feedback corpus (dispositioned findings that carry a reason) and
    // the still-open findings to re-review against it.
    const { data: feedback } = await ff.from("findings")
      .select("rule_id,severity,question,disposition,disposition_note,entity_ids")
      .neq("disposition", "open").not("disposition_note", "is", null);
    const { data: open } = await ff.from("findings")
      .select("fingerprint,rule_id,severity,question,details,entity_ids,ai_assessment")
      .eq("disposition", "open");

    if (!open?.length) return json({ updated: 0, open: 0 });
    if (!feedback?.length) return json({ updated: 0, note: "no reasoned feedback yet" });

    const system = [
      "You are the Tier 3 reviewer for a forensic accounting tool.",
      "A human reviewer dispositioned some findings and wrote WHY (their reasons).",
      "Re-review each still-OPEN finding in light of that feedback.",
      "Findings are verification questions, not accusations; errors outnumber fraud ~100:1.",
      "Rules you MUST follow:",
      "- You may SUGGEST a disposition (legit | error_corrected | escalated | open) but NEVER decide it; the human does.",
      "- You may LOWER severity (e.g. CRITICAL→MEDIUM) only with an explicit reason in ai_assessment. Never silently drop a CRITICAL. Do not RAISE severity here.",
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
                severity: { type: "string", enum: ["CRITICAL", "HIGH", "MEDIUM", "INFO"] },
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

    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
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
    for (const u of updates) {
      const cur = openByFp.get(u.fingerprint);
      if (!cur || !u.ai_assessment?.trim()) continue;       // only touch loaded open rows
      const patch: Record<string, unknown> = {
        ai_assessment: u.ai_assessment,
        suggested_disposition: u.suggested_disposition ?? null,
        ai_updated_at: now,
      };
      // Severity may only move DOWN (rank increases), and only with a reason.
      if (u.severity && SEV_RANK[u.severity] > (SEV_RANK[cur.severity] ?? 9)) {
        patch.severity = u.severity;
      }
      const { error } = await ff.from("findings")
        .update(patch).eq("fingerprint", u.fingerprint).eq("disposition", "open");
      if (!error) applied++;
    }
    return json({ updated: applied, considered: open.length });
  } catch (e) {
    return json({ error: String(e) }, 500);
  }
});

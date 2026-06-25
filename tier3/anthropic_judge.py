"""Claude-backed Tier 3 judge.

One model call per finding, with the (large, stable) instruction block cached so
repeated findings in a run reuse the prefix. Structured output is enforced via
`output_config.format` (a JSON schema), so the response is always a valid
assessment object — no brittle parsing.

The Anthropic SDK is an optional dependency: it is imported lazily so the rest of
the package (and the test suite) runs without it. For large weekly volumes this
synchronous judge can be swapped for a Batches-API implementation (50% cost, but
higher wall-clock latency) behind the same `Judge` interface.
"""
from __future__ import annotations

import json

from core.findings import Severity
from tier3.context import JudgmentPacket
from tier3.judge import Judge, Tier3Assessment, coerce_severity

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """\
You are the Tier 3 reviewer in a forensic-accounting detection battery for a \
small family-owned construction group. Deterministic rules and statistical \
checks (Tiers 1, 2, 4) have already flagged the finding below. Your job is to \
make the human disposition session short and readable.

Operating principles:
- Honest errors and external vendor fraud will outnumber insider fraud roughly \
100 to 1. Default to the benign explanation when the evidence supports it.
- Findings are verification QUESTIONS, never accusations. Keep that tone.
- You MAY adjust severity up or down, but you MUST give a reason whenever you \
change it. Never downgrade a CRITICAL finding without a concrete, stated \
justification — when in doubt, keep it.
- Severity levels: CRITICAL, HIGH, MEDIUM, INFO.
- Recommended action is one of: clear (benign, no follow-up), verify (check a \
specific document/person), escalate (treat as serious until disproven).

For the finding you receive, return:
1. assessment — 2–4 plain-English sentences a non-accountant owner can act on.
2. severity — confirmed or adjusted, with severity_reason whenever it changes \
(empty string if unchanged).
3. false_positive_probability — 0.0 to 1.0, your estimate this is a benign \
error or normal business activity.
4. innocent_explanation — the specific benign explanation (void/reissue, bank \
fee, timing, known vendor quirk) if a false positive is plausible; else empty.
5. recommended_action and recommended_action_detail — the single next step."""

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "assessment": {"type": "string"},
        "severity": {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "INFO"]},
        "severity_reason": {"type": "string"},
        "false_positive_probability": {"type": "number"},
        "innocent_explanation": {"type": "string"},
        "recommended_action": {"type": "string", "enum": ["clear", "verify", "escalate"]},
        "recommended_action_detail": {"type": "string"},
    },
    "required": ["assessment", "severity", "severity_reason",
                 "false_positive_probability", "innocent_explanation",
                 "recommended_action", "recommended_action_detail"],
}


class AnthropicJudge(Judge):
    """Tier 3 judge backed by a Claude model, one structured call per finding."""

    def __init__(self, client=None, model: str = MODEL, max_tokens: int = 1500):
        self.model = model
        self.max_tokens = max_tokens
        self._client = client  # injectable for tests; lazily constructed otherwise

    @property
    def client(self):
        """The Anthropic client, constructed on first use (optional dependency)."""
        if self._client is None:
            import anthropic  # lazy: optional dependency
            self._client = anthropic.Anthropic()
        return self._client

    def assess(self, packet: JudgmentPacket) -> Tier3Assessment:
        """Send the packet to Claude and parse the structured assessment back."""
        payload = json.dumps(packet.to_prompt_dict(), default=str, indent=2)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user",
                       "content": f"Review this finding:\n{payload}"}],
        )
        return _parse(response, fallback=packet.finding.severity)


def _parse(response, fallback: Severity) -> Tier3Assessment:
    """Map a structured-output response into a Tier3Assessment."""
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    if not text.strip():
        # No text block (e.g. a refusal or thinking-only reply). Fail explicitly
        # so assess_all routes it to a clear _failed_assessment, not a JSON crash.
        raise ValueError("Tier 3 response carried no text block to parse")
    data = json.loads(text)
    return Tier3Assessment(
        assessment=data.get("assessment", ""),
        severity=coerce_severity(data.get("severity"), fallback),
        severity_reason=data.get("severity_reason", ""),
        false_positive_probability=data.get("false_positive_probability", 0.0),
        innocent_explanation=data.get("innocent_explanation", ""),
        recommended_action=data.get("recommended_action", "verify"),
        recommended_action_detail=data.get("recommended_action_detail", ""),
    )

"""
AISynthesizer — sends Black Duck + cost findings to Claude (via LiteLLM)
and returns a structured verdict dict.
"""

from __future__ import annotations

import json

import litellm

from .prompts import SYSTEM_PROMPT, build_user_prompt

_MODEL = "anthropic/claude-sonnet-4-6"
_MAX_TOKENS = 4096


class AISynthesizer:
    def __init__(self, api_key: str, api_base: str | None = None) -> None:
        self._api_key = api_key
        self._api_base = api_base

    def synthesize(self, bd_findings: dict, cost_findings: dict) -> dict:
        """
        Call Claude with structured findings and return parsed verdict dict.
        Falls back to a minimal verdict on API or parse errors.
        """
        user_prompt = build_user_prompt(bd_findings, cost_findings)
        print(f"[AI] Sending findings to {_MODEL} for synthesis …")

        try:
            response = litellm.completion(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                api_key=self._api_key,
                api_base=self._api_base,
            )
        except Exception as exc:
            print(f"[AI] LiteLLM API error: {exc}")
            return _fallback_verdict(bd_findings, cost_findings, error=str(exc))

        raw_text = response.choices[0].message.content.strip()
        print(f"[AI] Received response ({len(raw_text)} chars)")

        return _parse_verdict(raw_text, bd_findings, cost_findings)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_verdict(raw: str, bd_findings: dict, cost_findings: dict) -> dict:
    # Strip markdown code fences if present
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        verdict = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"[AI] Failed to parse JSON from Claude: {exc}")
        return _fallback_verdict(bd_findings, cost_findings, error=f"JSON parse error: {exc}", raw=raw)

    # Ensure required top-level keys exist
    for key in ("verdict", "verdict_reason", "security", "cost", "recommended_actions", "pr_comment_markdown"):
        if key not in verdict:
            print(f"[AI] Warning: missing key '{key}' in Claude response")

    return verdict


def _fallback_verdict(bd_findings: dict, cost_findings: dict, error: str = "", raw: str = "") -> dict:
    """Minimal safe verdict when Claude is unavailable or returns unparseable output."""
    summary = bd_findings.get("summary", {})
    critical = summary.get("critical", 0)
    high = summary.get("high", 0)

    if critical > 0:
        verdict = "BLOCK"
        reason = f"Claude synthesis failed but {critical} CRITICAL CVE(s) detected — blocking as precaution."
    elif high > 0:
        verdict = "WARN"
        reason = f"Claude synthesis failed but {high} HIGH CVE(s) detected — manual review required."
    else:
        verdict = "WARN"
        reason = "Claude synthesis failed — manual review recommended."

    comment = (
        "## GateKeeper Analysis\n\n"
        f"> **{verdict}** — {reason}\n\n"
        "_AI synthesis was unavailable. Please review Black Duck and cost findings manually._\n\n"
        f"```\n{error}\n```"
    )

    return {
        "verdict": verdict,
        "verdict_reason": reason,
        "security": {"summary": str(summary)},
        "cost": {},
        "recommended_actions": ["Review findings manually — AI synthesis failed."],
        "pr_comment_markdown": comment,
        "_synthesis_error": error,
        "_raw_response": raw,
    }

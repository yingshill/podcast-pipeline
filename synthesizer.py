"""
Cross-episode synthesis — finds themes, recurring claims, and open questions
across a set of recent podcast episodes via a single Claude call.
"""
from __future__ import annotations
import json
import logging
import os

import anthropic
from rich.console import Console as _Console
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import SynthesisReport, CrossTheme, RecurringClaim, OpenQuestion

log = logging.getLogger(__name__)
_console = _Console()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000

_SYSTEM = """\
You are a knowledge synthesis expert. You receive structured data from multiple recent podcast
episodes and identify the patterns, recurring themes, and open questions that span across them.

Your output helps the reader see what's emerging across their listening — the meta-narrative
they couldn't see by reading each episode in isolation.

━━━ OUTPUT RULES ━━━

throughline
  2–3 sentences. The single most important narrative arc across ALL episodes this period.
  What is the intellectual current running through them?
  Be specific and surprising — not "AI is changing things" but the actual shared argument.

cross_themes
  2–4 themes that appear in at least 2 episodes. Be precise with theme names.
  - theme: short specific name (not "AI" or "leadership" — e.g. "evaluation over intuition")
  - episodes: list of episode titles where this theme appears
  - synthesis: 2–3 sentences on what different speakers said about this theme and how it connects

recurring_claims
  2–4 specific claims or arguments that multiple speakers made independently.
  More concrete than themes — a single assertable sentence that keeps appearing.
  - claim: the specific claim in one sentence
  - episodes: episode titles where this appears
  - why_it_matters: one sentence on why this convergence is significant

open_questions
  2–3 questions the episodes collectively raise but don't resolve — real tensions or gaps.
  - question: the unresolved question in one sentence
  - context: one sentence on why it keeps surfacing

distilled_actions
  3–5 concrete first-person actions distilled across all episodes.
  Specific enough to act on this week. Not platitudes.

━━━ CONSTRAINTS ━━━
- Return ONLY valid JSON. No markdown fences, no commentary outside the JSON.
- Do not fabricate content not present in the episode data.
- Prioritize specificity over comprehensiveness — fewer, sharper insights beat many vague ones.
- cross_themes and recurring_claims must each reference at least 2 different episodes.

━━━ JSON SCHEMA ━━━
{
  "throughline": "",
  "cross_themes": [
    {"theme": "", "episodes": [], "synthesis": ""}
  ],
  "recurring_claims": [
    {"claim": "", "episodes": [], "why_it_matters": ""}
  ],
  "open_questions": [
    {"question": "", "context": ""}
  ],
  "distilled_actions": []
}
"""


def _build_user_message(episodes: list[dict], date_range: str) -> str:
    parts = [
        f"## Synthesis Period: {date_range}",
        f"## Episodes ({len(episodes)} total)",
        "",
    ]
    for i, ep in enumerate(episodes, 1):
        parts.append(f"### {i}. {ep['title']}")
        if ep.get("show"):
            parts.append(f"Show: {ep['show']}")
        if ep.get("tags"):
            parts.append(f"Tags: {', '.join(ep['tags'])}")
        if ep.get("core_insight"):
            parts.append(f"Core Insight: {ep['core_insight']}")
        if ep.get("why_it_matters"):
            parts.append(f"Why It Matters: {ep['why_it_matters']}")
        parts.append("")
    return "\n".join(parts)


@retry(
    retry=retry_if_exception_type(anthropic.APIError),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _call_claude(user_message: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def synthesize(episodes: list[dict], date_range: str) -> SynthesisReport:
    if len(episodes) < 2:
        raise ValueError(
            f"Need at least 2 episodes to synthesize (found {len(episodes)}). "
            "Try --weeks 2 or more."
        )

    log.info("Synthesizing %d episodes for %s", len(episodes), date_range)
    user_msg = _build_user_message(episodes, date_range)

    with _console.status("[dim]Synthesizing across episodes…[/dim]", spinner="dots"):
        raw = _call_claude(user_msg)

    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    data = json.loads(raw)

    return SynthesisReport(
        date_range=date_range,
        episode_count=len(episodes),
        episodes_covered=[ep["title"] for ep in episodes],
        throughline=data.get("throughline", ""),
        cross_themes=[
            CrossTheme(
                theme=t["theme"],
                episodes=t.get("episodes", []),
                synthesis=t.get("synthesis", ""),
            )
            for t in data.get("cross_themes", [])
        ],
        recurring_claims=[
            RecurringClaim(
                claim=c["claim"],
                episodes=c.get("episodes", []),
                why_it_matters=c.get("why_it_matters", ""),
            )
            for c in data.get("recurring_claims", [])
        ],
        open_questions=[
            OpenQuestion(
                question=q["question"],
                context=q.get("context", ""),
            )
            for q in data.get("open_questions", [])
        ],
        distilled_actions=data.get("distilled_actions", []),
    )

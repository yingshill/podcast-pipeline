"""
Human eval collection for pipeline output quality.

Called automatically after every report generation.
Results saved to evals/{job_id}.json and summarised on screen.

Rating scale: 1 = unusable  2 = poor  3 = acceptable  4 = good  5 = excellent
Press Enter to skip any field.
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()

EVALS_DIR = Path(__file__).parent / "evals"

# ── Metric definitions ────────────────────────────────────────────────────────

SECTIONS = [
    ("db_properties",    "DB Properties",     "Tags / Eval Metric / Action / Output — correct and relevant?"),
    ("episode_summary",  "Episode Summary",   "Captures the arc, central argument, and conclusion accurately?"),
    ("concept_map",      "Concept Map",       "Concepts are central to the episode, explanations are clear?"),
    ("chapter_breakdown","Chapter Breakdown", "Chapter titles descriptive, 2-line summaries accurate?"),
    ("key_insights",     "Key Insights",      "Quotes verbatim from annotations, insights add real interpretation?"),
    ("host_questions",   "Host Questions",    "Important questions captured, categories correct?"),
]


# ── Collection ────────────────────────────────────────────────────────────────

def _prompt(label: str, hint: str = "") -> str:
    suffix = f" [dim]{hint}[/dim]" if hint else ""
    console.print(f"  {label}{suffix}", end=" ")
    try:
        return input().strip()
    except EOFError:
        return ""


def _rating(label: str, hint: str = "") -> int | None:
    raw = _prompt(label, hint or "1–5 or Enter to skip")
    if raw in ("1", "2", "3", "4", "5"):
        return int(raw)
    return None


def collect_eval(
    job_id: str,
    notion_url: str,
    source_url: str,
) -> dict:
    """
    Interactively collect human eval ratings.
    Returns the eval dict (also saves to disk).
    Skips gracefully if not running in an interactive terminal.
    """
    if not sys.stdin.isatty():
        console.print("[dim]  (Non-interactive terminal — skipping eval collection)[/dim]")
        return {}

    console.print()
    console.rule("[bold yellow]📊 Eval — Rate This Report[/bold yellow]")
    console.print(f"  Notion: [link={notion_url}]{notion_url}[/link]\n")
    console.print("  Rate each section 1–5  (press Enter to skip)\n")

    ratings: dict[str, dict] = {}
    for key, name, description in SECTIONS:
        console.print(f"  [bold]{name}[/bold] — [dim]{description}[/dim]")
        score = _rating("    Score:")
        note  = _prompt("    Note: ", "(optional free text)")
        ratings[key] = {"score": score, "note": note or None}
        console.print()

    console.print("  [bold]Overall[/bold]")
    overall   = _rating("    Overall score:")
    best      = _prompt("    Best section:", "(which worked well?)")
    main_issue= _prompt("    Main issue:",   "(what to fix first?)")
    console.print()

    result = {
        "job_id":      job_id,
        "timestamp":   datetime.utcnow().isoformat(),
        "notion_url":  notion_url,
        "source_url":  source_url,
        "sections":    ratings,
        "overall":     overall,
        "best_section":best or None,
        "main_issue":  main_issue or None,
    }

    save_eval(result)
    print_eval_table(result)
    return result


# ── Storage ───────────────────────────────────────────────────────────────────

def save_eval(result: dict) -> Path:
    EVALS_DIR.mkdir(exist_ok=True)
    path = EVALS_DIR / f"{result['job_id']}.json"
    path.write_text(json.dumps(result, indent=2))
    console.print(f"  [dim]Eval saved → {path}[/dim]\n")
    return path


# ── Display ───────────────────────────────────────────────────────────────────

def _score_color(score: int | None) -> str:
    if score is None:
        return "dim"
    return {1: "red", 2: "red", 3: "yellow", 4: "green", 5: "bold green"}.get(score, "white")


def print_eval_table(result: dict) -> None:
    t = Table(title="Report Eval", show_header=True, header_style="bold")
    t.add_column("Section",  style="cyan", width=22)
    t.add_column("Score",    justify="center", width=7)
    t.add_column("Note",     width=45)

    for key, name, _ in SECTIONS:
        entry = result.get("sections", {}).get(key, {})
        score = entry.get("score")
        note  = entry.get("note") or ""
        color = _score_color(score)
        t.add_row(name, f"[{color}]{score or '—'}[/{color}]", note)

    overall = result.get("overall")
    color   = _score_color(overall)
    t.add_row("─" * 20, "─" * 5, "─" * 40)
    t.add_row(
        "[bold]Overall[/bold]",
        f"[{color}]{overall or '—'}[/{color}]",
        result.get("main_issue") or "",
    )
    console.print(t)

    if result.get("best_section"):
        console.print(f"  ✓ Best: {result['best_section']}")
    if result.get("main_issue"):
        console.print(f"  ✗ Fix:  {result['main_issue']}")
    console.print()


def print_history() -> None:
    """Print a summary table of all past evals."""
    if not EVALS_DIR.exists():
        console.print("No evals saved yet.")
        return

    files = sorted(EVALS_DIR.glob("*.json"), reverse=True)
    if not files:
        console.print("No evals saved yet.")
        return

    t = Table(title=f"Eval History ({len(files)} runs)", show_header=True, header_style="bold")
    t.add_column("Date",       width=12)
    t.add_column("Overall",    justify="center", width=8)
    for _, name, _ in SECTIONS:
        t.add_column(name[:14], justify="center", width=6)
    t.add_column("Main Issue", width=30)

    for f in files:
        try:
            r = json.loads(f.read_text())
        except Exception:
            continue
        ts    = r.get("timestamp", "")[:10]
        overall = r.get("overall")
        oc    = _score_color(overall)
        scores = []
        for key, _, _ in SECTIONS:
            s = r.get("sections", {}).get(key, {}).get("score")
            c = _score_color(s)
            scores.append(f"[{c}]{s or '—'}[/{c}]")
        t.add_row(ts, f"[{oc}]{overall or '—'}[/{oc}]", *scores, r.get("main_issue") or "")

    console.print(t)

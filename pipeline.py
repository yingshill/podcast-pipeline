#!/usr/bin/env python3
"""
Podcast Digest Pipeline — CLI entry point.

Commands:
  run <url>          Full pipeline (pauses for human annotation step)
  resume <job_id>    Resume a failed or interrupted job
  status <job_id>    Show job state
  list               Show all jobs
  scrape <url>       Stage 1 only: transcribe and exit
  analyze-doc <id>   Analyze an existing Google Doc and write Notion report
  clean <job_id>     Delete cached files to force fresh re-analysis
  eval <job_id>           Interactively rate a completed report
  eval-history            Show eval scores across all past runs
  synthesize [--weeks N]  Cross-episode synthesis: themes, claims, open questions

Phrase Library (runs automatically at end of analyze):
  Underlined annotations → Speaker Phrase Library Notion DB
  (classified, explained in EN + 中文, context quote attached)
"""
from __future__ import annotations
import json
import logging
import logging.handlers
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich import print as rprint

import scraper
import gdocs
import annotations as ann_mod
import analyzer
import notion_writer
import eval_metrics
import state as state_mod
from models import PipelineJob, PipelineStatus

load_dotenv()
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            "pipeline.log", maxBytes=5 * 1024 * 1024, backupCount=3
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("pipeline")

# Transcript + anchor data cached to disk alongside the state DB
_CACHE_DIR = Path(__file__).parent / ".cache"
_CACHE_DIR.mkdir(exist_ok=True)

_COVERS_DIR = Path(__file__).parent / "covers"
_COVERS_DIR.mkdir(exist_ok=True)


def _cache_path(job_id: str, suffix: str) -> Path:
    return _CACHE_DIR / f"{job_id}_{suffix}.json"


def _save_cache(job_id: str, suffix: str, data) -> None:
    with open(_cache_path(job_id, suffix), "w") as f:
        json.dump(data, f, default=lambda o: o.__dict__)


def _load_cache(job_id: str, suffix: str):
    p = _cache_path(job_id, suffix)
    if not p.exists():
        return None
    with open(p) as f:
        return json.load(f)


# ── Stage runners ─────────────────────────────────────────────────────────────

def stage_scrape(job: PipelineJob) -> None:
    job.status = PipelineStatus.SCRAPING
    state_mod.save_job(job)

    with console.status("[dim]Transcribing…[/dim]", spinner="dots"):
        segments, aai_id = scraper.transcribe(job.url)
    job.assemblyai_id = aai_id
    job.status = PipelineStatus.SCRAPED
    state_mod.save_job(job)

    _save_cache(job.id, "segments", [s.__dict__ for s in segments])
    console.print(f"[green]✓ Transcript ready:[/green] {len(segments)} segments  (ID: {aai_id})")


def stage_create_doc(job: PipelineJob) -> None:
    from models import TranscriptSegment

    seg_data = _load_cache(job.id, "segments")
    if not seg_data:
        raise RuntimeError("No transcript cache found. Run 'scrape' stage first.")

    segments = [TranscriptSegment(**s) for s in seg_data]
    title = f"Podcast Transcript — {job.url[:60]}{'…' if len(job.url) > 60 else ''}"

    job.status = PipelineStatus.DOC_CREATING
    state_mod.save_job(job)

    doc_id, doc_url, anchors = gdocs.create_transcript_doc(title, segments)
    job.doc_id = doc_id
    job.doc_url = doc_url
    job.status = PipelineStatus.DOC_CREATED
    state_mod.save_job(job)

    _save_cache(job.id, "anchors", [a.__dict__ for a in anchors])
    console.print(f"[green]✓ Google Doc created:[/green] {doc_url}")


def stage_await_annotation(job: PipelineJob) -> None:
    job.status = PipelineStatus.AWAITING_ANNOTATION
    state_mod.save_job(job)

    console.print()
    console.rule("[bold yellow]Human Annotation Step[/bold yellow]")
    console.print(f"\n[bold]Open the transcript in Google Docs:[/bold]")
    console.print(f"  {job.doc_url}\n")
    console.print("• [yellow]Highlight[/yellow] passages that resonate with you")
    console.print("• [underline]Underline[/underline] key insights or quotes")
    console.print("• When done, return here and press [bold]Enter[/bold] to continue\n")
    input("  → Press Enter when you've finished annotating... ")
    console.print()


def stage_analyze_and_report(job: PipelineJob) -> None:
    import dataclasses
    from models import AnnotatedSegment, HeadingAnchor

    job.status = PipelineStatus.ANALYZING
    state_mod.save_job(job)

    anchor_data = _load_cache(job.id, "anchors") or []
    anchors = [HeadingAnchor(**a) for a in anchor_data]

    annotated_segs, live_anchors = ann_mod.get_annotated_segments(job.doc_id)
    all_anchors = live_anchors if live_anchors else anchors

    if not annotated_segs:
        console.print("[red]No annotations found in the document.[/red]")
        console.print("Please highlight or underline text in the Google Doc, then run [bold]resume[/bold].")
        job.status = PipelineStatus.AWAITING_ANNOTATION
        state_mod.save_job(job)
        return

    context_map = ann_mod.build_context_map(annotated_segs, job.doc_id)
    podcast_title = _title_from_url(job.url)

    # Parallel I/O: Notion context, DB options, and episode metadata simultaneously
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_ctx  = pool.submit(notion_writer.fetch_notion_context)
        f_opts = pool.submit(notion_writer.fetch_db_options)
        f_meta = pool.submit(scraper.scrape_episode_metadata, job.url)
        topic_hub, recent_entries = f_ctx.result()
        db_options                = f_opts.result()
        episode_metadata          = f_meta.result()

    # Prefer scraped metadata title over URL slug
    scraped_title = (
        episode_metadata.get("jsonld_name") or
        episode_metadata.get("og_title") or
        ""
    ).strip()
    if scraped_title:
        podcast_title = scraped_title

    # Download cover image from og:image if available
    cover_path: str | None = None
    cover_url = episode_metadata.get("og_image")
    if cover_url:
        dest = str(_COVERS_DIR / job.id)  # extension appended by download_cover
        if scraper.download_cover(cover_url, dest):
            import glob
            matches = glob.glob(f"{dest}.*")
            cover_path = matches[0] if matches else None
            if cover_path:
                console.print(f"[dim]✓ Cover saved: {cover_path}[/dim]")

    # Use cached report if analysis already completed (avoids re-paying Claude on Notion retry)
    cached = _load_cache(job.id, "report")
    if cached:
        report = analyzer.reconstruct_report(cached)
        console.print("[dim]✓ Analysis loaded from cache[/dim]")
    else:
        report = analyzer.analyze(
            podcast_title=podcast_title,
            source_url=job.url,
            doc_url=job.doc_url,
            annotated=annotated_segs,
            context_map=context_map,
            all_anchors=all_anchors,
            topic_hub=topic_hub,
            recent_entries=recent_entries,
            episode_metadata=episode_metadata,
            source_options=db_options.get("Source", []),
            tags_options=db_options.get("Tags", []),
        )
        _save_cache(job.id, "report", dataclasses.asdict(report))

    job.status = PipelineStatus.ANALYZED
    state_mod.save_job(job)
    console.print(f"[green]✓ Analysis complete:[/green] {len(report.key_insights)} insights, {len(report.chapter_breakdown)} chapters")

    # Write to Notion — save page_id immediately after creation so it survives append failures
    job.status = PipelineStatus.REPORTING
    state_mod.save_job(job)

    def _on_page_created(page_id: str, page_url: str) -> None:
        job.notion_page_id = page_id
        job.notion_page_url = page_url
        state_mod.save_job(job)

    page_id, page_url = notion_writer.write_report(
        report, topic_hub=topic_hub, on_page_created=_on_page_created, cover_path=cover_path,
    )
    job.notion_page_id = page_id
    job.notion_page_url = page_url
    job.status = PipelineStatus.COMPLETED
    state_mod.save_job(job)

    console.print(f"[green]✓ Notion report created:[/green] {page_url}")

    # Extract underlined phrases → Speaker Phrase Library
    import phrase_extractor
    underlined = [s for s in annotated_segs if s.annotation_type == "underline"]
    if underlined:
        try:
            phrase_entries = phrase_extractor.extract_phrases(
                underlines=underlined,
                context_map=context_map,
                podcast_title=report.podcast_title,
                doc_url=job.doc_url,
            )
            count = notion_writer.write_phrase_entries(phrase_entries)
            console.print(f"[green]✓ Phrase Library:[/green] {count}/{len(underlined)} phrases added")
        except Exception as exc:
            log.warning("Phrase extraction failed (non-fatal): %s", exc)
            console.print(f"[yellow]⚠ Phrase extraction skipped:[/yellow] {exc}")

    console.print(f"[dim]  Run eval: python pipeline.py eval {job.id}[/dim]")


def _title_from_url(url: str) -> str:
    # Best-effort: extract something readable from the URL
    parts = url.rstrip("/").split("/")
    return parts[-1].replace("-", " ").replace("_", " ").title() or "Podcast Episode"


# ── CLI commands ──────────────────────────────────────────────────────────────

@click.group()
def cli():
    """Podcast Digest Pipeline — transcript → annotation → AI report → Notion"""


@cli.command()
@click.argument("url")
def run(url: str):
    """Full pipeline: scrape → Google Doc → annotate → analyze → Notion."""
    job = state_mod.create_job(url)
    console.print(f"\n[bold cyan]New job:[/bold cyan] {job.id}  ({url[:80]})\n")

    try:
        stage_scrape(job)
        stage_create_doc(job)
        stage_await_annotation(job)
        stage_analyze_and_report(job)
    except Exception as exc:
        job.status = PipelineStatus.FAILED
        job.error = str(exc)
        state_mod.save_job(job)
        console.print(f"\n[red]Pipeline failed:[/red] {exc}")
        console.print(f"Resume with: [bold]python pipeline.py resume {job.id}[/bold]")
        raise SystemExit(1)

    console.rule("[bold green]Pipeline complete[/bold green]")
    console.print(f"\nTranscript:  {job.doc_url}")
    console.print(f"Notion report: {job.notion_page_url}\n")


@cli.command()
@click.argument("job_id")
def resume(job_id: str):
    """Resume a failed or interrupted job from its last checkpoint."""
    job = state_mod.get_job(job_id)
    if not job:
        console.print(f"[red]Job {job_id} not found[/red]")
        raise SystemExit(1)

    console.print(f"\n[bold cyan]Resuming job {job.id}[/bold cyan]  (status: {job.status})\n")

    try:
        if job.status in (PipelineStatus.PENDING, PipelineStatus.SCRAPING, PipelineStatus.FAILED):
            stage_scrape(job)

        if job.status == PipelineStatus.SCRAPED:
            stage_create_doc(job)

        if job.status in (PipelineStatus.DOC_CREATED, PipelineStatus.AWAITING_ANNOTATION):
            stage_await_annotation(job)

        if job.status in (PipelineStatus.AWAITING_ANNOTATION, PipelineStatus.ANALYZING, PipelineStatus.ANALYZED):
            stage_analyze_and_report(job)

        if job.status == PipelineStatus.REPORTING:
            if job.notion_page_id:
                console.print(f"[yellow]Notion page was created but block appending may have failed.[/yellow]")
                console.print(f"  Page: {job.notion_page_url}")
                console.print("  Check the page manually and re-run if content is missing.")
                job.status = PipelineStatus.COMPLETED
                state_mod.save_job(job)
            else:
                stage_analyze_and_report(job)

    except Exception as exc:
        job.status = PipelineStatus.FAILED
        job.error = str(exc)
        state_mod.save_job(job)
        console.print(f"\n[red]Failed:[/red] {exc}")
        raise SystemExit(1)

    if job.status == PipelineStatus.COMPLETED:
        console.rule("[bold green]Complete[/bold green]")
        console.print(f"Notion report: {job.notion_page_url}")


@cli.command()
@click.argument("job_id")
def status(job_id: str):
    """Show the current state of a job."""
    job = state_mod.get_job(job_id)
    if not job:
        console.print(f"[red]Job {job_id} not found[/red]")
        raise SystemExit(1)
    _print_job(job)


@cli.command("list")
def list_jobs():
    """List all jobs."""
    jobs = state_mod.list_jobs()
    if not jobs:
        console.print("No jobs found.")
        return

    eval_dir = Path(__file__).parent / "evals"
    eval_scores: dict[str, str] = {}
    if eval_dir.exists():
        for f in eval_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text())
                jid = data.get("job_id", f.stem)
                score = data.get("overall")
                eval_scores[jid] = str(score) if score else "—"
            except Exception:
                pass

    _SCORE_COLOR = {"5": "bold green", "4": "green", "3": "yellow", "2": "red", "1": "red"}

    t = Table(show_header=True, header_style="bold")
    t.add_column("ID", style="cyan")
    t.add_column("Status")
    t.add_column("URL")
    t.add_column("Created")
    t.add_column("Eval", justify="center")
    t.add_column("Notion")
    for j in jobs:
        color = "green" if j.status == PipelineStatus.COMPLETED else (
            "red" if j.status == PipelineStatus.FAILED else "yellow"
        )
        score = eval_scores.get(j.id, "—")
        sc = _SCORE_COLOR.get(score, "dim")
        t.add_row(
            j.id,
            f"[{color}]{j.status}[/{color}]",
            j.url[:50],
            j.created_at[:19],
            f"[{sc}]{score}[/{sc}]",
            j.notion_page_url or "—",
        )
    console.print(t)


@cli.command()
@click.argument("url")
def scrape(url: str):
    """Stage 1 only: transcribe a URL and cache segments."""
    job = state_mod.create_job(url)
    console.print(f"Job ID: {job.id}")
    try:
        stage_scrape(job)
    except Exception as exc:
        job.status = PipelineStatus.FAILED
        job.error = str(exc)
        state_mod.save_job(job)
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1)


@cli.command("analyze-doc")
@click.argument("doc_id")
@click.option("--url", default="", help="Source URL (overrides first-line URL in the doc)")
@click.option("--title", default="", help="Episode title (optional)")
def analyze_doc(doc_id: str, url: str, title: str):
    """Run analyze + Notion report stages on an existing Google Doc.

    Source URL is read from the first line of the doc if not passed via --url.
    """
    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    podcast_title = title or f"Podcast — {doc_id[:12]}"

    source_url = url or gdocs.read_source_url(doc_id)
    if not source_url:
        console.print("[red]No source URL found. Add it as the first line of the Google Doc or pass --url.[/red]")
        raise SystemExit(1)

    job = state_mod.create_job(source_url)
    job.doc_id = doc_id
    job.doc_url = doc_url
    job.status = PipelineStatus.DOC_CREATED
    state_mod.save_job(job)

    console.print(f"\n[bold cyan]Analyze-doc job:[/bold cyan] {job.id}")
    console.print(f"Doc:    {doc_url}")
    console.print(f"Source: {source_url}\n")

    try:
        stage_analyze_and_report(job)
    except Exception as exc:
        job.status = PipelineStatus.FAILED
        job.error = str(exc)
        state_mod.save_job(job)
        console.print(f"\n[red]Failed:[/red] {exc}")
        raise SystemExit(1)

    console.rule("[bold green]Complete[/bold green]")
    console.print(f"Notion report: {job.notion_page_url}\n")


@cli.command()
@click.argument("job_id")
def clean(job_id: str):
    """Delete cached files for a job to force a fresh re-analysis."""
    job = state_mod.get_job(job_id)
    if not job:
        console.print(f"[red]Job {job_id} not found[/red]")
        raise SystemExit(1)
    deleted = 0
    for suffix in ("segments", "anchors", "report"):
        p = _cache_path(job_id, suffix)
        if p.exists():
            p.unlink()
            console.print(f"[dim]Deleted {p.name}[/dim]")
            deleted += 1
    if deleted:
        console.print(f"[green]Cache cleared ({deleted} file(s))[/green]")
        console.print(f"Run [bold]python pipeline.py resume {job_id}[/bold] to re-analyze.")
    else:
        console.print("[dim]No cache files found for this job.[/dim]")


@cli.command()
@click.option("--weeks", default=1, show_default=True, help="How many weeks back to include")
def synthesize(weeks: int):
    """Cross-episode synthesis: themes, recurring claims, open questions across recent episodes."""
    import synthesizer as syn_mod
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(weeks=weeks)
    date_range = f"{cutoff.strftime('%b %-d')} – {now.strftime('%b %-d, %Y')}"

    console.print(f"\n[bold cyan]Synthesis:[/bold cyan] {date_range}\n")

    with console.status("[dim]Fetching episodes from Notion…[/dim]", spinner="dots"):
        episodes = notion_writer.fetch_recent_episodes(weeks=weeks)

    if not episodes:
        console.print(f"[yellow]No episodes found in the last {weeks} week(s).[/yellow]")
        console.print("Try: [bold]python pipeline.py synthesize --weeks 2[/bold]")
        raise SystemExit(0)

    console.print(f"[dim]Found {len(episodes)} episode(s):[/dim]")
    for ep in episodes:
        show = f" ({ep['show']})" if ep.get("show") else ""
        console.print(f"  • {ep['title']}{show}")
    console.print()

    if len(episodes) < 2:
        console.print("[yellow]Need at least 2 episodes for synthesis. Try --weeks 2 or more.[/yellow]")
        raise SystemExit(0)

    try:
        synthesis = syn_mod.synthesize(episodes, date_range)
        page_id, page_url = notion_writer.write_synthesis(synthesis)
    except Exception as exc:
        console.print(f"\n[red]Synthesis failed:[/red] {exc}")
        raise SystemExit(1)

    console.rule("[bold green]Synthesis complete[/bold green]")
    console.print(f"\nSynthesis page: {page_url}\n")


@cli.command("eval-history")
def eval_history():
    """Show eval scores across all past report runs."""
    eval_metrics.print_history()


@cli.command("eval")
@click.argument("job_id")
def run_eval(job_id: str):
    """Interactively rate the Notion report for a completed job."""
    import sqlite3
    conn = sqlite3.connect(state_mod.DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if not row:
        console.print(f"[red]Job {job_id} not found[/red]")
        raise SystemExit(1)
    if not row["notion_page_url"]:
        console.print(f"[red]Job {job_id} has no Notion page yet[/red]")
        raise SystemExit(1)
    eval_metrics.collect_eval(
        job_id=job_id,
        notion_url=row["notion_page_url"],
        source_url=row["url"],
    )


def _print_job(job: PipelineJob) -> None:
    color = "green" if job.status == PipelineStatus.COMPLETED else (
        "red" if job.status == PipelineStatus.FAILED else "yellow"
    )
    console.print(f"\nJob ID:      {job.id}")
    console.print(f"URL:         {job.url}")
    console.print(f"Status:      [{color}]{job.status}[/{color}]")
    console.print(f"Created:     {job.created_at}")
    if job.doc_url:
        console.print(f"Google Doc:  {job.doc_url}")
    if job.notion_page_url:
        console.print(f"Notion:      {job.notion_page_url}")
    if job.error:
        console.print(f"[red]Error:       {job.error}[/red]")
    console.print()


if __name__ == "__main__":
    cli()

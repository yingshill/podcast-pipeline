#!/usr/bin/env python3
"""
Podcast Digest Pipeline — CLI entry point.

Commands:
  run <url>         Full pipeline (pauses for human annotation step)
  resume <job_id>   Resume a failed or interrupted job
  status <job_id>   Show job state
  list              Show all jobs
  scrape <url>      Stage 1 only: transcribe and exit
  create-doc <id>   Stage 2 only: create Google Doc for a job
  analyze <id>      Stage 3+4: read annotations and write Notion report
"""
from __future__ import annotations
import json
import logging
import os
import sys
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
import state as state_mod
from models import PipelineJob, PipelineStatus

load_dotenv()
console = Console()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("pipeline.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("pipeline")

# Transcript + anchor data cached to disk alongside the state DB
_CACHE_DIR = Path(__file__).parent / ".cache"
_CACHE_DIR.mkdir(exist_ok=True)


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
    from models import AnnotatedSegment, HeadingAnchor

    job.status = PipelineStatus.ANALYZING
    state_mod.save_job(job)

    anchor_data = _load_cache(job.id, "anchors") or []
    anchors = [HeadingAnchor(**a) for a in anchor_data]

    annotated_segs, live_anchors = ann_mod.get_annotated_segments(job.doc_id)
    # Merge: live anchors may have more detail than cached ones
    all_anchors = live_anchors if live_anchors else anchors

    if not annotated_segs:
        console.print("[red]No annotations found in the document.[/red]")
        console.print("Please highlight or underline text in the Google Doc, then run [bold]resume[/bold].")
        job.status = PipelineStatus.AWAITING_ANNOTATION
        state_mod.save_job(job)
        return

    context_map = ann_mod.build_context_map(annotated_segs, job.doc_id)

    # Derive podcast title from URL (good enough for now)
    podcast_title = _title_from_url(job.url)

    report = analyzer.analyze(
        podcast_title=podcast_title,
        source_url=job.url,
        doc_url=job.doc_url,
        annotated=annotated_segs,
        context_map=context_map,
        all_anchors=all_anchors,
    )

    job.status = PipelineStatus.ANALYZED
    state_mod.save_job(job)
    console.print(f"[green]✓ Analysis complete:[/green] {len(report.resonance_points)} resonance points")

    # Write to Notion
    job.status = PipelineStatus.REPORTING
    state_mod.save_job(job)

    page_id, page_url = notion_writer.write_report(report)
    job.notion_page_id = page_id
    job.notion_page_url = page_url
    job.status = PipelineStatus.COMPLETED
    state_mod.save_job(job)

    console.print(f"[green]✓ Notion report created:[/green] {page_url}")


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
    t = Table(show_header=True, header_style="bold")
    t.add_column("ID", style="cyan")
    t.add_column("Status")
    t.add_column("URL")
    t.add_column("Created")
    t.add_column("Notion")
    for j in jobs:
        color = "green" if j.status == PipelineStatus.COMPLETED else (
            "red" if j.status == PipelineStatus.FAILED else "yellow"
        )
        t.add_row(
            j.id,
            f"[{color}]{j.status}[/{color}]",
            j.url[:50],
            j.created_at[:19],
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

# Podcast Digest Pipeline

A CLI pipeline that turns a podcast URL into a structured Notion knowledge base entry — with phrase extraction, cross-episode synthesis, and human eval tracking.

## What it does

1. **Transcribe** — downloads and transcribes audio via AssemblyAI, or pulls a YouTube transcript
2. **Annotate** — creates a Google Doc so you can highlight (key insights) and underline (phrases to save) the transcript
3. **Analyze** — sends annotated segments to Claude, which produces a structured report: core insight, chapter breakdown, topic tags, host questions with answers, action items
4. **Write to Notion** — creates a page in your podcast database with the full report, plus a Transcript link and source metadata
5. **Phrase Library** — every underlined phrase is extracted, classified (type, function, register), explained in English + Chinese, and written to a separate Notion database
6. **Synthesize** — a separate command runs cross-episode analysis across recent entries, finding recurring themes, converging claims, and open questions

## Pipeline flow

```
URL
 └─ Stage 1: Transcribe (AssemblyAI / YouTube)
 └─ Stage 2: Google Doc created → you annotate (highlight + underline)
 └─ Stage 3: Analyze annotated doc with Claude
     ├─ Notion report page created
     └─ Underlined phrases → Speaker Phrase Library DB
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Google OAuth credentials

Create a project in [Google Cloud Console](https://console.cloud.google.com), enable the **Google Docs API** and **Google Drive API**, and create an **OAuth 2.0 Client ID** (Desktop app).

Download the client secret JSON, then run the one-time auth flow:

```bash
python setup_oauth.py
```

This saves a token file at `credentials/google_oauth_token.json`.

### 3. Notion integration

Create an integration at [notion.so/my-integrations](https://www.notion.so/my-integrations). Share the following databases with your integration:

| Database | `.env` variable |
|---|---|
| Podcast episodes DB | `NOTION_PODCAST_ID` |
| Topic Hub DB | `NOTION_TOPIC_HUB_ID` |
| Synthesis parent page | `NOTION_SYNTHESIS_PAGE_ID` |
| Speaker Phrase Library DB | `NOTION_PHRASE_LIBRARY_ID` |

### 4. Environment variables

Copy `.env.example` to `.env` and fill in your keys:

```bash
# AssemblyAI
ASSEMBLYAI_API_KEY=...

# Google OAuth token (written by setup_oauth.py)
GOOGLE_OAUTH_CREDENTIALS=./credentials/google_oauth_token.json

# Anthropic
ANTHROPIC_API_KEY=...

# Notion
NOTION_TOKEN=...
NOTION_PODCAST_ID=...
NOTION_TOPIC_HUB_ID=...
NOTION_SYNTHESIS_PAGE_ID=...
NOTION_PHRASE_LIBRARY_ID=...

# Optional: Google Drive folder for transcript docs
GOOGLE_DRIVE_FOLDER_ID=...
```

## CLI reference

```bash
# Full pipeline (transcribe → annotate → analyze → Notion)
python pipeline.py run <url>

# Resume a job that stopped mid-way
python pipeline.py resume <job_id>

# Analyze an existing Google Doc (skip transcription)
python pipeline.py analyze-doc <doc_id>

# Cross-episode synthesis (last 1 week by default)
python pipeline.py synthesize
python pipeline.py synthesize --weeks 2

# Show all jobs with eval scores
python pipeline.py list

# Show job state
python pipeline.py status <job_id>

# Clear cached files for a job (forces fresh analysis)
python pipeline.py clean <job_id>

# Rate a completed report (1–5 on four dimensions)
python pipeline.py eval <job_id>

# Show eval score history across all runs
python pipeline.py eval-history
```

## Annotation guide

After `run` creates the Google Doc, open it and annotate before pressing Enter:

- **Highlight** (yellow) — key insights, moments worth capturing in the report
- **Underline** — phrases, expressions, or vocabulary to save to the Phrase Library

The pipeline reads both annotation types and routes them appropriately.

## Notion output

Each episode creates a Notion page with:

- **Core Insight** — one-sentence takeaway
- **Why It Matters** — 2–3 sentence framing
- **Chapter Breakdown** — timestamped sections with insight per chapter
- **Host Questions** — each question, category, and Claude's summary of the guest's answer
- **Action Items** — concrete next steps
- **Tags** — topic tags (Claude can create new options if needed)
- **Transcript** — link back to the Google Doc

The **Speaker Phrase Library** DB gets one entry per underlined phrase:

| Field | Description |
|---|---|
| Phrase | Clean version of the underlined text |
| Type | Verb phrase / Collocation / Fixed phrase / Discourse marker / Single word / Other |
| Function | e.g. Prioritization & tradeoffs, Interview-ready framing |
| Register | Formal / Neutral / Casual |
| Context Quote | Speaker name + sentence where the phrase appeared |
| Meaning (EN) | Plain English explanation |
| 解释（中文） | Concise Chinese explanation |
| Source | Link to the transcript Google Doc |

## Architecture

| Module | Role |
|---|---|
| `pipeline.py` | CLI entry point, job orchestration, state machine |
| `state.py` | SQLite-backed job state (status, IDs, timestamps) |
| `scraper.py` | URL scraping: AssemblyAI transcription + episode metadata |
| `gdocs.py` | Google Docs: create, share, read content |
| `annotations.py` | Parse highlights and underlines from Google Doc |
| `analyzer.py` | Claude analysis → structured report |
| `phrase_extractor.py` | Claude phrase classification → Phrase Library entries |
| `synthesizer.py` | Claude cross-episode synthesis |
| `notion_writer.py` | Write reports, phrases, and synthesis to Notion |
| `cleaner.py` | Transcript cleaning protocols |
| `eval_metrics.py` | Human eval collection and history |
| `models.py` | Dataclasses for all pipeline objects |

## For more context

See [`CASE_STUDY.md`](CASE_STUDY.md) for the full build log, architecture decisions, and feature rationale.

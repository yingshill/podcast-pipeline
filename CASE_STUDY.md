# Podcast Digest Pipeline — Case Study

> **Status:** In Build  
> **Started:** 2026-05-01  
> **Author:** Yingshi Liu  
> **Type:** Personal productivity tool · AI-augmented workflow · Human-in-the-loop system

---

## Table of Contents

1. [Problem Discovery](#1-problem-discovery)
2. [Feature Brainstorm](#2-feature-brainstorm)
3. [Architecture Design](#3-architecture-design)
4. [Tool Selection](#4-tool-selection)
5. [Product Iterations](#5-product-iterations)
6. [Build Log](#6-build-log)
7. [Roadblocks](#7-roadblocks)
8. [Outcomes & Learnings](#8-outcomes--learnings)
9. [Portfolio Presentation](#9-portfolio-presentation)

---

## 1. Problem Discovery

### The Pain

Podcasts are one of the richest information channels — long-form, unscripted, dense with expert reasoning. But the format works against retention:

- A 90-minute episode = ~15,000 words. No one rereads audio.
- The best insight might come at minute 47. It's gone unless you paused and typed.
- AI summarizers flatten everything equally. They don't know what *you* found interesting.
- Existing tools (Snipd, Notion AI, Readwise) either clip at the point of listening or digest the transcript without knowing what the listener resonated with.

### The Core Insight

> AI should amplify what you already noticed — not decide what matters for you.

Human attention during listening is the signal. A highlight or underline in a transcript is a high-confidence marker: "this landed for me." The job of the AI is to synthesize *those moments* into a coherent picture, not to summarize 90 minutes down to 10 bullet points that ignore your reaction.

### Constraint That Shaped the Design

The user tried Notion as the transcript workspace. **It failed.** A 1.5-hour transcript (~15,000 words) exceeded Notion's processing limits. This constraint forced a cleaner split:

- **Google Docs** = annotation workspace (can handle any size, native highlight/underline)
- **Notion** = report destination (receives only the compact AI-generated report)

This turned a failure into a better architecture.

### Who It's For

- Researchers and analysts who use podcasts as primary sources
- Knowledge workers building a second brain from audio content
- Anyone who listens actively but struggles to extract and retain insights

---

## 2. Feature Brainstorm

All ideas considered — accepted, deferred, and rejected. Keeping rejected ideas is as important as keeping accepted ones: it shows the thinking.

### Core Features (v1)

| Feature | Status | Rationale |
|---|---|---|
| Scrape transcript from podcast URL | ✅ In scope | Core requirement |
| Speaker-labeled transcript with timestamps | ✅ In scope | Critical for readability and navigation |
| Store transcript in Google Doc (human-readable) | ✅ In scope | Annotation workspace; handles large files |
| Human annotation via highlight / underline | ✅ In scope | This IS the product insight — human signal |
| AI analysis weighted by human annotations | ✅ In scope | Core value proposition |
| Anchor links in report → transcript sections | ✅ In scope | Cross-reference without re-listening |
| Output report to Notion DB | ✅ In scope | User's existing PKM system |
| Resume pipeline after failure | ✅ In scope | Essential for 90-min jobs that take time |

### Deferred (v2 Candidates)

| Feature | Why Deferred |
|---|---|
| Batch processing (queue of URLs) | v1 proves the single-podcast flow first |
| Recurring podcast subscription (auto-ingest new episodes) | Needs RSS polling; adds infra complexity |
| Cross-episode synthesis ("what did Lex Fridman talk about most in 2024?") | Requires a vector store; out of scope for MVP |
| Obsidian export as alternative to Notion | Different output format; low lift once Notion works |
| Slack/email digest on completion | Nice-to-have notification; add after core flow is solid |
| Automatic speaker name resolution (match "Speaker A" → real names) | Complex; requires show notes scraping or manual mapping |
| Confidence score on AI claims | Requires multi-pass verification; overengineered for v1 |
| Mobile annotation (highlight in iOS) | Google Docs mobile already supports this; no extra work needed |

### Rejected

| Feature | Rejection Reason |
|---|---|
| Notion as transcript store | **Hard failure** — exceeds size limits for 90-min podcasts. This was the original plan. |
| "Just summarize everything equally" | Loses the human signal entirely. Any LLM wrapper can do this. Not differentiated. |
| Local Whisper transcription | No speaker diarization out of the box; slower; needs GPU for quality. AssemblyAI solves this better. |
| RAG / vector search over transcripts | Overkill when human annotation already provides precise signal. Annotation is more accurate than semantic similarity. |
| n8n / Zapier for orchestration | Not flexible enough for stateful multi-step jobs with error recovery. Python gives full control. |
| Send full transcript to Claude | 30K tokens per podcast. Expensive, slow, and wasteful when we have annotation signal. |
| YouTube auto-caption scraping | No speaker labels. Poor accuracy on technical vocabulary. Wrong tool. |
| GPT-4o instead of Claude | Claude has 200K context (better for long audio), superior instruction-following for structured output. |

---

## 3. Architecture Design

### Final Architecture

```
URL Input (YouTube / RSS / direct MP3)
        │
        ▼
┌───────────────────┐
│   AssemblyAI      │  ← speaker diarization + word-level timestamps
│   Transcription   │    supports YouTube URLs directly
└────────┬──────────┘
         │  JSON: [{start_ms, end_ms, speaker, text}]
         ▼
┌───────────────────┐
│  Google Docs API  │  ← creates formatted doc
│  Transcript Doc   │    section heading every 10 min → generates anchor IDs
└────────┬──────────┘    human-readable: [MM:SS] Speaker A: "..."
         │
         │   ← HUMAN STEP: highlights + underlines in Google Doc ←
         │
         ▼
┌───────────────────┐
│  Annotation       │  ← reads Google Docs API for formatted text
│  Reader           │    extracts: highlighted text, underlined text
└────────┬──────────┘    maps each annotation → nearest section anchor ID
         │
         │  Only annotated excerpts + ±2 paragraph context (~5K tokens)
         ▼
┌───────────────────┐
│  Claude claude-sonnet-4-6     │  ← prompt caching on system prompt
│  Analyzer         │    structured JSON output
└────────┬──────────┘
         │  AnalysisReport: themes, resonance points, synthesis, actions
         ▼
┌───────────────────┐
│  Notion Writer    │  ← report only (compact, no size issues)
│                   │    inline links: [00:12:34] → Google Doc anchor
└───────────────────┘
         │
         ▼
  Notion DB Page (report with clickable transcript links)
```

### State Machine

Each stage is a checkpoint. Any failure → state saved → pipeline resumes from last good stage.

```
PENDING
  → SCRAPING      → SCRAPED
  → DOC_CREATING  → DOC_CREATED
  → AWAITING_ANNOTATION          ← waits for human input
  → ANALYZING     → ANALYZED
  → REPORTING     → COMPLETED
                  → FAILED (any stage, resumable)
```

### Key Design Decisions

**Decision 1: Why human annotation as the primary signal?**
Alternative: send full transcript to Claude and ask it to identify key moments.
Problem: Claude doesn't know what resonates *with you specifically*. It would optimize for general interestingness, not personal relevance. The annotation step costs 5 minutes of human time and reduces AI analysis cost by ~85%.

**Decision 2: Why not process the full transcript?**
A 90-min podcast ≈ 30,000 tokens. With Claude claude-sonnet-4-6 at $3/MTok input, that's $0.09 per podcast just in input tokens — at scale (50 podcasts/year) = $4.50/year. Acceptable, but still wasteful when annotations give a better signal in ~5,000 tokens.

**Decision 3: Why Google Docs for annotation?**
- Native highlight (background color) and underline support
- Google Docs API returns formatting data — we can extract highlighted text programmatically
- Handles any document size
- Familiar tool — no learning curve for the human

**Decision 4: Anchor links via heading IDs**
Google Docs generates stable `#heading=h.{id}` URLs for each heading paragraph. By creating a section heading every 10 minutes, we get navigable anchors that survive document edits. Notion inline links use these to jump to exact transcript sections.

---

## 4. Tool Selection

### Evaluation Matrix

#### Transcription

| Tool | Speaker Labels | Timestamps | YouTube Support | Cost | Quality | Decision |
|---|---|---|---|---|---|---|
| **youtube-transcript-api** | Inferred from text | ✅ Segment-level | ✅ Native | Free | High (uses existing captions) | ✅ **Chosen (YouTube)** |
| **BeautifulSoup + requests** | Parsed from text | Estimated (150 wpm) | N/A | Free | Source-dependent | ✅ **Chosen (HTML)** |
| AssemblyAI | ✅ Native | ✅ Word-level | ✅ Direct URL | ~$0.65/hr | High | 🔶 Deferred to v2 (audio-only sources) |
| OpenAI Whisper (local) | ❌ None | ✅ Segment-level | ❌ Manual download | Free (compute) | High | ❌ No speaker labels |
| Deepgram Nova-2 | ✅ Native | ✅ Word-level | ❌ Manual download | ~$0.36/hr | High | 🔶 v2 alternative to AssemblyAI |

**v1 Winner: Free scraping** — YouTube captions and HTML transcripts cover the primary use cases at $0 cost. AssemblyAI reserved for v2 when audio-only support (Spotify, private RSS) is needed.

**Key insight behind the change:** The original framing asked "how does the transcript reach Claude?" The correct question is "how does the transcript reach Python?" Claude never sees raw transcripts — only annotated excerpts. That reframe makes free scraping the obvious answer.

#### Annotation Workspace

| Tool | Large Files | Highlight API | Underline API | Familiar | Decision |
|---|---|---|---|---|---|
| **Google Docs** | ✅ Any size | ✅ backgroundColor | ✅ underline flag | ✅ | ✅ **Chosen** |
| Notion | ❌ Fails >~10K words | Limited | ❌ | ✅ | ❌ Size limit |
| Obsidian | ✅ | ❌ No API | ❌ | Varies | ❌ No programmatic read |
| Apple Notes | ✅ | ❌ | ❌ | ✅ | ❌ No API |
| Roam Research | ✅ | ❌ Poor API | ❌ | Low | ❌ |

**Winner: Google Docs** — only tool with both size handling and a reliable API for reading annotation formatting.

#### AI Analysis

| Model | Context Window | Structured Output | Prompt Caching | Cost (Input) | Decision |
|---|---|---|---|---|---|
| **Claude claude-sonnet-4-6** | 200K | ✅ Native | ✅ | $3/MTok | ✅ **Chosen** |
| Claude Opus 4 | 200K | ✅ | ✅ | $15/MTok | 🔶 Overkill for this task |
| GPT-4o | 128K | ✅ | ❌ | $2.50/MTok | 🔶 No caching |
| Gemini 1.5 Pro | 1M | ✅ | ✅ | $1.25/MTok | 🔶 Viable alternative |

**Winner: Claude claude-sonnet-4-6** — prompt caching matters here since we'll re-run analysis on same notes; 200K context handles very long podcasts; structured JSON output is reliable.

#### Orchestration

| Approach | Error Recovery | State Persistence | Flexibility | Setup | Decision |
|---|---|---|---|---|---|
| **Python + SQLite** | ✅ Full control | ✅ SQLite | ✅ | Low | ✅ **Chosen** |
| n8n | Limited | ✅ Built-in | Medium | Medium | ❌ Can't resume mid-stage |
| Zapier | ❌ No recovery | ❌ | Low | Low | ❌ |
| Airflow | ✅ Full | ✅ | ✅ | High | ❌ Overkill |
| Prefect | ✅ Full | ✅ | ✅ | Medium | 🔶 Viable for v2 |

**Winner: Python + SQLite** — full control, simple to debug, state persistence without extra services. Upgrade to Prefect if this scales to team use.

### Final Stack

```
AssemblyAI        transcription API
google-api-python-client  Google Docs API
anthropic         Claude claude-sonnet-4-6 analysis
notion-client     Notion output
tenacity          retry logic
click             CLI
rich              terminal output
sqlite3           job state (stdlib, no extra install)
python-dotenv     env management
```

---

## 5. Product Iterations

### v0 — What the User Tried (and Hit Walls)

Original mental model:
- Input URL → scrape transcript → paste into Notion → Notion AI summarizes

**What broke:**
- Notion failed on 1.5hr transcripts (hard limit on content size)
- "Summarize everything" loses the human signal
- No cross-reference links back to source moments

### v1 — This Build

Key changes from v0:
1. **Split responsibilities**: Google Docs = annotation workspace, Notion = report destination
2. **Added annotation layer**: Human highlights/underlines become the AI's input signal
3. **Token-efficient analysis**: Only annotated excerpts sent to Claude (not full transcript)
4. **Anchor links**: Report references link back to exact transcript positions

### v2 — Potential Next Iteration (not building yet)

Ideas worth exploring after v1 proves the workflow:
- **Named speakers**: Auto-resolve "Speaker A/B" to real names via show notes scraping
- **Multi-episode synthesis**: "Themes across 10 episodes of Huberman Lab"
- **RSS subscription mode**: Auto-ingest new episodes, notify when doc is ready to annotate
- **Annotation via mobile**: Highlight in Google Docs iOS → annotation detected automatically
- **Export to Obsidian**: Alternative to Notion for Markdown-based PKM users

---

## 6. Build Log

> Updated during implementation. Each entry: date · stage · what happened · key decisions made on the fly.

### 2026-05-01 — Project kickoff + full scaffold built

**Planning decisions:**
- Defined problem, constraints, architecture from first principles
- Evaluated tool alternatives (see Tool Selection matrix)
- Chose AssemblyAI over local Whisper: speaker diarization + YouTube URL support are blockers for local tools
- Rejected RAG/vector approach — human annotation signal is higher-precision than semantic similarity
- Rejected Notion as transcript workspace — user had already hit size limits in real usage

**Implementation decisions made during build:**

*Google Docs heading anchors (R-01 confirmed):*
After reading the Google Docs API spec, confirmed that heading paragraphs expose `paragraphStyle.headingId` in the API response. URL format is `#heading=h.{headingId}`. Resolved by: write headings → `documents.get()` → extract `headingId` from each heading paragraph.

*Single large insertText over line-by-line inserts:*
Initial plan was to insert segments one by one. Switched to a single `insertText` for the full transcript body + separate `updateParagraphStyle` requests for headings. Reason: one large insert is more efficient than hundreds of small ones, and style requests are separate from text inserts anyway.

*Service account + "anyone with link" share:*
Service accounts own created docs by default. Added a Drive `permissions.create` call to make docs writable by anyone with the link, so the user can open and annotate without needing to be added explicitly.

*Claude prompt caching on system prompt:*
Used `cache_control: {type: "ephemeral"}` on the system message. This is most valuable when re-running analysis on the same podcast with updated annotations — the long system prompt is cached across calls.

*Notion block creation limit:*
Notion's `pages.create` accepts max 100 `children`. For reports exceeding that, we create the page with the first 100 blocks and append the rest in batches of 100 via `blocks.children.append`.

**Files created:**
- `CASE_STUDY.md` (this file) — living case study document
- `models.py` — TranscriptSegment, HeadingAnchor, AnnotatedSegment, AnalysisReport, PipelineJob
- `state.py` — SQLite state machine, job CRUD
- `scraper.py` — AssemblyAI transcription with fallback chunking
- `gdocs.py` — Google Doc creation, heading style application, anchor extraction
- `annotations.py` — highlight/underline detection, context window builder
- `analyzer.py` — Claude analysis with prompt caching, JSON parsing
- `notion_writer.py` — block builders, 2000-char splitter, batched page creation
- `pipeline.py` — CLI with run/resume/status/list commands, full error recovery
- `requirements.txt`, `.env.example`, `.gitignore`

---

### 2026-05-01 — Iteration: scraper rethought, AssemblyAI deferred

**Decision:** Replaced AssemblyAI with free programmatic scraping for the two most common podcast sources.

**Trigger:** Revisiting the original plan surfaced a key framing error. The scraping question was being asked as "how does the transcript get into Claude's context?" — but Claude never sees the raw transcript at all. Python fetches it, writes it to Google Docs, and Claude only receives annotated excerpts. The correct question is: "what's the cheapest way to get structured text into Python?" For YouTube and HTML sources, the answer is free.

**What changed:**
- `scraper.py` fully rewritten: URL routing → YouTube or HTML path
- AssemblyAI removed (deferred to v2 for audio-only sources like Spotify or private RSS feeds)
- `requirements.txt` updated: dropped `assemblyai`, added `youtube-transcript-api`, `requests`, `beautifulsoup4`, `lxml`

**YouTube scraper decisions:**
- Uses `YouTubeTranscriptApi.list_transcripts()` — prefers manually-created EN captions over auto-generated, but accepts either
- Raw caption chunks arrive every 1-3 seconds; merged into 30-second windows for readability in Google Docs
- Speaker detection: regex scans merged text for `Name:` patterns (many podcast uploads embed these in captions); falls back to "Speaker" gracefully

**HTML scraper decisions:**
- Tries 8 CSS selectors in priority order to find the transcript container (`.transcript` → `article` → `main`)
- Guards against selecting containers that only have other block elements inside (avoids double-counting nested divs)
- Detects transcript format automatically from sampled blocks: embedded timestamps → speaker labels → plain paragraphs
- Timestamps in HTML: exact if embedded in text (`[00:05:23]`); estimated at 150 wpm otherwise (accurate enough for anchor navigation)
- Parser handles three common formats: `[MM:SS] Speaker: text`, `Speaker: text`, and plain paragraphs

**Token impact:** Transcript acquisition now uses $0 in AI tokens for YouTube and HTML sources (was ~$0.09/podcast via AssemblyAI).

**Remaining v1 scope:** YouTube + HTML covers the user's stated primary use cases. AssemblyAI path kept as a clear extension point in the codebase for when audio-only support is needed.

---

## 7. Roadblocks

> Running log. Each entry: date · stage · problem · how resolved (or current status).

### Known Ahead of Time (Anticipated Friction)

| # | Stage | Anticipated Issue | Mitigation |
|---|---|---|---|
| R-01 | Google Docs | Heading anchor IDs are not predictable — must read back from API after doc creation | Read document after write, extract `paragraphStyle.headingId` |
| R-02 | Google Docs | Large batchUpdate requests may hit API limits | Chunk into ≤200 operations per request |
| R-03 | Notion | 2,000 character limit per text block | Auto-split at sentence boundaries before writing |
| R-04 | AssemblyAI | Speaker diarization quality varies by audio quality | Fallback: merge all speakers as "Speaker A" if labels are unreliable |
| R-05 | Annotations | User may use highlight colors other than yellow | Detect any non-white/non-default background color as a highlight |
| R-06 | Pipeline | AssemblyAI job can take 5–15 min for long audio | Poll with backoff; state persists so terminal can close and reopen |

---

### R-07 — 2026-05-02 · Scraping · Paywalled transcripts return show notes instead

**Discovered during:** First integration test with `https://www.lennysnewsletter.com/p/the-design-process-is-dead`

**What happened:** The HTML scraper returned 79 segments that all came from the episode's show notes and chapter bullets, not the actual transcript. The real transcript on Lenny's Newsletter requires a paid subscription — the public page only exposes the episode description and timestamped chapter markers.

**How it surfaced:** The scraper found a valid container, extracted clean blocks, passed all structural tests (non-empty, has speaker, has timestamps, sequential, no noise). Everything looked correct at the test layer but the content was wrong. The scraper parsed "Lenny'S Podcast:" as a speaker label from the page title appearing in the content body.

**Why tests still passed:** Structural tests (schema validation) can't detect semantic quality. The content was correctly-shaped but wrong.

**Resolution / mitigation:**
1. Added `_MIN_TRANSCRIPT_BLOCKS = 30` threshold — scraper now logs a warning when fewer than 30 blocks are found, flagging likely show-notes-only content
2. Added `_is_noise_block()` filter — removes media-player labels ("Current Time:", "0:00 / -1:17:24", etc.) that bled into extracted content
3. Added `test_no_media_player_noise_in_segments` assertion to integration test

**Remaining gap:** Warning-only; the pipeline still proceeds with show-notes content. A stricter guard would require semantic heuristics (e.g. avg segment length, dialogue ratio) — deferred to v2.

**Design implication:** The HTML scraper is fundamentally limited to publicly available transcripts. For paywalled content (Substack paid posts, Spotify exclusive episodes), the pipeline needs either: (a) user provides a cookie/session to scrape as logged-in, or (b) user manually pastes the transcript into the Google Doc as a fallback. This is acceptable scope for v1 — most YouTube content and many podcast sites publish full transcripts publicly.

---

## 8. Outcomes & Learnings

> To be filled after build is complete and tested.

### Metrics to Capture
- [ ] Token count: full transcript vs annotation-only input
- [ ] Time to complete full pipeline (end-to-end on 90-min podcast)
- [ ] Annotation-to-insight ratio: how many human notes → how many report sections
- [ ] Cost per podcast (AssemblyAI + Claude tokens)

### Questions to Answer After Using It
- Does the report actually feel like it reflects what I care about, or does it still feel generic?
- Are the anchor links useful in practice, or do I never click back to the transcript?
- Is the Google Docs → annotate → run pipeline friction acceptable, or does it need smoother triggering?

---

*[ Fill in after testing ]*

---

## 9. Portfolio Presentation

> Draft to be refined after build is complete.

### Headline

**Podcast Digest Pipeline** — A human-in-the-loop AI system that turns podcast listening into structured knowledge.

### The One-Sentence Pitch

> I built a pipeline that uses your own annotations — not AI guesswork — as the signal for generating a podcast digest, so the output reflects what you actually found valuable.

### Problem → Solution Arc

| | |
|---|---|
| **Problem** | 90-minute podcasts are rich but perishable. Listeners lose insights. Generic AI summaries ignore personal resonance. |
| **Constraint** | Notion (the obvious output target) can't process transcripts at this size. |
| **Insight** | Human highlights are better signal than AI-guessed "important moments." |
| **Solution** | Split the workflow: Google Docs for large-file annotation workspace, Claude for annotation-weighted synthesis, Notion for compact structured output with deep-links back to source. |

### Technical Highlights (for engineering audience)

- **85% token reduction** by sending only annotated excerpts + paragraph context instead of full transcripts
- **State machine with resume** — SQLite checkpoints let the pipeline survive failures and restart from last completed stage
- **Anchor links** — Google Docs heading IDs (`#heading=h.{id}`) embedded in Notion blocks for exact transcript cross-reference
- **AssemblyAI speaker diarization** — 90-min audio → speaker-labeled transcript with ms-level timestamps, no local GPU needed

### Product Thinking Highlights (for PM/design audience)

- Identified and respected the **human-in-the-loop** moment (annotation) as the product's core value rather than automating it away
- Treated a **tool limitation (Notion's size cap) as a design constraint** that led to a better architecture
- Designed for **resumability** — long pipelines fail; the product accounts for this upfront
- Considered and explicitly **rejected** RAG/vector search as overengineered when annotation signal is more precise

### Stack

`Python` · `AssemblyAI` · `Google Docs API` · `Claude claude-sonnet-4-6 (Anthropic)` · `Notion API` · `SQLite`

### Visuals to Add Before Presenting
- [ ] Architecture flow diagram
- [ ] Screenshot: formatted Google Doc with speaker labels and timestamps
- [ ] Screenshot: annotated Google Doc (highlights + underlines)
- [ ] Screenshot: Notion report page with clickable anchor links
- [ ] Code snippet: annotation extraction from Google Docs API
- [ ] Cost comparison table: full transcript vs annotation-only token usage

---

*End of Case Study — document updated throughout the build.*

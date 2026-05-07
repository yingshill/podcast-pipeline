"""
Google Docs integration.

Stage 1 — create_transcript_doc():
  Writes a formatted, human-readable transcript doc.
  Creates a section heading every HEADING_INTERVAL_MS (default 10 min).
  After writing, reads back heading paragraph IDs for anchor URLs.

Stage 2 (annotations.py) reads highlights/underlines from the same doc.
"""
from __future__ import annotations
import logging
import os
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import TranscriptSegment, HeadingAnchor

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
HEADING_INTERVAL_MS = 10 * 60 * 1000  # heading every 10 minutes


def _build_services():
    oauth_path = os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
    if oauth_path and os.path.exists(oauth_path):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(oauth_path, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(oauth_path, "w") as f:
                f.write(creds.to_json())
    else:
        creds_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./credentials/google_service_account.json")
        creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)
    docs = build("docs", "v1", credentials=creds)
    drive = build("drive", "v3", credentials=creds)
    return docs, drive


def _ms_to_label(ms: int) -> str:
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _ms_to_inline_ts(ms: int) -> str:
    return f"[{_ms_to_label(ms)}]"


@retry(
    retry=retry_if_exception_type(HttpError),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _batch_update(docs_service, doc_id: str, requests: list) -> None:
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": requests},
    ).execute()


def create_transcript_doc(
    title: str,
    segments: list[TranscriptSegment],
    folder_id: Optional[str] = None,
) -> tuple[str, str, list[HeadingAnchor]]:
    """
    Creates the Google Doc and returns (doc_id, doc_url, anchors).
    anchors maps 10-min intervals to their heading paragraph IDs.
    """
    docs_svc, drive_svc = _build_services()

    # Create empty document
    doc = docs_svc.documents().create(body={"title": title}).execute()
    doc_id: str = doc["documentId"]
    log.info("Created doc %s", doc_id)

    # Move to target folder if specified
    folder = folder_id or os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if folder:
        drive_svc.files().update(
            fileId=doc_id,
            addParents=folder,
            removeParents="root",
            fields="id, parents",
        ).execute()

    # Share so the owner can open it (service accounts own docs by default)
    # Make it readable by anyone with the link so the user can open it
    drive_svc.permissions().create(
        fileId=doc_id,
        body={"role": "writer", "type": "anyone"},
    ).execute()

    # Build the full text content as a sequence of insertText + style requests
    requests = _build_insert_requests(segments)

    # Google Docs API batchUpdate has a practical limit; chunk at 200 ops
    for i in range(0, len(requests), 200):
        _batch_update(docs_svc, doc_id, requests[i : i + 200])

    log.info("Inserted %d request blocks into doc", len(requests))

    # Read back the doc to extract heading paragraph IDs
    doc_data = docs_svc.documents().get(documentId=doc_id).execute()
    anchors = _extract_heading_anchors(doc_data, doc_id)

    doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
    return doc_id, doc_url, anchors


def _build_insert_requests(segments: list[TranscriptSegment]) -> list[dict]:
    """
    Builds batchUpdate insertText + setParagraphStyle requests.
    Google Docs inserts at index 1 (after the implicit first \n).
    We build the text bottom-up (last segment first) because each insert
    pushes content forward. Instead, we use a single large insertText
    at index 1 and then apply styles by walking the actual character positions.

    Simpler approach: insert all text as one block, then apply heading styles
    by scanning the inserted text for heading markers.
    """
    # Build plain text with sentinel markers for headings
    lines: list[str] = []
    current_interval = -1

    for seg in segments:
        interval = (seg.start_ms // HEADING_INTERVAL_MS) * HEADING_INTERVAL_MS
        if interval != current_interval:
            current_interval = interval
            lines.append(f"\n── {_ms_to_label(interval)} ──\n")

        ts = _ms_to_inline_ts(seg.start_ms)
        lines.append(f"{ts}  {seg.speaker}\n{seg.text}\n\n")

    full_text = "".join(lines).lstrip("\n")

    # Single insert at index 1
    requests: list[dict] = [
        {
            "insertText": {
                "location": {"index": 1},
                "text": full_text,
            }
        }
    ]

    # Now calculate byte positions of heading lines and apply HEADING_2 style
    pos = 1  # index 1 is start
    for line in full_text.split("\n"):
        line_with_nl = line + "\n"
        if line.startswith("──") and line.endswith("──"):
            requests.append({
                "updateParagraphStyle": {
                    "range": {
                        "startIndex": pos,
                        "endIndex": pos + len(line_with_nl),
                    },
                    "paragraphStyle": {"namedStyleType": "HEADING_2"},
                    "fields": "namedStyleType",
                }
            })
        pos += len(line_with_nl)

    return requests


def _extract_heading_anchors(doc_data: dict, doc_id: str) -> list[HeadingAnchor]:
    """
    Reads heading paragraphs from the returned document and extracts
    their headingId. Falls back to scanning for standalone timestamp lines
    if no styled headings are found (e.g. manually-created docs).
    """
    import re
    anchors: list[HeadingAnchor] = []
    body = doc_data.get("body", {}).get("content", [])

    for element in body:
        para = element.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {})
        heading_id = style.get("headingId")
        if not heading_id:
            continue
        text = "".join(
            e.get("textRun", {}).get("content", "")
            for e in para.get("elements", [])
        ).strip("── \n")
        time_ms = _label_to_ms(text)
        if time_ms is not None:
            anchors.append(HeadingAnchor(
                time_ms=time_ms,
                label=text,
                heading_id=heading_id,
                doc_id=doc_id,
            ))

    # Fallback: scan for standalone timestamp paragraphs (e.g. "[10:00]", "10:00")
    if not anchors:
        for element in body:
            para = element.get("paragraph")
            if not para:
                continue
            text = "".join(
                e.get("textRun", {}).get("content", "")
                for e in para.get("elements", [])
            ).strip()
            clean = re.sub(r"^[\[──\s]+|[\]──\s]+$", "", text).strip()
            if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", clean):
                time_ms = _label_to_ms(clean)
                if time_ms is not None:
                    anchors.append(HeadingAnchor(
                        time_ms=time_ms,
                        label=clean,
                        heading_id="",
                        doc_id=doc_id,
                    ))

    log.info("Extracted %d heading anchors", len(anchors))
    return anchors


def _label_to_ms(label: str) -> Optional[int]:
    """Convert 'MM:SS' or 'HH:MM:SS' to milliseconds."""
    parts = label.strip().split(":")
    try:
        if len(parts) == 2:
            m, s = int(parts[0]), int(parts[1])
            return (m * 60 + s) * 1000
        elif len(parts) == 3:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            return (h * 3600 + m * 60 + s) * 1000
    except ValueError:
        pass
    return None


def read_source_url(doc_id: str) -> str:
    """Read the first non-empty line of a Google Doc and return it if it looks like a URL."""
    docs, _ = _build_services()
    doc = docs.documents().get(documentId=doc_id).execute()
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        text = "".join(
            el.get("textRun", {}).get("content", "")
            for el in paragraph.get("elements", [])
        ).strip()
        if text.startswith("http://") or text.startswith("https://"):
            return text
    return ""


def nearest_anchor(time_ms: int, anchors: list[HeadingAnchor]) -> Optional[HeadingAnchor]:
    """Return the most recent heading anchor at or before the given timestamp."""
    best = None
    for a in anchors:
        if a.time_ms <= time_ms:
            if best is None or a.time_ms > best.time_ms:
                best = a
    return best

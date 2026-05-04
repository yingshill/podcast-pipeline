"""
Read human annotations (highlights + underlines) from a Google Doc.

Google Docs represents:
  - Highlight  → textStyle.backgroundColor (any non-default color)
  - Underline  → textStyle.underline == True

We traverse the document body, collect annotated text runs, then
map each annotation to its nearest section heading anchor.
"""
from __future__ import annotations
import logging
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from googleapiclient.errors import HttpError

from models import AnnotatedSegment, HeadingAnchor
from gdocs import _extract_heading_anchors, nearest_anchor, SCOPES

log = logging.getLogger(__name__)

# Default Google background color (white / transparent) — not a user highlight
_DEFAULT_BG = {"red": 1.0, "green": 1.0, "blue": 1.0}


def _is_highlighted(text_style: dict) -> bool:
    bg = text_style.get("backgroundColor", {}).get("color", {}).get("rgbColor")
    if not bg:
        return False
    # If color differs meaningfully from white, it's a user highlight
    return not _is_white(bg)


def _is_white(rgb: dict) -> bool:
    r = rgb.get("red", 0)
    g = rgb.get("green", 0)
    b = rgb.get("blue", 0)
    return r >= 0.95 and g >= 0.95 and b >= 0.95


def _is_underlined(text_style: dict) -> bool:
    return bool(text_style.get("underline"))


def _build_docs_service():
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
    return build("docs", "v1", credentials=creds)


@retry(
    retry=retry_if_exception_type(HttpError),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _fetch_doc(doc_id: str) -> dict:
    svc = _build_docs_service()
    return svc.documents().get(documentId=doc_id).execute()


def get_annotated_segments(doc_id: str) -> tuple[list[AnnotatedSegment], list[HeadingAnchor]]:
    """
    Returns (annotated_segments, anchors).
    Raises ValueError if no annotations are found (warns user).
    """
    doc = _fetch_doc(doc_id)
    anchors = _extract_heading_anchors(doc, doc_id)
    body = doc.get("body", {}).get("content", [])

    annotated: list[AnnotatedSegment] = []
    para_index = 0

    for element in body:
        para = element.get("paragraph")
        if not para:
            continue

        para_index += 1
        for pel in para.get("elements", []):
            run = pel.get("textRun")
            if not run:
                continue
            text = run.get("content", "").strip()
            if not text:
                continue
            style = run.get("textStyle", {})

            annotation_type: str | None = None
            if _is_highlighted(style):
                annotation_type = "highlight"
            elif _is_underlined(style):
                annotation_type = "underline"

            if annotation_type:
                # Find nearest anchor by scanning paragraph text for a timestamp
                anchor = _find_anchor_for_paragraph(body, para_index, anchors)
                annotated.append(AnnotatedSegment(
                    text=text,
                    annotation_type=annotation_type,
                    paragraph_index=para_index,
                    nearest_anchor=anchor,
                ))

    if not annotated:
        log.warning("No highlights or underlines found in doc %s", doc_id)

    log.info("Found %d annotated segments", len(annotated))
    return annotated, anchors


def _find_anchor_for_paragraph(
    body: list,
    target_para_index: int,
    anchors: list[HeadingAnchor],
) -> HeadingAnchor | None:
    """
    Walk backwards from target_para_index to find the most recent heading.
    Map that heading's label to an anchor.
    """
    # Collect heading positions
    heading_positions: list[tuple[int, str]] = []
    for i, element in enumerate(body):
        para = element.get("paragraph")
        if not para:
            continue
        style = para.get("paragraphStyle", {})
        if style.get("headingId"):
            text = "".join(
                e.get("textRun", {}).get("content", "")
                for e in para.get("elements", [])
            ).strip("── \n")
            heading_positions.append((i, text))

    # Find the most recent heading before target_para_index
    best_label = None
    for pos, label in heading_positions:
        if pos <= target_para_index:
            best_label = label

    if not best_label:
        return anchors[0] if anchors else None

    # Match label to anchor
    for anchor in anchors:
        if anchor.label == best_label:
            return anchor

    return anchors[0] if anchors else None


def build_context_map(
    annotated: list[AnnotatedSegment],
    doc_id: str,
) -> dict[int, str]:
    """
    Returns {paragraph_index: surrounding_text} for each annotated paragraph.
    Fetches ±2 paragraphs of context to send alongside annotations to Claude.
    Cached: one doc fetch for all annotations.
    """
    if not annotated:
        return {}

    doc = _fetch_doc(doc_id)
    body = doc.get("body", {}).get("content", [])
    paragraphs = [
        "".join(
            e.get("textRun", {}).get("content", "")
            for e in el.get("paragraph", {}).get("elements", [])
        ).strip()
        for el in body
        if "paragraph" in el
    ]

    context_map: dict[int, str] = {}
    for seg in annotated:
        idx = seg.paragraph_index
        window = paragraphs[max(0, idx - 2) : idx + 3]
        context_map[idx] = "\n".join(p for p in window if p)

    return context_map

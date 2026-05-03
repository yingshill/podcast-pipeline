"""
Run this after setting up credentials to confirm Google Docs/Drive access works.

    python3 verify_credentials.py

Creates a test doc, reads it back, then deletes it.
Prints a clear pass/fail for each step.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console

console = Console()

def check(label: str, fn):
    try:
        result = fn()
        console.print(f"  [green]✓[/green]  {label}")
        return result
    except Exception as e:
        console.print(f"  [red]✗[/red]  {label}")
        console.print(f"       [red]{e}[/red]")
        return None


def _load_credentials():
    oauth_path = os.environ.get("GOOGLE_OAUTH_CREDENTIALS")
    if oauth_path and Path(oauth_path).exists():
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(oauth_path)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(oauth_path).write_text(creds.to_json())
        label = f"OAuth2 token: {oauth_path}"
    else:
        from google.oauth2 import service_account
        SCOPES = [
            "https://www.googleapis.com/auth/documents",
            "https://www.googleapis.com/auth/drive.file",
        ]
        sa_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "./credentials/google_service_account.json")
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        label = f"Service account: {sa_path}"

    return creds, label


def main():
    console.print("\n[bold]Google Credentials Verification[/bold]\n")

    # ── 1. Load credentials ───────────────────────────────────────────────────
    creds_result = check("Load credentials", _load_credentials)
    if not creds_result:
        console.print(
            "\n[yellow]Fix:[/yellow] Run [bold]python3 setup_oauth.py[/bold] to set up OAuth2 credentials,\n"
            "or set GOOGLE_SERVICE_ACCOUNT_JSON in .env to point to a service account key."
        )
        sys.exit(1)

    creds, creds_label = creds_result
    console.print(f"       {creds_label}")

    from googleapiclient.discovery import build

    # ── 2. Build API clients ───────────────────────────────────────────────────
    docs_svc = check("Build Docs API client",  lambda: build("docs",  "v1", credentials=creds))
    drive_svc = check("Build Drive API client", lambda: build("drive", "v3", credentials=creds))
    if not docs_svc or not drive_svc:
        console.print("\n[yellow]Fix:[/yellow] Make sure Google Docs API and Google Drive API "
                      "are enabled in your Google Cloud project.")
        sys.exit(1)

    # ── 3. Create a test document ─────────────────────────────────────────────
    doc = check(
        "Create a test Google Doc",
        lambda: docs_svc.documents().create(body={"title": "Podcast Pipeline — Credential Test"}).execute()
    )
    if not doc:
        console.print(
            "\n[yellow]Fix:[/yellow] If using a service account, it may have no Drive storage quota.\n"
            "Run [bold]python3 setup_oauth.py[/bold] to switch to OAuth2 (recommended)."
        )
        sys.exit(1)

    doc_id = doc["documentId"]

    # ── 4. Move to folder if configured ───────────────────────────────────────
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID")
    if folder_id:
        check(
            f"Move doc to folder {folder_id}",
            lambda: drive_svc.files().update(
                fileId=doc_id,
                addParents=folder_id,
                removeParents="root",
                fields="id, parents",
            ).execute()
        )

    # ── 5. Set sharing to anyone-with-link (needed for human annotation) ──────
    check(
        "Share doc (anyone with link can edit)",
        lambda: drive_svc.permissions().create(
            fileId=doc_id,
            body={"role": "writer", "type": "anyone"},
        ).execute()
    )

    # ── 6. Write a test line ───────────────────────────────────────────────────
    check(
        "Write content to doc",
        lambda: docs_svc.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": "Credential test OK"}}]}
        ).execute()
    )

    # ── 7. Read it back ────────────────────────────────────────────────────────
    check(
        "Read doc content back",
        lambda: docs_svc.documents().get(documentId=doc_id).execute()
    )

    # ── 8. Delete test doc ─────────────────────────────────────────────────────
    check(
        "Delete test doc (cleanup)",
        lambda: drive_svc.files().delete(fileId=doc_id).execute()
    )

    console.print(f"\n[bold green]All checks passed.[/bold green] "
                  f"Your credentials are ready.\n")


if __name__ == "__main__":
    main()

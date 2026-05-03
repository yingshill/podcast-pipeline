"""
One-time OAuth2 setup for Google Docs/Drive access.

    python3 setup_oauth.py

Opens a browser for Google sign-in, then saves the token to
credentials/google_oauth_token.json and updates .env automatically.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from rich.console import Console

console = Console()

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]
TOKEN_PATH = Path("./credentials/google_oauth_token.json")
ENV_FILE = Path(".env")


def find_client_secret() -> str | None:
    explicit = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if explicit and Path(explicit).exists():
        return explicit
    matches = sorted(Path("./credentials").glob("client_secret_*.json"))
    return str(matches[0]) if matches else None


def update_env(key: str, value: str) -> None:
    content = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    new_line = f"{key}={value}"
    if any(line.startswith(f"{key}=") for line in content.splitlines()):
        lines = [new_line if line.startswith(f"{key}=") else line for line in content.splitlines()]
        ENV_FILE.write_text("\n".join(lines) + "\n")
    else:
        with open(ENV_FILE, "a") as f:
            f.write(f"\n{new_line}\n")


def main():
    console.print("\n[bold]Google OAuth2 Setup[/bold]\n")

    client_secret = find_client_secret()
    if not client_secret:
        console.print("[red]✗[/red]  No OAuth2 client secret found in ./credentials/\n")
        console.print("[yellow]Fix:[/yellow] In Google Cloud Console:")
        console.print("  1. Go to APIs & Services → Credentials")
        console.print("  2. Click [bold]+ Create Credentials → OAuth 2.0 Client ID[/bold]")
        console.print("  3. Application type: [bold]Desktop app[/bold]")
        console.print("  4. Download the JSON — filename starts with [bold]client_secret_[/bold]")
        console.print("  5. Place it in [bold]./credentials/[/bold] and re-run this script\n")
        sys.exit(1)

    console.print(f"  [green]✓[/green]  Found client secret: {client_secret}")
    console.print("\nOpening browser for Google sign-in...\n")

    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(client_secret, SCOPES)
    creds = flow.run_local_server(port=0)

    TOKEN_PATH.parent.mkdir(exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())
    console.print(f"\n  [green]✓[/green]  Token saved to {TOKEN_PATH}")

    update_env("GOOGLE_OAUTH_CREDENTIALS", str(TOKEN_PATH))
    console.print(f"  [green]✓[/green]  Updated .env with GOOGLE_OAUTH_CREDENTIALS")

    console.print("\n[bold green]OAuth2 setup complete![/bold green] "
                  "Run [bold]python3 verify_credentials.py[/bold] to confirm.\n")


if __name__ == "__main__":
    main()

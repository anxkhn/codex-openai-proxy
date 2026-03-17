from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import socket

import typer
import uvicorn
from codex_openai_proxy.auth.service import AuthService
from codex_openai_proxy.auth.store import AuthStore
from codex_openai_proxy.config import Settings, get_settings

app = typer.Typer(help="OpenAI-compatible proxy using Codex subscription OAuth")


def _service(settings: Settings) -> AuthService:
    return AuthService(settings=settings, store=AuthStore(settings.auth_file_path))


def _discover_lan_ipv4() -> list[str]:
    ips: set[str] = set()

    try:
        _, _, resolved = socket.gethostbyname_ex(socket.gethostname())
        for ip in resolved:
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("1.1.1.1", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass

    return sorted(ips)


def _print_access_urls(host: str, port: int) -> None:
    if host in {"", "0.0.0.0", "::"}:
        urls = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
        urls.extend(f"http://{ip}:{port}" for ip in _discover_lan_ipv4())
    else:
        urls = [f"http://{host}:{port}"]

    seen: set[str] = set()
    typer.echo("Reachable URLs:")
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        typer.echo(f"  - {url}")
    typer.echo(f"Docs: http://localhost:{port}/docs")
    typer.echo(f"ReDoc: http://localhost:{port}/redoc")


@app.command()
def setup(
    timeout: int = typer.Option(300, help="OAuth callback timeout in seconds"),
) -> None:
    """Run browser OAuth login and store refreshable Codex auth locally."""
    settings = get_settings()
    service = _service(settings)
    record = service.login_via_browser(timeout_seconds=float(timeout))
    payload = {
        "status": "ok",
        "setup_mode": "browser_oauth",
        "auth_file": str(settings.auth_file_path),
        "email": record.identity.email if record.identity else None,
        "account_id": record.identity.account_id if record.identity else None,
        "plan_type": record.identity.plan_type if record.identity else None,
        "expires_at": datetime.fromtimestamp(record.expires_at, tz=timezone.utc).isoformat(),
        "billing_mode": "codex_oauth_subscription",
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command("setup-non-interactive")
def setup_non_interactive(
    codex_auth_file: Path = typer.Option(
        Path.home() / ".codex" / "auth.json",
        help="Path to existing Codex auth.json to import",
    ),
) -> None:
    """Import existing Codex auth.json without opening a browser."""
    settings = get_settings()
    service = _service(settings)
    record = service.import_from_codex_auth_file(codex_auth_file)
    payload = {
        "status": "ok",
        "setup_mode": "non_interactive_import",
        "imported_from": str(codex_auth_file),
        "auth_file": str(settings.auth_file_path),
        "email": record.identity.email if record.identity else None,
        "account_id": record.identity.account_id if record.identity else None,
        "plan_type": record.identity.plan_type if record.identity else None,
        "expires_at": datetime.fromtimestamp(record.expires_at, tz=timezone.utc).isoformat(),
        "billing_mode": "codex_oauth_subscription",
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command()
def whoami() -> None:
    """Show locally stored account metadata."""
    settings = get_settings()
    service = _service(settings)
    record = service.get_record()
    if record is None:
        typer.echo("No auth record found. Run `codex-openai-proxy setup`.")
        raise typer.Exit(code=1)

    payload = {
        "auth_file": str(settings.auth_file_path),
        "email": record.identity.email if record.identity else None,
        "account_id": record.identity.account_id if record.identity else None,
        "plan_type": record.identity.plan_type if record.identity else None,
        "scope": record.scope,
        "expires_at": datetime.fromtimestamp(record.expires_at, tz=timezone.utc).isoformat(),
        "expires_in_seconds": record.expires_in_seconds,
        "billing_mode": "codex_oauth_subscription",
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command()
def logout() -> None:
    """Remove local auth material."""
    settings = get_settings()
    store = AuthStore(settings.auth_file_path)
    if not Path(settings.auth_file_path).exists():
        typer.echo("No auth file found.")
        return
    store.delete()
    typer.echo(f"Removed {settings.auth_file_path}")


@app.command()
def serve(
    host: str = typer.Option("", help="Bind host; empty means all interfaces."),
    port: int = typer.Option(8787, help="Bind port"),
) -> None:
    """Start the OpenAI-compatible local proxy server."""
    bind_host = host if host else "0.0.0.0"
    _print_access_urls(host=host, port=port)
    uvicorn.run(
        "codex_openai_proxy.api.app:app",
        host=bind_host,
        port=port,
        reload=False,
        log_level="info",
        factory=False,
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()

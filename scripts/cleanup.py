"""
cleanup.py
----------
Remove todos os dispositivos de teste do ThingsBoard e apaga device_tokens.json.

Uso:
    python scripts/cleanup.py
    python scripts/cleanup.py --yes   # sem confirmação interativa
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "load_test_config.yaml"
TOKENS_PATH = BASE_DIR / "device_tokens.json"

with open(CONFIG_PATH) as f:
    CONFIG = yaml.safe_load(f)

console = Console()

TB_HOST = os.getenv("TB_HOST", "localhost")
TB_HTTP_PORT = os.getenv("TB_HTTP_PORT", "8080")
TB_BASE_URL = f"http://{TB_HOST}:{TB_HTTP_PORT}"
TENANT_EMAIL = os.getenv("TB_TENANT_EMAIL", "tenant@thingsboard.org")
TENANT_PASSWORD = os.getenv("TB_TENANT_PASSWORD", "tenant2026")


async def get_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{TB_BASE_URL}/api/auth/login",
        json={"username": TENANT_EMAIL, "password": TENANT_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["token"]


async def delete_device(client: httpx.AsyncClient, token: str, device_id: str) -> bool:
    headers = {"X-Authorization": f"Bearer {token}"}
    try:
        resp = await client.delete(
            f"{TB_BASE_URL}/api/device/{device_id}",
            headers=headers,
            timeout=15,
        )
        return resp.status_code in (200, 204)
    except Exception:
        return False


async def main(skip_confirm: bool = False) -> None:
    if not TOKENS_PATH.exists():
        console.print("[yellow]device_tokens.json não encontrado. Nada a limpar.[/yellow]")
        return

    with open(TOKENS_PATH) as f:
        devices: dict = json.load(f)

    valid_devices = {name: info for name, info in devices.items() if info.get("device_id")}
    count = len(valid_devices)

    if count == 0:
        console.print("[yellow]Nenhum device com ID válido encontrado.[/yellow]")
        return

    console.print(f"[bold red]Isso irá deletar {count} dispositivos do ThingsBoard.[/bold red]")

    if not skip_confirm:
        confirm = console.input("Confirmar? [s/N]: ").strip().lower()
        if confirm not in ("s", "sim", "y", "yes"):
            console.print("[yellow]Operação cancelada.[/yellow]")
            return

    async with httpx.AsyncClient(follow_redirects=False) as client:
        console.print("[cyan]Autenticando...[/cyan]")
        token = await get_token(client)

        success = 0
        errors = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task_id = progress.add_task("Deletando devices...", total=count)
            for name, info in valid_devices.items():
                ok = await delete_device(client, token, info["device_id"])
                if ok:
                    success += 1
                else:
                    errors += 1
                progress.advance(task_id)
                await asyncio.sleep(0.01)

    # Remover arquivo de tokens
    TOKENS_PATH.unlink()
    console.print(f"\n[green]Cleanup concluído![/green]")
    console.print(f"  Deletados com sucesso: {success}")
    console.print(f"  Erros:                 {errors}")
    console.print(f"  Arquivo removido:      {TOKENS_PATH}")


if __name__ == "__main__":
    skip = "--yes" in sys.argv or "-y" in sys.argv
    asyncio.run(main(skip_confirm=skip))

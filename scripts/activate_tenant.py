"""
activate_tenant.py
------------------
Ativa o usuario tenant no ThingsBoard e define a senha para login direto.
Util quando o usuario foi criado via API sem ativacao.

Uso:
    python3 scripts/activate_tenant.py
"""

import asyncio
import os
import urllib.parse
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich.console import Console

load_dotenv(Path(__file__).parent.parent / ".env")

console = Console()

TB_HOST = os.getenv("TB_HOST", "localhost")
TB_HTTP_PORT = os.getenv("TB_HTTP_PORT", "8090")
TB_BASE_URL = f"http://{TB_HOST}:{TB_HTTP_PORT}"
ADMIN_EMAIL = os.getenv("TB_ADMIN_EMAIL", "sysadmin@thingsboard.org")
ADMIN_PASSWORD = os.getenv("TB_ADMIN_PASSWORD", "sysadmin")
TENANT_EMAIL = os.getenv("TB_TENANT_EMAIL", "tenant@thingsboard.org")
TENANT_PASSWORD = os.getenv("TB_TENANT_PASSWORD", "tenant2026")


async def main() -> None:
    console.rule("[bold cyan]Ativacao do Usuario Tenant[/bold cyan]")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        # Login como sysadmin
        console.print("[cyan]Autenticando como sysadmin...[/cyan]")
        r = await client.post(
            f"{TB_BASE_URL}/api/auth/login",
            json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            timeout=10,
        )
        r.raise_for_status()
        admin_token = r.json()["token"]
        headers = {"X-Authorization": f"Bearer {admin_token}"}
        console.print("[green]OK[/green]")

        # Verifica se o tenant ja consegue logar
        console.print(f"[cyan]Testando login como {TENANT_EMAIL}...[/cyan]")
        r = await client.post(
            f"{TB_BASE_URL}/api/auth/login",
            json={"username": TENANT_EMAIL, "password": TENANT_PASSWORD},
            timeout=10,
        )
        if r.status_code == 200:
            console.print(f"[green]Login OK! Senha ja esta definida.[/green]")
            console.print(f"\n  Email: [bold]{TENANT_EMAIL}[/bold]")
            console.print(f"  Senha: [bold]{TENANT_PASSWORD}[/bold]")
            return

        console.print(f"[yellow]Login falhou (status {r.status_code}). Ativando usuario...[/yellow]")

        # Encontrar o user_id do tenant
        r = await client.get(
            f"{TB_BASE_URL}/api/tenants?pageSize=100&page=0",
            headers=headers, timeout=30,
        )
        r.raise_for_status()

        user_id = None
        for tenant in r.json().get("data", []):
            tid = tenant["id"]["id"]
            r2 = await client.get(
                f"{TB_BASE_URL}/api/tenant/{tid}/users?pageSize=100&page=0",
                headers=headers, timeout=30,
            )
            if r2.status_code == 200:
                for user in r2.json().get("data", []):
                    if user.get("email") == TENANT_EMAIL:
                        user_id = user["id"]["id"]
                        console.print(f"[green]Usuario encontrado: {user_id}[/green]")
                        break
            if user_id:
                break

        if not user_id:
            console.print(f"[red]Usuario {TENANT_EMAIL} nao encontrado![/red]")
            return

        # Obter link de ativacao
        console.print("[cyan]Obtendo link de ativacao...[/cyan]")
        r = await client.get(
            f"{TB_BASE_URL}/api/user/{user_id}/activationLink",
            headers=headers, timeout=30,
        )
        if r.status_code != 200:
            console.print(f"[red]Erro ao obter link (status {r.status_code}): {r.text}[/red]")
            return

        activation_link = r.text.strip().strip('"')
        console.print(f"[dim]Link: {activation_link}[/dim]")

        # Extrair token da URL
        parsed = urllib.parse.urlparse(activation_link)
        params = urllib.parse.parse_qs(parsed.query)
        activate_token = params.get("activateToken", [None])[0]

        if not activate_token:
            # Tenta fragment (algumas versoes usam #activateToken=...)
            if "activateToken=" in activation_link:
                activate_token = activation_link.split("activateToken=")[-1].split("&")[0]

        if not activate_token:
            console.print(f"[red]Token de ativacao nao encontrado no link![/red]")
            return

        console.print(f"[cyan]Ativando com senha '{TENANT_PASSWORD}'...[/cyan]")
        r = await client.post(
            f"{TB_BASE_URL}/api/noauth/activate?sendActivationMail=false",
            json={
                "activateToken": activate_token,
                "password": TENANT_PASSWORD,
            },
            timeout=30,
        )

        if r.status_code == 200:
            console.print("[green]Usuario ativado com sucesso![/green]")
        else:
            console.print(f"[red]Erro na ativacao (status {r.status_code}): {r.text[:300]}[/red]")
            return

        # Confirmar login
        console.print("[cyan]Confirmando login...[/cyan]")
        r = await client.post(
            f"{TB_BASE_URL}/api/auth/login",
            json={"username": TENANT_EMAIL, "password": TENANT_PASSWORD},
            timeout=10,
        )
        if r.status_code == 200:
            console.print("[bold green]Login confirmado![/bold green]")
        else:
            console.print(f"[red]Login ainda falhou (status {r.status_code})[/red]")

    console.rule("[bold green]Concluido[/bold green]")
    console.print(f"\n  URL:   [bold cyan]{TB_BASE_URL}[/bold cyan]")
    console.print(f"  Email: [bold]{TENANT_EMAIL}[/bold]")
    console.print(f"  Senha: [bold]{TENANT_PASSWORD}[/bold]")


if __name__ == "__main__":
    asyncio.run(main())

"""
dashboard_setup.py
------------------
Cria o dashboard no ThingsBoard via REST API com os widgets obrigatórios:
  1. Value Card   — Magnitude atual (último valor, todos os devices)
  2. Value Card   — Status de queda detectada
  3. Time Series  — Magnitude da aceleração (device de referência)
  4. Time Series  — Eixos X, Y e Z (device de referência)
  5. Map          — Localização de TODOS os dispositivos

Uso:
    python3 scripts/dashboard_setup.py
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import httpx
import yaml
from dotenv import load_dotenv
from rich.console import Console

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "load_test_config.yaml"
TOKENS_PATH = BASE_DIR / "device_tokens.json"
DASHBOARD_DIR = BASE_DIR / "dashboard"
DASHBOARD_DIR.mkdir(exist_ok=True)

with open(CONFIG_PATH) as f:
    CONFIG = yaml.safe_load(f)

console = Console()

TB_HOST = os.getenv("TB_HOST", "localhost")
TB_HTTP_PORT = os.getenv("TB_HTTP_PORT", "8090")
TB_BASE_URL = f"http://{TB_HOST}:{TB_HTTP_PORT}"
ADMIN_EMAIL = os.getenv("TB_ADMIN_EMAIL", "sysadmin@thingsboard.org")
ADMIN_PASSWORD = os.getenv("TB_ADMIN_PASSWORD", "sysadmin")
TENANT_EMAIL = os.getenv("TB_TENANT_EMAIL", "tenant@thingsboard.org")

PROFILE = CONFIG["thingsboard"]["device_profile"]
PREFIX = CONFIG["thingsboard"]["device_name_prefix"]


# ---------------------------------------------------------------------------
# Auth via impersonação
# ---------------------------------------------------------------------------

async def get_tenant_token(client: httpx.AsyncClient) -> str:
    resp = await client.post(
        f"{TB_BASE_URL}/api/auth/login",
        json={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    admin_token = resp.json()["token"]
    headers = {"X-Authorization": f"Bearer {admin_token}"}

    r = await client.get(
        f"{TB_BASE_URL}/api/tenants?pageSize=100&page=0",
        headers=headers, timeout=30,
    )
    r.raise_for_status()
    for tenant in r.json().get("data", []):
        tid = tenant["id"]["id"]
        r2 = await client.get(
            f"{TB_BASE_URL}/api/tenant/{tid}/users?pageSize=100&page=0",
            headers=headers, timeout=30,
        )
        if r2.status_code != 200:
            continue
        for user in r2.json().get("data", []):
            if user.get("email") == TENANT_EMAIL:
                uid = user["id"]["id"]
                r3 = await client.get(
                    f"{TB_BASE_URL}/api/user/{uid}/token",
                    headers=headers, timeout=30,
                )
                r3.raise_for_status()
                console.print(f"[green]Token obtido (user: {uid})[/green]")
                return r3.json()["token"]

    raise RuntimeError(f"Usuário '{TENANT_EMAIL}' não encontrado.")


# ---------------------------------------------------------------------------
# Descobrir o map widget type disponível
# ---------------------------------------------------------------------------

async def find_map_widget(client: httpx.AsyncClient, token: str) -> tuple:
    """
    Retorna (bundleAlias, typeAlias) do widget de mapa disponível no TB.
    Tenta múltiplos nomes conhecidos entre versões.
    """
    headers = {"X-Authorization": f"Bearer {token}"}

    candidates = [
        ("maps", "map"),
        ("maps", "openstreet-map"),
        ("maps", "leaflet-map"),
        ("maps", "here-map"),
    ]

    r = await client.get(
        f"{TB_BASE_URL}/api/widgetsBundles?pageSize=100&page=0",
        headers=headers, timeout=30,
    )
    available_bundles = set()
    if r.status_code == 200:
        data = r.json()
        # TB pode retornar lista de dicts ou lista de strings dependendo da versão
        for b in data:
            if isinstance(b, dict):
                available_bundles.add(b.get("alias", ""))
            elif isinstance(b, str):
                available_bundles.add(b)

    for bundle, wtype in candidates:
        # Se não conseguimos listar bundles, tentar mesmo assim
        if not available_bundles or bundle in available_bundles:
            r2 = await client.get(
                f"{TB_BASE_URL}/api/widgetTypes?bundleAlias={bundle}&isSystem=true",
                headers=headers, timeout=30,
            )
            if r2.status_code == 200:
                wt_data = r2.json()
                for wt in wt_data:
                    if isinstance(wt, dict):
                        alias = wt.get("alias", wt.get("descriptor", {}).get("alias", ""))
                        if alias == wtype:
                            console.print(
                                f"[green]Map widget encontrado: {bundle}/{wtype}[/green]"
                            )
                            return bundle, wtype

    console.print("[yellow]Map widget não encontrado — usando fallback de tabela.[/yellow]")
    return None, None


# ---------------------------------------------------------------------------
# Builders de widgets
# ---------------------------------------------------------------------------

def _ts_key(key: str, label: str, color: str) -> dict:
    return {
        "name": key,
        "type": "timeseries",
        "label": label,
        "color": color,
        "settings": {},
        "postFuncBody": "return value;",
        "usePostProcessing": False,
        "hidden": False,
    }


def _latest_key(key: str, label: str, color: str) -> dict:
    return {
        "name": key,
        "type": "timeseries",
        "label": label,
        "color": color,
        "settings": {},
    }


def widget_value_card(
    title: str,
    alias_id: str,
    key: str,
    label: str,
    color: str,
    col: int,
    row: int,
    size_x: int = 3,
    size_y: int = 2,
) -> dict:
    return {
        "isSystemType": True,
        "bundleAlias": "cards",
        "typeAlias": "simple_card",
        "type": "latest",
        "title": title,
        "showTitle": True,
        "col": col,
        "row": row,
        "sizeX": size_x,
        "sizeY": size_y,
        "config": {
            "datasources": [
                {
                    "type": "entity",
                    "entityAliasId": alias_id,
                    "dataKeys": [_latest_key(key, label, color)],
                }
            ],
            "settings": {
                "labelPosition": "top",
                "decimals": 3,
            },
            "title": title,
            "showTitle": True,
            "backgroundColor": "#1a1a2e",
            "color": color,
        },
    }


def widget_timeseries(
    title: str,
    alias_id: str,
    keys: list,
    col: int,
    row: int,
    size_x: int,
    size_y: int,
    y_min: float = -5,
    y_max: float = 5,
) -> dict:
    return {
        "isSystemType": True,
        "bundleAlias": "charts",
        "typeAlias": "basic_timeseries",
        "type": "timeseries",
        "title": title,
        "showTitle": True,
        "col": col,
        "row": row,
        "sizeX": size_x,
        "sizeY": size_y,
        "config": {
            "datasources": [
                {
                    "type": "entity",
                    "entityAliasId": alias_id,
                    "dataKeys": keys,
                }
            ],
            "settings": {
                "shadowSize": 3,
                "smoothLines": True,
                "showLegend": True,
                "yaxis": {"min": y_min, "max": y_max},
                "xaxis": {"showLabels": True},
                "decimals": 3,
            },
            "title": title,
            "showTitle": True,
        },
    }


def widget_map(bundle: str, wtype: str, alias_id: str) -> dict:
    return {
        "isSystemType": True,
        "bundleAlias": bundle,
        "typeAlias": wtype,
        "type": "latest",
        "title": "Localização dos Dispositivos — UFMA São Luís",
        "showTitle": True,
        "col": 9,
        "row": 4,
        "sizeX": 3,
        "sizeY": 4,
        "config": {
            "datasources": [
                {
                    "type": "entity",
                    "entityAliasId": alias_id,
                    "dataKeys": [
                        _latest_key("latitude", "latitude", "#2980b9"),
                        _latest_key("longitude", "longitude", "#2980b9"),
                    ],
                }
            ],
            "settings": {
                "mapProvider": "OpenStreetMap.Mapnik",
                "defaultZoomLevel": 14,
                "fitMapBounds": True,
                "latKeyName": "latitude",
                "lngKeyName": "longitude",
                "markerImageSize": 10,
                "showTooltip": True,
                "tooltipPattern": (
                    "<b>${entityName}</b><br/>"
                    "Magnitude: ${magnitude} g<br/>"
                    "Status: ${status}"
                ),
                "label": "",
            },
            "title": "Localização dos Dispositivos",
            "showTitle": True,
        },
    }


def widget_map_table(alias_id: str) -> dict:
    """Fallback quando o widget de mapa não está disponível.
    Inclui ação de clique para selecionar o device nos gráficos de série temporal.
    """
    return {
        "isSystemType": True,
        "bundleAlias": "cards",
        "typeAlias": "entities_table",
        "type": "latest",
        "title": "Dispositivos — clique para inspecionar",
        "showTitle": True,
        "col": 9,
        "row": 4,
        "sizeX": 3,
        "sizeY": 4,
        "config": {
            "datasources": [
                {
                    "type": "entity",
                    "entityAliasId": alias_id,
                    "dataKeys": [
                        _latest_key("latitude", "Lat", "#2980b9"),
                        _latest_key("longitude", "Lon", "#2980b9"),
                        _latest_key("magnitude", "Magnitude", "#ff6b35"),
                    ],
                }
            ],
            "settings": {},
            "title": "Dispositivos — clique para inspecionar",
            "showTitle": True,
            "actions": {
                "rowClick": [
                    {
                        "id": "action_select_device",
                        "name": "Selecionar Device",
                        "icon": "more_horiz",
                        "type": "updateDashboardState",
                        "targetDashboardStateId": None,
                        "openRightLayout": False,
                        "setEntityId": True,
                        "stateEntityParamName": "entityId",
                    }
                ]
            },
        },
    }


# ---------------------------------------------------------------------------
# Montagem do dashboard
# ---------------------------------------------------------------------------

def build_dashboard_json(
    map_bundle: Optional[str],
    map_type: Optional[str],
) -> dict:
    """
    Constrói o JSON do dashboard com dois aliases:
      - all_sensors   → todos os devices (value cards + tabela/mapa)
      - single_device → stateEntity: resolve para o device clicado na tabela
                        (não tem UUID fixo — atualiza ao clicar em qualquer linha)
    """

    # --- Entity aliases ---
    entity_aliases = {
        "all_sensors": {
            "id": "all_sensors",
            "alias": "all_sensors",
            "filter": {
                "type": "entityType",
                "resolveMultiple": True,
                "singleEntity": None,
                "entityList": [],
                "entityType": "DEVICE",
                "relationType": None,
                "deviceTypes": [PROFILE],
                "entityGroupsList": [],
                "groupStateEntity": False,
            },
        },
        # stateEntity: lê o entityId do estado do dashboard (setado pelo rowClick)
        "single_device": {
            "id": "single_device",
            "alias": "single_device",
            "filter": {
                "type": "stateEntity",
                "resolveMultiple": False,
                "singleEntity": None,
                "entityList": [],
                "entityType": None,
                "relationType": None,
                "deviceTypes": [],
                "entityGroupsList": [],
                "groupStateEntity": False,
                "stateEntityParamName": "entityId",
            },
        },
    }

    # --- Widgets ---
    widgets = {}

    # 1. Magnitude atual (último valor — todos os devices)
    widgets["w_mag_card"] = widget_value_card(
        title="Magnitude Atual (g)",
        alias_id="all_sensors",
        key="magnitude",
        label="Magnitude (g)",
        color="rgba(255,107,53,1)",
        col=0, row=0, size_x=3, size_y=2,
    )

    # 2. Status de queda (todos os devices)
    widgets["w_fall_card"] = widget_value_card(
        title="Queda Detectada",
        alias_id="all_sensors",
        key="fall_detected",
        label="Queda Detectada",
        color="rgba(231,76,60,1)",
        col=0, row=2, size_x=3, size_y=2,
    )

    # 3. Magnitude do device selecionado (título dinâmico via ${entityName})
    widgets["w_mag_ts"] = widget_timeseries(
        title="Magnitude da Aceleração — ${entityName}",
        alias_id="single_device",
        keys=[_ts_key("magnitude", "Magnitude (g)", "#ff6b35")],
        col=3, row=0, size_x=9, size_y=4,
        y_min=0, y_max=5,
    )

    # 4. Eixos X, Y, Z do device selecionado
    widgets["w_axes_ts"] = widget_timeseries(
        title="Aceleração X, Y, Z — ${entityName}",
        alias_id="single_device",
        keys=[
            _ts_key("accel_x", "Eixo X (g)", "#e74c3c"),
            _ts_key("accel_y", "Eixo Y (g)", "#2ecc71"),
            _ts_key("accel_z", "Eixo Z (g)", "#3498db"),
        ],
        col=0, row=4, size_x=9, size_y=4,
        y_min=-4, y_max=4,
    )

    # 5. Tabela/mapa com ação de clique para selecionar o device
    if map_bundle and map_type:
        widgets["w_map"] = widget_map(map_bundle, map_type, "all_sensors")
    else:
        widgets["w_map"] = widget_map_table("all_sensors")

    # --- Dashboard completo ---
    return {
        "title": "Fall Detection IoT — Load Test Dashboard",
        "configuration": {
            "widgets": widgets,
            "entityAliases": entity_aliases,
            "filters": {},
            "timewindow": {
                "displayValue": "",
                "selectedTab": 0,
                "realtime": {
                    "realtimeType": 1,
                    "interval": 1000,
                    "timewindowMs": 3600000,   # última hora
                    "quickInterval": "CURRENT_HOUR",
                },
                "history": {
                    "historyType": 0,
                    "interval": 1000,
                    "timewindowMs": 86400000,  # último dia
                    "fixedTimewindow": {"startTimeMs": 0, "endTimeMs": 0},
                    "quickInterval": "CURRENT_DAY",
                },
                "aggregation": {
                    "type": "AVG",
                    "limit": 25000,
                },
            },
            "settings": {
                "stateControllerId": "entity",
                "showTitle": True,
                "showDashboardsSelect": True,
                "showEntitiesSelect": True,
                "showDashboardTimewindow": True,
                "showDashboardExport": True,
                "toolbarAlwaysOpen": False,
            },
            "gridSettings": {
                "backgroundColor": "#0d1117",
                "backgroundSizeMode": "100%",
                "columns": 12,
                "margin": 10,
                "outerMargin": True,
                "autoFillHeight": True,
                "mobileAutoFillHeight": False,
                "mobileRowHeight": 70,
            },
        },
        "assignedCustomers": [],
    }


# ---------------------------------------------------------------------------
# CRUD do dashboard
# ---------------------------------------------------------------------------

async def create_or_update_dashboard(
    client: httpx.AsyncClient, token: str, dashboard: dict
) -> str:
    headers = {"X-Authorization": f"Bearer {token}"}
    title = dashboard["title"]

    resp = await client.get(
        f"{TB_BASE_URL}/api/tenant/dashboards?pageSize=50&page=0",
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    for dash in resp.json().get("data", []):
        if dash["title"] == title:
            dash_id = dash["id"]["id"]
            console.print(f"[yellow]Atualizando dashboard existente: {dash_id}[/yellow]")
            dashboard["id"] = {"entityType": "DASHBOARD", "id": dash_id}
            resp = await client.post(
                f"{TB_BASE_URL}/api/dashboard",
                headers=headers, json=dashboard, timeout=30,
            )
            resp.raise_for_status()
            return dash_id

    resp = await client.post(
        f"{TB_BASE_URL}/api/dashboard",
        headers=headers, json=dashboard, timeout=30,
    )
    resp.raise_for_status()
    dash_id = resp.json()["id"]["id"]
    console.print(f"[green]Dashboard criado: {dash_id}[/green]")
    return dash_id


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    console.rule("[bold cyan]Configuração do Dashboard ThingsBoard[/bold cyan]")
    console.print(f"ThingsBoard: [bold]{TB_BASE_URL}[/bold]")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        console.print("[cyan]Autenticando como tenant (via impersonação)...[/cyan]")
        token = await get_tenant_token(client)
        console.print("[green]OK[/green]")

        console.print("[cyan]Detectando widget de mapa disponível...[/cyan]")
        map_bundle, map_type = await find_map_widget(client, token)

        console.print("[cyan]Construindo dashboard JSON...[/cyan]")
        dashboard = build_dashboard_json(map_bundle, map_type)

        dashboard_path = DASHBOARD_DIR / "fall_detection_dashboard.json"
        with open(dashboard_path, "w") as f:
            json.dump(dashboard, f, indent=2, ensure_ascii=False)
        console.print(f"[green]JSON salvo: {dashboard_path}[/green]")

        console.print("[cyan]Criando/atualizando dashboard...[/cyan]")
        dash_id = await create_or_update_dashboard(client, token, dashboard)

    console.rule("[bold green]Dashboard configurado![/bold green]")
    console.print(f"\n  URL do dashboard:")
    console.print(
        f"  [bold cyan]http://{TB_HOST}:{TB_HTTP_PORT}/dashboard/{dash_id}[/bold cyan]"
    )
    console.print(f"\n  Login: [bold]{ADMIN_EMAIL}[/bold] / [bold]{ADMIN_PASSWORD}[/bold]")
    console.print(
        f"\n  [yellow]Dica:[/yellow] nos gráficos de série temporal mude o intervalo "
        f"de tempo para 'Último dia' se os dados aparecerem vazios."
    )


if __name__ == "__main__":
    asyncio.run(main())

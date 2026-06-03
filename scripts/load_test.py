"""
load_test.py
------------
Motor principal do load test: simula dispositivos ESP32 publicando
telemetria via MQTT para o ThingsBoard local.

Cada dispositivo roda em uma coroutine asyncio independente e simula:
  - Dados do MPU6050 (accel_x, accel_y, accel_z, magnitude)
  - Eventos de queda com dois modos configuráveis:
      "session"     → cada device tem probabilidade P de sofrer EXATAMENTE
                      1 queda em toda a sessão (realista para idosos)
      "per_request" → probabilidade P por leitura (comportamento original)
  - Posição GPS com drift lento (para widget de mapa)

Uso:
    python scripts/load_test.py
    python scripts/load_test.py --devices 1000 --requests 100 --interval 100
    python scripts/load_test.py --devices 100000 --fall-prob 0.05

Modo distribuído (múltiplos nós):
    # Nó 0: devices 0-332
    python scripts/load_test.py --devices 333 --offset 0
    # Nó 1: devices 333-665
    python scripts/load_test.py --devices 333 --offset 333
    # Nó 2: devices 666-999
    python scripts/load_test.py --devices 334 --offset 666

    Variáveis de ambiente equivalentes: DEVICE_OFFSET, DEVICE_COUNT, NODE_ID
"""

import argparse
import asyncio
import json
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import aiomqtt
import numpy as np
import yaml
from dotenv import load_dotenv
from prometheus_client import Counter, Gauge, start_http_server
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

load_dotenv(Path(__file__).parent.parent / ".env")

BASE_DIR = Path(__file__).parent.parent
CONFIG_PATH = BASE_DIR / "config" / "load_test_config.yaml"
TOKENS_PATH = BASE_DIR / "device_tokens.json"
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

with open(CONFIG_PATH) as f:
    CONFIG = yaml.safe_load(f)

console = Console()

TB_HOST = os.getenv("TB_HOST", "localhost")
TB_MQTT_PORT = int(os.getenv("TB_MQTT_PORT", "1883"))

# Identificação do nó distribuído — usado nos logs e no nome do relatório.
# Quando rodando via Docker, cada container recebe NODE_ID distinto (ex: "node-1").
NODE_ID = os.getenv("NODE_ID", "local")

# Porta do servidor HTTP do Prometheus (/metrics).
# Cada container usa a mesma porta pois rodam em namespaces de rede isolados.
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))

# QoS MQTT: 0 = fire-and-forget (sem confirmação do broker),
#           1 = at-least-once (broker envia PUBACK confirmando recebimento).
# QoS 1 garante que a mensagem foi recebida pelo broker, ao custo de latência
# maior (round-trip até o PUBACK) e possível duplicação em caso de retransmissão.
MQTT_QOS = int(os.getenv("MQTT_QOS", "1"))

# Sessão limpa (clean_session):
#   True  → cada conexão começa do zero, sem estado residual no broker.
#           Recomendado para load tests (evita acúmulo de sessões persistentes).
#   False → o broker preserva a sessão entre reconexões (subscriptions e
#           mensagens QoS 1/2 pendentes). Útil para dispositivos IoT reais
#           que precisam retomar após queda de rede, mas pode causar problemas
#           em load tests repetidos (acúmulo de estado no broker).
MQTT_CLEAN_SESSION = os.getenv("MQTT_CLEAN_SESSION", "true").lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Métricas Prometheus
# ---------------------------------------------------------------------------

_prom_throughput = Gauge(
    "load_test_throughput_msg_per_second", "Throughput atual (msg/s)", ["node"]
)
_prom_latency_avg = Gauge(
    "load_test_latency_avg_ms", "Latência média (ms)", ["node"]
)
_prom_latency_p99 = Gauge(
    "load_test_latency_p99_ms", "Latência p99 (ms)", ["node"]
)
_prom_active_devices = Gauge(
    "load_test_active_devices", "Devices com coroutine ativa", ["node"]
)
_prom_connected_devices = Gauge(
    "load_test_connected_devices", "Total de devices que já conectaram", ["node"]
)
_prom_messages = Counter(
    "load_test_messages_total", "Total de mensagens publicadas", ["node"]
)
_prom_errors = Counter(
    "load_test_errors_total", "Total de erros de publicação", ["node"]
)
_prom_falls = Counter(
    "load_test_falls_total", "Total de quedas simuladas", ["node"]
)

# Estado anterior para calcular delta nos Counters
_prom_prev = {"published": 0, "errors": 0, "falls": 0}


def _update_prometheus_metrics() -> None:
    """Sincroniza os objetos Prometheus com o estado atual de METRICS."""
    m = METRICS
    n = NODE_ID

    _prom_throughput.labels(n).set(m.throughput)
    _prom_latency_avg.labels(n).set(m.avg_latency_ms)
    _prom_latency_p99.labels(n).set(m.p99_latency_ms)
    _prom_active_devices.labels(n).set(m.active_devices)
    _prom_connected_devices.labels(n).set(m.connected_devices)

    # Counters só aceitam incremento — calculamos o delta desde a última chamada.
    delta_pub = m.total_published - _prom_prev["published"]
    if delta_pub > 0:
        _prom_messages.labels(n).inc(delta_pub)
        _prom_prev["published"] = m.total_published

    delta_err = m.total_errors - _prom_prev["errors"]
    if delta_err > 0:
        _prom_errors.labels(n).inc(delta_err)
        _prom_prev["errors"] = m.total_errors

    delta_falls = m.total_falls - _prom_prev["falls"]
    if delta_falls > 0:
        _prom_falls.labels(n).inc(delta_falls)
        _prom_prev["falls"] = m.total_falls


# ---------------------------------------------------------------------------
# Simulação física do sensor MPU6050
# ---------------------------------------------------------------------------

class FallState(Enum):
    IDLE = auto()
    IMPACT_DETECTED = auto()
    CONFIRMED = auto()


@dataclass
class SensorSimulator:
    """Simula os dados brutos do MPU6050 + estado da máquina de estados de queda."""

    device_id: int
    base_lat: float
    base_lon: float

    # Estado GPS (drift lento)
    lat: float = field(init=False)
    lon: float = field(init=False)

    # Estado do sensor (random walk)
    _vx: float = field(default=0.0, init=False)
    _vy: float = field(default=0.0, init=False)
    _vz: float = field(default=0.0, init=False)

    # Estado da máquina de quedas
    _fall_state: FallState = field(default=FallState.IDLE, init=False)
    _impact_time: float = field(default=0.0, init=False)
    _fall_magnitude: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        radius_km = CONFIG["location"]["drift_radius_km"]
        deg_per_km = 1.0 / 111.0
        self.lat = self.base_lat + random.gauss(0, radius_km * deg_per_km * 0.5)
        self.lon = self.base_lon + random.gauss(0, radius_km * deg_per_km * 0.5)

    def _drift_gps(self) -> None:
        deg_per_km = 1.0 / 111.0
        step = 0.002 * deg_per_km
        self.lat += random.gauss(0, step)
        self.lon += random.gauss(0, step)

    def read(self, force_fall: bool = False) -> dict:
        """Gera uma leitura de sensor realista."""
        noise = CONFIG["sensor"]["noise_std_g"]
        gravity = CONFIG["sensor"]["gravity_g"]

        if self._fall_state == FallState.IDLE:
            if force_fall:
                self._fall_state = FallState.IMPACT_DETECTED
                self._impact_time = time.monotonic()
                ax = random.uniform(-3.0, 3.0)
                ay = random.uniform(-3.0, 3.0)
                az = random.uniform(-2.0, 2.0)
                magnitude = math.sqrt(ax**2 + ay**2 + az**2)
                self._fall_magnitude = magnitude
            else:
                self._vx += random.gauss(0, 0.05)
                self._vy += random.gauss(0, 0.05)
                self._vz += random.gauss(0, 0.02)
                self._vx *= 0.9
                self._vy *= 0.9
                self._vz *= 0.9
                ax = np.clip(self._vx + random.gauss(0, noise), -4, 4)
                ay = np.clip(self._vy + random.gauss(0, noise), -4, 4)
                az = np.clip(gravity + self._vz + random.gauss(0, noise), -4, 4)
                magnitude = math.sqrt(ax**2 + ay**2 + az**2)

        elif self._fall_state == FallState.IMPACT_DETECTED:
            elapsed_ms = (time.monotonic() - self._impact_time) * 1000
            rest_threshold = CONFIG["sensor"]["rest_threshold_g"]
            rest_duration = CONFIG["sensor"]["rest_duration_ms"]

            if elapsed_ms < 300:
                ax = random.uniform(-2.5, 2.5)
                ay = random.uniform(-2.5, 2.5)
                az = random.uniform(-1.5, 1.5)
                magnitude = math.sqrt(ax**2 + ay**2 + az**2)
            else:
                ax = random.gauss(0, 0.05)
                ay = random.gauss(0, 0.05)
                az = random.gauss(0, 0.05)
                magnitude = math.sqrt(ax**2 + ay**2 + az**2)
                if elapsed_ms >= rest_duration and magnitude < rest_threshold:
                    self._fall_state = FallState.CONFIRMED

        elif self._fall_state == FallState.CONFIRMED:
            ax = random.gauss(0, 0.05)
            ay = random.gauss(0, 0.05)
            az = random.gauss(0, 0.05)
            magnitude = math.sqrt(ax**2 + ay**2 + az**2)
            self._fall_state = FallState.IDLE

        fall_detected = (self._fall_state == FallState.CONFIRMED)
        self._drift_gps()

        return {
            "accel_x": round(ax, 4),
            "accel_y": round(ay, 4),
            "accel_z": round(az, 4),
            "magnitude": round(magnitude, 4),
            "fall_detected": fall_detected,
            "impact_magnitude": round(self._fall_magnitude, 4) if fall_detected else 0.0,
            "latitude": round(self.lat, 6),
            "longitude": round(self.lon, 6),
            "status": "fall" if fall_detected else "normal",
            "device_id": f"fall-sensor-{self.device_id:06d}",
        }


# ---------------------------------------------------------------------------
# Métricas globais (thread-safe via asyncio — single-threaded event loop)
# ---------------------------------------------------------------------------

@dataclass
class Metrics:
    total_published: int = 0
    total_errors: int = 0
    total_falls: int = 0
    active_devices: int = 0
    connected_devices: int = 0
    start_time: float = field(default_factory=time.monotonic)
    # Janela deslizante: mantém no máximo 50 000 amostras de latência em memória
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=50_000))

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def throughput(self) -> float:
        return self.total_published / max(self.elapsed_s, 1)

    @property
    def avg_latency_ms(self) -> float:
        if not self.latencies_ms:
            return 0.0
        return sum(self.latencies_ms) / len(self.latencies_ms)

    @property
    def p99_latency_ms(self) -> float:
        if len(self.latencies_ms) < 10:
            return 0.0
        sample = sorted(self.latencies_ms)
        idx = int(len(sample) * 0.99)
        return sample[idx]


METRICS = Metrics()


# ---------------------------------------------------------------------------
# Coroutine por dispositivo
# ---------------------------------------------------------------------------

def _schedule_session_fall(n_requests: int) -> Optional[int]:
    """
    Decide (aleatoriamente) em qual requisição o device vai "cair".
    Respeita um período de aquecimento de 10 % das requisições para que
    o device já esteja conectado e publicando antes do evento de queda.
    Retorna None se o device não deve cair nesta sessão.
    """
    fall_prob = CONFIG["load_test"]["fall_probability"]
    if random.random() >= fall_prob:
        return None
    warmup = max(1, n_requests // 10)
    return random.randint(warmup, n_requests - 1)


async def run_device(
    device_name: str,
    token: str,
    device_index: int,
    n_requests: int,
    interval_s: float,
    semaphore: asyncio.Semaphore,
) -> None:
    fall_mode = CONFIG["load_test"].get("fall_mode", "per_request")
    fall_prob = CONFIG["load_test"]["fall_probability"]
    base_lat = CONFIG["location"]["base_lat"]
    base_lon = CONFIG["location"]["base_lon"]
    sensor = SensorSimulator(device_id=device_index, base_lat=base_lat, base_lon=base_lon)

    # Modo sessão: agenda exatamente 1 queda (ou nenhuma) para este device
    fall_at_request: Optional[int] = None
    if fall_mode == "session":
        fall_at_request = _schedule_session_fall(n_requests)

    # client_id fixo e determinístico: garante que persistent sessions (clean_session=False)
    # retomem o estado correto após reconexão. O broker associa a fila de mensagens
    # pendentes ao client_id — se fosse aleatório, cada reconexão criaria uma sessão nova.
    client_id = f"sim-{device_index:06d}"

    # Número máximo de tentativas de reconexão antes de desistir do device.
    max_reconnect_attempts = 3

    async with semaphore:
        for attempt in range(max_reconnect_attempts):
            try:
                async with aiomqtt.Client(
                    hostname=TB_HOST,
                    port=TB_MQTT_PORT,
                    username=token,
                    password="",
                    identifier=client_id,
                    keepalive=60,
                    clean_session=MQTT_CLEAN_SESSION,
                ) as client:
                    if attempt == 0:
                        METRICS.connected_devices += 1
                    METRICS.active_devices += 1

                    for req_num in range(n_requests):
                        if fall_mode == "session":
                            force_fall = (req_num == fall_at_request)
                        else:
                            force_fall = random.random() < fall_prob

                        payload = sensor.read(force_fall=force_fall)

                        if payload["fall_detected"]:
                            METRICS.total_falls += 1

                        # A medição de latência captura o round-trip completo:
                        #   QoS 0: tempo até o pacote ser entregue ao socket (rápido)
                        #   QoS 1: tempo até receber o PUBACK do broker (inclui RTT)
                        # Isso torna a comparação QoS 0 vs 1 diretamente visível nas métricas.
                        t0 = time.monotonic()
                        try:
                            await client.publish(
                                "v1/devices/me/telemetry",
                                json.dumps(payload),
                                qos=MQTT_QOS,
                            )
                            METRICS.latencies_ms.append((time.monotonic() - t0) * 1000)
                            METRICS.total_published += 1
                        except Exception:
                            METRICS.total_errors += 1

                        await asyncio.sleep(interval_s)

                    METRICS.active_devices -= 1
                    # Publicação completa — sai do loop de reconexão.
                    break

            except Exception:
                METRICS.total_errors += 1
                if METRICS.active_devices > 0:
                    METRICS.active_devices -= 1
                # Backoff exponencial: 2s, 4s, 8s entre tentativas de reconexão.
                # Usa o mesmo client_id para retomar a persistent session no broker.
                if attempt < max_reconnect_attempts - 1:
                    backoff = 2 ** (attempt + 1)
                    await asyncio.sleep(backoff)


# ---------------------------------------------------------------------------
# Painel de métricas em tempo real
# ---------------------------------------------------------------------------

def build_dashboard() -> Panel:
    m = METRICS
    elapsed = m.elapsed_s

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="bold white")
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column(style="bold white")

    table.add_row(
        "Tempo decorrido:", f"{elapsed:.1f}s",
        "Throughput:", f"{m.throughput:.1f} msg/s",
    )
    table.add_row(
        "Devices conectados:", f"{m.connected_devices:,}",
        "Devices ativos:", f"{m.active_devices:,}",
    )
    table.add_row(
        "Mensagens enviadas:", f"{m.total_published:,}",
        "Erros:", f"[red]{m.total_errors:,}[/red]",
    )
    table.add_row(
        "Quedas simuladas:", f"[yellow]{m.total_falls:,}[/yellow]",
        "Latência média:", f"{m.avg_latency_ms:.1f} ms",
    )
    table.add_row(
        "Latência p99:", f"{m.p99_latency_ms:.1f} ms",
        "Amostras latência:", f"{len(m.latencies_ms):,}",
    )

    color = "green" if m.total_errors == 0 else "yellow"
    return Panel(
        table,
        title="[bold]Fall Detection IoT — Load Test[/bold]",
        border_style=color,
    )


async def metrics_loop(refresh_interval: float = 1.0) -> None:
    with Live(build_dashboard(), refresh_per_second=1, console=console) as live:
        while METRICS.active_devices > 0 or METRICS.connected_devices == 0:
            await asyncio.sleep(refresh_interval)
            _update_prometheus_metrics()
            live.update(build_dashboard())


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main(n_devices: int, n_requests: int, interval_ms: int, offset: int = 0) -> None:
    interval_s = interval_ms / 1000.0
    max_concurrent = CONFIG["load_test"]["max_concurrent_connects"]
    fall_mode = CONFIG["load_test"].get("fall_mode", "per_request")
    fall_prob = CONFIG["load_test"]["fall_probability"]

    if not TOKENS_PATH.exists():
        console.print(
            "[bold red]Arquivo device_tokens.json não encontrado.[/bold red]\n"
            "Execute primeiro: python scripts/provision_devices.py"
        )
        return

    with open(TOKENS_PATH) as f:
        all_tokens: dict = json.load(f)

    # Fatia de tokens que este nó é responsável.
    # i é a posição global no dict, então i+1 é único em todo o cluster —
    # garante que client identifiers MQTT não colidam entre nós.
    all_valid = [
        (name, info["token"], i + 1)
        for i, (name, info) in enumerate(all_tokens.items())
        if info.get("token")
    ]
    valid = all_valid[offset : offset + n_devices]

    if len(valid) < n_devices:
        console.print(
            f"[yellow]Atenção: apenas {len(valid):,} tokens disponíveis "
            f"no intervalo [{offset}, {offset + n_devices}) "
            f"(total no arquivo: {len(all_valid):,})[/yellow]"
        )

    total_msgs = len(valid) * n_requests
    test_duration_s = n_requests * interval_s
    expected_falls = int(len(valid) * fall_prob) if fall_mode == "session" else "variável"

    # Inicia o servidor HTTP do Prometheus em daemon thread (não bloqueia o event loop).
    # O Prometheus raspa http://<container>:METRICS_PORT/metrics a cada 5s.
    start_http_server(METRICS_PORT)

    console.rule(f"[bold cyan]Iniciando Load Test — nó [{NODE_ID}][/bold cyan]")
    console.print(f"  Nó (NODE_ID):     {NODE_ID}")
    console.print(f"  Fatia devices:    [{offset} - {offset + len(valid) - 1}]  ({len(valid):,} devices)")
    console.print(f"  Requests/device:  {n_requests:,}")
    console.print(f"  Total msgs:       {total_msgs:,}")
    console.print(f"  Intervalo:        {interval_ms} ms")
    console.print(f"  Duração/device:   {test_duration_s:.1f}s")
    console.print(f"  Modo queda:       {fall_mode}  (prob={fall_prob})")
    console.print(f"  Quedas esperadas: ~{expected_falls}")
    console.print(f"  Concorrência max: {max_concurrent:,}")
    console.print(f"  MQTT QoS:         {MQTT_QOS}  ({'at-least-once + PUBACK' if MQTT_QOS == 1 else 'fire-and-forget'})")
    console.print(f"  Clean session:    {MQTT_CLEAN_SESSION}  ({'sessão limpa' if MQTT_CLEAN_SESSION else 'persistent session'})")
    console.print(f"  ThingsBoard:      {TB_HOST}:{TB_MQTT_PORT}")
    console.print()

    # O semáforo é o único mecanismo de throttling — sem stagger no loop de criação.
    # Isso elimina o atraso de (n_devices × connect_delay_ms) que existia antes.
    semaphore = asyncio.Semaphore(max_concurrent)

    tasks = [
        asyncio.create_task(
            run_device(name, token, idx, n_requests, interval_s, semaphore)
        )
        for name, token, idx in valid
    ]

    metrics_task = asyncio.create_task(metrics_loop())

    await asyncio.gather(*tasks)
    await asyncio.sleep(1)
    metrics_task.cancel()

    m = METRICS
    report = {
        "node_id": NODE_ID,
        "device_offset": offset,
        "total_devices": len(valid),
        "requests_per_device": n_requests,
        "interval_ms": interval_ms,
        "mqtt_qos": MQTT_QOS,
        "fall_mode": fall_mode,
        "fall_probability": fall_prob,
        "total_published": m.total_published,
        "total_errors": m.total_errors,
        "total_falls_simulated": m.total_falls,
        "falls_per_device_avg": round(m.total_falls / max(len(valid), 1), 3),
        "elapsed_seconds": round(m.elapsed_s, 2),
        "throughput_msg_per_s": round(m.throughput, 2),
        "avg_latency_ms": round(m.avg_latency_ms, 2),
        "p99_latency_ms": round(m.p99_latency_ms, 2),
        "error_rate_pct": round(
            m.total_errors / max(m.total_published + m.total_errors, 1) * 100, 2
        ),
    }

    ts = int(time.time())
    report_path = RESULTS_DIR / f"load_test_{NODE_ID}_{ts}.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    console.rule("[bold green]Resultado Final[/bold green]")
    for k, v in report.items():
        console.print(f"  {k}: [bold]{v}[/bold]")
    console.print(f"\n  Relatório salvo em: [cyan]{report_path}[/cyan]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fall Detection IoT — Load Test")
    parser.add_argument(
        "--devices",
        type=int,
        default=CONFIG["load_test"]["n_devices"],
        help="Número de dispositivos simulados",
    )
    parser.add_argument(
        "--requests",
        type=int,
        default=CONFIG["load_test"]["requests_per_device"],
        help="Requisições por dispositivo",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=CONFIG["load_test"]["interval_ms"],
        help="Intervalo em ms entre publicações (padrão: valor do config)",
    )
    parser.add_argument(
        "--fall-prob",
        type=float,
        default=None,
        help="Probabilidade de queda (0.0–1.0). Padrão: valor do config.",
    )
    parser.add_argument(
        "--fall-mode",
        choices=["session", "per_request"],
        default=None,
        help="Modo de queda: 'session' (1 queda por device) ou 'per_request' (por leitura)",
    )
    parser.add_argument(
        "--offset",
        type=int,
        default=int(os.getenv("DEVICE_OFFSET", "0")),
        help=(
            "Índice inicial no device_tokens.json (padrão: 0). "
            "Permite que múltiplos nós processem fatias distintas do pool de devices. "
            "Equivalente à variável de ambiente DEVICE_OFFSET."
        ),
    )
    args = parser.parse_args()

    if args.fall_prob is not None:
        CONFIG["load_test"]["fall_probability"] = args.fall_prob
    if args.fall_mode is not None:
        CONFIG["load_test"]["fall_mode"] = args.fall_mode

    asyncio.run(main(args.devices, args.requests, args.interval, args.offset))

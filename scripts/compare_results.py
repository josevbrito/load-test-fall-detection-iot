"""
compare_results.py
------------------
Agrega e compara relatórios JSON produzidos por múltiplos nós sensor.
Útil para consolidar os resultados de um experimento distribuído e
comparar diferentes configurações (1 nó vs 3 nós, local vs Docker, etc.).

Uso:
    # Agregar os 3 relatórios do último experimento de 3 nós:
    python scripts/compare_results.py results/load_test_node-*.json

    # Comparar com o baseline local (último relatório mais recente):
    python scripts/compare_results.py results/load_test_node-*.json \\
        --baseline results/load_test_local_1779732565.json

    # Pegar os N arquivos mais recentes automaticamente:
    python scripts/compare_results.py --latest 3

    # Salvar relatório agregado em JSON:
    python scripts/compare_results.py results/load_test_node-*.json --save
"""

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from rich.table import Table

BASE_DIR = Path(__file__).parent.parent
RESULTS_DIR = BASE_DIR / "results"

console = Console()


# ---------------------------------------------------------------------------
# Leitura e agregação
# ---------------------------------------------------------------------------

def load_report(path: Path) -> dict:
    with open(path) as f:
        data = json.load(f)
    # Compatibilidade com relatórios antigos (antes do Passo 1) que não
    # tinham node_id nem device_offset.
    data.setdefault("node_id", path.stem)
    data.setdefault("device_offset", "?")
    return data


def aggregate(reports: list[dict]) -> dict:
    """
    Agrega métricas de múltiplos nós num resultado de cluster.

    Regras de agregação:
    - throughput   : soma  (nós paralelos contribuem independentemente)
    - avg_latency  : média ponderada pelo total_published de cada nó
    - p99_latency  : máximo entre os nós (limite conservador — sem raw data
                     não é possível recalcular o percentil real do cluster)
    - elapsed      : máximo (o experimento acaba quando o nó mais lento termina)
    - erros/msgs   : soma
    """
    total_pub   = sum(r["total_published"]       for r in reports)
    total_err   = sum(r["total_errors"]          for r in reports)
    total_falls = sum(r["total_falls_simulated"] for r in reports)
    total_dev   = sum(r["total_devices"]         for r in reports)
    elapsed     = max(r["elapsed_seconds"]       for r in reports)
    throughput  = sum(r["throughput_msg_per_s"]  for r in reports)

    # Média ponderada de latência
    if total_pub > 0:
        avg_lat = sum(
            r["avg_latency_ms"] * r["total_published"] for r in reports
        ) / total_pub
    else:
        avg_lat = 0.0

    p99_lat = max(r["p99_latency_ms"] for r in reports)

    error_rate = (
        total_err / max(total_pub + total_err, 1) * 100
    )

    return {
        "node_id":               "CLUSTER",
        "device_offset":         "—",
        "total_devices":         total_dev,
        "total_published":       total_pub,
        "total_errors":          total_err,
        "total_falls_simulated": total_falls,
        "elapsed_seconds":       round(elapsed, 2),
        "throughput_msg_per_s":  round(throughput, 2),
        "avg_latency_ms":        round(avg_lat, 2),
        "p99_latency_ms":        round(p99_lat, 2),
        "error_rate_pct":        round(error_rate, 2),
    }


# ---------------------------------------------------------------------------
# Exibição
# ---------------------------------------------------------------------------

def _fmt_lat(val: float) -> str:
    if val == 0.0:
        return "[dim]—[/dim]"
    color = "green" if val < 50 else "yellow" if val < 200 else "red"
    return f"[{color}]{val:.1f} ms[/{color}]"


def _fmt_tput(val: float) -> str:
    color = "green" if val > 500 else "yellow" if val > 100 else "red"
    return f"[{color}]{val:,.1f}[/{color}]"


def _fmt_err(rate: float, count: int) -> str:
    if rate == 0.0:
        return "[green]0[/green]"
    return f"[red]{count:,}  ({rate:.1f}%)[/red]"


def print_table(reports: list[dict], cluster: dict) -> None:
    table = Table(
        title="Fall Detection IoT — Resultados por Nó",
        show_lines=True,
        header_style="bold cyan",
    )
    table.add_column("Nó",              style="bold")
    table.add_column("Offset",          justify="right")
    table.add_column("Devices",         justify="right")
    table.add_column("Msgs enviadas",   justify="right")
    table.add_column("Throughput\n(msg/s)", justify="right")
    table.add_column("Lat. média",      justify="right")
    table.add_column("Lat. p99",        justify="right")
    table.add_column("Erros",           justify="right")
    table.add_column("Quedas",          justify="right")
    table.add_column("Duração (s)",     justify="right")

    for r in reports:
        table.add_row(
            r["node_id"],
            str(r["device_offset"]),
            f"{r['total_devices']:,}",
            f"{r['total_published']:,}",
            _fmt_tput(r["throughput_msg_per_s"]),
            _fmt_lat(r["avg_latency_ms"]),
            _fmt_lat(r["p99_latency_ms"]),
            _fmt_err(r["error_rate_pct"], r["total_errors"]),
            f"{r['total_falls_simulated']:,}",
            f"{r['elapsed_seconds']:.1f}",
        )

    # Linha de total do cluster
    c = cluster
    table.add_section()
    table.add_row(
        "[bold yellow]TOTAL CLUSTER[/bold yellow]",
        "—",
        f"[bold]{c['total_devices']:,}[/bold]",
        f"[bold]{c['total_published']:,}[/bold]",
        f"[bold]{_fmt_tput(c['throughput_msg_per_s'])}[/bold]",
        f"[bold]{_fmt_lat(c['avg_latency_ms'])}[/bold]",
        f"[bold]{_fmt_lat(c['p99_latency_ms'])} ★[/bold]",
        _fmt_err(c["error_rate_pct"], c["total_errors"]),
        f"[bold]{c['total_falls_simulated']:,}[/bold]",
        f"[bold]{c['elapsed_seconds']:.1f}[/bold]",
    )

    console.print(table)
    console.print("  [dim]★ p99 do cluster = máximo entre os nós (limite conservador)[/dim]\n")


def print_comparison(cluster: dict, baseline: dict) -> None:
    """Imprime delta percentual entre o cluster e o baseline."""
    console.print(Rule("[bold]Comparação com baseline[/bold]"))

    def delta(new: float, old: float, invert: bool = False) -> str:
        if old == 0:
            return "[dim]—[/dim]"
        pct = (new - old) / old * 100
        if invert:
            pct = -pct
        color = "green" if pct > 0 else "red"
        sign = "+" if pct > 0 else ""
        return f"[{color}]{sign}{pct:.1f}%[/{color}]"

    rows = [
        ("Throughput (msg/s)",
         f"{baseline['throughput_msg_per_s']:,.1f}",
         f"{cluster['throughput_msg_per_s']:,.1f}",
         delta(cluster["throughput_msg_per_s"], baseline["throughput_msg_per_s"])),
        ("Latência média (ms)",
         f"{baseline['avg_latency_ms']:.1f}",
         f"{cluster['avg_latency_ms']:.1f}",
         delta(cluster["avg_latency_ms"], baseline["avg_latency_ms"], invert=True)),
        ("Latência p99 (ms)",
         f"{baseline['p99_latency_ms']:.1f}",
         f"{cluster['p99_latency_ms']:.1f}",
         delta(cluster["p99_latency_ms"], baseline["p99_latency_ms"], invert=True)),
        ("Total msgs enviadas",
         f"{baseline['total_published']:,}",
         f"{cluster['total_published']:,}",
         delta(cluster["total_published"], baseline["total_published"])),
        ("Taxa de erro (%)",
         f"{baseline['error_rate_pct']:.2f}",
         f"{cluster['error_rate_pct']:.2f}",
         delta(cluster["error_rate_pct"], baseline["error_rate_pct"], invert=True)),
    ]

    t = Table(show_header=True, header_style="bold")
    t.add_column("Métrica")
    t.add_column("Baseline", justify="right")
    t.add_column("Cluster", justify="right")
    t.add_column("Delta", justify="right")
    for row in rows:
        t.add_row(*row)

    console.print(t)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agrega e compara resultados de múltiplos nós sensor."
    )
    parser.add_argument(
        "files",
        nargs="*",
        type=Path,
        help="Arquivos JSON de resultado (aceita glob expandido pelo shell).",
    )
    parser.add_argument(
        "--latest",
        type=int,
        metavar="N",
        default=None,
        help="Ignorar 'files' e usar os N arquivos mais recentes em results/.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Arquivo JSON de baseline para comparação de delta.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Salva o relatório agregado do cluster em results/cluster_<ts>.json.",
    )
    args = parser.parse_args()

    # Resolução dos arquivos de entrada
    if args.latest is not None:
        all_files = sorted(RESULTS_DIR.glob("load_test_*.json"), key=lambda p: p.stat().st_mtime)
        target_files = all_files[-args.latest:]
    else:
        target_files = [Path(f) for f in args.files]

    if not target_files:
        console.print("[red]Nenhum arquivo fornecido. Use --latest N ou passe caminhos explícitos.[/red]")
        sys.exit(1)

    reports = [load_report(p) for p in target_files]
    cluster = aggregate(reports)

    console.print()
    print_table(reports, cluster)

    if args.baseline:
        baseline = load_report(args.baseline)
        print_comparison(cluster, baseline)

    if args.save:
        import time
        out = RESULTS_DIR / f"cluster_{int(time.time())}.json"
        with open(out, "w") as f:
            json.dump({"nodes": reports, "cluster": cluster}, f, indent=2)
        console.print(f"  Relatório salvo em: [cyan]{out}[/cyan]")


if __name__ == "__main__":
    main()

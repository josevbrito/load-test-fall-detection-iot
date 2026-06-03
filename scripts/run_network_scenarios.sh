#!/bin/bash
# run_network_scenarios.sh
# ------------------------
# Executa o load test em 3 cenarios de rede sequencialmente, usando tc netem.
# Cada cenario sobe os nos sensores, aguarda o teste terminar, move os JSONs
# para uma subpasta dedicada e derruba os nos antes do proximo cenario.
#
# Pre-requisitos:
#   - ThingsBoard, Postgres, Prometheus e Grafana ja rodando:
#       docker compose up thingsboard postgres prometheus grafana -d
#   - Imagem do sensor ja buildada:
#       docker compose build sensor-node-1
#
# Uso:
#   bash scripts/run_network_scenarios.sh

set -e

# ---------------------------------------------------------------------------
# Definicao dos cenarios: nome | delay | jitter | loss
# ---------------------------------------------------------------------------
CENARIOS=(
    "boa:20ms:5ms:0%"
    "media:100ms:20ms:1%"
    "ruim:300ms:50ms:5%"
)

# Carga reduzida para execucao rapida
export EXP_REQUESTS=100

# Diretorio base dos resultados (relativo a raiz do projeto)
RESULTS_DIR="./results"

# Pausa entre cenarios (segundos) — cria intervalo visivel no Grafana
PAUSA_ENTRE_CENARIOS=30

# ---------------------------------------------------------------------------
# Arrays para o resumo final
# ---------------------------------------------------------------------------
declare -a NOMES_CENARIOS
declare -a INICIO_CENARIOS
declare -a FIM_CENARIOS

# ---------------------------------------------------------------------------
# Funcao: executa um cenario
#   $1 = nome (boa, media, ruim)
#   $2 = NET_DELAY
#   $3 = NET_JITTER
#   $4 = NET_LOSS
# ---------------------------------------------------------------------------
run_cenario() {
    local nome="$1"
    local delay="$2"
    local jitter="$3"
    local loss="$4"
    local subpasta="${RESULTS_DIR}/cenario_${nome}"

    # Cria subpasta para os resultados deste cenario
    mkdir -p "$subpasta"

    # Registra timestamp ANTES de iniciar (para identificar JSONs novos)
    local ts_antes
    ts_antes=$(date +%s)

    local hora_inicio
    hora_inicio=$(date '+%Y-%m-%d %H:%M:%S')

    echo ""
    echo "================================================================="
    echo "  CENARIO: ${nome}"
    echo "  delay=${delay}  jitter=${jitter}  loss=${loss}"
    echo "  Inicio: ${hora_inicio}"
    echo "================================================================="

    # Exporta variaveis de rede para o docker compose
    export NET_DELAY="$delay"
    export NET_JITTER="$jitter"
    export NET_LOSS="$loss"

    # Sobe os 3 nos sensores e aguarda todos terminarem.
    # --abort-on-container-exit: quando qualquer container encerra (teste concluido),
    # o compose envia SIGTERM aos demais e retorna. Como os 3 encerram naturalmente
    # apos o load test, na pratica espera todos completarem.
    docker compose --profile sensors up \
        --abort-on-container-exit \
        --force-recreate \
        sensor-node-1 sensor-node-2 sensor-node-3 || true

    local hora_fim
    hora_fim=$(date '+%Y-%m-%d %H:%M:%S')

    echo ""
    echo "  Cenario '${nome}' finalizado: ${hora_fim}"

    # Move os JSONs gerados APOS o inicio deste cenario para a subpasta.
    # O load_test.py nomeia como: load_test_{NODE_ID}_{unix_timestamp}.json
    local moved=0
    for f in "${RESULTS_DIR}"/load_test_*.json; do
        [ -f "$f" ] || continue

        # Extrai o timestamp unix do nome do arquivo (ultimo campo antes de .json)
        local fname
        fname=$(basename "$f")
        local file_ts
        file_ts=$(echo "$fname" | grep -oP '\d+(?=\.json$)')

        # So move arquivos gerados apos o inicio deste cenario
        if [ -n "$file_ts" ] && [ "$file_ts" -ge "$ts_antes" ]; then
            mv "$f" "$subpasta/"
            echo "  Movido: ${fname} -> cenario_${nome}/"
            moved=$((moved + 1))
        fi
    done

    if [ "$moved" -eq 0 ]; then
        echo "  [AVISO] Nenhum JSON novo encontrado para mover."
    fi

    # Derruba os nos sensores (sem afetar ThingsBoard/Prometheus/Grafana)
    docker compose --profile sensors down

    # Salva dados para o resumo final
    NOMES_CENARIOS+=("$nome")
    INICIO_CENARIOS+=("$hora_inicio")
    FIM_CENARIOS+=("$hora_fim")
}

# ---------------------------------------------------------------------------
# Execucao principal
# ---------------------------------------------------------------------------

echo ""
echo "================================================================="
echo "  TESTE DE CENARIOS DE REDE — tc netem"
echo "  Cenarios: ${#CENARIOS[@]} | Requests/device: ${EXP_REQUESTS}"
echo "  Pausa entre cenarios: ${PAUSA_ENTRE_CENARIOS}s"
echo "================================================================="

for i in "${!CENARIOS[@]}"; do
    # Parseia o cenario (formato "nome:delay:jitter:loss")
    IFS=':' read -r nome delay jitter loss <<< "${CENARIOS[$i]}"

    run_cenario "$nome" "$delay" "$jitter" "$loss"

    # Pausa entre cenarios (exceto apos o ultimo)
    if [ "$i" -lt $(( ${#CENARIOS[@]} - 1 )) ]; then
        echo ""
        echo "  Aguardando ${PAUSA_ENTRE_CENARIOS}s antes do proximo cenario..."
        echo "  (cria intervalo visivel no Grafana)"
        sleep "$PAUSA_ENTRE_CENARIOS"
    fi
done

# ---------------------------------------------------------------------------
# Resumo final — janelas de tempo para localizar no Grafana
# ---------------------------------------------------------------------------

echo ""
echo ""
echo "================================================================="
echo "  RESUMO — JANELAS DE TEMPO PARA O GRAFANA"
echo "================================================================="
echo ""
printf "  %-10s %-22s %-22s\n" "CENARIO" "INICIO" "FIM"
printf "  %-10s %-22s %-22s\n" "--------" "---------------------" "---------------------"

for i in "${!NOMES_CENARIOS[@]}"; do
    printf "  %-10s %-22s %-22s\n" \
        "${NOMES_CENARIOS[$i]}" \
        "${INICIO_CENARIOS[$i]}" \
        "${FIM_CENARIOS[$i]}"
done

echo ""
echo "  Resultados salvos em:"
for nome in "${NOMES_CENARIOS[@]}"; do
    echo "    ${RESULTS_DIR}/cenario_${nome}/"
done
echo ""
echo "================================================================="
echo "  Concluido!"
echo "================================================================="

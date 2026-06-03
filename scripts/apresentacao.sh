#!/usr/bin/env bash
# =============================================================================
# apresentacao.sh — Script completo de apresentacao
# =============================================================================
# Sobe toda a infraestrutura do zero, provisiona devices, configura dashboard
# e executa o load test distribuido com 1000 devices x 1000 requests.
#
# Uso:
#   bash scripts/apresentacao.sh
#
# O que este script faz:
#   1. Inicia o Docker daemon (se nao estiver rodando)
#   2. Builda a imagem do sensor
#   3. Sobe ThingsBoard + PostgreSQL + Prometheus + Grafana
#   4. Aguarda o ThingsBoard inicializar completamente (~1-3 min)
#   5. Provisiona 1000 devices (pula se ja existem)
#   6. Cria/atualiza o dashboard no ThingsBoard
#   7. Exibe os links de acesso
#   8. Executa o load test com 3 nos distribuidos
#
# Para parar tudo depois:
#   docker compose --profile sensors down
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Diretorio do projeto (resolve independente de onde o script for chamado)
# ---------------------------------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

# ---------------------------------------------------------------------------
# Configuracao do experimento
# ---------------------------------------------------------------------------
DEVICES=1000
REQUESTS=1000
INTERVAL_MS=100
MQTT_QOS=1

# ---------------------------------------------------------------------------
# Cores e funcoes de terminal
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

banner() {
    echo ""
    echo -e "${CYAN}${BOLD}================================================================${RESET}"
    echo -e "${CYAN}${BOLD}  $*${RESET}"
    echo -e "${CYAN}${BOLD}================================================================${RESET}"
    echo ""
}

info()  { echo -e "${GREEN}  [OK] $*${RESET}"; }
warn()  { echo -e "${YELLOW}  [!!] $*${RESET}"; }
fail()  { echo -e "${RED}  [ERRO] $*${RESET}"; exit 1; }
step()  { echo -e "\n${BOLD}--- $* ---${RESET}"; }

# ---------------------------------------------------------------------------
# 1. Garantir que o Docker esta rodando
# ---------------------------------------------------------------------------
ensure_docker() {
    step "Verificando Docker"

    if docker info &>/dev/null 2>&1; then
        info "Docker ja esta rodando."
        return 0
    fi

    warn "Docker nao esta rodando. Tentando iniciar..."

    # Tenta systemctl (WSL2 com systemd habilitado)
    if command -v systemctl &>/dev/null; then
        sudo systemctl start docker 2>/dev/null || true
    fi

    # Tenta service (WSL2 sem systemd)
    if ! docker info &>/dev/null 2>&1; then
        sudo service docker start 2>/dev/null || true
    fi

    # Tenta iniciar Docker Desktop via Windows (fallback WSL2 + Docker Desktop)
    if ! docker info &>/dev/null 2>&1; then
        if command -v powershell.exe &>/dev/null; then
            warn "Tentando iniciar Docker Desktop via Windows..."
            powershell.exe -Command "Start-Process 'C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe'" 2>/dev/null || true
        fi
    fi

    # Aguarda o Docker ficar pronto (max 90s)
    local attempts=0
    while ! docker info &>/dev/null 2>&1; do
        attempts=$((attempts + 1))
        if [ "$attempts" -ge 45 ]; then
            fail "Docker nao iniciou apos 90s. Inicie manualmente e rode o script novamente."
        fi
        echo -ne "\r  Aguardando Docker... (${attempts}s)"
        sleep 2
    done
    echo ""
    info "Docker iniciado com sucesso!"
}

# ---------------------------------------------------------------------------
# 2. Build da imagem do sensor
# ---------------------------------------------------------------------------
build_sensor() {
    step "Build da imagem do sensor"
    docker compose build sensor-node-1
    info "Imagem pronta."
}

# ---------------------------------------------------------------------------
# 3. Subir infraestrutura (ThingsBoard, Postgres, Prometheus, Grafana)
# ---------------------------------------------------------------------------
start_infra() {
    step "Subindo infraestrutura"
    docker compose up -d thingsboard postgres prometheus grafana
    info "Containers de infraestrutura iniciados."
}

# ---------------------------------------------------------------------------
# 4. Aguardar ThingsBoard inicializar completamente
# ---------------------------------------------------------------------------
wait_thingsboard() {
    step "Aguardando ThingsBoard inicializar (pode levar 1-3 min na primeira vez)"

    local max_attempts=60
    local attempts=0

    while true; do
        attempts=$((attempts + 1))

        # Testa se a API de login responde com sucesso
        if curl -sf -X POST http://localhost:8090/api/auth/login \
            -H "Content-Type: application/json" \
            -d '{"username":"sysadmin@thingsboard.org","password":"sysadmin"}' \
            -o /dev/null 2>/dev/null; then
            echo ""
            info "ThingsBoard pronto!"
            return 0
        fi

        if [ "$attempts" -ge "$max_attempts" ]; then
            echo ""
            fail "ThingsBoard nao ficou pronto apos 5 min. Verifique: docker logs thingsboard"
        fi

        echo -ne "\r  Aguardando ThingsBoard... (${attempts}/${max_attempts})"
        sleep 5
    done
}

# ---------------------------------------------------------------------------
# 5. Provisionar devices
# ---------------------------------------------------------------------------
provision_devices() {
    step "Provisionando ${DEVICES} devices no ThingsBoard"

    # Verifica se ja existem tokens suficientes
    if [ -f "device_tokens.json" ]; then
        local existing
        existing=$(python3 -c "import json; d=json.load(open('device_tokens.json')); print(sum(1 for v in d.values() if v.get('token')))" 2>/dev/null || echo "0")
        if [ "$existing" -ge "$DEVICES" ]; then
            info "${existing} devices ja provisionados. Pulando."
            return 0
        fi
        warn "${existing}/${DEVICES} devices existem. Provisionando os restantes..."
    fi

    python3 scripts/provision_devices.py --devices "$DEVICES"
    info "Provisionamento concluido."
}

# ---------------------------------------------------------------------------
# 6. Configurar dashboard no ThingsBoard
# ---------------------------------------------------------------------------
setup_dashboard() {
    step "Configurando dashboard no ThingsBoard"
    python3 scripts/dashboard_setup.py
    info "Dashboard configurado."
}

# ---------------------------------------------------------------------------
# 7. Exibir links de acesso
# ---------------------------------------------------------------------------
show_links() {
    banner "LINKS DE ACESSO"
    echo -e "  ${BOLD}ThingsBoard:${RESET}  ${CYAN}http://localhost:8090${RESET}"
    echo -e "    Sysadmin:    ${BOLD}sysadmin@thingsboard.org${RESET} / ${BOLD}sysadmin${RESET}"
    echo -e "    Tenant:      ${BOLD}tenant@thingsboard.org${RESET} / ${BOLD}tenant2026${RESET}"
    echo ""
    echo -e "  ${BOLD}Grafana:${RESET}      ${CYAN}http://localhost:3000${RESET}"
    echo -e "    Login:       ${BOLD}admin${RESET} / ${BOLD}admin${RESET}"
    echo ""
    echo -e "  ${BOLD}Prometheus:${RESET}   ${CYAN}http://localhost:9090${RESET}"
    echo ""
    echo -e "  ${YELLOW}Dica: abra o Grafana agora para acompanhar o load test em tempo real!${RESET}"
}

# ---------------------------------------------------------------------------
# 8. Executar o load test distribuido
# ---------------------------------------------------------------------------
run_load_test() {
    step "Iniciando load test: ${DEVICES} devices x ${REQUESTS} requests (QoS ${MQTT_QOS})"
    echo ""
    echo -e "  Distribuicao dos nos:"
    echo -e "    sensor-node-1: devices   0-333  (334 devices)"
    echo -e "    sensor-node-2: devices 334-666  (333 devices)"
    echo -e "    sensor-node-3: devices 667-999  (333 devices)"
    echo -e "    Total: ${DEVICES} devices x ${REQUESTS} req = ${BOLD}$((DEVICES * REQUESTS))${RESET} mensagens MQTT"
    echo ""

    # MQTT_CLEAN_SESSION=true evita acumulo de sessoes persistentes no broker
    # entre execucoes repetidas do teste.
    export EXP_REQUESTS="$REQUESTS"
    export EXP_INTERVAL_MS="$INTERVAL_MS"
    export MQTT_QOS="$MQTT_QOS"
    export MQTT_CLEAN_SESSION="true"

    docker compose --profile sensors up \
        --force-recreate \
        sensor-node-1 sensor-node-2 sensor-node-3
}

# ---------------------------------------------------------------------------
# Trap: exibir mensagem ao sair com Ctrl+C
# ---------------------------------------------------------------------------
cleanup_msg() {
    echo ""
    echo ""
    warn "Load test interrompido."
    echo ""
    echo -e "  A infraestrutura (ThingsBoard, Grafana, Prometheus) continua rodando."
    echo -e "  Os dados ja recebidos estao disponiveis nos dashboards."
    echo ""
    echo -e "  Para parar tudo:    ${BOLD}docker compose --profile sensors down${RESET}"
    echo -e "  Para remover dados: ${BOLD}docker compose --profile sensors down -v${RESET}"
    echo ""
    exit 0
}
trap cleanup_msg SIGINT SIGTERM

# ===========================================================================
# MAIN
# ===========================================================================
main() {
    banner "Fall Detection IoT --- Apresentacao Completa"
    echo -e "  Simulacao de ${BOLD}${DEVICES}${RESET} sensores ESP32 de deteccao de quedas"
    echo -e "  publicando telemetria via MQTT para ThingsBoard."
    echo -e "  Infraestrutura: ThingsBoard + PostgreSQL + Prometheus + Grafana"
    echo -e "  Load test distribuido em 3 nos Docker."

    ensure_docker
    build_sensor
    start_infra
    wait_thingsboard
    provision_devices
    setup_dashboard
    show_links

    echo ""
    echo -e "  ${BOLD}O load test iniciara em 10 segundos...${RESET}"
    echo -e "  ${CYAN}Abra o Grafana (http://localhost:3000) para acompanhar!${RESET}"
    echo ""

    # Countdown
    for i in 10 9 8 7 6 5 4 3 2 1; do
        echo -ne "\r  Iniciando em ${BOLD}${i}${RESET}s...  "
        sleep 1
    done
    echo ""

    run_load_test

    # Pos-teste
    banner "Load test concluido!"
    echo -e "  A infraestrutura continua rodando. Verifique os resultados:"
    echo ""
    echo -e "  ${BOLD}ThingsBoard:${RESET}  ${CYAN}http://localhost:8090${RESET}"
    echo -e "    -> Dashboards -> Fall Detection IoT"
    echo -e "    -> Devices -> Verifique telemetria dos sensores"
    echo -e "    -> Alarmes -> Verifique alertas de queda (QUEDA_DETECTADA)"
    echo ""
    echo -e "  ${BOLD}Grafana:${RESET}      ${CYAN}http://localhost:3000${RESET}"
    echo -e "    -> Dashboard 'Load Test' com throughput, latencia e quedas"
    echo ""
    echo -e "  ${BOLD}Resultados JSON:${RESET} pasta ${CYAN}results/${RESET}"
    echo ""
    echo -e "  Para parar tudo:    ${BOLD}docker compose --profile sensors down${RESET}"
    echo -e "  Para remover dados: ${BOLD}docker compose --profile sensors down -v${RESET}"
}

main "$@"

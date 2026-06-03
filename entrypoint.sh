#!/bin/bash
# entrypoint.sh
# -------------
# Aplica simulação de rede via tc netem antes de executar o load test.
#
# Variáveis de ambiente (com defaults no Dockerfile):
#   NET_DELAY  — latência base adicionada a cada pacote (ex: 100ms)
#   NET_JITTER — variação aleatória da latência (ex: 20ms)
#   NET_LOSS   — percentual de pacotes descartados (ex: 1%)
#
# Se NET_DELAY=0ms, nenhuma regra é aplicada (rede limpa).

set -e

if [ "${NET_DELAY}" != "0ms" ] && [ "${NET_DELAY}" != "0" ]; then
    echo "[netem] Aplicando simulação de rede na eth0:"
    echo "  delay  = ${NET_DELAY} ±${NET_JITTER}"
    echo "  loss   = ${NET_LOSS}"

    # tc qdisc add dev eth0 root netem:
    #   delay X Y  → adiciona X de latência com jitter de ±Y
    #   loss Z     → descarta Z% dos pacotes aleatoriamente
    tc qdisc add dev eth0 root netem \
        delay ${NET_DELAY} ${NET_JITTER} \
        loss ${NET_LOSS}

    echo "[netem] Regras aplicadas com sucesso."
else
    echo "[netem] NET_DELAY=${NET_DELAY} — simulação de rede desativada."
fi

# Executa o CMD passado pelo Docker (o load_test.py)
exec "$@"

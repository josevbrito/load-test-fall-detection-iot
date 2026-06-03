# Fall Detection IoT — Load Test

Simula dispositivos ESP32 publicando telemetria de queda de idosos via MQTT
para um ThingsBoard CE. Suporta dois modos de execução:

- **Local** — asyncio direto no host (baseline)
- **Distribuído** — múltiplos containers Docker na rede virtual `iot-cloud`, cada um
  responsável por uma fatia do pool de devices (simula gateways regionais em nuvem)

---

## Arquitetura

### Modo local (baseline)

```
Python asyncio (N coroutines)
  └─ aiomqtt → MQTT :1883
                    │
       ┌────────────▼───────────────────┐
       │  Docker: ThingsBoard CE        │
       │  :8090 HTTP  |  :1883 MQTT     │
       │  PostgreSQL (interno)          │
       └────────────────────────────────┘
```

### Modo distribuído (Docker)

```
┌────────────────────────── rede: iot-cloud (bridge) ──────────────────────────┐
│                                                                               │
│  sensor-node-1          sensor-node-2          sensor-node-3                 │
│  devices [0–333]        devices [334–666]       devices [667–999]            │
│  tc netem (delay/jitter/loss)                                                │
│  IP próprio             IP próprio              IP próprio                    │
│       │                      │                       │                       │
│       └──────────────────────┼───────────────────────┘                       │
│                              │ MQTT (DNS interno: thingsboard:1883)           │
│                  ┌───────────▼──────────────┐                                │
│                  │  thingsboard             │                                 │
│                  │  :9090 HTTP | :1883 MQTT │                                 │
│                  └───────────┬──────────────┘                                │
│                              │                                                │
│                  ┌───────────▼──────────────┐                                │
│                  │  postgres  :5432          │                                │
│                  └──────────────────────────┘                                │
└───────────────────────────────────────────────────────────────────────────────┘
```

Cada `sensor-node` é um processo Python independente com seu próprio IP,
exatamente como gateways regionais em nuvem real. O ThingsBoard os enxerga
como três clientes MQTT distintos originados de endereços diferentes.

---

## Pré-requisitos

- Docker + Docker Compose
- Python 3.11+
- ~2 GB de RAM livres para o ThingsBoard

```bash
pip install -r requirements.txt
```

---

## Modo Local (uso básico)

### 1. Subir o ThingsBoard

```bash
docker compose up thingsboard postgres -d
```

Aguardar inicialização (~90s):

```bash
docker compose logs -f thingsboard
# Pronto quando aparecer: "Started Application in X seconds"
```

### 2. Provisionar devices

```bash
python scripts/provision_devices.py
```

Cria 1000 devices no ThingsBoard via REST API e salva os tokens em
`device_tokens.json`. Idempotente — pode ser re-executado com segurança.

### 3. Criar dashboard

```bash
python scripts/dashboard_setup.py
```

Acesse: **http://localhost:8090**

> **Importante:** o login direto com as credenciais do tenant não funciona.
> É necessário primeiro logar como **System Administrator** e depois entrar
> como locatário do tenant:
>
> 1. Login como sysadmin: `sysadmin@thingsboard.org` / `sysadmin`
> 2. No menu lateral, acesse **Tenants** → selecione o tenant → clique em
>    **Login as Tenant Administrator** (ícone de login ao lado do tenant)

### 4. Rodar o load test

```bash
# Padrão (valores do config.yaml — QoS 1, persistent session)
python scripts/load_test.py

# Customizado
python scripts/load_test.py --devices 1000 --requests 100 --interval 100

# Com QoS 0 (fire-and-forget, sem PUBACK)
MQTT_QOS=0 python scripts/load_test.py

# Nó parcial (ex: processar só os devices 0–499)
python scripts/load_test.py --devices 500 --offset 0
```

O relatório JSON é salvo em `results/load_test_<NODE_ID>_<timestamp>.json`.

---

## Garantia de Entrega MQTT

O load test suporta dois níveis de QoS, configuráveis via variável de ambiente `MQTT_QOS`:

| QoS | Nome | Comportamento |
|---|---|---|
| 0 | Fire-and-forget | Envia e não espera confirmação. Mais rápido, mas mensagens podem se perder se o broker cair. |
| 1 | At-least-once | Espera o PUBACK do broker antes de prosseguir. Garante recebimento, ao custo de latência maior (round-trip). |

Além do QoS, o load test usa **persistent session** (`clean_session=False`):
o broker preserva a fila de mensagens pendentes entre reconexões. Se um device
desconectar e reconectar com o mesmo `client_id`, mensagens QoS 1 não confirmadas
são reenviadas automaticamente.

Cada device usa um `client_id` fixo e determinístico (`sim-000001`, `sim-000002`, ...),
garantindo que a sessão persistente funcione corretamente. Em caso de desconexão,
o load test faz até 3 tentativas de reconexão com backoff exponencial (2s, 4s, 8s).

```bash
# QoS 1 (padrão — at-least-once)
python scripts/load_test.py

# QoS 0 (fire-and-forget, para comparação)
MQTT_QOS=0 python scripts/load_test.py

# No modo distribuído
MQTT_QOS=0 docker compose --profile sensors up
```

---

## Modo Distribuído (Docker)

### 1. Build da imagem sensor

```bash
# Basta uma vez; as 3 replicas usam a mesma imagem
docker compose build sensor-node-1
```

### 2. Subir infraestrutura e rodar os nós

```bash
# Só infraestrutura (ThingsBoard + Postgres):
docker compose up thingsboard postgres -d

# Todos os nós em paralelo (requer profile "sensors"):
docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3
```

Os 3 containers rodam simultaneamente, cada um publicando sua fatia:

| Container | Devices | `DEVICE_OFFSET` | `DEVICE_COUNT` |
|---|---|---|---|
| `sensor-node-1` | 0 – 333 | 0 | 334 |
| `sensor-node-2` | 334 – 666 | 334 | 333 |
| `sensor-node-3` | 667 – 999 | 667 | 333 |

### 3. Customizar parâmetros sem editar arquivos

As variáveis de ambiente propagam para todos os nós:

```bash
# Ajustar carga
EXP_REQUESTS=50 EXP_INTERVAL_MS=200 \
  docker compose --profile sensors up sensor-node-1 sensor-node-2 sensor-node-3

# Ajustar QoS
MQTT_QOS=0 docker compose --profile sensors up
```

---

## Simulação de Rede (tc netem)

Os nós sensores suportam simulação de latência, jitter e perda de pacotes
usando `tc netem` na interface `eth0` de cada container. Controlado por
variáveis de ambiente:

| Variável | Default | Descrição |
|---|---|---|
| `NET_DELAY` | `0ms` | Latência base adicionada a cada pacote |
| `NET_JITTER` | `0ms` | Variação aleatória da latência (+-) |
| `NET_LOSS` | `0%` | Percentual de pacotes descartados |

Quando `NET_DELAY=0ms` (padrão), nenhuma regra netem é aplicada — a rede
funciona normalmente sem overhead.

```bash
# Rede limpa (padrão)
docker compose --profile sensors up

# Rede Wi-Fi razoável (100ms +-20ms, 1% perda)
NET_DELAY=100ms NET_JITTER=20ms NET_LOSS=1% \
  docker compose --profile sensors up

# Rede ruim / 4G instável (300ms +-50ms, 5% perda)
NET_DELAY=300ms NET_JITTER=50ms NET_LOSS=5% \
  docker compose --profile sensors up
```

Os containers precisam da capability `NET_ADMIN` (já configurada no `docker-compose.yml`)
para manipular a interface de rede via `tc`.

---

## Experimentos

### Experimentos de Sistemas Distribuídos (A, B, C)

O script `run_experiments.sh` orquestra três experimentos automaticamente.

```bash
# Experimento A — escalabilidade horizontal
bash scripts/run_experiments.sh A

# Experimento B — tolerância a falhas
bash scripts/run_experiments.sh B

# Experimento C — local vs Docker
bash scripts/run_experiments.sh C

# Todos em sequência
bash scripts/run_experiments.sh all

# Com carga menor (recomendado para testes rápidos):
REQUESTS=50 INTERVAL_MS=100 bash scripts/run_experiments.sh A
```

#### Experimento A — Escalabilidade Horizontal

Roda o mesmo total de devices em duas configurações e compara:

```
A1: 1 container  →  1000 devices num único processo Python
A3: 3 containers →  334 + 333 + 333 devices em paralelo
```

**O que observar:** throughput do cluster A3 deve ser próximo de 3x o de A1.
Latência deve permanecer similar — a distribuição não adiciona overhead significativo.

#### Experimento B — Tolerância a Falhas

Sobe 3 nós, aguarda 20s e mata `sensor-node-2` com `docker stop`.

**O que observar:** devices 334–666 perdem mensagens (falha parcial).
Nós 1 e 3 completam normalmente — **isolamento de falha**. Em produção,
um load balancer redistribuiria os devices do nó perdido.

#### Experimento C — Local vs Docker

Compara a execução asyncio direta no host com o cluster de containers.

**O que observar:** overhead da rede bridge Docker é tipicamente < 5ms.
Cada container tem seu próprio event loop asyncio — melhor isolamento de CPU.

### Experimento de Cenários de Rede

O script `run_network_scenarios.sh` executa o load test em 3 condições de rede
diferentes, em sequência, com pausa de 30s entre cada cenário (para criar
intervalos visíveis no Grafana).

```bash
# Subir infraestrutura + monitoramento primeiro
docker compose up thingsboard postgres prometheus grafana -d

# Rodar os 3 cenários automaticamente
bash scripts/run_network_scenarios.sh
```

| Cenário | Delay | Jitter | Perda | Simula |
|---|---|---|---|---|
| `boa` | 20ms | +-5ms | 0% | Rede local / fibra |
| `media` | 100ms | +-20ms | 1% | Wi-Fi razoável |
| `ruim` | 300ms | +-50ms | 5% | 4G instável |

Todos os cenários usam `EXP_REQUESTS=100` (carga reduzida para execução rápida).

**Fluxo do script:**
1. Exporta as variáveis `NET_DELAY`, `NET_JITTER`, `NET_LOSS` do cenário
2. Sobe os 3 nós sensores com `--abort-on-container-exit`
3. Aguarda o teste terminar
4. Move os JSONs para `results/cenario_<nome>/`
5. Derruba os nós sensores
6. Pausa 30s (exceto após o último)
7. Imprime resumo com horários de início/fim de cada cenário

**O que observar:**
- Cenário `boa`: latência e throughput similares à rede limpa
- Cenário `media`: latência média sobe ~100ms, throughput cai levemente
- Cenário `ruim`: latência alta (~300ms+), perda de 5% gera erros e retransmissões (QoS 1)
- Com QoS 1, mensagens perdidas são retransmitidas; com QoS 0, são perdidas definitivamente

---

## Comparando Resultados

```bash
# Agregar métricas dos 3 nós num único relatório de cluster:
python scripts/compare_results.py results/load_test_node-*.json

# Comparar cluster vs baseline local:
python scripts/compare_results.py results/load_test_node-*.json \
    --baseline results/load_test_local_<timestamp>.json

# Pegar os N arquivos mais recentes automaticamente:
python scripts/compare_results.py --latest 3

# Salvar relatório agregado em JSON:
python scripts/compare_results.py results/load_test_node-*.json --save
```

### Métricas de agregação do cluster

| Métrica | Regra |
|---|---|
| Throughput | Soma (nós paralelos contribuem independentemente) |
| Latência média | Média ponderada pelo `total_published` de cada nó |
| Latência p99 | Máximo entre os nós (limite conservador) |
| Duração | Máximo (teste acaba quando o nó mais lento termina) |
| Msgs / Erros / Quedas | Soma |

---

## Payload MQTT (idêntico ao firmware real)

```json
{
  "accel_x": 0.0231,
  "accel_y": -0.0145,
  "accel_z": 1.0012,
  "magnitude": 1.0014,
  "fall_detected": false,
  "impact_magnitude": 0.0,
  "latitude": -2.5489,
  "longitude": -44.2029,
  "status": "normal",
  "device_id": "fall-sensor-000001"
}
```

---

## Configuração

`config/load_test_config.yaml`:

```yaml
load_test:
  n_devices: 1000
  requests_per_device: 100
  interval_ms: 100          # igual ao ESP32 real
  fall_probability: 0.05    # 5% dos devices terão 1 queda na sessão
  fall_mode: "session"      # "session" | "per_request"
  max_concurrent_connects: 5000
```

### Variáveis de ambiente

| Variável | Default | Descrição |
|---|---|---|
| `MQTT_QOS` | `1` | QoS MQTT: 0 (fire-and-forget) ou 1 (at-least-once) |
| `EXP_REQUESTS` | `1000` | Requests por device (modo distribuído) |
| `EXP_INTERVAL_MS` | `100` | Intervalo entre publicações em ms |
| `NET_DELAY` | `0ms` | Latência netem na eth0 do container |
| `NET_JITTER` | `0ms` | Jitter netem |
| `NET_LOSS` | `0%` | Perda de pacotes netem |
| `NODE_ID` | `local` | Identificador do nó nos relatórios |
| `DEVICE_OFFSET` | `0` | Índice inicial no device_tokens.json |
| `DEVICE_COUNT` | `333` | Quantos devices o nó processa |

---

## Estrutura

```
load-test-fall-detection-iot/
├── docker-compose.yml          # ThingsBoard + Postgres + 3 sensor-nodes
├── Dockerfile.sensor           # Imagem do nó sensor (inclui iproute2 para tc netem)
├── entrypoint.sh               # Aplica regras tc netem antes do load test
├── .env                        # Credenciais (não commitado)
├── requirements.txt
├── config/
│   └── load_test_config.yaml
├── scripts/
│   ├── provision_devices.py    # Cria devices via REST API
│   ├── load_test.py            # Motor MQTT (asyncio + QoS configurável + persistent session)
│   ├── compare_results.py      # Agrega e compara relatórios de múltiplos nós
│   ├── run_experiments.sh      # Orquestra experimentos A, B e C
│   ├── run_network_scenarios.sh # Executa load test em 3 cenários de rede (boa/media/ruim)
│   ├── dashboard_setup.py      # Cria dashboard com widgets
│   └── cleanup.py              # Remove devices de teste
├── monitoring/
│   ├── prometheus.yml
│   └── grafana/
│       ├── provisioning/
│       └── dashboards/
├── dashboard/
│   └── fall_detection_dashboard.json
└── results/
    ├── load_test_<node-id>_<timestamp>.json   # Relatório por nó
    ├── cluster_<timestamp>.json               # Relatório agregado do cluster
    ├── cenario_boa/                           # Resultados do cenário de rede boa
    ├── cenario_media/                         # Resultados do cenário de rede média
    └── cenario_ruim/                          # Resultados do cenário de rede ruim
```

---

## Baseline de referência

Execução local com asyncio direto no host:

| Métrica | Valor |
|---|---|
| Devices | 1 000 |
| Total mensagens | 1 000 000 |
| Throughput | 816,5 msg/s |
| Latência média | 14,6 ms |
| Latência p99 | 71,5 ms |
| Taxa de erro | 0 % |
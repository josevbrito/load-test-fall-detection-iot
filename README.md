# Fall Detection IoT — Load Test

Simula **1000 dispositivos ESP32** publicando telemetria via MQTT para um
ThingsBoard CE rodando localmente via Docker. Replica fielmente o comportamento
do firmware real (same payload, same state machine, same thresholds).

## Arquitetura

```
┌─────────────────────────────────────────────────────────────┐
│  Python asyncio (1000 coroutines)                           │
│  ├─ SensorSimulator (MPU6050 fake)                          │
│  │   ├─ accel_x/y/z com ruído gaussiano                     │
│  │   ├─ magnitude = sqrt(x²+y²+z²)                         │
│  │   ├─ state machine de queda (igual ao firmware real)     │
│  │   └─ GPS drift (UFMA São Luís, MA)                       │
│  └─ aiomqtt → MQTT :1883                                    │
│                        │                                    │
│  ┌─────────────────────▼────────────────────────────────┐   │
│  │  Docker: ThingsBoard CE (thingsboard/tb-postgres)    │   │
│  │  :8080 HTTP  |  :1883 MQTT  |  PostgreSQL interno    │   │
│  └──────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## Pré-requisitos

- Docker + Docker Compose
- Python 3.11+
- ~2 GB de RAM livres para o ThingsBoard

## Instalação

```bash
cd load-test-fall-detection-iot

# Instalar dependências Python
pip install -r requirements.txt
```

## Execução (ordem obrigatória)

### 1. Subir o ThingsBoard

```bash
docker compose up -d
```

Aguardar inicialização (~90 segundos). Verificar com:

```bash
docker compose logs -f thingsboard
# Pronto quando aparecer: "Started Application in X seconds"
```

### 2. Provisionar 1000 dispositivos

```bash
python scripts/provision_devices.py
```

Cria 1000 devices no ThingsBoard via REST API e salva os tokens em
`device_tokens.json`. Pode ser re-executado com segurança (idempotente).

### 3. Criar o dashboard

```bash
python scripts/dashboard_setup.py
```

Cria o dashboard com os widgets obrigatórios e salva o JSON em
`dashboard/fall_detection_dashboard.json`.

Acesse o dashboard em: **http://localhost:8080**
- Email: `tenant@thingsboard.org`
- Senha: `tenant2026`

### 4. Executar o load test

```bash
# Padrão: 1000 devices, 1000 requests cada
python scripts/load_test.py

# Customizado
python scripts/load_test.py --devices 100 --requests 500
```

O painel em tempo real mostra throughput, latência, quedas simuladas e erros.
O relatório JSON é salvo em `results/load_test_<timestamp>.json`.

### 5. Limpeza (opcional)

```bash
python scripts/cleanup.py
# ou sem confirmação:
python scripts/cleanup.py --yes
```

## Dashboard ThingsBoard

| Widget | Tipo | Dado |
|--------|------|------|
| Magnitude Atual | Value Card | `magnitude` (g) |
| Série Temporal — Magnitude | Time Series Chart | `magnitude` |
| Série Temporal — Eixos X/Y/Z | Time Series Chart | `accel_x`, `accel_y`, `accel_z` |
| Mapa de Localização | OpenStreetMap | `latitude`, `longitude` |
| Status de Queda | Value Card | `fall_detected` |

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
  "device_id": "fall-sensor-0001"
}
```

## Configuração

Edite `config/load_test_config.yaml`:

```yaml
load_test:
  n_devices: 1000          # número de devices simultâneos
  requests_per_device: 1000 # mensagens por device
  interval_ms: 100          # intervalo entre mensagens (igual ESP32 real)
  fall_probability: 0.03    # 3% de chance de queda por leitura
```

## Estrutura

```
load-test-fall-detection-iot/
├── docker-compose.yml          # ThingsBoard CE + PostgreSQL
├── .env                        # Credenciais
├── requirements.txt
├── config/
│   └── load_test_config.yaml
├── scripts/
│   ├── provision_devices.py    # Cria 1000 devices via REST API
│   ├── load_test.py            # Motor MQTT (1000 coroutines asyncio)
│   ├── dashboard_setup.py      # Cria dashboard com 5 widgets
│   └── cleanup.py              # Remove devices de teste
├── dashboard/
│   └── fall_detection_dashboard.json   # JSON exportável
└── results/
    └── load_test_<timestamp>.json      # Relatório gerado
```

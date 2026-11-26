# Council

Consejo de agentes que evalúa ideas de software de forma **neutral**: ni te felicita por todo ni te tira la idea abajo. Cuatro miembros con lentes y modelos distintos evalúan a ciegas y en paralelo, y un supervisor sintetiza un veredicto con matices. El resultado vuelve al agente de coding que consultó (Claude Code, OpenCode, Hermes…), que verifica la factibilidad en el repo real.

```
agente de coding --MCP--> consult_council
                            ├─ tecnico     (modelo A) ┐
                            ├─ producto    (modelo B) │ en paralelo, sin verse entre sí
                            ├─ riesgos     (modelo C) │
                            └─ constructor (modelo D) ┘
                                       ↓
                               supervisor: síntesis + veredicto
                                       ↓
              report + checks_for_coding_agent  →  el agente de coding verifica en el repo
```

## Instalación

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```

En `.env`, completa `OPENROUTER_API_KEY` y `COUNCIL_TOKEN` (mínimo 16 caracteres).

## Uso

Levanta el servidor y déjalo corriendo:

```powershell
.venv\Scripts\python -m council
```

Escucha en `http://127.0.0.1:8765/mcp` (MCP Streamable HTTP) y exige `Authorization: Bearer <COUNCIL_TOKEN>`.

### Conectar Claude Code

```powershell
claude mcp add --transport http council http://127.0.0.1:8765/mcp --header "Authorization: Bearer <COUNCIL_TOKEN>"
```

Una consulta tarda entre 1 y 3 minutos. Si el cliente corta antes, sube su timeout de tools (en Claude Code: `MCP_TOOL_TIMEOUT=300000`).

### Conectar OpenCode

En `opencode.json`:

```json
{
  "mcp": {
    "council": {
      "type": "remote",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer <COUNCIL_TOKEN>" }
    }
  }
}
```

Cualquier otro cliente MCP con soporte HTTP se conecta igual: URL más header.

## Tool MCP

| Tool | Parámetros | Devuelve |
|---|---|---|
| `consult_council` | `idea` (obligatorio), `project_context`, `session_id` | `session_id`, `report`, `members_responded`, `members_failed`, `cost_usd` |

- Primera consulta: sin `session_id`. La respuesta trae uno nuevo.
- Seguimiento: el mismo `session_id` más la información nueva en `idea` (respuestas a `open_questions`, o lo que encontraste en el código). Cada miembro recuerda su evaluación anterior y dice qué cambió.
- `report.verdict`: `viable`, `viable_con_cambios` o `no_viable_hoy`.
- `report.checks_for_coding_agent`: verificaciones concretas para hacer en el repo.
- Si responden menos miembros que el `quorum`, el tool devuelve error.
- `cost_usd` es el costo real de la consulta según OpenRouter. Con el stack por defecto ronda los 2 centavos.

## Costo y presupuesto

- Cada consulta suma su costo al gasto del día, que se guarda en `data/council.db`. El server también lo loguea.
- Cuando el gasto del día llega a `COUNCIL_DAILY_BUDGET_USD`, el tool responde "presupuesto diario agotado" y no llama a ningún modelo. El contador se renueva a las 00:00 UTC.
- Las consultas fallidas también cuentan, porque gastaron lo que alcanzaron a llamar.
- Las consultas que ya estaban en curso pueden pasar el tope, como mucho por el costo de una consulta cada una.

## Personalizar

Todo el comportamiento vive en `council.yaml` y `prompts/`; no hace falta tocar Python.

| Campo de `council.yaml` | Qué controla |
|---|---|
| `members[].name` | Nombre del miembro (minúsculas, `a-z0-9_`) |
| `members[].model` / `supervisor.model` | ID del modelo en [OpenRouter](https://openrouter.ai/models) |
| `temperature` | Temperatura del modelo (0 a 2) |
| `prompt` | Ruta al `.md` con la lente (dentro de `prompts/`) |
| `tools` | Tools del miembro; por ahora solo `package_info` |
| `memory_window` | Mensajes recientes que se conservan sin resumir (mínimo 2) |
| `structured_output` | `tool` (default: reintenta si la salida no valida) o `native` (salida estructurada del proveedor; necesaria para modelos que rechazan `tool_choice` forzado, como Claude Sonnet 5.5) |
| `member_base_prompt` | Prompt común a todos los miembros |
| `quorum` | Mínimo de miembros que deben responder |
| `summarizer_model` | Modelo barato que resume la memoria |

- Para agregar un miembro, suma una entrada en `members` y su `.md` en `prompts/members/` (máximo 8).
- Conviene que cada miembro use una familia de modelos distinta para que sus sesgos no se parezcan.
- La configuración se valida al arrancar: un campo desconocido, una ventana menor a 2 o un prompt fuera de `prompts/` impiden levantar el server.

### Tool `package_info`

Consulta PyPI o npm: si el paquete existe, su última versión, la fecha y la licencia. Sirve para que el consejo no dé por buena una librería inventada o abandonada. Solo habla con `pypi.org` y `registry.npmjs.org`.

## Memoria y sesiones

- Cada sesión es un hilo propio en `data/council.db` (SQLite). Cada agente guarda ahí su memoria, y las sesiones no se ven entre sí.
- Cuando la conversación supera `COUNCIL_SUMMARY_TRIGGER_TOKENS`, lo viejo se resume y se conservan los últimos `memory_window` mensajes.
- Las sesiones sin uso por `COUNCIL_SESSION_TTL_DAYS` se borran al arrancar. Si se supera `COUNCIL_MAX_SESSIONS`, se descartan las más viejas.

## Variables de entorno

| Variable | Default | Qué controla |
|---|---|---|
| `OPENROUTER_API_KEY` | (obligatoria) | Clave de OpenRouter |
| `COUNCIL_TOKEN` | (obligatoria) | Token que deben enviar los clientes |
| `COUNCIL_HOST` / `COUNCIL_PORT` | `127.0.0.1` / `8765` | Dónde escucha el server |
| `COUNCIL_MAX_CONCURRENT` | `4` | Consultas simultáneas en total |
| `COUNCIL_DAILY_BUDGET_USD` | `1.0` | Gasto máximo por día (UTC) en dólares; al llegar al tope, el tool rechaza nuevas consultas |
| `COUNCIL_MAX_SESSIONS` | `200` | Sesiones guardadas como máximo |
| `COUNCIL_SESSION_TTL_DAYS` | `30` | Días sin uso antes de borrar una sesión |
| `COUNCIL_MEMBER_TIMEOUT_S` | `180` | Tiempo máximo por miembro y para el supervisor |
| `COUNCIL_MODEL_TIMEOUT_S` | `120` | Timeout de cada llamada al modelo |
| `COUNCIL_MAX_OUTPUT_TOKENS` | `8000` | Tope de tokens de salida por llamada; sin él, OpenRouter reserva crédito para el máximo del modelo y rechaza con saldo bajo |
| `COUNCIL_SUMMARY_TRIGGER_TOKENS` | `6000` | Tokens de memoria a partir de los cuales se resume |
| `COUNCIL_MAX_IDEA_CHARS` / `COUNCIL_MAX_CONTEXT_CHARS` | `8000` / `30000` | Topes de tamaño de la entrada |
| `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_AGENT_ID` / `LANGSMITH_AGENT_ENVIRONMENT` | (apagado) | Trazas en LangSmith |

## Seguridad

- Escucha solo en localhost, exige token y trae protección contra DNS rebinding.
- Dos consultas a la misma sesión se ejecutan una después de la otra; distintas sesiones corren en paralelo hasta `COUNCIL_MAX_CONCURRENT`.
- La idea y el contexto llegan a los modelos delimitados como datos, y se les indica que no sigan instrucciones que vengan dentro.
- El reporte es consultivo: `checks_for_coding_agent` solo pide verificaciones de lectura. El agente de coding debe revisar cualquier comando antes de ejecutarlo, porque el contexto que mandó puede traer texto malicioso.
- Cada agente hace como máximo un número fijo de llamadas al modelo por consulta, así que un modelo que no logra cumplir el formato no repite indefinidamente.

### Dónde quedan tus datos

- La idea y el contexto de cada consulta se guardan completos en `data/council.db`, en tu máquina, hasta que la sesión pasa `COUNCIL_SESSION_TTL_DAYS` días sin uso.
- Se mandan a OpenRouter para que los modelos los evalúen y, si `LANGSMITH_TRACING` está activo, quedan en las trazas de LangSmith.
- No mandes claves, tokens ni datos personales dentro de `project_context`.

## Tests

```powershell
.venv\Scripts\python -m unittest
```

Usan modelos falsos y HTTP simulado: no gastan tokens ni necesitan red.

# Remote-Agent-First Architecture

This document describes the control-plane / data-plane architecture introduced in the remote-agent-first refactor.

## Architectural Overview

Intercept operates as a **distributed sensor platform** with two distinct planes:

```
┌──────────────────────────────────────────────────────────────────┐
│                     CONTROL PLANE (this host)                    │
│                                                                  │
│  ┌─────────────┐ ┌──────────────┐ ┌────────────────────────────┐│
│  │  Flask UI    │ │  Fleet API   │ │  Capability Aggregator     ││
│  │  (index.html)│ │  (/fleet/*)  │ │  (live fleet model)        ││
│  └──────┬───────┘ └──────┬───────┘ └────────────┬───────────────┘│
│         │                │                      │                │
│  ┌──────┴────────────────┴──────────────────────┴───────────────┐│
│  │  Resource Manager (lease/lock for exclusive SDR hardware)     ││
│  │  History Store   (master record for all captures)             ││
│  │  Task Scheduler  (route work to available agents)             ││
│  └──────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────┘
              ▲              ▲              ▲
              │ heartbeat /  │ push data /  │ command /
              │ capability   │ buffer sync  │ proxy
              │              │              │
┌─────────────┴──┐ ┌────────┴──────┐ ┌─────┴────────────┐
│  DATA PLANE    │ │  DATA PLANE   │ │  DATA PLANE      │
│  Agent 1       │ │  Agent 2      │ │  Agent 3         │
│                │ │               │ │                   │
│ [RTL-SDR]      │ │ [HackRF]      │ │ [Airspy + WiFi]  │
│ ADS-B, Pager,  │ │ SubGHz, ACARS │ │ Weather Sat,     │
│ 433MHz         │ │               │ │ Bluetooth, WiFi   │
└────────────────┘ └───────────────┘ └───────────────────┘
```

### Control Plane (Central Host)

The machine running the main Intercept instance acts as:

- **User interface** — presents the web UI
- **Agent registry** — manages agent registration and liveness
- **Capability aggregator** — merges agent capabilities into a live fleet model
- **Command router** — sends commands to the right agent
- **Task scheduler** — assigns work to available agents
- **Resource manager** — tracks exclusive hardware leases
- **Master data store** — owns the authoritative historical record

The control plane does **not** need local SDR hardware, GPS, Wi-Fi adapters, or Bluetooth adapters in the default `remote` deployment mode.

### Data Plane (Remote Agents)

Each remote agent:

- **Owns the SDR hardware** — performs local tuning and capture
- **Decodes locally** — demodulates and decodes signals at the edge
- **Ships derived telemetry** — sends small structured outputs to the control plane
- **Buffers during partitions** — holds data locally when connectivity is lost
- **Reports capabilities** — tells the control plane what it can do

## Key Modules

### `utils/capability_aggregator.py`

The **CapabilityAggregator** is the single source of truth for the UI. It:

1. Collects capability reports from all connected agents
2. Aggregates them into a fleet-wide capability model
3. Determines the state of each capability:
   - `available` — at least one online agent can perform this
   - `busy` — supported but all agents' resources are occupied
   - `offline` — supported by the fleet but all agents are offline
   - `unsupported` — no agent supports this at all

The frontend reads this model via `GET /fleet/capabilities` and renders only what's available.

### `utils/resource_manager.py`

The **ResourceManager** implements a lease-based model for exclusive SDR resources:

- Each physical device (tuner) is registered as a `Resource`
- Tasks acquire a `Lease` before using a device
- Leases have TTL, renewal, and expiry semantics
- Abandoned leases (agent failure) are automatically reclaimed

### `utils/history_store.py`

Provides the master historical record with:

- **intercept_history** — captures, decoded outputs, metadata
- **tasks** — job lifecycle tracking
- **agent_buffers** — store-and-forward for partition recovery

### `routes/fleet.py`

The Fleet API blueprint exposes:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/fleet/capabilities` | GET | Aggregated fleet capability model |
| `/fleet/capabilities/<mode>` | GET | Detail for a specific mode |
| `/fleet/capabilities/<mode>/select-agent` | GET | Auto-select best agent |
| `/fleet/resources` | GET | All registered hardware resources |
| `/fleet/leases` | GET/POST | Lease management |
| `/fleet/leases/<id>/renew` | POST | Renew a lease |
| `/fleet/leases/<id>` | DELETE | Release a lease |
| `/fleet/tasks` | GET/POST | Task lifecycle |
| `/fleet/tasks/<id>/state` | PUT | Update task state |
| `/fleet/history` | GET/POST | Historical intercept records |
| `/fleet/buffer/sync` | POST | Agent buffer sync (store-and-forward) |

## Configuration

### Deployment Mode

Set `INTERCEPT_DEPLOYMENT_MODE` environment variable:

- `remote` (default) — control-plane only, UI driven by fleet capabilities
- `local` — legacy single-box mode with static mode lists

### Fleet Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `INTERCEPT_DEPLOYMENT_MODE` | `remote` | Deployment mode |
| `INTERCEPT_AGENT_OFFLINE_THRESHOLD` | `120.0` | Seconds before agent is considered offline |
| `INTERCEPT_RESOURCE_LEASE_TTL` | `300.0` | Default lease TTL in seconds |

## Frontend Behaviour

### Remote Mode (default)

The frontend JavaScript module (`static/js/fleet.js`) polls `/fleet/capabilities` every 30 seconds and:

1. **Hides** modes with no supporting agent (state: `unsupported`)
2. **Disables** modes where all agents are offline (state: `offline`)
3. **Shows busy badge** for modes where all agents are occupied (state: `busy`)
4. **Shows normally** for available modes (state: `available`)
5. **Hides empty groups** when all modes in a group are hidden
6. **Updates fleet badge** showing online/total agent count

This approach keeps the UI honest without violent reshaping — the shell and layout remain stable, only individual mode buttons change state.

### Local Mode (legacy)

When `INTERCEPT_DEPLOYMENT_MODE=local`, the fleet manager still initializes but the static mode list is preserved, giving the same experience as a standalone SDR workstation.

## Data Flow

### Normal Operation

```
User clicks "Start ADS-B"
  → Frontend calls /fleet/capabilities/adsb/select-agent
  → Backend picks best available agent
  → Frontend calls /controller/agents/{id}/proxy/adsb/start
  → Agent starts dump1090, decodes locally
  → Agent ships decoded aircraft positions to control plane
  → Control plane stores in history, streams via SSE
```

### Partition Recovery

```
Agent loses network connectivity
  → Agent continues capturing, buffers data locally
  → Control plane marks agent as offline
  → UI shows ADS-B mode as "offline" (if last supporting agent)
  
Agent reconnects
  → Agent sends buffered data via POST /fleet/buffer/sync
  → Control plane stores in history, marks agent online
  → UI re-enables ADS-B mode
```

## Database Schema

The refactor adds three new tables to the SQLite database:

### `intercept_history`

Central master record for all intercept captures. Indexed by mode, agent, and timestamp.

### `tasks`

Tracks the lifecycle of work assigned to agents: pending → assigned → running → completed/failed.

### `agent_buffers`

Store-and-forward queue for data buffered during network partitions.

## Testing

```bash
# Run all fleet-related tests
pytest tests/test_capability_aggregator.py tests/test_resource_manager.py tests/test_fleet_api.py -v

# Run just capability aggregator tests
pytest tests/test_capability_aggregator.py -v

# Run resource manager tests
pytest tests/test_resource_manager.py -v

# Run fleet API endpoint tests
pytest tests/test_fleet_api.py -v
```

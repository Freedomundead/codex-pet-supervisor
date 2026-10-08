# Architecture

## Product principle

Timer mode is intentionally not a second Codex agent. It is a small supervisor around an existing Codex Desktop workflow.

```text
Codex Desktop
     ↑
     │ one continuation message after a verified reset
     │
Codex Pet Supervisor
     │
     ├─ allowance reader
     ├─ timer lifecycle
     ├─ local persistent state
     └─ Windows desktop dispatcher
```

## Stable Timer lifecycle

```text
ARMED
  │ allowance denied
  ▼
WAITING_RESET
  │ allowance available after reset
  ▼
DISPATCH ONCE
  ▼
SENT
  │ next real allowance denial
  └──────────────→ WAITING_RESET
```

The Pet does nothing while allowance remains available.

## Frozen core

Four files form the tested Timer core:

- `supervisor.py` — lifecycle orchestration.
- `rate_limits.py` — allowance normalization/decision.
- `app_server.py` — Codex App Server transport and discovery.
- `desktop_uia.py` — Windows desktop discovery/focus/dispatch.

`tests/test_frozen_timer_core.py` hashes these files to prevent accidental changes during UI-only work.

## UI

`ui.py` presents the Timer product and a separate Labs surface. UI work may evolve without changing the frozen core.

## Local persistence

`store.py` owns SQLite persistence for timer state, project/scope metadata, and experimental queue data.

## Labs

Managed Goal queues and adoption experiments are isolated conceptually from Timer mode. They are not required for the supported lifecycle.

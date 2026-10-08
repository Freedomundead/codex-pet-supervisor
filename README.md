# 🐾 Codex Pet Supervisor

> **Created by Freedomundead — my first vibe-coded open-source project.**

A small Windows companion for **Codex Desktop** that solves one very specific problem: when a long Codex task stops because your usage allowance is exhausted, the Pet waits for the reset and sends **one saved continuation message** into the Codex chat you already have open.

**Unofficial community project. Not affiliated with or endorsed by OpenAI.**

## Why this exists

Long-running work can hit a usage limit while you are away from the computer. The task itself may already know what it was doing; the missing part is simply returning at the reset time and saying “continue.”

Codex Pet Supervisor automates that small handoff:

```text
You work normally in Codex Desktop
        ↓
usage limit stops the task
        ↓
Pet detects the denied allowance
        ↓
Pet waits for the reset
        ↓
allowance becomes available
        ↓
Pet focuses the open Codex Desktop chat
        ↓
Pet sends your saved continuation message ONCE
        ↓
Codex continues normally
```

The Pet does **not** need to own your Codex task, replace Codex, or create a second writer for Timer mode.

## Stable feature: Auto Continue Timer

The supported workflow is intentionally small:

- Reads the Codex allowance state.
- Detects a real usage-limit stop.
- Records the reset time.
- Waits locally without spending work turns.
- Rechecks that allowance is actually available.
- Sends exactly one continuation message to the Codex Desktop chat you left open.
- Waits for the next real limit event.
- Persists timer state and the continuation message locally.

### Lifecycle

```text
ARMED
  │
  ├─ allowance available → do nothing
  │
  └─ allowance denied
        ↓
   WAITING_RESET
        ↓
   allowance returns
        ↓
   SEND ONCE
        ↓
      SENT
        ↓
   next real limit
        ↓
   WAITING_RESET
```

## Requirements

- Windows
- Python 3.11+
- Codex Desktop installed and signed in
- The Codex conversation you want continued should remain selected/open when the Pet dispatches

The current desktop interaction is Windows-specific.

## Install

Clone or download the repository, then from its root:

```powershell
python -m pip install -e .
```

Launch the UI:

```powershell
python -m codex_supervisor --db .\data\supervisor.db ui
```

You can also use:

```text
start-pet.cmd
start-pet-console.cmd
```

## First-run checklist

Before trusting a future reset:

1. Open Codex Desktop.
2. Select a harmless test chat.
3. Launch Codex Pet Supervisor.
4. Edit the continuation message if desired.
5. Click **Send Test Now**.
6. Confirm Codex Desktop comes to the foreground and receives the test message.
7. Click **Arm Timer**.

Do not skip the test-send step after installing a new release; Windows focus behavior can vary between systems and app versions.

## Continuation presets

The UI includes reusable templates such as:

- **Default** — continue without repeating completed work.
- **Finish the task** — preserve completed work and continue until genuinely complete or another limit stops execution.
- **Verify, then continue** — verify current state first, then proceed from the next unfinished step.
- **Minimal** — a short continuation prompt.
- **Custom** — write your own.

Presets only fill the editor. **Save Message** persists your chosen text.

## What is frozen

The timer release is built on a proven core. The repository contains a regression test that hashes the working lifecycle/dispatch files so UI-only changes cannot silently alter them.

Frozen core files:

- `codex_supervisor/supervisor.py`
- `codex_supervisor/desktop_uia.py`
- `codex_supervisor/rate_limits.py`
- `codex_supervisor/app_server.py`

Core lifecycle:

```text
LIMIT → WAIT → RESET → SEND ONCE → WATCH
```

Changes to this core should be deliberate, explained, and accompanied by lifecycle regression tests.

## 🧪 Labs

The **Labs** tab contains earlier experiments around managed Goals, project/scope queues, and adopting existing tasks.

These features are:

- experimental;
- not required for Timer mode;
- not the primary supported workflow;
- available for contributors who want to explore or evolve them.

Labs default to conservative settings. Please do not report a Timer bug based only on a Labs experiment.

## Privacy and local state

The Pet stores local runtime state in the SQLite database you provide, commonly:

```text
data/supervisor.db
```

The `data/` directory and database files are ignored by Git and should not be committed.

The Timer workflow interacts with your local Codex Desktop window. Review the code before using automation on a sensitive workstation.

## Development

Run the test suite:

```powershell
python -m pytest -q
```

See:

- [`CONTRIBUTING.md`](CONTRIBUTING.md)
- [`ROADMAP.md`](ROADMAP.md)
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)

## Contributing

Issues, bug reports, Windows compatibility findings, UX improvements, and pull requests are welcome.

The most useful contributions are currently around:

- reliable Windows foreground/focus behavior across Codex Desktop releases;
- system tray support;
- Windows notifications;
- start-with-Windows;
- optional keep-awake/wake behavior;
- better lifecycle/event history;
- automated tests for additional Windows versions.

If you change the frozen timer core, explain why the current invariant is insufficient and add regression coverage.

## License

MIT. See [`LICENSE`](LICENSE).

---

**Built by Freedomundead as a practical tool for a real Codex workflow, then opened up so other people can use it, break it, improve it, and grow it.**

# Development

## Environment

Primary target: Windows with Python 3.11+ and Codex Desktop installed.

Install editable:

```powershell
python -m pip install -e .
```

Run tests:

```powershell
python -m pytest -q
```

## Regression discipline

Before changing Timer behavior:

1. Reproduce the defect against the current working baseline.
2. Identify the exact failing layer: allowance, lifecycle, persistence, window discovery, foreground activation, composer targeting, or send.
3. Change the smallest justified scope.
4. Add a regression test.
5. Verify the entire suite.

Do not redesign the lifecycle to solve a UI problem.

## Manual release smoke test

1. Launch Codex Desktop and select a harmless chat.
2. Launch the Pet.
3. Verify Codex Desktop is detected.
4. Use **Send Test Now**.
5. Arm Timer while allowance is available; verify no message is sent.
6. Verify a real denied allowance enters `WAITING_RESET`.
7. Verify reset causes exactly one continuation send.
8. Verify the Pet waits for the next real limit.

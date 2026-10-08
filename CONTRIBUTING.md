# Contributing to Codex Pet Supervisor

Thanks for helping improve the Pet.

## Product boundary

The supported product is the **Auto Continue Timer**. The Labs tab is experimental.

The core invariant is:

```text
LIMIT → WAIT → RESET → SEND ONCE → WATCH
```

The following files are intentionally protected by regression hashes:

- `codex_supervisor/supervisor.py`
- `codex_supervisor/desktop_uia.py`
- `codex_supervisor/rate_limits.py`
- `codex_supervisor/app_server.py`

Do not modify them casually just to make a test pass. If a real defect requires a core change, document the defect, make the smallest justified change, and add lifecycle regression coverage.

## Setup

```powershell
python -m pip install -e .
python -m pytest -q
```

## Pull requests

A good PR should include:

1. The exact problem being solved.
2. Whether the change affects Timer, Labs, or UI only.
3. The smallest justified code change.
4. Tests that fail before and pass after the fix when practical.
5. Windows/Codex Desktop version details for desktop-automation bugs.

## UI/UX contributions

UI changes should preserve the working timer lifecycle. Prefer clear states, accessible controls, and fail-closed behavior over clever automation.

## Bug reports

For dispatch bugs, include:

- Windows version;
- Python version;
- Codex Desktop version if known;
- whether **Send Test Now** works;
- the exact error dialog/traceback;
- whether another app was foregrounded when the problem happened.

Do not post authentication tokens, account identifiers, private project contents, or local database files.

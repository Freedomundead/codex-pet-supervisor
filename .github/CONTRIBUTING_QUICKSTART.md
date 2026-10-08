# Quick contributor loop

```powershell
python -m pip install -e .
python -m pytest -q
python -m codex_supervisor --db .\data\supervisor.db ui
```

For Timer changes, always test **Send Test Now** before waiting for a quota reset.

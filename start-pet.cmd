@echo off
cd /d "%~dp0"
pythonw -m codex_supervisor --db "%~dp0data\supervisor.db" ui
if errorlevel 1 (
  echo Failed to start Codex Pet Supervisor UI.
  echo Try: python -m codex_supervisor --db "%~dp0data\supervisor.db" ui
  pause
)

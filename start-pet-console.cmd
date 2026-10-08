@echo off
cd /d "%~dp0"
python -m codex_supervisor --db "%~dp0data\supervisor.db" ui
pause

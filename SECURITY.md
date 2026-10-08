# Security

Codex Pet Supervisor performs local desktop automation on Windows. Treat it like any utility capable of focusing an application and sending keyboard input.

## Supported security reports

Please report issues involving:

- input being sent to the wrong window;
- unsafe focus/foreground behavior;
- unexpected network behavior;
- secrets or local state being exposed;
- path/scope escapes in Labs features.

Do not include real credentials, private repository content, account identifiers, or your local SQLite database in a public issue.

## Safe usage

- Use **Send Test Now** after installing a new version.
- Keep the intended Codex chat selected when relying on Timer dispatch.
- Review changes before running on sensitive systems.
- Keep `data/` and local database files out of source control.

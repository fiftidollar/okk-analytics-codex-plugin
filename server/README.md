# Standalone MCP gateway

FastAPI + MCP 1.27.2 service that bridges Codex OAuth to the normal OKK account
login and composes read-only statistics from the existing OKK HTTP API.

It owns a separate PostgreSQL schema for OAuth clients, grants, hashed MCP
tokens and encrypted upstream sessions. It does not import the OKK backend,
connect to the OKK database or know the OKK JWT signing secret.

Run migrations before starting:

```powershell
Set-Location server
python -m alembic -c alembic.ini upgrade head
uvicorn okk_mcp.main:app --host 0.0.0.0 --port 8020
```

See the root `README.md` and `docs/` for the full contract.

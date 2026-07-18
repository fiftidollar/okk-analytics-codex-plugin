# Contributing

1. Create a focused branch.
2. Preserve the 19-tool read-only and ACL contracts.
3. Add regression tests for every projection, auth or access-control change.
4. Run:

   ```powershell
   $env:PYTHONPATH = "server"
   python -m pytest
   python -m ruff check server
   python -m ruff format server --check
   python -m compileall -q server/okk_mcp server/scripts server/tests server/migrations
   Set-Location server
   python -m alembic -c alembic.ini upgrade head --sql
   ```

5. Never include credentials, live tokens, customer data or smoke exports in a
   commit. Security-sensitive issues should follow `SECURITY.md`.

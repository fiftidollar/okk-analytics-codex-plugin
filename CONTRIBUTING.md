# Contributing

1. Create a focused branch.
2. Preserve the 19-tool read-only and ACL contracts.
3. Add regression tests for every projection, auth or access-control change.
4. Run:

   ```powershell
   python -m pytest
   python -m compileall -q server/okk_mcp server/scripts server/tests server/migrations
   cd server
   python -m alembic -c alembic.ini upgrade head --sql
   ```

5. Never include credentials, live tokens, customer data or smoke exports in a
   commit. Security-sensitive issues should follow `SECURITY.md`.

# Production deployment and release gate

This runbook targets live production OKK. Do not point the published community
plugin at the test-stand API.

## Live production inventory

- Public repository: `https://github.com/fiftidollar/okk-analytics-codex-plugin`.
- Dokploy project/environment: OKK production, Compose `okk-analytics-mcp`
  (`4vZgCU2D0Jj7LBv2_DA2k`).
- Source: branch `main`, `docker-compose.dokploy.yml`.
- Public issuer/resource: `https://okk-mcp.akfixdev.ru` and
  `https://okk-mcp.akfixdev.ru/mcp`.
- Dedicated PostgreSQL and authenticated Redis are private Compose services;
  neither publishes a host port.
- Deployment authority is the latest `done` record for this Compose in Dokploy.
  Record the exact verified release commit in the platform operations inventory;
  do not infer live state from repository `main` alone.

## Required infrastructure

- HTTPS hostname for the gateway, currently planned as
  `https://okk-mcp.akfixdev.ru`.
- Separate PostgreSQL database/user for OAuth state.
- Redis for login throttling.
- Production OKK API:
  `https://okk-backend.akfixdev.ru/api/v1`.
- Independent high-entropy OAuth and session-encryption secrets.

Never reuse the OKK JWT signing secret; this gateway authenticates through the
public OKK login API and does not mint OKK tokens.

## Rollout order

1. Create the deployment environment from `.env.production.example`, replace
   every placeholder and keep `APP_ENV=production`.
2. Run `alembic -c alembic.ini upgrade head` from `server/`.
3. Deploy the container behind TLS and verify forwarded headers.
4. Verify health and both OAuth metadata documents.
5. Verify unauthenticated `/mcp` returns `401` with a
   `resource_metadata` challenge.
6. Complete Authorization Code + PKCE in a real Codex client.
   Open two authorization pages before submitting either one and verify both
   forms remain independently usable. Verify the gateway returns `302` to the
   exact loopback callback, Chrome does not report a `form-action` violation,
   and Codex reports a successful login with authenticated MCP state. Codex may
   also show a local browser completion page, but that page is not the release
   authority. Each form carries its own signed CSRF nonce and does not depend on
   cookies.
   Start a new task with `Проверить подключение OKK и показать мой доступ` and
   require `get_access_context.data.authenticated=true`, the explicit chat
   message `OKK подключён`, and only the test account's visible departments.
7. Run the read matrix against production with dedicated accounts that are safe
   for read-only verification:
   - admin;
   - viewer with one department;
   - viewer with several departments;
   - viewer with an empty ACL;
   - an admin with explicit private-supervisor grants;
   - an admin without private-supervisor grants;
   - deactivated user after an already issued MCP token.
8. Check direct inaccessible IDs return neutral `not_available`, and mixed
   filters expose only `omitted_filters_count`.
   For every named department in the test matrix, query by code and full name
   and assert `effective_scope` resolves to that department. For a
   one-department ORD viewer, request B2B and assert `not_available`, zero
   employee/statistics calls after resolution, and an `access_context` that
   names only ORD.
9. Exercise all 27 tools. For the 21 non-transcript tools, search saved JSON
   for forbidden fields/values: password, phone, audio, transcript, prompt,
   reasoning, script, Megafon, routing and pipeline. For the six transcript
   tools, verify text appears only under their documented transcript/preview/
   excerpt fields, while structured phone, audio, PBX/external IDs and internal
   processing fields remain absent. Test raw, diarized and segment formats,
   search caps, result limits and a call ID outside the viewer ACL.
   For `get_client_statistics` on B2B, also require the reset window `30`, the
   first/repeat counters and the reactivated-first subset from repeat state;
   assert that no phone or per-number transition row is present.
   For the private-supervisor matrix, require the granted account to receive
   only the live restricted catalog and dedicated call/transcript data. Require
   the ungranted admin to receive an empty catalog and `not_available` for a
   known supervisor UUID without any downstream `/calls` request. Confirm that
   no supervisor is merged into `list_employees` or a department ranking.
10. Validate refresh rotation, reuse revocation, logout/revoke and concurrent
    refresh behavior. Upgrade an account holding a pre-transcript token and
    prove that refresh cannot silently add `okk.transcripts.read`; after a fresh
    authorization, prove the new scope is present and transcript tools work.
11. Install the marketplace plugin and repeat the main user flows in both
    clients:
    - Codex: confirm installation itself starts OAuth (`ON_INSTALL`) and the
      loaded MCP configuration contains the exact production `oauth_resource`;
    - current Claude Code: add `fiftidollar/okk-analytics-codex-plugin`, install
      `okk-analytics@alpes-community`, authenticate through `/mcp`, run
      `/okk-analytics:check-connection`, and verify the role and department ACL;
      inspect the DCR response and require an absent `client_uri` to be omitted,
      never serialized as JSON `null`.
12. Inspect structured `okk_analytics_tool_call` logs. Confirm request IDs,
    timings, status and department code are present, while credentials, raw
    selectors, entity IDs, employee names and response payloads are absent.

## Dokploy production compose

The repository includes `docker-compose.dokploy.yml` for the production OKK
environment. It creates an isolated MCP gateway with dedicated PostgreSQL and
Redis services. Only the MCP container joins `dokploy-network`; the databases
stay on an internal Compose network and publish no host ports.

Create the Dokploy Compose from this repository on branch `main`, path
`docker-compose.dokploy.yml`, then set four independent high-entropy values:

- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `MCP_OAUTH_SECRET`
- `MCP_SESSION_ENCRYPTION_SECRET`

Traefik terminates TLS for `okk-mcp.akfixdev.ru` and forwards only to port
`8020`. The container trusts forwarded headers because that port is exposed
only on the ingress network; do not add a host `ports` mapping.

For the bundled Compose stack, copy the template to `.env.production` and use:

```powershell
docker compose --env-file .env.production up --build -d
```

## Local verification

```powershell
$env:PYTHONPATH = "server"
python -m pytest
python -m ruff check server
python -m ruff format server --check
python -m compileall -q server/okk_mcp server/scripts server/tests server/migrations
Set-Location server
python -m alembic -c alembic.ini heads
python -m alembic -c alembic.ini upgrade head --sql
Set-Location ..
$env:OKK_MCP_SMOKE_URL = "https://your-mcp-host"
# Optional dedicated test token; never put it in CLI arguments.
$env:OKK_MCP_SMOKE_ACCESS_TOKEN = "..."
python server/scripts/smoke_release.py --output artifacts/mcp-smoke.json
```

The historical `1.1.0` live gate passed TLS/health, OAuth and
protected-resource metadata, the unauthenticated MCP challenge, authenticated
`/auth/me` revalidation for a one-department ORD viewer, and the 22-tool runtime
transcript catalog/routing contract. The `1.2.0` release suite now requires the
27-tool inventory plus the granted/ungranted private-supervisor matrix above.
Live transcript-read smoke requires an account holding
`okk.transcripts.read`; a pre-`1.1.0` token cannot be upgraded through refresh
by design. Before each release, repeat the complete account/ACL matrix;
accounts outside the available smoke inventory are an explicit remaining
coverage item, not a reason to weaken live ACL checks.

Set `FORWARDED_ALLOW_IPS` only to the actual ingress proxy addresses. Using `*`
is acceptable only when the application port is unreachable except through an
ingress that overwrites client-IP headers; otherwise login IP throttling can be
spoofed.

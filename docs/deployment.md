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
- Current verified deployment: commit `ef75db5d962f6a03c2a40967943ca08ae6fd70e8`,
  status `done` on `2026-07-14`.

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
7. Run the read matrix against production with dedicated accounts that are safe
   for read-only verification:
   - admin;
   - viewer with one department;
   - viewer with several departments;
   - viewer with an empty ACL;
   - deactivated user after an already issued MCP token.
8. Check direct inaccessible IDs return neutral `not_available`, and mixed
   filters expose only `omitted_filters_count`.
9. Exercise all 19 tools and search saved JSON for forbidden fields/values:
   password, phone, audio, transcript, prompt, reasoning, script, Megafon,
   routing and pipeline.
10. Validate refresh rotation, reuse revocation, logout/revoke and concurrent
    refresh behavior.
11. Install the marketplace plugin and repeat the main user flows in Codex.

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
python -m pytest
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

The initial live gate passed TLS, migration, native production OAuth login, a
one-department viewer ACL, and all 19 tools. Before each release, repeat the
complete account/ACL matrix; accounts outside the available smoke inventory are
an explicit remaining coverage item, not a reason to weaken live ACL checks.

Set `FORWARDED_ALLOW_IPS` only to the actual ingress proxy addresses. Using `*`
is acceptable only when the application port is unreachable except through an
ingress that overwrites client-IP headers; otherwise login IP throttling can be
spoofed.

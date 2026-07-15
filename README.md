# OKK Analytics for Codex

Community plugin and standalone MCP gateway for read-only OKK analytics. A user
connects their own OKK account with the normal email/password login page. Codex
never receives the password, and every result is restricted by the account's
current role and department ACL.

The account must have a working local OKK password. An account provisioned only
through HR/SSO needs a local OKK password set by the normal account-management
process first; the plugin never authenticates through the HR platform.

This repository is independent of the OKK platform codebase: it uses the
existing HTTP API and owns only OAuth grants, encrypted upstream sessions and
the Codex plugin package.

## Included

- 19 strictly typed MCP tools for company, department, employee, call, client,
  plan/fact, CRM, growth, mentoring, scenario and criterion statistics.
- Full employee-card view available through the current OKK API: KPI, plan/fact,
  client and CRM metrics, AI strengths, growth areas, weekly/saved focus, active
  tasks and recent completed tasks.
- Scenario catalog and complete business criterion configuration without
  prompts or scripts, plus scenario/criterion performance aggregation.
- OAuth Authorization Code + PKCE S256, dynamic client registration, refresh
  rotation/reuse detection and revocation.
- Cookie-independent authorization form with a signed short-lived CSRF nonce,
  plus recovery for older in-flight forms while their OAuth request is valid.
- Live `/auth/me` verification on every MCP request.
- Admin/viewer/empty-ACL semantics and neutral inaccessible-ID responses.

## Deliberately excluded

Audio, transcripts, phone numbers, raw prompts, prompt runtime, raw AI
reasoning, scripts, Megafon administration, processing pipeline, routing, bulk
operations and every write action.

## Repository layout

```text
plugin/                  Codex plugin manifest, MCP declaration and skill
.agents/plugins/         Local/community marketplace manifest
server/okk_mcp/          OAuth server, encrypted sessions and 19 MCP tools
server/migrations/       Standalone PostgreSQL schema
server/tests/            Security, ACL, projection and tool-contract tests
docs/                    Architecture, tools, security and deployment runbooks
```

## Local run

1. Copy `.env.example` to `.env` and replace every secret/default password.
2. Set `OKK_API_BASE_URL` to the target OKK `/api/v1` URL.
3. Start the gateway:

   ```powershell
   docker compose up --build
   ```

4. Check:

   ```powershell
   Invoke-RestMethod http://localhost:8020/health
   Invoke-RestMethod http://localhost:8020/.well-known/oauth-authorization-server
   Invoke-RestMethod http://localhost:8020/.well-known/oauth-protected-resource/mcp
   ```

5. For a local marketplace checkout, update `plugin/.mcp.json` to your HTTPS
   gateway URL, then add this repository as a Codex marketplace and install
   `okk-analytics@alpes-community`.

## Install in Codex

Add the public marketplace once:

```powershell
codex plugin marketplace add fiftidollar/okk-analytics-codex-plugin
```

Restart Codex, open **Plugins**, choose **Alpes Community**, and install
**OKK Analytics**. In Codex CLI, open the same plugin browser with:

```text
codex
/plugins
```

Start a new task after installation so the skill and MCP server are loaded.
`scripts/install.ps1` remains an optional compatibility helper for managed or
older environments; cloning this repository is not part of the normal user
installation flow.

The production target is the live OKK API at
`https://okk-backend.akfixdev.ru/api/v1`; use `.env.production.example` as the
deployment template. Production requires HTTPS for both the MCP gateway and
the OKK API. See
[deployment](docs/deployment.md), [security](docs/security.md) and the complete
[tool catalog](docs/tool-catalog.md).

## Production rollout status

This is a live production plugin, not a test-stand connector. Its public MCP URL
is `https://okk-mcp.akfixdev.ru/mcp`, and its upstream is the production OKK API
above. Production OAuth, a one-department viewer ACL and all 19 read-only tools
were verified on `2026-07-14`; the broader role/ACL matrix remains a recurring
release gate described in `docs/deployment.md`.

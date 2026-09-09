# OKK Analytics for Codex and Claude Code

Community plugin and standalone MCP gateway for read-only OKK analytics. A user
connects their own OKK account with the normal email/password login page. Codex
and Claude Code never receive the password, and every result is restricted by
the account's current role, department ACL and explicit private-supervisor
grants.

The account must have a working local OKK password. An account provisioned only
through HR/SSO needs a local OKK password set by the normal account-management
process first; the plugin never authenticates through the HR platform.

This repository is independent of the OKK platform codebase: it uses the
existing HTTP API and owns only OAuth grants, encrypted upstream sessions and
the Codex/Claude Code plugin packages.

## Included

- 27 strictly typed MCP tools for company, department, employee, call, client,
  plan/fact, CRM, growth, mentoring, scenario and criterion statistics, plus
  an ACL-safe private Supervisors catalog, call metrics, transcript catalog,
  full transcript reading and full-text search.
- Full employee-card view available through the current OKK API: KPI, plan/fact,
  client and CRM metrics, AI strengths, growth areas, weekly/saved focus, active
  tasks and recent completed tasks.
- Client statistics include the B2B 30-day cycle: acting clients, new first
  touches, new repeat touches and the subset reactivated from repeat back into
  first touch after at least 30 days without a valid call. A number must enter
  repeat touch before that reset is possible. No phone rows are exposed.
- Scenario catalog and complete business criterion configuration without
  prompts or scripts, plus scenario/criterion performance aggregation.
- OAuth Authorization Code + PKCE S256, dynamic client registration, refresh
  rotation/reuse detection and revocation.
- Cookie-independent authorization form with a signed short-lived CSRF nonce,
  plus recovery for older in-flight forms while their OAuth request is valid.
  The gateway then redirects directly to the exact callback registered by
  the client; Codex or Claude Code owns the local listener, token exchange and
  completion state.
- Native remote-MCP packaging for both clients: Codex receives its explicit
  `oauth_resource`, while Claude Code uses standard HTTP MCP discovery and its
  `/mcp` OAuth flow.
- Live `/auth/me` verification on every MCP request.
- Admin/viewer/empty-ACL semantics and neutral inaccessible-ID responses.
- A separate `Руководители` contract driven by live explicit OKK grants. An
  admin without a grant receives an empty catalog; newly granted supervisors
  appear without a plugin release.
- Exact department selection by visible UUID, code or name. A failed named
  department never falls back to an unfiltered employee population.
- An authoritative live employee roster in every employee-bearing department
  response. Dashboard/ranking/plan rows are cross-checked by employee ID,
  foreign rows are removed, and any supplied name is normalized to the current
  OKK employee-directory name before the model sees it.
- Redacted operational tool traces with request ID, timing, applied department
  code, result status and completeness markers; raw business payloads, names,
  IDs and credentials are never logged.

## Deliberately excluded

Audio, structured phone-number fields, raw prompts, prompt runtime, raw AI
reasoning, scripts, Megafon administration, processing pipeline, routing, bulk
operations and every write action.

## Repository layout

```text
plugins/okk-analytics/   Shared skill plus Codex and Claude Code plugin manifests
.agents/plugins/         Codex community marketplace manifest
.claude-plugin/          Claude Code community marketplace manifest
server/okk_mcp/          OAuth server, encrypted sessions and 27 MCP tools
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

5. For a local marketplace checkout, update both
   `plugins/okk-analytics/.mcp.codex.json` and
   `plugins/okk-analytics/.mcp.json` to your HTTPS gateway URL, then install the
   **OKK Analytics** entry in the client being tested.

## Install in Codex

Add the public marketplace once:

```powershell
codex plugin marketplace add fiftidollar/okk-analytics-codex-plugin
```

Restart Codex, open **Plugins**, choose **Alpes Community**, and install
**OKK Analytics**. Installation immediately starts the normal Codex OAuth
window; enter the credentials on the OKK page and wait until Codex reports that
authentication is complete. The browser callback is owned by Codex, so the
gateway does not show a premature hosted «success» page.

Start a new task and choose the starter prompt **«Проверить подключение OKK и
показать мой доступ»**. The plugin calls the authenticated access check and
Codex should answer in this shape:

```text
OKK подключён.
Роль: viewer
Доступные отделы: ОРД
```

This chat response is the reliable confirmation: it proves token exchange and
live `/auth/me` verification both succeeded. If the browser shows a localhost
or `127.0.0.1` error, return to Codex and run the same check. If it returns the
role and departments, the login succeeded; if Codex shows **Authenticate**, run
authentication again from that fresh action. In Codex CLI, open the same plugin
browser with:

```text
codex
/plugins
```

Start a new task after installation so the skill and MCP server are loaded.
`scripts/install.ps1` remains an optional compatibility helper for managed or
older environments; cloning this repository is not part of the normal user
installation flow.

To pick up a newer published version, refresh the marketplace, restart Codex,
then open **Plugins → Alpes Community → OKK Analytics** and choose the offered
update/reinstall action:

```powershell
codex plugin marketplace upgrade alpes-community
```

To force a fresh account login later, use **Authenticate** in Codex or run
`codex mcp logout okk-analytics` followed by `codex mcp login okk-analytics`.
The `127.0.0.1:<port>/callback/<id>` URL is the standard temporary Codex
callback; the MCP gateway does not invent or host that address.

Release `1.1.0` adds the dedicated `okk.transcripts.read` scope. Users upgrading
from an older authenticated version must run the fresh-login action once before
the transcript tools can be used; refresh tokens intentionally cannot gain a
scope that was not granted during their original authorization.

Release `1.2.0` adds the private `Руководители` catalog and dedicated call and
transcript tools. It uses the existing statistics/transcript scopes, so an
already authenticated `1.1.0` connection needs only the plugin update and a
new task; access continues to come from live OKK grants.

Release `1.2.1` packages the registered ChatGPT MCP integration and the shared
`OKK Analytics` skill as one installable plugin. The technical
`plugin_asdk_app_...` URL remains a developer-mode MCP connection and is useful
for testing tool metadata, but it is not the public universal listing by
itself. Public ChatGPT Web and Codex discovery starts only after the
skills-plus-MCP submission is reviewed and explicitly published in the
universal Plugins Directory. Published skill content is a reviewed snapshot;
changing `SKILL.md` therefore requires a new plugin version and review.

Release `1.2.2` adds server-enforced employee roster grounding. Users must
refresh/update the installed plugin and start a new chat so the client loads
the new output schema and skill snapshot. A custom developer MCP connection is
cached separately from the marketplace package; reconnect or recreate that
connection after the gateway rollout if its tool list still shows fewer than
27 tools or lacks `department_ref` on department-scoped tools.

## Install in Claude Code

Use a current Claude Code release, then add the same public repository as a
marketplace and install the plugin for the current user:

```powershell
claude plugin marketplace add fiftidollar/okk-analytics-codex-plugin
claude plugin install okk-analytics@alpes-community --scope user
```

Start an interactive Claude Code session and reload the installed components:

```text
claude
/reload-plugins
/mcp
```

In `/mcp`, select the MCP server supplied by **OKK Analytics** (it can be shown
as `plugin:okk-analytics:okk-analytics`) and choose **Authenticate**. Enter the
credentials only on the hosted OKK page. After returning to Claude Code, run:

```text
/okk-analytics:check-connection
```

The command is successful only when Claude receives
`get_access_context.data.authenticated=true`; it then prints `OKK подключён`,
the account role and the live visible departments. If the browser callback
cannot return automatically, copy its complete URL and paste it into the URL
prompt shown by Claude Code. To re-authenticate later, use **Clear
authentication** and **Authenticate** in `/mcp`. To update the plugin, run:

```powershell
claude plugin marketplace update alpes-community
claude plugin update okk-analytics@alpes-community
```

After upgrading from a pre-`1.1.0` connection, clear authentication and
authenticate once so Claude receives the new transcript-read scope.

The production target is the live OKK API at
`https://okk-backend.akfixdev.ru/api/v1`; use `.env.production.example` as the
deployment template. Production requires HTTPS for both the MCP gateway and
the OKK API. See
[deployment](docs/deployment.md), [security](docs/security.md) and the complete
[tool catalog](docs/tool-catalog.md). Public policies are in
[PRIVACY.md](PRIVACY.md) and [TERMS.md](TERMS.md).

## ChatGPT Web publication

Create the release as **With MCP → Universal** with
`https://okk-mcp.akfixdev.ru/mcp`, and upload the final
`plugins/okk-analytics/skills/okk-analytics/` bundle to the same draft. The
portal must scan all 27 tools and retain the exact five positive and three
negative cases from `chatgpt-app-submission.json`. OAuth discovery advertises
`openid` and `email`; `/userinfo` returns only the live account subject and
verified email for workspace domain policy. Domain ownership is proved by
setting the portal-issued `OPENAI_APPS_CHALLENGE_TOKEN` in Dokploy; the
well-known route returns only that token.

## Production rollout status

This is a live production plugin, not a test-stand connector. Its public MCP URL
is `https://okk-mcp.akfixdev.ru/mcp`, and its upstream is the production OKK API
above. Release `1.2.2` is live from runtime commit `337002b` with 27 read-only
tools, including the ACL-safe private Supervisors catalog and its dedicated
call/transcript tools. On `2026-09-09`, the production Compose completed
successfully; health reported `1.2.2`, OAuth and protected-resource metadata
were available, unauthenticated MCP returned the expected `401`, and the full
authenticated release smoke passed under an account with three departments
and three explicitly granted supervisors. An unknown named department returned
neutral `not_available`. Department reports are now grounded to the complete
live employee roster: production DB, MCP output and a real ChatGPT Web report
matched on all 14 ORD employee UUID/name pairs and their monthly averages, with
zero foreign records. Existing pre-`1.1.0` sessions must re-authorize before
calling transcript tools.

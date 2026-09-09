# Production deployment and release gate

## 2026-09-09 release 1.2.3

The user explicitly approved production release in the current task. Runtime
commit `ce2d51d9fc9d83867f1517c09bf25ea5d4303a4e` was pushed to public main.
After waiting and twice checking that the previous deployment was still latest,
the documented Dokploy MCP `deploy_compose` fallback queued the deployment.
Dokploy browser records show this exact commit done (5 seconds); the MCP
container `11cf4e173e05` and the unchanged PostgreSQL/Redis containers are healthy.
Public health reports 1.2.3. OAuth metadata and the unauthenticated challenge
passed; authenticated ORD and B2B connector responses passed both release
grounding and report-semantics validators. A standalone protocol smoke token
was not available; authenticated evidence uses the installed connector.

A fresh ChatGPT Web chat with OKK selected produced ORD September and B2B August
reports with the verified roster, counts and scores. Both correctly distinguish
successful, duration-qualified and evaluated calls, exclude missed/no-answer
from successful totals, and explain that unset plans cannot yield completion
percentages. Browser chat: `https://chatgpt.com/c/6aa17a50-3fb0-83ec-9288-904ba8f950b6`.
Saved local evidence uses the `released-*` prefix under the artifact directory
below. Baseline 1.2.2 findings remain historical evidence, not current status.

Post-deploy adversarial checks also passed: B2B on 2020-01-01 retains unavailable
scores and explains the current/historical roster distinction; an insistence
that Ivanov Petr works in B2B triggers a fresh directory check and no invented
person or score. The private-supervisor request returns exactly Vorobyev Evgeny,
Donaeva Amina and Tetnev Artyom from the live restricted catalog, and explains
that quality scoring is unavailable in this transcription-only section.
All five saved post-deploy screenshots were manually opened and inspected;
DOM evidence retains full reports. The captured post-deploy browser console
is empty. Complete browser network tracing was not captured. The in-app browser
is still unauthenticated, so browser evidence uses the existing Chrome profile.

## 2026-09-09 reporting candidate and fresh baseline

Production health was independently verified as 1.2.2. A fresh ChatGPT Web
request initially required reconnecting OKK; after the normal reconnect,
`get_access_context` confirmed an admin with three departments and three
private supervisors. This proves current reconnection, not the cause of the
original invented-employee complaint.

Two browser requests were exercised: an open-ended ORD current-month report,
then a B2B August 2026 report in the same chat. Live MCP and read-only production
SQL matched every employee UUID/name, successful-call count and average score:
ORD 14 employees / 278 successful / 184 evaluated; B2B 2 / 205 / 126. Mismatches
were zero. Browser prose still called the successful population all employee
calls and the duration-qualified average an evaluated-call average. Unset plan
rows were null while MCP aggregate totals were fabricated zeroes.

The 1.2.3 candidate fixes the plan representation and supplies explicit metric
definitions in tool data plus matching MCP/skill guidance. Existing source
fields and all 27 tools remain available. Local evidence is under ignored
`artifacts/report-qa-2026-09-09/`; it includes browser screenshots/AX snapshots,
MCP comparison data and the read-only DB verification script and result.
The browser runs above tested production 1.2.2, not the unpublished candidate.
The candidate passed 128 tests, Ruff, compilation, package/skill validation,
Alembic offline SQL and replay of both saved live-source snapshots through the
changed adapter plus grounding/report-semantics smoke validators. Replay
preserved identities and metrics and returned null for unset plan totals.

Two additional production browser cases passed: an unknown department produced
no invented report, and B2B on 2020-01-01 returned unavailable scores while
explicitly distinguishing the current roster from historical membership.
All saved report screenshots were manually inspected. Public health, OAuth
metadata and the unauthenticated challenge passed the release smoke; its
authenticated protocol checks were skipped because no smoke token was supplied.
Business-data evidence above came from authenticated connector calls. Console
capture contains ChatGPT Russian-translation warnings; a complete network trace
was not captured in this baseline session.

For 1.2.3 release acceptance, run `validate_department_report_semantics` as part
of authenticated smoke, then repeat both ordinary browser prompts, no-data,
disputed-name and supervisor requests. Preserve screenshots, DOM, tool evidence
and console/network observations. Require correct populations in the prose,
null preservation, exact roster identity and no substituted people. A passing
local suite alone does not close this post-deploy browser gate.

Dokploy MCP deployment listing for Compose currently returned a validation error
requiring applicationId, and SSH tools could not open their configured key.
Direct DB and the local proxy were unavailable. The existing Dokploy browser
terminal supplied read-only SQL evidence; no deploy/worker control or write SQL
was performed. Diagnose these independently of report correctness.

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
- During universal-directory submission only, the exact
  `OPENAI_APPS_CHALLENGE_TOKEN` issued by the OpenAI Platform portal.

Never reuse the OKK JWT signing secret; this gateway authenticates through the
public OKK login API and does not mint OKK tokens.

## Rollout order

1. Create the deployment environment from `.env.production.example`, replace
   every placeholder and keep `APP_ENV=production`.
2. Run `alembic -c alembic.ini upgrade head` from `server/`.
3. Deploy the container behind TLS and verify forwarded headers.
4. Verify health and both OAuth metadata documents. Require `openid` and
   `email` in `scopes_supported`, and require the advertised
   `userinfo_endpoint` to use the same HTTPS issuer origin.
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
   For every employee-bearing department response, require an authoritative
   `employee_roster_grounding` whose department equals `effective_scope`.
   Assert every ranking/trend/plan row has a canonical ID/name from that
   allowlist. Inject one foreign ID and one conflicting name in the adapter
   regression: the former must be absent, the latter must be normalized, and
   the result must be `partial` with non-zero diagnostic counters.
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
13. For a universal Plugins Directory submission, configure the portal-issued
    `OPENAI_APPS_CHALLENGE_TOKEN`, verify
    `/.well-known/openai-apps-challenge` returns exactly that token as plain
    text, and verify `/userinfo` with a newly authorized `openid email` token
    returns only `sub`, normalized `email` and `email_verified=true`. Upload the
    final skill tree, import `chatgpt-app-submission.json`, scan all 27 tools,
    and review the five positive and three negative cases before submission.

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

The historical `1.1.0` live gate passed the 22-tool transcript contract. The
`1.2.0` production rollout on `2026-09-09` deployed commit `f30f9d6`; health
reported `1.2.0`, OAuth metadata and the unauthenticated challenge passed, and
an authenticated MCP release smoke returned the exact 27-tool inventory. The
live account context returned role `admin`, three departments and three
explicitly granted private supervisors; an unknown named department returned
neutral `not_available`. The automated suite separately proves that an admin
without a supervisor grant receives no supervisor data and triggers no
downstream `/calls` request. A pre-`1.1.0` token cannot gain
`okk.transcripts.read` through refresh by design. Before each release, repeat
the complete account/ACL matrix; accounts outside the available smoke inventory
are an explicit coverage item, not a reason to weaken live ACL checks.

The `1.2.2` production rollout on `2026-09-09` deployed commit `337002b` and
added server-enforced employee-roster grounding. Health reported `1.2.2`; the
unauthenticated release smoke passed, and an authenticated `ord` department
read returned one complete authoritative roster with 14 active employees,
zero excluded or normalized source records, 184 evaluated calls and an 81.3
average score. A separate strict-read-only query executed inside the production
backend container returned the same department UUID, the same 14 employee
UUID/name pairs, the same per-employee averages and the same summary values.
A real ChatGPT Web run through the published OKK Analytics integration then
rendered exactly those 14 names and scores, with no foreign or invented person.
In the same chat, an adversarial follow-up asserted that two invented people
belonged to ORD and demanded fabricated averages. ChatGPT rechecked the live
directory, stated that neither person existed (including inactive employees),
kept the authoritative count at 14 and did not manufacture a score.

Set `FORWARDED_ALLOW_IPS` only to the actual ingress proxy addresses. Using `*`
is acceptable only when the application port is unreachable except through an
ingress that overwrites client-IP headers; otherwise login IP throttling can be
spoofed.

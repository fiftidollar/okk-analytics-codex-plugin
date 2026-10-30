# Production deployment and release gate

## 1.6.0 — released 2026-10-27

Deployed to the production Compose on 2026-10-27 14:26 MSK (PR #15,
`87cb037`). After the deployment: `/health` reported `1.6.0`, `/ready`
reported `ready`, the authorization server metadata advertises
`authorization_response_iss_parameter_supported: true` and
`client_id_metadata_document_supported: false`. CIMD stays off: from the
production server Claude's document answered Cloudflare 302 and ChatGPT's 403.
Not tested yet: a signed-in client (the person signs in once; then tools/list,
the step-up error and the call limits are checked live). The eval suite has
only had a dry run; a paid run needs the owner's approval.

Eval run on production, 2026-10-27 (owner approved one client, one repeat):
Codex 0.154, reasoning medium, signed in as an administrator; 37 of 39 cases
ready (2 skipped by their preconditions). First grade 31/37; every one of the 6
failures was the grader, not the agent: numbers more precise than the rounded
truth (90,72 for 90.7; 84,15 % for 84.146 %), a refusal worded «запрещена», and
the connection check `get_access_context` the instructions ask for before a
refusal. After fixing the grader the recorded answers grade 37/37 — no wrong
number, no invented person or data, every refusal correct. 2.08 tool calls per
question, 0 tool errors, 15 minutes, 3.87 M input tokens (2.94 M cached), 11 k
output tokens. The marketplace install was checked in clean configurations:
Codex adds the marketplace from `fiftidollar/okk-analytics-codex-plugin` and
installs `okk-analytics@alpes-community` 1.6.1; Claude Code 2.1.118 needs the
full HTTPS address (the short form clones over SSH), then installs 1.6.1.

## 1.5.0 — released 2026-10-27

Deployed to the production Compose on 2026-10-27 13:10 MSK (PR #13,
`7890993`), right after the OKK platform release (alpes-it/Akfix-OKK-platform
PR #273, production 12:57–12:58 MSK). After the deployment: `/health` reported
`1.5.0`, `/ready` reported `ready`, the 401 on `/mcp` carried `scope` and
`resource_metadata`, and the public release smoke passed. The OKK side was
checked read-only with the new production code: «Оценено» of csm for October
in the journal (`kpi_evaluated`) 82 = the KPI 82; transcript search «до
свидания» 120 calls, the phrase in all 20 rows checked; 200 rows of two pages
without repeats; the call card names its department. Not tested yet: an
authenticated client against the production gateway (tools/list of 26 tools
and the new filters) — it needs a person to sign in.

Order: the OKK platform first, then the gateway. The gateway needs from OKK
`POST /calls/transcript-search`, the journal filter `kpi_evaluated`, the
journal order by start time and call id, `department_id` in the call card and
`step=auto` on the trend routes (the last one is on production since
2026-10-23). Until OKK serves the search route, both search tools answer
`unavailable` (OKK 404); an OKK without `kpi_evaluated` would ignore it and
count every scored call, as 1.4.0 did — so never deploy the gateway first.

Release gate: `/health` reports `1.5.0`; tools/list has 26 tools; on the stand
`search_call_transcripts` over «Всё время» answers `ok` with a
`matching_calls_total` equal to the OKK journal filtered by the same text;
`list_call_transcripts` with `filters.quality=below_norm` returns as many calls
as the dashboard «Требуют внимания» for the same period; the scenario
evaluations of a one-scenario department equal its `calls_evaluated`.

## 1.4.0 — released 2026-10-21

Deployed to the production Compose on 2026-10-21 (PR #10, `260c7ea`). After the
deployment: `/health` reported `1.4.0`, `/ready` reported `ready`, the 401 on
`/mcp` carried `scope` and `resource_metadata` without an error code, and the
public release smoke passed. The authenticated smoke passed against the same
code on a local gateway with the test stand; a signed-in client against the
production gateway was not tested.

Notes written before the release:

No migration. Tool contract changes (removed tools, `department` parameter,
criterion answer shape): clients reload the tool list in a new chat; a cached
1.3 tool call with `department_ref` fails validation instead of answering for
the wrong scope.

Evidence, 2026-10-21, local gateways on the OKK test stand (admin account of
the okk_info kit), Codex desktop CLI 0.163 with gpt-6.1-sol, reasoning
`medium`, no plugin skill (the MCP server alone), 36 realistic Russian
questions with checkable answers (periods, departments, people, transcripts,
plans, criteria, CRM, phone history, refusals):

| | 1.3.2 | 1.4.0 |
|---|---|---|
| Correct answers | 35/36 | 36/36 |
| Tool calls | 88 | 77 |
| Characters of tool results read | 251,998 | 210,221 |
| Input tokens | 4.03 M | 4.22 M |

- The 1.3.2 failure was a gateway defect: «сколько раз сработали стоп-слова»
  answered 13 (every observation of a critical criterion counted); 1.4.0
  answers 1 of 13 — confirmed in 3 of 3 repeats.
- On the first 1.4.0 run the answer to «вот мой пароль» did not warn about
  sharing passwords; the server instructions got that rule and 3 of 3
  repeats warned.
- Input tokens are within run-to-run noise: Codex resends its own ~30k-token
  context on every model turn, so the number of turns dominates. Fewer calls
  and less data are the measured gain.
- Codex OAuth end to end against the 1.4.0 gateway (`codex mcp login
  --no-browser`, the page filled by a script): registration, the login page
  with both checkboxes, the loopback callback and a tool call with the stored
  token. A sign-in with the transcripts box unchecked got a token without
  `okk.transcripts.read`; `list_call_transcripts` then answered with the
  reconnect message, statistics still worked.
- Not tested: Claude Code (its CLI login on the test machine was revoked) and
  ChatGPT.

Release gate: `/health` reports `1.4.0`; tools/list has 25 tools; the 401 on
`/mcp` carries `scope`; the login page shows the two checkboxes.

## 1.3.2 — released 2026-10-21

Deployed to the production Compose on 2026-10-21 (PR #8, `6351f97`). After the
deployment: `/health` reported `1.3.2`, `/ready` reported `ready`, the
authorization metadata lists `revocation_endpoint_auth_methods_supported`,
`/mcp` without a token answered 401, registration with a foreign path on
claude.ai answered 400, the gateway database is at `20261021_01` (10 of 55
clients marked as in use; the other 45 have no tokens or codes and are removed
by maintenance as abandoned — a client that still holds such an id signs in
again). Before the release, Codex (desktop CLI 0.163, gpt-6.1-sol) answered
«average score of Сопровождение in September 2026» through a local 1.3.2
gateway on the test stand by passing only `start_date`/`end_date` — the call
1.3.1 answered with the current month — and got the September value (90).
A signed-in client against the production gateway was not tested.

Notes written before the release:

Fixes from the 2026-10-20 review against the MCP specification (revision
2026-07-28; the SDK serves 2025-11-25), Claude Code and Codex guidance.
Migration `20261021_01` adds two nullable columns; 1.3.1 runs against the new
schema, so a rollback is the previous image without a downgrade.

Verified locally on 2026-10-21: 273 tests (Ruff lint and format), including
9 on a real PostgreSQL 17: refresh retried after a lost answer and two
parallel refreshes get the same pair; reuse after the window or after the new
token was used revokes the family; an expired OKK token is renewed once for 5
parallel requests; 4 parallel requests with a 0.4 s OKK answer finish under
1 s (not serialized); an OKK outage keeps the MCP token valid; maintenance
deletes abandoned registrations and old retry pairs; the migration marks
clients in use and rolls back. The tests of the lost answer, parallel refresh,
parallel requests and OKK outage fail on 1.3.1 code. Tool calls through the
MCP server: dates alone reach OKK as a custom range, conflicting or non-ISO
dates are errors. Not verified before release: sign-in and tool calls from
Claude Code and Codex against the deployed gateway.

Release gate:

- `/health` reports `1.3.2`, `/ready` is ready, OAuth metadata is served,
  `/mcp` without a token answers 401.
- After the release, a signed-in client asks for a past month by dates and
  gets that month (`period.start`/`period.end` in the answer).

## 1.3.1 — released 2026-10-20

Deployed to the production Compose on 2026-10-20 (PR #5, `ae4c18d`). After the
deployment: `/health` reported `1.3.1`, `/ready` reported `ready`, the OAuth
protected-resource metadata was served, and `/mcp` without a token answered 401.
A tool call from a signed-in client was not made by the release author.


Fixes from a read-only check of 1.3.0 against the OKK test stand; no new
upstream endpoint, OAuth scope or tool. Verified locally on 2026-10-20: the
full test suite, Ruff lint and format, and a replay of saved read-only
test-stand responses (CSM, 01.10–20.10) through the changed adapter:
`get_scenario_performance` gives `csm_universal` 13 evaluations, 89.0 and
76.9% (8 on the current version, 5 on the archived one), as OKK shows;
`get_criterion_performance` gives 143 observations over 11 criteria for an
admin, and for a CSM viewer 88 observations with 5 evaluations / 55
observations reported as excluded (`partial`). Not verified: a deployed
gateway.

Release gate in addition to the steps below:

- On the stand period with an archived scenario version, scenario and
  criterion statistics equal the OKK numbers for admin and viewer accounts;
  the viewer's result is `partial` with `criteria_version_not_readable`.
- `get_employee_card` with `period=week` shows the same `period_summary`
  dates as the OKK employee page for Monday–today.
- The authenticated release smoke passes the 28-tool inventory.

## 1.3.0 release (2026-10-20)

Public `/health` reported `1.3.0` on 2026-10-20. The candidate notes below are
kept as they were written before the release.

Prerequisite: the OKK platform this gateway reads must already serve
`GET /call-plans/plan-fact` (the plan constructor). Against an older platform
the gateway still works, but every plan section is `not_available`. The old
`/plans/*` API is removed from the platform only after this gateway is live
(expand → migrate → contract).

Verified locally on 2026-10-14: 157 tests, Ruff lint and format;
`claude plugin validate` for the marketplace and the plugin; installation of
the package into isolated homes of Codex CLI 0.154.0 and Claude Code 2.1.118
(each client registers exactly one server from its own file). Not verified:
OAuth login and tool calls against a live gateway in Codex, Claude Code and
ChatGPT.

Release gate in addition to the steps below:

- `MCP_OAUTH_REDIRECT_URIS` keeps its default unless another web client must
  connect; add its exact callback. Before release, check that fresh ChatGPT,
  Codex and Claude Code connections register successfully; a refused
  registration answers `invalid_redirect_uri`.
- On the login page, the return destination reads «в приложение на этом
  компьютере» for Codex/Claude Code and the exact callback for a web client.
- A department report and `get_plan_fact_statistics` return plan items whose
  plan and fact equal the OKK «Планы» page for the same period.
- Bumping the version makes clients reload the skill: update the plugin and
  start a new chat.

## 2026-09-24 production release 1.2.4

The gateway fixes direct transcript reads where the upstream call-detail
employee has `department_id` but no nested `department`. Long transcripts can
be read through `start_char` / `next_start_char` chunks. The new
`list_call_phone_records` tool requires `okk.phones.read` and the platform
read-only `POST /calls/phone-lookup` exact indexed filter with the number in
the JSON body, plus the normalized phone field
in each call-list item. The platform contract is live on test-stand and
production. Fresh OAuth authorization is required for the new scope.

Platform test-stand PR122 reached all four Dokploy apps at `c40c8a6c` on
2026-09-24. Live OpenAPI exposes POST JSON body lookup and no GET phone query;
authenticated empty/no-match requests return `200`, `total=0`,
`Cache-Control: private, no-store`, and malformed input returns `422`. The
first GET version in PR119 was superseded because nginx logs URLs. The stand
has no calls, so it cannot prove a positive live count or direct transcript
read. Focused platform tests and local gateway adapter tests cover those cases.
Production platform PR120 merged as `a4c5041b846602bd88bc0919f06dae033e93937d`;
all five apps auto-deployed that exact commit with status `done`. The stand's
focused API smoke retains
five pre-existing V39 scorecard fingerprint mismatches; its 19 headless visual
screenshots were manually inspected and have no UI/API/console failures, with
one no-data employee warning.
Live production MCP 1.2.3 roster probes on all three visible departments
(B2B, CSM, ORD) returned complete authoritative rosters of 3, 3, and 13;
ranking rows outside those rosters or with conflicting names were zero.
Unknown-name employee and private-supervisor searches both returned no data.
This verifies the sampled tool outputs. After rollout, direct production MCP
`get_call_transcript` by call ID returned `13,778` of `13,778` characters,
`truncated=false`, for a live ORD call that previously returned `not_available`.
The production API lookup returned `total=5` with `page_size=1`; all five unique
matching records were retrieved through pagination. No-match, malformed input,
and cache behavior also passed. This already-open Codex task retains the old
tool inventory and OAuth grant, so the new phone tool and fresh browser prose
remain to be exercised after reconnecting with `okk.phones.read`.
Post-rollout MCP roster reads still report authoritative B2B/CSM/ORD employee
counts of `3/3/13`, with live-directory IDs and names. This checks the tool
data boundary; it does not guarantee every future free-form answer.

Gateway PR1 merged as `89ab83e2fa417ca5daa919b3c4095a2ec01624de`.
No automatic Compose deployment appeared after the wait; one documented
Dokploy MCP `deploy_compose` fallback was used. Public `/health` reports
`1.2.4` and `/ready` reports ready. The Dokploy Compose deployment-list tool
rejects its compose ID and container SSH cannot open its configured key, so
the exact running SHA could not be independently read from Dokploy.

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
- Dokploy project/environment: «OKK (Grand Briar)» production, Compose
  `okk-analytics-mcp` (`xSJhXno3g6xrjYavLSTa-`, stack `okk-analytics-mcp-gb-cqekoa`
  on `31.128.41.185` since 2026-10-14; the old `4vZgCU2D0Jj7LBv2_DA2k` is retired).
- Source: branch `main`, `docker-compose.dokploy.yml`. Auto-deploy is off: a push
  to `main` does not deploy; the release deploys the Compose explicitly.
- **1.3.0 needs the new OKK platform.** Its plan/fact reads `/call-plans/plan-fact`,
  which the old production API lacks. Deploy 1.3.0 only after OKK production is
  switched to the new architecture (step 8 of the switch window in the platform
  `docs/RELEASE_FLOW_RUNBOOK.md`); deployed earlier, plan sections become
  `not_available`.
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
- Redis for pending sign-ins and connection throttling.
- Production OKK API:
  `https://okk-backend.akfixdev.ru/api/v1`.
- Independent high-entropy OAuth and session-encryption secrets.
- During universal-directory submission only, the exact
  `OPENAI_APPS_CHALLENGE_TOKEN` issued by the OpenAI Platform portal.

Never reuse the OKK JWT signing secret; this gateway exchanges a one-time OKK
sign-in code for an OKK session and does not mint OKK tokens.

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
9. Exercise all 26 tools (`server/scripts/smoke_release.py` checks the exact
   inventory). For the 19 tools that are neither transcript tools nor
   `list_call_phone_records`, search saved JSON
   for forbidden fields/values: password, phone, audio, transcript, prompt,
   reasoning, script, Megafon, routing and pipeline. The phone tool returns
   numbers only under `okk.phones.read`. For the six transcript
   tools, verify text appears only under their documented transcript/preview/
   excerpt fields, while structured phone, audio, PBX/external IDs and internal
   processing fields remain absent. Test raw, diarized and segment formats,
   the exact search count and its pages, and a call ID outside the viewer ACL.
   For `get_client_statistics` on B2B, also require the reset window `30`, the
   first/repeat counters and the reactivated-first subset from repeat state;
   assert that no phone or per-number transition row is present.
   For the private-supervisor matrix, require the granted account to receive
   only the live restricted catalog and dedicated call/transcript data. Require
   the ungranted admin to receive an empty catalog and `not_available` for a
   known supervisor UUID without any downstream `/calls` request. Confirm that
   no supervisor is merged into `list_employees` or a department ranking.
10. Validate refresh rotation, reuse revocation, authorization-code replay
    revocation, logout/revoke and concurrent refresh behavior. Upgrade an account holding a pre-transcript token and
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
    final skill tree, import `chatgpt-app-submission.json`, scan all 26 tools,
    and review the five positive and three negative cases before submission.

## Dokploy production compose

The repository includes `docker-compose.dokploy.yml` for the production OKK
environment. It creates an isolated MCP gateway with dedicated PostgreSQL and
Redis services. Only the MCP container joins `dokploy-network`; the databases
stay on an internal Compose network and publish no host ports.

Create the Dokploy Compose from this repository on branch `main`, path
`docker-compose.dokploy.yml`, then set five independent high-entropy values:

- `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `MCP_OAUTH_SECRET`
- `MCP_SESSION_ENCRYPTION_SECRET`
- `OKK_MCP_CLIENT_SECRET` — the same value as `MCP_GATEWAY_CLIENT_SECRET` of the
  OKK backend, which also lists `https://okk-mcp.akfixdev.ru/oauth/okk/callback`
  in `MCP_GATEWAY_REDIRECT_URIS`. Without it the gateway does not start.

Sign-in through the OKK page (branch `claude/mcp-okk-login`) needs OKK
`/auth/mcp/authorize` and `/auth/mcp/token`: release OKK first, then the
gateway. The released 1.6.x gateway works with that OKK release for
administrators and supervisors (checked in Docker on 2026-10-30), refuses OKK
employees (it does not know the role) and is refused by OKK while the company's
MCP module is off.

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

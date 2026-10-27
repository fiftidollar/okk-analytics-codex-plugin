# OKK Analytics MCP eval suite

A repeatable check of how well a real agent answers OKK questions through the gateway: it asks
Codex or Claude Code the questions in `cases.json`, records which tools were called and the final
answer, and grades the answer against values read from OKK **at run time with the same account**.

- `cases.json` — 39 cases: 36 analytics and safety questions plus the 3 negative cases from
  `docs/submission-test-cases.md`. 8 cases (about 20%) are `held_out`: do not tune tool
  descriptions or instructions on them; they show over-fitting.
- `run.py` — the runner (`--dry-run`, `--client codex|claude`, `--repeat N`, `--baseline`).
- `get_token.py` — signs in to a gateway through its OAuth page and saves refreshable tokens.
- `fixtures/synthetic.json` — made-up OKK reads for the dry run and the test.
- `out/` — plans, runs and reports (ignored by Git: they hold real names and numbers).

The repo is public. Cases hold no names, phone numbers, call or department ids, transcript text or
stand credentials: placeholders such as `{department_name}`, `{employee_card_name}`, `{call_id}`,
`{phone}` are resolved from OKK reads, and every expected value is a path into a read (for example
`get_department_statistics(department, period=month)` → `data.summary.average_score`). The test
`server/tests/test_evals_suite.py` refuses ids and phone numbers in `cases.json`.

## A case

| Field | Meaning |
|---|---|
| `prompt` | The Russian question, with placeholders. |
| `tools` | `any` — at least one listed tool; `all`; `ordered` — in this order; `none` — no OKK call (negative cases); `free` — not graded. |
| `args_any` | Optional: some call must use one of these argument sets (for example the previous month as `period` or as dates). |
| `truth.reads` | OKK reads made through the gateway's own tools, without a model. |
| `truth.preconditions` | When the live data does not fit (a plan is set, CRM is available), the case is `skipped` with the reason. |
| `truth.expect` | `number` (any rounding to 0–2 decimals; `0` also accepts «ни одного»), `names` (every surname), `names_exact` (every surname and no other person from `universe`), `text`, `date` (Moscow time), `duration` (seconds or minutes and seconds). `any_of` accepts one of several fields. |
| `answer.any` / `answer.none` | Phrases for refusals and «no data»: at least one / none of them. |

A run passes when the answer is complete, every expectation and phrase check holds, the tool check
holds and, if given, the arguments check holds.

## Dry run (free)

```
python evals/run.py --dry-run
```

Validates `cases.json`, resolves every placeholder and expected value against
`fixtures/synthetic.json`, prints the plan, writes it to `out/dry-run-*/plan.json` and checks the
graders: an ideal answer must pass and an empty one must fail. No model and no network.

To check the cases against live data before paying for a run (reads only, still no model):

```
python evals/run.py --dry-run --truth-from gateway --gateway-url http://localhost:8020/mcp --token-file evals/out/token-stand.json --record-fixture evals/out/stand-fixture.json
```

`--record-fixture` saves those reads, so later dry runs can use `--fixture evals/out/stand-fixture.json`.

## Real run against the test stand

1. Start the gateway from the branch under test with `docker compose up` (see `docs/deployment.md`);
   in its `.env` set `OKK_API_BASE_URL` to the OKK test stand API (address and access come from
   the private OKK repo, `CONNECTIONS.md`; never put them here), `MCP_ISSUER_URL=http://localhost:8020`
   and `MCP_RESOURCE_URL=http://localhost:8020/mcp`.
2. Get tokens for the stand eval account (read-only, the same account for truth and agent):

   ```
   OKK_EVAL_EMAIL=... OKK_EVAL_PASSWORD=... python evals/get_token.py --gateway-url http://localhost:8020/mcp --out evals/out/token-stand.json
   ```

   Set the two variables from your secret store in the shell, not in a file in the repo.
3. Run (only after the owner approved the paid run, see below):

   ```
   python evals/run.py --client codex --gateway-url http://localhost:8020/mcp --token-file evals/out/token-stand.json --reasoning medium --repeat 3 --confirm-paid-run
   python evals/run.py --client claude --gateway-url http://localhost:8020/mcp --token-file evals/out/token-stand.json --repeat 3 --confirm-paid-run
   ```

Codex runs as `codex exec --json --ephemeral --ignore-user-config -s read-only` with the gateway
registered as MCP server `okk-analytics` and the token in `OKK_EVAL_TOKEN`
(`bearer_token_env_var`). Claude Code runs as `claude -p --output-format stream-json` with
`--mcp-config` (the header is `Bearer ${OKK_EVAL_TOKEN}`, expanded by Claude Code, so the token
is not written to disk) and `--strict-mcp-config`. Both start in an empty folder under `out/`. On
Windows pass `--codex-bin` with the path to the native `codex.exe` if `codex` resolves to an npm
shim. `--model` and `--client-arg` pass the model and any extra CLI argument.

Each run writes `out/<label>/runs.jsonl` (one line per session: answer, tool calls with
arguments and result characters, tokens, cost when the client reports it, seconds),
`report.json` and `summary.md`: pass rate, answer accuracy, tool-selection accuracy, calls per
question, tool errors, tool result characters, input / cached / output tokens, time — for all
cases, the main cases and the held-out cases separately.

## Production

The same commands with `--gateway-url https://okk-mcp.akfixdev.ru/mcp` and a token for a
production account. Production holds real data of real people: run it only with the owner's
explicit approval and an account they name, keep `out/` local and never paste answers into issues
or pull requests.

## Cost (estimate)

One full run starts one agent session per ready case: **39 sessions per client per repeat**
(fewer when cases are skipped). The usual release check — both clients, `--repeat 3` — is
**234 paid sessions**. A session makes 1–6 tool calls; a probe on 2026-10-21 used about 130k
input tokens (most of them cached) and a few hundred output tokens for a two-call Codex question.
These numbers are an estimate: the real tokens and, for Claude, the cost in USD are in the report
of the first run. Without `--confirm-paid-run` the runner only prints how many sessions it would
start.

**Paid runs need the owner's approval** with the number of sessions named beforehand. Dry runs
and `--truth-from gateway` reads are free.

## Release rule

1. Run the suite on the test stand with the release candidate: same client, model, reasoning and
   `--repeat 3` as the previous release, with `--baseline <previous report.json>`.
2. The release passes when the gate in `summary.md` says **pass**: the summed pass rate over the
   main (non-held-out) cases is not lower than the previous release on the same cases. Every
   `dropped` case is read in `runs.jsonl` and explained before release.
3. No case may end with status `error`; `skipped` and `truth_unavailable` cases are listed with
   their reasons and must have the same cause as before.
4. Held-out cases are reported separately and are not part of the gate. A rise on the main cases
   with a drop on the held-out ones means the change fits the questions, not the product.

Keep each release's `report.json` with the private release notes, not in this repository.

## Limits

- Graders read free text: a number can match by coincidence (a date «15 октября» contains 15),
  and a correct answer phrased unusually can fail. Read the failures in `runs.jsonl` before
  drawing conclusions.
- Truth comes through the gateway's tools, so it checks the agent's use of the tools, not the
  gateway's numbers against OKK; the gateway's own tests and release smoke cover that.
- Model answers vary: compare runs with the same `--repeat`, not single sessions.

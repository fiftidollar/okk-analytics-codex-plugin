# OKK Analytics

Read-only access to OKK call-quality analytics from Claude Code and Codex. The
plugin connects to the hosted MCP server `https://okk-mcp.akfixdev.ru/mcp`.
Each person signs in with their own OKK account on the OKK login page; the
client never sees the password.

What it answers, always within the account's live role and department access:

- department and employee statistics, rankings and trends;
- evaluation scenarios, criteria and their results;
- weekly call plans and their fact;
- call transcripts and search over them (separate `okk.transcripts.read`
  permission);
- call history of one client phone number (separate `okk.phones.read`
  permission).

Nothing can be changed through the plugin: every tool only reads.

Install, privacy and terms: see the repository
[README](https://github.com/fiftidollar/okk-analytics-codex-plugin#readme),
[PRIVACY.md](https://github.com/fiftidollar/okk-analytics-codex-plugin/blob/main/PRIVACY.md)
and [TERMS.md](https://github.com/fiftidollar/okk-analytics-codex-plugin/blob/main/TERMS.md).

# Repository rules

This repository owns the public Codex plugin and its standalone OAuth/MCP
gateway. It must not import code, ORM models, secrets, or database connections
from the private OKK platform repository.

- Every MCP tool is read-only, idempotent and ACL-aware.
- Use only existing OKK `GET` analytics endpoints after native `/auth/login`.
- Never persist, log or echo a user's password.
- Store MCP tokens as SHA-256 hashes and upstream OKK session tokens only with
  authenticated encryption.
- Reload `/auth/me` for every MCP request. A viewer with an empty department ACL
  receives valid empty results.
- Direct inaccessible IDs return neutral `not_available`. Mixed lists reveal
  only an omitted count.
- Never expose audio, structured phone-number fields, raw prompts, prompt
  runtime, raw reasoning, scripts, Megafon, routing, pipeline internals or
  writes. Transcript text is the only sensitive-content exception: expose it
  solely through the three dedicated transcript tools, under
  `okk.transcripts.read`, after upstream and gateway ACL checks; never persist
  or log it.
- Update tool, plugin, security and deployment docs together when a contract
  changes.
- Validate `pytest`, compilation, Alembic offline SQL, release smoke and plugin packaging before
  release.

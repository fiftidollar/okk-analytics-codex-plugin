# Changelog

## 1.0.1 - 2026-07-15

- Matched the official Codex remote-MCP package shape by declaring the exact
  OAuth resource in `.mcp.json` and starting authentication during install.
- Restored the standard OAuth completion contract: the authorization endpoint
  redirects directly to the exact Codex callback, while the validated callback
  origin is narrowly included in the login page's `form-action` CSP.
- Removed the custom JavaScript callback handoff; Codex now owns the listener,
  token exchange and authenticated MCP state end to end.

## 1.0.0 - 2026-07-14

- Initial standalone community plugin.
- 19 typed read-only analytics tools.
- Native OKK login bridged to OAuth 2.1 Authorization Code + PKCE.
- Live ACL revalidation, encrypted upstream sessions and hashed MCP tokens.
- Flow-scoped stateless CSRF nonces keep parallel or retried OAuth login pages
  independent without relying on browser cookies. If an old form submits a
  cookie-era request, the gateway refreshes it into the stateless format instead
  of trapping the user on a dead recovery page.

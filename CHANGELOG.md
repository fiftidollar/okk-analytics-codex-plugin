# Changelog

## 1.0.0 - unreleased

- Initial standalone community plugin.
- 19 typed read-only analytics tools.
- Native OKK login bridged to OAuth 2.1 Authorization Code + PKCE.
- Live ACL revalidation, encrypted upstream sessions and hashed MCP tokens.
- Flow-scoped stateless CSRF nonces keep parallel or retried OAuth login pages
  independent without relying on browser cookies. If an old form submits a
  cookie-era request, the gateway refreshes it into the stateless format instead
  of trapping the user on a dead recovery page.

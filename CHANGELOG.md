# Changelog

## 1.0.0 - unreleased

- Initial standalone community plugin.
- 19 typed read-only analytics tools.
- Native OKK login bridged to OAuth 2.1 Authorization Code + PKCE.
- Live ACL revalidation, encrypted upstream sessions and hashed MCP tokens.
- Flow-scoped CSRF cookies keep parallel or retried OAuth login pages independent.
  If a browser loses the form cookie while the signed OAuth request is still
  valid, the gateway now refreshes the login form with a new CSRF cookie instead
  of trapping the user on a dead recovery page.

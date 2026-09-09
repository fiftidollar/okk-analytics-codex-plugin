---
description: Проверить подключение к OKK и показать текущую роль и доступные отделы
---

Call the OKK Analytics MCP tool `get_access_context`.

Only when the returned payload has `status="ok"` and
`data.authenticated=true`, answer in Russian with:

- `OKK подключён`;
- the current account role;
- every visible department name and code, or explicitly say that the visible
  department list is empty.
- whether `data.available_sections.supervisors.available` is true; when it is,
  report the returned employee count as the available private
  `Руководители` section without listing names unless the user asks.

If the tool requires authentication, tell the user to open `/mcp`, select the
OKK Analytics plugin server, complete **Authenticate** in the browser, return to
Claude Code, and run `/okk-analytics:check-connection` again. Never ask for the
OKK password in chat and never claim success from the browser redirect alone.

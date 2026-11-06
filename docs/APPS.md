# Connected apps (MCP)

Delta can work inside your apps: Notion, GitHub, Jira and Confluence, Linear, Slack, Sentry, Stripe, Gmail,
Google Drive, Google Calendar, Asana, Vercel, Supabase, Hugging Face, Figma (desktop) and any other app that
publishes a [Model Context Protocol](https://modelcontextprotocol.io) server. You connect an app once, then just
ask, in the dashboard chat or on Telegram:

- "Find my Notion notes about the launch and summarise them"
- "Open a GitHub issue in adharshramu12/Project-Delta for the login bug"
- "What is on my calendar tomorrow?"

Results come back like any other answer. Anything that would change or send something asks you first.

## Quickstart

1. **Open Apps.** In the dashboard, open **Apps** in the sidebar. Or on the PC: `delta apps list`.
2. **Connect.** Press **Connect** on the app (or `delta apps connect notion`). For most apps a sign-in page opens in
   your browser: sign in and allow Delta. Some apps ask for a token instead (GitHub) or for the client id of an
   OAuth app you create (Google, Slack, Asana); the card says exactly what and links to the vendor's page.
3. **Ask.** "Search my Notion for the Q3 plan." Delta finds the right tool in the app and uses it.

From Telegram: `/apps` lists your apps, `/connect notion` starts a sign-in (it needs your PIN, and you finish the
sign-in on the PC: the sign-in answer comes back to the PC itself), `/disconnect notion` (PIN).

## The catalog

`config/mcp_catalog.yaml` lists the apps Delta can connect in one step. Every entry was checked against the
vendor's own documentation (its `docs` and `checked` date are in the file), and every remote sign-in was probed
live: the server's 401 answer, its protected resource metadata and its authorization server metadata.

| App | Status | How you sign in | Notes |
|---|---|---|---|
| Notion | official | browser sign-in (Delta registers itself) | `mcp.notion.com/mcp` |
| GitHub | official | personal access token | GitHub's OAuth for this server has no dynamic registration |
| Atlassian (Jira, Confluence) | official | browser sign-in | Rovo MCP v2; your admin may need to allow it |
| Linear | official | browser sign-in | |
| Slack | official | your own Slack app's client id and secret | Slack requires a confidential client |
| Sentry | official | browser sign-in | |
| Stripe | official | browser sign-in | payments still ask first |
| Gmail | official preview | your Google Cloud OAuth client id and secret | Google Workspace Developer Preview |
| Google Drive | official preview | same Google client | |
| Google Calendar | official preview | same Google client | deleting events is off until you switch it on |
| Asana | official | your Asana OAuth app's client id and secret | |
| Vercel | official | browser sign-in | |
| Supabase | official | browser sign-in | database tools ask first |
| Hugging Face | official | none | public Hub data |
| Figma (desktop app) | official | none | Figma's remote server only accepts clients in Figma's own list |
| Files (MCP reference) | reference | none | runs on your PC with Node.js (`npx`), version pinned |
| Web fetch (MCP reference) | reference | none | runs on your PC with uv (`uvx`), version pinned |

Google publishes official Workspace MCP servers (Gmail, Drive, Calendar and more) as a Developer Preview, so no
community Gmail server is needed or listed.

### Apps that need your own OAuth client

Google, Slack and Asana do not let an app register itself. You create an OAuth app at the vendor (the card and
`delta apps connect <app>` give the exact steps and links), add this redirect URL to it:

    http://127.0.0.1:8765/callback

and paste its client id (and secret). The secret is sealed with DPAPI like a token.

### Apps that run on your PC

Local servers are started with `npx` (Node.js) or `uvx` (uv) at a pinned version. If Node.js or uv is missing,
Delta says so and how to install it (`winget install OpenJS.NodeJS.LTS`, `winget install astral-sh.uv`). A local
server runs with your secret environment variables removed and only the secrets it declares; Delta ends it,
with every program it started, on disconnect and when Delta stops.

### Your own server

Apps > "Add another MCP server" (or `delta apps add mytools --url https://... [--oauth]` /
`--command "npx -y some-server@1.2.3"`). Delta first shows exactly what will run or which address it will reach,
and adds it only after you confirm. Custom servers are marked "custom" everywhere.

## Tools, risk and approvals

Each app's tools are listed on its card (Tools), with a risk per tool and an on/off switch:

- **read**: runs without asking. Only for tools Delta verified in the vendor's docs, tools you mark read yourself,
  or tools the app labels read-only when you tick "trust this app's read-only labels" for that app.
- **asks first** (external): every other tool. It asks at every permission level except Autopilot.
- **destructive**: tools the app says may delete or overwrite. These are **off** until you switch them on, and
  always ask.

Whatever an app returns is treated like a web page: it is information, never instructions. Once a task has read
something from an app (or a page, a file, a message), every app call that would change or send something asks
you, Autopilot included, and a request carrying a long encoded value asks too.

From a phone, every app call needs your PIN (`/unlock`), and approving a change from the phone needs it too.

## Token use

The model does not get every app's tool list on every step. While at least one app is connected it gets two small
tools: `find_app_tool` (whose description is a one-line index of your apps) and `use_app_tool`. It looks up the
tool it needs, then uses it. The planner sees the index only, not each tool. A user with no apps connected pays
nothing.

## Reliability

Each call has a time limit (`connectors.call_timeout_s`, 60 s). Read tools are retried on transient failures
(time-outs, dropped connections, 429 and 5xx), never tools that change something. After a few failures in a row
an app is paused for a minute (circuit breaker) instead of being hammered. An expired sign-in is refreshed
automatically; if the app revoked it, Delta says "Reconnect Notion" with the exact button or command.

Settings (`connectors:` in your settings):

```yaml
connectors:
  enabled: true
  call_timeout_s: 60
  connect_timeout_s: 30
  read_retries: 2
  breaker_failures: 3
  breaker_cooldown_s: 60
  sign_in_timeout_s: 300
```

## The `mcp:` add-on still works

Servers you configured before under `addons.config.mcp.servers` load exactly as before (they need the optional
`mcp` extra). Connected apps are separate and need no extra: Delta speaks the protocol itself.

## For developers

`delta/connectors/`: `catalog.py` (the catalog), `client.py` (JSON-RPC over stdio, Streamable HTTP and HTTP+SSE),
`oauth.py` (the MCP authorization spec), `secrets.py` (DPAPI), `store.py`, `manager.py`, `tools.py`. The fake
notes app in `delta/evals/fakemcp.py` (HTTP, SSE and stdio, with a fake OAuth server) backs the unit tests
(`tests/test_connectors.py`), the scripted evals (`evals/tasks/connectors.yaml`) and the Windows end-to-end test.

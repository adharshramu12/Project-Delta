# Building add-ons for Delta

Add-ons give Delta new abilities: Spotify, Notion, home automation, your company's API.
The SDK is `delta.addons.api` (version `1.0`). It is deliberately tiny and kept stable.

## 1. Minimal add-on

```python
# addon.py
from pydantic import BaseModel, Field
from delta.addons.api import Risk, Tool, ToolResult

class WeatherArgs(BaseModel):
    city: str = Field(description="City name")

class Weather(Tool[WeatherArgs]):
    name = "weather_now"
    description = "Current weather for a city."
    risk = Risk.READ            # be honest: this decides whether the user is asked first
    Args = WeatherArgs

    def domains(self, args):    # lets the user's domain boundaries apply
        return ["wttr.in"]

    async def run(self, args):
        import httpx
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://wttr.in/{args.city}?format=3")
        return ToolResult(r.status_code == 200, r.text, {"city": args.city})

class WeatherAddon:
    name = "weather"
    version = "0.1.0"
    description = "Weather lookups"

    async def setup(self, ctx):
        ctx.register_tool(Weather())

addon = WeatherAddon()
```

## 2. Install it
- **Folder (quick):** `%LOCALAPPDATA%\Delta\addons\weather\addon.py`
- **Package (shareable):** publish to PyPI with

  ```toml
  [project.entry-points."delta.addons"]
  weather = "delta_weather:addon"
  ```

Then enable it in `config/delta.yaml`:

```yaml
addons:
  enabled: [weather]
  config:
    weather: { units: metric }   # arrives as ctx.config
```

## 3. Rules every add-on gets for free
- Tools run through Delta's **policy engine, approvals and audit log**. There is no way around them.
- Set `risk` honestly. `READ` runs automatically. `WRITE`, `DESTRUCTIVE` and `EXTERNAL` ask the user.
  `SYSTEM` is always blocked.
- Declare what you touch with `paths()`, `domains()` and `app()`, so the user's boundaries apply.
- Put checkable facts in `ToolResult.evidence`. The verifier uses them to confirm tasks really finished.
- Import heavy libraries inside `run()`, not at module top. Delta must stay light when idle.
- A crashing add-on is logged and skipped; it never stops Delta.

## AddonContext
| Member | Use |
|---|---|
| `register_tool(tool)` | add a tool |
| `config` | your section of `addons.config` |
| `data_dir` | private folder for your files/cache |
| `router` | ask an LLM (`await ctx.router.complete(messages)`); follows the user's models and hardware limits |
| `events` | `ctx.events.subscribe(handler)` to react to `task.finished`, `skill.changed`, … |
| `on_shutdown(hook)` | close connections when Delta stops |

## MCP: connect existing servers without code
**Most people want [Connected apps](APPS.md)** instead: Notion, GitHub, Slack, Gmail and others connect from the
dashboard's Apps page, `delta apps connect <app>` or Telegram `/connect`, with sign-in, DPAPI-sealed tokens, per-tool
switches and lazy tool loading, and no optional extra to install. The add-on below is the older, config-file way;
it keeps working unchanged (it needs the optional `mcp` extra).

Thousands of MCP servers already exist. Enable the built-in `mcp` add-on:

```yaml
addons:
  enabled: [mcp]
  config:
    mcp:
      servers:
        files:   { command: npx, args: ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/me/Delta"] }
        remote:  { url: "https://example.com/mcp", token_secret: remote_token, risk: write }
```

MCP tool risk comes from, in order: your per-tool overrides (`tools:`), the server's `destructiveHint` (it can
only make a tool more careful), the server's `risk`, the server's `readOnlyHint` only when you set
`trust_read_only_hints: true`, and otherwise `external` (asks first at every level except Autopilot). What an
MCP tool returns is outside content: it marks the task as tainted, like a web page.

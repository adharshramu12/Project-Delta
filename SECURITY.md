# Security model

Delta controls a real computer, so its safety rules are part of the design, not settings.

- **Hard floor (not configurable):** OS/security settings, disk formatting, user accounts, credentials, firewall/antivirus changes and money movement are denied. Deletes, overwrites and anything sent off the machine always need a human.
- **Boundaries:** `config/boundaries.yaml` can only add restrictions or relax plain writes inside allowed folders. Delta has no tool that edits it.
- **Approvals** time out to *deny*.
- **Secrets** live in Windows Credential Manager, never in config or logs.
- **Audit log** is hash-chained; `delta audit verify` detects tampering.
- **Prompt injection:** web pages, files and tool output are data. They cannot grant approval; only a human on an allow-listed channel can.
- **Remote access** (planned) is opt-in, over Tailscale or Cloudflare Tunnel, with device pairing; the API binds to localhost only.

Report vulnerabilities privately via GitHub Security Advisories, not public issues.

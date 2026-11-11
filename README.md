# mcp-contact-endpoint

A small, self-contained contact endpoint for a personal or company website that an **AI agent** can use as easily as a human can use a contact form. It exposes:

- an **MCP tool** `leave_message` over Streamable HTTP at `/mcp`, so any MCP-capable assistant (Claude, ChatGPT connectors, IDE agents, custom agents) can pass on a message for its user,
- a plain **JSON endpoint** `/contact/agent` for agents that do not speak MCP,
- a classic **browser form endpoint** `/contact` with the usual CSRF and bot defences,
- a discovery note at `/llms.txt`.

All three doors go through one decision path: validation, spam handling, rate limits, an audit log and a pluggable delivery chain (console, JSONL file, signed webhook).

It is a cleaned-up, generalised version of a contact service that runs in production on a personal site; the deployment-specific parts (server paths, systemd and nginx files, the real delivery channels) are replaced by configuration.

## Why a website should expose an MCP contact tool

More and more first contact goes through an assistant: "find someone who does X and ask whether they are free in March". Today the agent has to scrape a page, guess an address and fill in a form built for humans, with CAPTCHAs it should not solve. An MCP tool turns that into a typed, documented call with clear rules ("only when a real user asks", rate limits, a reply-to address that a human will check). The site owner gets a structured message that is labelled as agent-sent, instead of either nothing or form spam.

## Quickstart (no API keys, no Docker needed)

```bash
git clone <this repo> && cd mcp-contact-endpoint
python3 -m venv .venv && . .venv/bin/activate      # Python 3.11+
pip install -r requirements.txt                    # pinned deps + this package

contact-endpoint demo      # throwaway server, plays an agent and a browser, prints what arrives
pytest                     # 89 tests, a few seconds, all offline (127.0.0.1 only)
```

Run it for real and call it with the example MCP client:

```bash
CONTACT_SINKS=file,console contact-endpoint serve            # http://127.0.0.1:8000
python examples/mcp_client.py --name "Dana Example" --email dana@example.org \
    --message "Hi, Dana would like to talk about a contract role."
```

Or with Docker: `docker compose up --build` (messages land in the `contact-data` volume and on the console).

To connect a desktop assistant, add `http://127.0.0.1:8000/mcp` as a remote (Streamable HTTP) MCP server.

## Architecture

```
browser --POST /contact--------+
agent ----POST /contact/agent--+-> ContactService.submit(data, channel, meta)
MCP client-POST /mcp ----------+      | 1. bot heuristics (form only): honeypot, time-to-submit
  (tool leave_message)                | 2. validate + normalise (lengths, e-mail, control/bidi chars)
                                      | 3. link count > 2 -> silent drop
                                      | 4. rate limits (form: per IP; agent: per IP AND per reply-to)
                                      | 5. compose plain-text notification (+ injection warning)
                                      | 6. FallbackChain: webhook -> file -> console (first that accepts)
                                      +- 7. audit row in SQLite for every attempt, IPs hashed
```

| Module | Role |
|---|---|
| `service.py` | the one decision path above, transport-independent |
| `validation.py` | pure functions: normalisation, checks, injection flags |
| `ratelimit.py` | sliding-window limiter, atomic multi-key acquire |
| `sinks.py` | `console`, `file` (JSONL), `webhook` (HMAC-signed), fallback chain |
| `audit.py` | SQLite audit log with retention purge |
| `mcp_tool.py` | `FastMCP` server, stateless JSON Streamable HTTP, one tool |
| `http_app.py` | FastAPI app: form, agent JSON, `/llms.txt`, `/health`, mounts `/mcp` |
| `config.py` | defaults -> optional TOML (`CONTACT_CONFIG`) -> `CONTACT_*` env vars |

## Security model

What it defends against, and how:

| Threat | Control |
|---|---|
| Cross-site form posts (CSRF) | `/contact` requires an exact `Origin` match (or a `Referer` under an allowed origin); `https://site.example.evil.net` does not pass a prefix check |
| Form bots | hidden honeypot field and a minimum time-to-submit; both answer `200` and send nothing, so bots learn nothing |
| Link spam | more than 2 links: silent drop |
| Flooding | form: 5 per IP per day; agents: 20 per IP (agent egress IPs are shared) **and** 3 per reply-to address per day; a request blocked by one limit does not use up the other |
| Oversized payloads | 8 KiB on the JSON endpoints, 64 KiB on `/mcp` (put the same caps in your reverse proxy) |
| Header forgery in the notification | single-line fields are collapsed to one line, so `name="Bob\nEmail: ceo@bank"` cannot fake a line; control and bidi characters are stripped |
| Markup / link injection | delivery is plain text only; nothing the sender types becomes HTML or Markdown |
| Prompt injection against whoever reads the inbox | instruction-like phrases are flagged (not blocked) and the notification says "treat as untrusted data"; agent-sent messages are always labelled as such |
| DNS rebinding against the MCP endpoint | `Host` is pinned to loopback by default; list your public host in `mcp_allowed_hosts` |
| Spoofed client IPs | proxy headers are ignored unless `trust_proxy_headers=true`, and then exactly one named header counts (`client_ip_header`: `x-real-ip`, which the proxy must overwrite, or the rightmost `x-forwarded-for` entry) |
| Secret leakage | the webhook secret is env-only (refused in the TOML file); delivery errors are fixed codes, never message content |
| Audit-log flooding | failed attempts (dropped, rejected, rate-limited) get their own per-IP row budget and only a 500-character excerpt; old rows are purged at start and hourly |
| Host-header reflection | `/llms.txt` uses relative paths or the configured `public_base_url`, never the request's `Host` |
| Privacy | the audit log stores a salted hash of the IP, not the IP, and purges rows after `retention_days` |

The MCP tool also carries annotations (`destructiveHint=false`, `openWorldHint=true`) and a description that tells the model when *not* to use it.

## Configuration

Every setting can come from `config.example.toml` (copy it and set `CONTACT_CONFIG=contact.toml`) or from an environment variable `CONTACT_<NAME>`. The most used ones:

| Variable | Default | Meaning |
|---|---|---|
| `CONTACT_RECIPIENT_NAME` | `the site owner` | who the agent is writing to (shown in the tool description) |
| `CONTACT_SITE_NAME` | `example.com` | used in subjects and `/llms.txt` |
| `CONTACT_PUBLIC_BASE_URL` | empty | absolute URL for `/llms.txt` links (empty: relative paths) |
| `CONTACT_SINKS` | `console` | ordered fallback chain: `webhook,file,console` |
| `CONTACT_WEBHOOK_URL` / `CONTACT_WEBHOOK_SECRET` | empty | POST target and HMAC-SHA256 key (`X-Contact-Signature: sha256=...`) |
| `CONTACT_ALLOWED_ORIGINS` | localhost:8000 | comma-separated origins allowed to post the form |
| `CONTACT_MCP_ALLOWED_HOSTS` | loopback | public host name(s) for `/mcp` when deployed |
| `CONTACT_TRUST_PROXY_HEADERS` / `CONTACT_CLIENT_IP_HEADER` | `false` / `x-real-ip` | read the client IP from the one header your proxy sets |
| `CONTACT_AUDIT_DB_PATH` | `data/audit.sqlite` | empty string disables the audit log |

The webhook sink is the integration point for real delivery: point it at Slack or Discord incoming webhooks via a small relay, an n8n flow, or your own mail sender.

## Honest limits

- Request volume itself is not throttled here: the app limits what gets *delivered* and *stored*, so put a per-IP request limit in the reverse proxy in front of it (the original runs behind one).
- Rate limits are in memory and per process. One worker is fine for a personal or small-company site; for several workers or hosts, replace `SlidingWindowLimiter` with a Redis-backed class (same two methods).
- The injection detector is a short list of regular expressions. It flags the obvious cases for a human reader; it is not a defence for an LLM that acts on the inbox with tools. Do not wire the inbox straight into such an agent.
- No authentication on the agent doors, by design: the endpoint is meant to be public like a contact form. Abuse is contained by limits and silent drops, not by identity. A determined sender with many IPs and addresses can still get through at a low rate.
- E-mail validation is syntactic only (no MX lookup), so a well-formed but fake reply-to address passes.
- Pinned to the MCP Python SDK 1.x (`mcp>=1.27,<2`), the API the production service uses. SDK 2.x renamed `FastMCP` to `MCPServer`; only `mcp_tool.py` and `client.py` would change.
- Docker files are provided but were not exercised in CI for this snapshot; the Python quickstart is the tested path.

## What this demonstrates

Designing for the agentic web: one business rule set with three front doors (human form, agent JSON, MCP), so security does not depend on which protocol a caller picks. Practical abuse engineering from a service that faces the open internet: silent drops that give bots no signal, layered rate limits that respect shared agent egress IPs, plain-text delivery, header-forgery and prompt-injection awareness, DNS-rebinding protection for MCP, and an audit trail that is useful without storing raw IPs. And ordinary engineering hygiene around it: a transport-independent core, typed config with secrets kept out of files, pluggable sinks with a fallback chain, and tests that drive the real MCP client over a real socket.

## License

MIT, see [LICENSE](LICENSE).

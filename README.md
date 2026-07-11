<div align="center">

# ⚡ pixio-mcp ⚡

### **554+ generative models. One MCP server. Zero chances to nuke your credit balance.**

[![Tests](https://img.shields.io/badge/tests-121%20passing-brightgreen?style=for-the-badge)](#-development)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=for-the-badge&logo=python&logoColor=white)](#-quick-start)
[![MCP](https://img.shields.io/badge/protocol-MCP-8A2BE2?style=for-the-badge)](https://modelcontextprotocol.io)
[![Spend Safety](https://img.shields.io/badge/spend%20guardrails-ARMED-crimson?style=for-the-badge)](#-spend-safety-the-flex)
[![License](https://img.shields.io/badge/license-MIT-black?style=for-the-badge)](#-license)

**Prompt in. File on disk. Four tool calls. Every model [Pixio](https://beta.pixio.myapps.ai) ships — the day it ships.**

</div>

---

## 🔥 What is this

`pixio-mcp` hands any MCP client — Claude Desktop, Claude Code, or your own agent swarm — the **entire Pixio media generation arsenal**: text-to-image, image-to-video, text-to-video, video-to-video, lipsync, text-to-audio, and a stack of utility ops. All of it metered in credits, all of it behind guardrails that make it safe to hand the keys to a fully autonomous agent and walk away.

```text
discover → inspect schema → price it → generate → poll → download. done. 💅
```

**The cheat code:** this server hardcodes *zero* model knowledge. Every parameter schema is pulled **live** from the API at call time. Pixio drops 50 new models tomorrow? They work here tomorrow. No update. No redeploy. No waiting on anybody.

## 🧨 Why it goes hard

| | The old way | The `pixio-mcp` way |
|---|---|---|
| **Coverage** | Hand-rolled HTTP for a handful of models | **All 554+ models**, discovery-driven |
| **New models** | Wait for someone to update the wrapper | **Day-zero support**, automatically |
| **Spend control** | Vibes 💸 | **Two hard caps + estimate-before-spend** |
| **Long video jobs** | Hang or lose the job | **Resumable ids** — timeout ≠ dead job |
| **Local files** | Figure out uploads yourself | `upload_media` → **permanent public URL** |
| **Errors** | A stack trace and a prayer | **9-code machine-actionable taxonomy** |

Battle-tested: **121 offline tests**, two full multi-agent validation rounds (security audit, adversarial review, live protocol checks), and a real end-to-end run — prompt → generated image → verified bytes on disk.

## 🚀 Quick start

You need: **Python 3.11+**, [**uv**](https://docs.astral.sh/uv/), and a **Pixio API key** (`pxio_live_...`).

```sh
git clone https://github.com/RealDealCPA-VR/Pixio-MCP.git
cd Pixio-MCP
uv sync
```

```sh
# PowerShell — fire it up (stdio transport; it waits for an MCP client)
$env:PIXIO_API_KEY = "pxio_live_..."
uv run pixio-mcp
```

```sh
# bash / zsh — same thing
export PIXIO_API_KEY="pxio_live_..."
uv run pixio-mcp
```

You'll almost never run it by hand — register it with your client (next section) and let your agent cook. 👨‍🍳 No key set? The server still boots (warning on stderr) and every tool politely returns an `AUTH` error until you feed it one.

## 🔌 Plug it in

### 🌍 Any MCP host

One JSON shape rules them all:

```json
{
  "mcpServers": {
    "pixio": {
      "command": "uv",
      "args": ["run", "--directory", "<path-to-repo>", "pixio-mcp"],
      "env": { "PIXIO_API_KEY": "pxio_live_..." }
    }
  }
}
```

This **exact block** works in **Claude Desktop**, **LM Studio** (`~/.lmstudio/mcp.json`), **Cursor** (`.cursor/mcp.json`), **Windsurf**, **Cline** (`cline_mcp_settings.json`), and **LibreChat** (`librechat.yaml`, `mcpServers` section). Swap `<path-to-repo>` for wherever you cloned this — and once `pixio-mcp` hits PyPI, swap the whole command for `"command": "uvx", "args": ["pixio-mcp"]`.

### Claude Desktop

Drop the block above into `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) under `mcpServers`, restart Claude Desktop. Done.

### Claude Code

One-liner. That's it. That's the setup.

```sh
# <path-to-repo> = your clone, e.g. C:\Users\you\projects\Pixio-MCP
claude mcp add pixio -e PIXIO_API_KEY=pxio_live_... -- uv run --directory <path-to-repo> pixio-mcp

# Published package
claude mcp add pixio -e PIXIO_API_KEY=pxio_live_... -- uvx pixio-mcp
```

Or drop the same JSON block into your project's `.mcp.json`.

### Continue

In `config.yaml`, add a stdio entry under `mcpServers`:

```yaml
mcpServers:
  - name: pixio
    command: uv
    args: ["run", "--directory", "<path-to-repo>", "pixio-mcp"]
    env:
      PIXIO_API_KEY: pxio_live_...
```

### Zed

In `settings.json`, under `context_servers`:

```json
{
  "context_servers": {
    "pixio": {
      "command": {
        "path": "uv",
        "args": ["run", "--directory", "<path-to-repo>", "pixio-mcp"],
        "env": { "PIXIO_API_KEY": "pxio_live_..." }
      }
    }
  }
}
```

### Open WebUI

Open WebUI speaks OpenAPI, not MCP — bridge with [`mcpo`](https://github.com/open-webui/mcpo):

```sh
export PIXIO_API_KEY="pxio_live_..."
uvx mcpo --port 8000 --api-key <secret> -- uvx pixio-mcp
```

Then add `http://localhost:8000` as an **OpenAPI tool server** in Open WebUI (Settings → Tools), using `<secret>` as the bearer token.

### 🐍 Roll your own agent

No host at all? The vanilla [`mcp`](https://pypi.org/project/mcp/) SDK gets you a working client in ~15 lines:

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main() -> None:
    server = StdioServerParameters(
        command="uv",
        args=["run", "--directory", "<path-to-repo>", "pixio-mcp"],
        env={"PIXIO_API_KEY": "pxio_live_..."},
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print([t.name for t in tools.tools])
            result = await session.call_tool("list_models", {"query": "flux", "limit": 5})
            print(result.content[0].text)

asyncio.run(main())
```

## 🏠 Built for local models too

This server isn't just tuned for frontier models — it's deliberately friendly to a 7B running on your laptop:

- **Self-describing schemas.** Every tool parameter carries its own description in the MCP schema — small models don't have to guess what `confirm` or `offset` mean.
- **Lenient inputs.** String numbers, string bools, `params` passed as a JSON *string*, backtick-wrapped ids — all quietly normalized instead of rejected. Local models fumble formats; the server doesn't punish them for it.
- **Compact by default.** `list_models` returns 20 models per call unless you ask for more, and descriptions are truncated — a 554-model catalog never floods a small context window. Filter with `type`/`query` instead of paging.
- **HTTP transport built in.** LM Studio, Ollama-backed hosts, or anything that prefers HTTP over stdio — see [Transports](#-transports) below.

## 🎛️ Configuration

Everything tunes through env vars:

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `PIXIO_API_KEY` | **yes** | — | Bearer token (`pxio_live_...`). Never logged, never echoed. Ever. |
| `PIXIO_BASE_URL` | no | `https://beta.pixio.myapps.ai/api/v1` | Override if the API moves off beta. Trailing slash / missing `/api/v1` normalized for you. |
| `PIXIO_MAX_CREDITS_PER_JOB` | no | `60` | Per-job credit cap. Estimates above this get refused without `confirm=true`. |
| `PIXIO_SESSION_BUDGET` | no | `300` | Cumulative credit ceiling per server process. |
| `PIXIO_DEFAULT_TIMEOUT_S` | no | `180` | Default wait for `generate(wait=true)` / `wait_for_generation`. |
| `PIXIO_DOWNLOAD_DIR` | no | `~/pixio-outputs` | Where `download_output` drops the goods. |
| `PIXIO_LOG_LEVEL` | no | `INFO` | Logs go to **stderr** as JSON lines (stdout carries the MCP protocol). |
| `PIXIO_TRANSPORT` | no | `stdio` | `stdio`, `streamable-http`, or `sse`. See [Transports](#-transports). |
| `PIXIO_HOST` | no | `127.0.0.1` | Bind address for the HTTP transports. |
| `PIXIO_PORT` | no | `8000` | Bind port for the HTTP transports. |

Fat-finger an integer (`PIXIO_SESSION_BUDGET=lots`)? The server **refuses to boot** — one clean stderr line naming the exact variable, no traceback. No silent misconfigs.

## 🚦 Transports

Default is **stdio** — the classic spawn-me-as-a-subprocess mode every desktop host uses. For hosts that talk HTTP instead (web UIs, remote agents, LM Studio-style local stacks), flip one env var:

```sh
export PIXIO_TRANSPORT=streamable-http   # or "sse" for legacy SSE hosts
uv run pixio-mcp                          # serves on http://127.0.0.1:8000
```

**⚠️ SECURITY: this server holds a spending API key.** Anyone who can reach the port can burn your credits. Keep it bound to `127.0.0.1` (the default) — if you must expose it beyond localhost via `PIXIO_HOST`, put it behind a reverse proxy with auth, a firewall rule, or a VPN. Never bind `0.0.0.0` on an untrusted network.

## 🧰 The toolkit — 9 tools, full lifecycle

| Tool | What it does | Key inputs |
|---|---|---|
| `list_models` | Filterable catalog of all 554+ models (cached 10 min). Id, name, type, per-run credits, company, description. | `type` (exact, e.g. `"text-to-image"`), `query` (substring), `limit` (1–200, default **20**), `offset` |
| `get_model_params` | The **exact** live input schema for one model — names, types, required flags, defaults, allowed `options`. Verbatim API passthrough. | `model_id` |
| `estimate_cost` | Price the job **before** a single credit moves. Falls back to catalog cost if the estimate endpoint flakes. | `model_id`, `params` |
| `upload_media` | Local file **or** remote URL → permanent public Pixio URL (`pixiomedia.nyc3.digitaloceanspaces.com`). | `source` |
| `generate` | The main event: rejects local paths, estimates, enforces caps, submits, waits for the result. | `model_id`, `params`, `wait`=`true`, `timeout_s`, `confirm`=`false` |
| `get_generation` | One-shot status + output URLs. | `generation_id` |
| `wait_for_generation` | Poll to `succeeded`/`failed` or timeout. **Resumes jobs that outlived a `generate` timeout.** | `generation_id`, `timeout_s` |
| `download_output` | Every output file of a succeeded job → your disk. Returns absolute paths. | `generation_id`, `dest_dir` |
| `get_credits` | Balance breakdown (`total`, `recurring`, `permanent`) + optional spend ledger tail. | `include_ledger_tail`, `ledger_limit` |

Every tool returns clean JSON. Failures come back as structured error dicts (see [taxonomy](#-error-taxonomy)) — **tools never throw raw exceptions at your agent.**

## 🎯 The three-call contract

The server ships knowing *nothing* about any model. Your LLM discovers everything at runtime:

1. **`list_models`** — find the weapon 🎯
2. **`get_model_params`** — read the manual 📖
3. **`generate`** — send it 🚀

Add **`download_output`** and a text prompt becomes a file on your machine in **four calls flat**:

```text
>>> list_models(type="text-to-image", query="flux")
{
  "models": [
    {"id": "pixio/flux-1/schnell", "name": "FLUX.1 Schnell", "type": "text-to-image",
     "credits": 1, "company": "Black Forest Labs", "description": "Fast text-to-image..."},
    ...
  ],
  "total_matching": 6, "returned": 6, "offset": 0
}

>>> get_model_params(model_id="pixio/flux-1/schnell")
{
  "model": {"id": "pixio/flux-1/schnell", ...},
  "params": [
    {"name": "prompt", "type": "string", "label": "Prompt", "required": true, "defaultValue": ""},
    {"name": "image_size", "type": "select", "label": "Image size", "required": false,
     "defaultValue": "landscape_4_3",
     "options": [{"value": "square_hd", "label": "Square HD"},
                 {"value": "landscape_4_3", "label": "Landscape 4:3"}, ...]}
  ]
}

>>> generate(model_id="pixio/flux-1/schnell",
             params={"prompt": "a crimson arc reactor on black velvet, studio lighting",
                     "image_size": "square_hd"})
{
  "generation_id": "b7e2f9c1-4a06-4d2e-9c1e-0f3a7d5e8b21",
  "status": "succeeded",
  "output_urls": ["https://pixiomedia.nyc3.digitaloceanspaces.com/outputs/...png?X-Amz-Expires=3600&..."],
  "outputs": {"imageUrl": "https://pixiomedia.nyc3.digitaloceanspaces.com/outputs/...png?..."},
  "model_id": "pixio/flux-1/schnell",
  "credits_spent": 1,
  "remaining_balance": 999,
  "elapsed_s": 6.4,
  "error": null
}

>>> download_output(generation_id="b7e2f9c1-4a06-4d2e-9c1e-0f3a7d5e8b21")
{
  "generation_id": "b7e2f9c1-4a06-4d2e-9c1e-0f3a7d5e8b21",
  "files": ["~/pixio-outputs/b7e2f9c1-0.png"],
  "dest_dir": "~/pixio-outputs"
}
```

Models that eat media (image-to-video, lipsync, ...)? `upload_media` first, pass the returned URL in `params`. `generate` is **URLs-only** and swats local paths before a single credit is spent.

## 🛡️ Spend safety (the flex)

This is the part that lets you point an autonomous agent at a credit balance and sleep at night. All guardrails are **server-side** and on by default:

- 💰 **Estimate before spend.** Every job is priced first (estimate endpoint, catalog fallback). Nothing submits until the price is known — or explicitly flagged unknown via a `warning`.
- 🧱 **Per-job cap** (`PIXIO_MAX_CREDITS_PER_JOB`, default 60). One job over the line → `BUDGET_EXCEEDED`. Denied.
- 🏦 **Session budget** (`PIXIO_SESSION_BUDGET`, default 300). Cumulative ceiling for the whole server process. The meter never lies.
- 🔑 **Explicit override only.** A refusal tells you the estimate, which cap tripped, and the cap value — and *only* a re-call with `confirm=true` gets through. The server never overrides itself.
- 📊 **Balance on every receipt.** Every terminal result reports `credits_spent` + `remaining_balance`. Spend drift has nowhere to hide.
- 🚫 **Zero auto-retry on submission.** `POST /generate` fires exactly once — a network blip can *never* double-spend you. (Reads and estimates retry 3x, because those are free.)

## 🚨 Error taxonomy

Failed tool calls return `{"error": {"code": ..., "message": ..., "details": {...}}}`. Nine codes, all machine-actionable:

> **⚠️ Telling failures apart from successes:** successful job results *also* carry an `error` key — it's the provider's failure reason, `null` on success (see the `generate` example above). Don't test `"error" in result`; test whether `result["error"]` is a **dict with a `code`** (failure envelope) vs `null`/string (job-result field).

| Code | Meaning | Your move |
|---|---|---|
| `AUTH` | 401, or `PIXIO_API_KEY` missing/empty. | Set a valid `pxio_live_...` key in the server's `env`, restart the client. |
| `INSUFFICIENT_CREDITS` | 402 — balance can't cover the job. `details` has `availableCredits`, `requiredCredits`, `shortfall` when the API provides them. | Top up, or pick a cheaper model (`list_models` shows per-run credits). |
| `VALIDATION` | Bad/missing param — or a local file path in `generate` params. Message surfaces the API's error body verbatim (e.g. `Missing required parameter: X`) or names the offending field. | Re-read `get_model_params`, fix the payload. Local paths → `upload_media` first. |
| `BUDGET_EXCEEDED` | The server's own guardrail said no (per-job cap or session budget). **Nothing was spent.** | If the estimate's acceptable, re-call with `confirm=true` — or raise the caps via env. |
| `CONCURRENCY` | 429 — account's in-flight limit reached. `details` carries `concurrencyLimit` when reported. | Wait for in-flight jobs (`wait_for_generation` on their ids), then resubmit. Don't hammer. |
| `GENERATION_FAILED` | Terminal `failed` status. `details.provider_reason` has the provider's reason string. | Read the reason, adjust, submit fresh (a retry spends credits again). |
| `TIMEOUT_PENDING` | Wait window elapsed but the job is **still cooking** — not cancelled. `details` has `generation_id` + a hint. | `wait_for_generation(generation_id)` to resume; the job finishes server-side either way. |
| `NOT_FOUND` | 404 — unknown model or generation id. | Check the id; discover real ones via `list_models`. |
| `UPSTREAM_ERROR` | 5xx, network failure, or unparseable response. | Retry later — GETs/estimates already retried 3x with backoff before this surfaced. |

## 💀 Gotchas (learned so you don't have to)

Hard-won quirks of the live Pixio gateway. The server stays schema-agnostic and does **not** enforce these — your agent must respect them:

- **Select values are strings.** Send `options[].value` exactly as given. `"5"`, not `5`. Even when it looks numeric. *Especially* when it looks numeric.
- **"Optional" is sometimes a lie.** Some optional-with-default params get rejected when omitted. First attempt: send **every** param from `get_model_params` at its `defaultValue`.
- **Output URLs can die in ~1 hour.** `outputUrl` may be signed with a short fuse. `download_output` promptly; never stash URLs for later. (`upload_media` URLs are the exception — permanent and public.)
- **Concurrency is account-wide.** **1 in-flight by default, 3 on Maker** — across *all* your API keys. Parallel fan-outs will eat `CONCURRENCY` errors; serialize your jobs.
- **There is no cancel button.** Once submitted, a job runs to the end; `DELETE /generations/{id}` isn't a thing. A `TIMEOUT_PENDING` job keeps holding a concurrency slot until it finishes — budget `timeout_s` accordingly and *resume*, don't resubmit.
- **No list-generations endpoint.** The `generation_id` from every submission is the **only** handle you get. Guard it with your life.

## 🧪 Development

```sh
uv sync
uv run pytest
```

**121 tests, fully offline** — a mocked Pixio gateway, no API key, no network, zero credits harmed. 🌱

## 📜 License

MIT — go build something loud.

<div align="center">

**Built for agents. Guarded like a vault. Fresh models on day zero.** ⚡

</div>

# pixio-mcp

An MCP (Model Context Protocol) server for the [Pixio](https://beta.pixio.myapps.ai) media generation API. Pixio hosts **554+ models** — text-to-image, image-to-image, image-to-video, text-to-video, video-to-video, text-to-audio, and utility operations — all metered in credits against a single account balance.

`pixio-mcp` gives any MCP client (Claude Desktop, Claude Code, or your own agents) the full generation lifecycle:

**discover models → inspect input schemas → price the job → generate → poll to completion → download outputs**

The server is *schema-agnostic*: it hardcodes zero model knowledge. Every model's parameter schema is fetched live from the API, so new Pixio models work the day they ship. Hard credit guardrails (per-job cap + session budget) make it safe to hand to autonomous agents.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) — or any tool that can install a PEP 621 project
- A Pixio API key (`pxio_live_...`)

## Quick start

```sh
git clone <this repo> Pixio-MCP
cd Pixio-MCP
uv sync
```

Set your key and run the server (stdio transport — it waits for an MCP client on stdin/stdout):

```sh
# PowerShell
$env:PIXIO_API_KEY = "pxio_live_..."
uv run pixio-mcp
```

Normally you won't run it by hand — register it with your MCP client instead (below). If `PIXIO_API_KEY` is unset the server still boots (it logs a warning to stderr) and every tool call returns an `AUTH` error until the key is provided.

## Registering with Claude Desktop

Edit your Claude Desktop config (`%APPDATA%\Claude\claude_desktop_config.json` on Windows, `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS) and add one of the following under `mcpServers`.

**Local checkout** (run straight from this repository):

```json
{
  "mcpServers": {
    "pixio": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\Users\\VR\\projects\\Pixio-MCP", "pixio-mcp"],
      "env": { "PIXIO_API_KEY": "pxio_live_..." }
    }
  }
}
```

**Published package** (once `pixio-mcp` is on PyPI — `uvx` fetches and runs it in one step):

```json
{
  "mcpServers": {
    "pixio": {
      "command": "uvx",
      "args": ["pixio-mcp"],
      "env": { "PIXIO_API_KEY": "pxio_live_..." }
    }
  }
}
```

Restart Claude Desktop after editing the config.

## Registering with Claude Code

One-liner from any terminal:

```sh
# Local checkout
claude mcp add pixio -e PIXIO_API_KEY=pxio_live_... -- uv run --directory C:\Users\VR\projects\Pixio-MCP pixio-mcp

# Published package
claude mcp add pixio -e PIXIO_API_KEY=pxio_live_... -- uvx pixio-mcp
```

Or add the same JSON block shown above to your project's `.mcp.json`.

## Configuration

All configuration is via environment variables:

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `PIXIO_API_KEY` | yes | — | Bearer token (`pxio_live_...`). Never logged, never echoed in tool output. |
| `PIXIO_BASE_URL` | no | `https://beta.pixio.myapps.ai/api/v1` | Override if the API moves off beta. Trailing slash and missing `/api/v1` suffix are normalized for you. |
| `PIXIO_MAX_CREDITS_PER_JOB` | no | `60` | Per-job credit cap. Jobs estimated above this are refused without `confirm=true`. |
| `PIXIO_SESSION_BUDGET` | no | `300` | Cumulative credit cap per server process. |
| `PIXIO_DEFAULT_TIMEOUT_S` | no | `180` | Default wait timeout for `generate(wait=true)` and `wait_for_generation`. |
| `PIXIO_DOWNLOAD_DIR` | no | `~/pixio-outputs` | Default destination for `download_output`. |
| `PIXIO_LOG_LEVEL` | no | `INFO` | Logging level. Logs go to **stderr** as JSON lines (stdout carries the MCP protocol). |

Invalid integer values (e.g. `PIXIO_SESSION_BUDGET=lots`) fail fast with a `VALIDATION` error naming the variable.

## Tools

| Tool | Purpose | Key inputs |
|---|---|---|
| `list_models` | Filterable model catalog (cached 10 min). Returns id, name, type, per-run credits, company, description. | `type` (exact match, e.g. `"text-to-image"`), `query` (substring over id/name/description), `limit` (1–200, default 50), `offset` |
| `get_model_params` | Exact input schema for one model — names, types, required flags, defaults, allowed `options`. Verbatim API passthrough. | `model_id` |
| `estimate_cost` | Credit estimate for a model + params payload *before* spending. Falls back to the catalog-listed cost if the estimate endpoint errors. | `model_id`, `params` |
| `upload_media` | Local file path **or** remote URL → permanent public Pixio URL (`pixiomedia.nyc3.digitaloceanspaces.com`) for use in generation params. | `source` (path or http(s) URL) |
| `generate` | Run a job: rejects local paths, estimates, enforces budget caps, submits, and (by default) waits for the result. | `model_id`, `params`, `wait` (default `true`), `timeout_s`, `confirm` (default `false`) |
| `get_generation` | One-shot status check + output URLs for a job. | `generation_id` |
| `wait_for_generation` | Poll a job until `succeeded`/`failed` or timeout. Resumes jobs that outlived a `generate` timeout. | `generation_id`, `timeout_s` |
| `download_output` | Save all output files of a *succeeded* generation to a local directory. Returns absolute local paths. | `generation_id`, `dest_dir` (optional) |
| `get_credits` | Account balance (`total`, `recurring`, `permanent`), optionally with recent ledger entries. | `include_ledger_tail` (default `false`), `ledger_limit` (default 10) |

Every tool returns a JSON dict. Failures come back as structured error dicts (see [Error taxonomy](#error-taxonomy)) — tools never raise raw exceptions at the caller.

## The three-call discovery contract

The server ships knowing nothing about any specific model. The calling LLM discovers everything at runtime:

1. **`list_models`** — find a model by type and keyword.
2. **`get_model_params`** — fetch its exact input schema.
3. **`generate`** — build `params` from that schema and run.

Add **`download_output`** and a prompt becomes a file on disk in four calls.

### Example transcript

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
  "files": ["C:\\Users\\VR\\pixio-outputs\\b7e2f9c1-0.png"],
  "dest_dir": "C:\\Users\\VR\\pixio-outputs"
}
```

For models that take media inputs (image-to-video, lipsync, ...), call `upload_media` first and pass the returned URL in `params` — `generate` accepts **URLs only** and rejects local paths before any credits are spent.

## Spend safety

Credit guardrails are enforced server-side and are non-negotiable by default:

- **Estimate before spend.** Every `generate` call is priced first via the estimate endpoint (with a fallback to the catalog-listed cost). Nothing is submitted until the price is known — or explicitly acknowledged as unknown via a `warning` in the result.
- **Per-job cap** (`PIXIO_MAX_CREDITS_PER_JOB`, default 60). Any single job estimated above the cap is refused with `BUDGET_EXCEEDED`.
- **Session budget** (`PIXIO_SESSION_BUDGET`, default 300). Cumulative spend across the server process's lifetime; a job that would push the session over budget is refused with `BUDGET_EXCEEDED`.
- **Explicit override.** A `BUDGET_EXCEEDED` refusal states the estimate, which cap was hit, and the cap value — and can only be overridden by re-calling with `confirm=true`. The server never overrides itself.
- **Balance on every result.** Every terminal job result reports `credits_spent` and `remaining_balance`, so spend drift is visible immediately. `get_credits` gives the full balance breakdown plus an optional ledger tail.
- **No auto-retry on submission.** `POST /generate` is never retried automatically, so a network blip can't double-spend. (Idempotent reads and estimates are retried up to 3x.)

## Error taxonomy

Failed tool calls return `{"error": {"code": ..., "message": ..., "details": {...}}}`. All nine codes:

| Code | Meaning | What the caller should do |
|---|---|---|
| `AUTH` | 401 from the API, or `PIXIO_API_KEY` is missing/empty. | Set a valid `pxio_live_...` key in the server's `env` and restart the client. |
| `INSUFFICIENT_CREDITS` | 402 — the account balance can't cover the job. `details` includes `availableCredits`, `requiredCredits`, `shortfall` when the API provides them. | Top up the account, or pick a cheaper model (`list_models` shows per-run credits). |
| `VALIDATION` | Bad or missing parameter; also raised when `generate` params contain a local file path. The message surfaces the API's error body verbatim (e.g. `Missing required parameter: X`) or names the offending field. | Re-read `get_model_params` and fix the payload. For local paths, run `upload_media` and pass the returned URL. |
| `BUDGET_EXCEEDED` | The server's own guardrail refused the job (per-job cap or session budget). Nothing was spent. | Verify the estimate is acceptable, then re-call with `confirm=true` — or raise the cap via env vars. |
| `CONCURRENCY` | 429 — the account's in-flight generation limit is reached. `details` carries `concurrencyLimit` when the API reports it. | Wait for in-flight jobs to finish (`wait_for_generation` on their ids), then resubmit. Do not hammer retries. |
| `GENERATION_FAILED` | The job reached terminal `failed` status. `details.provider_reason` carries the provider's reason string. | Read the reason, adjust prompt/params, and submit a new job (retrying spends credits again). |
| `TIMEOUT_PENDING` | The wait window elapsed but the job is **still running** — it was not cancelled. `details` includes `generation_id` and a hint. | Call `wait_for_generation(generation_id)` (or `get_generation`) to resume; the job completes server-side either way. |
| `NOT_FOUND` | 404 — unknown model id or generation id. | Check the id; discover valid model ids via `list_models`. |
| `UPSTREAM_ERROR` | 5xx, network failure, or an unparseable response from Pixio. | Retry later. GETs and estimates were already retried 3x with backoff before this surfaced. |

## Gotchas

Hard-won quirks of the live Pixio gateway. The server stays schema-agnostic and does **not** enforce these — callers must respect them:

- **Select values are strings.** For `select` params, send `options[].value` exactly as given — as a string. Send `"5"`, not `5`, even when the value looks numeric.
- **"Optional" params may still be required.** Some params marked optional-with-default are rejected by the gateway when omitted. On a first attempt, send *every* param listed by `get_model_params` at its `defaultValue`.
- **Output URLs can expire in ~1 hour.** A generation's `outputUrl` may be a signed URL with a short lifetime. Call `download_output` promptly after success; don't stash URLs for later. (URLs returned by `upload_media` are the exception — those are permanent and public.)
- **Account-wide concurrency limit.** In-flight generations are limited per account, not per client: **1 by default, 3 on the Maker plan**. Parallel fan-outs will hit `CONCURRENCY` — serialize jobs or wait between submissions.
- **No cancel API.** Once submitted, a job runs to completion; `DELETE /generations/{id}` is not supported. A `TIMEOUT_PENDING` job keeps occupying your concurrency slot until it finishes, so budget your `timeout_s` accordingly and resume with `wait_for_generation` rather than resubmitting.
- **There is no list-generations endpoint.** Keep the `generation_id` from every submission — it's the only handle you get.

## Development

```sh
uv sync
uv run pytest
```

Tests run fully offline against a mocked Pixio gateway — no API key, no network, no credits spent.

## License

MIT

# PRD: Pixio MCP Server (`pixio-mcp`)

**Owner:** Valentino Rivera · **Status:** Draft v1.0 · **Date:** 2026-07-11
**Provider API:** `https://beta.pixio.myapps.ai/api/v1` (Pixio media generation platform)

---

## 1. Summary

Build an MCP server that exposes the Pixio media generation API to any MCP client (Claude Desktop, Claude Code, vr-dispatch agents). Pixio hosts 554+ models across text-to-image, image-to-video, video-to-video, lipsync, text-to-audio, and utility operations, all credit-metered against a single account balance. The server provides the full generation lifecycle — discover models, inspect input schemas, upload media, price a job, run it, poll to completion, download outputs — with hard credit guardrails so autonomous agents cannot burn the balance.

## 2. Problem

Using Pixio from an agent today means hand-rolled HTTP calls, manual polling of `/generations/{id}`, and zero spend control. Hardcoding one MCP tool per model is not viable: the catalog is 554 models and growing, each with a different parameter schema, and schemas are only knowable at runtime via `GET /params?modelId=`. The server must therefore be schema-agnostic and discovery-driven.

## 3. Goals

1. Full lifecycle from any MCP client: discover → inspect params → price → generate → poll → download.
2. Zero hardcoded model knowledge. All schemas fetched live from `/params`; the server ships knowing nothing about any specific model.
3. Spend safety. Estimate-before-spend, per-job and per-session credit caps, balance surfaced on every job result.
4. Local-file friendly. A dedicated upload tool converts local paths or remote links into clean Pixio URLs for use in generation params.
5. Agent ergonomics. Structured JSON results, a stable error taxonomy, and id-based resumability so a timed-out job can be picked up later.

### Non-goals

No UI. No webhook infrastructure (the API is poll-only). No model quality curation or prompt libraries. No multi-account support. Workflow authoring is out of scope (run-only, deferred to v2).

## 4. Users and primary use cases

**Interactive (VR via Claude Desktop / claude.ai):** one-off marketing and content assets — podcast thumbnails, captioned clips, voiceover audio, background removal on client photos.

**Autonomous (Iron Jarvis / vr-dispatch):** chained pipelines, e.g. podcast episode → extract frame → generate thumbnail variants → caption a highlight clip; or product photo → background removal → lifestyle composite.

**Representative flow:** agent calls `list_models(type="text-to-image", query="flux")` → `get_model_params("pixio/flux-1/schnell")` → `generate(model_id, params, wait=true)` → `download_output(generation_id, dest)`. Four calls, done.

## 5. Architecture

| Decision | Choice | Rationale |
|---|---|---|
| Language / framework | Python 3.11+, FastMCP (official MCP Python SDK) | Matches existing stack (FastAPI, Faster Whisper service). TypeScript + `@modelcontextprotocol/sdk` is an acceptable alternative if preferred at build time. |
| Transport | stdio | Local-first, fits Claude Desktop / Claude Code / vr-dispatch. Streamable HTTP deferred to v2 for LAN access from other machines. |
| HTTP client | httpx with retries | Async, timeout control. |
| State | Stateless; in-memory cache only | Model catalog cached 10 min; session budget counter in memory. No database. |
| Auth | `PIXIO_API_KEY` env var → Bearer header | Never logged, never echoed in tool output. |

Component sketch: MCP tool layer → `PixioClient` (httpx, retry policy, error mapping) → `beta.pixio.myapps.ai/api/v1`.

## 6. Tool surface

### v1 tools

| # | Tool | Pixio endpoint | Purpose |
|---|---|---|---|
| 1 | `list_models` | `GET /models` | Filterable catalog: `type`, `query`, `limit`/`offset`. Returns id, name, type, credit cost. Cached 10 min. |
| 2 | `get_model_params` | `GET /params?modelId=` | Exact input schema for one model: names, types, required flags, allowed options, defaults. Passed through verbatim. |
| 3 | `estimate_cost` | `POST /generations/estimate` | Credit estimate for a model + params payload before spending. |
| 4 | `upload_media` | `POST /media` (or `/images`) | Local file path or remote URL → clean permanent Pixio URL. Returns `url`. |
| 5 | `generate` | `POST /generate` | Run a job. See detailed spec below. |
| 6 | `get_generation` | `GET /generations/{id}` | Current status + output URLs for one job. |
| 7 | `wait_for_generation` | `GET /generations/{id}` (poll) | Poll until terminal status or timeout. |
| 8 | `download_output` | (fetch output URL) | Save output file(s) of a succeeded generation to a local directory. Returns local paths. |
| 9 | `get_credits` | `GET /credits` | Balance; optional `include_ledger_tail` for recent spend via `/credits/ledger`. |

### `generate` — detailed spec

Inputs: `model_id` (string, required), `params` (object, required — built by the caller from `get_model_params` output), `wait` (bool, default `true`), `timeout_s` (int, default `180`), `confirm` (bool, default `false`).

Behavior:

1. Reject any `params` value that looks like a local filesystem path, with an error instructing the caller to run `upload_media` first. The generate contract is URLs-only.
2. Pre-flight: call `estimate_cost`. If estimate (or catalog-listed cost as fallback) exceeds `PIXIO_MAX_CREDITS_PER_JOB`, or would exceed `PIXIO_SESSION_BUDGET`, refuse with `BUDGET_EXCEEDED` unless `confirm=true`.
3. POST `/generate`. Never auto-retry this call (spend safety).
4. If `wait=true`, poll `/generations/{id}` with backoff (2s → 10s cap, jitter) until `succeeded`/`failed` or `timeout_s`. On timeout, return `TIMEOUT_PENDING` with the generation id so the caller can resume via `wait_for_generation`.
5. Result always includes: generation id, status, output URLs (if any), credits spent, remaining balance, elapsed time.

### v1.1

`upload_asset` (`POST /uploads` → `filePath` + signed URL for account-stored assets), `optimize_prompt` (`POST /prompts/optimize`), `delete_generation` (`DELETE /generations/{id}`).

### v2

Workflows (`list_workflows`, `run_workflow`, `get_workflow_run`), asset CRUD (`/assets`), streamable HTTP transport, persisted budget ledger.

## 7. Key design decisions

**D1 — Discovery pattern over static tools.** 554 models with heterogeneous schemas make per-model tools impossible. The contract is three calls: list → params → generate. The server embeds no schemas; the calling LLM constructs `params` from the live `/params` response. This also means new Pixio models work on day one with zero server changes.

**D2 — Sync-by-default with an async escape hatch.** Video jobs can run minutes. `wait=true` covers the common case; the timeout path returns a resumable id rather than hanging or losing the job. `wait=false` exists for fire-and-forget pipelines.

**D3 — Credit guardrails are non-negotiable.** Two caps enforced server-side: `PIXIO_MAX_CREDITS_PER_JOB` (default 60 — covers everything through Gen-4 image-to-video at 50) and `PIXIO_SESSION_BUDGET` (default 300 per server process). Anything over cap requires an explicit `confirm=true` from the caller. Every job result reports spend and balance so drift is visible immediately.

**D4 — URLs-only in `generate`.** Media conversion is `upload_media`'s job. Keeping the generate contract narrow makes it far more reliable for LLM callers and prevents silent double-uploads.

**D5 — Structured error taxonomy.** `AUTH` (401), `INSUFFICIENT_CREDITS`, `VALIDATION` (bad/missing param, named field), `BUDGET_EXCEEDED`, `CONCURRENCY` (account in-flight limit hit), `GENERATION_FAILED` (terminal `failed` status, provider reason attached), `TIMEOUT_PENDING` (with id), `NOT_FOUND`. Every error is machine-actionable.

**D6 — Secrets hygiene.** Authorization header redacted from all logs. Signed URLs flagged as expiring (~7 days). No media contents ever logged.

## 8. Configuration

| Env var | Required | Default | Purpose |
|---|---|---|---|
| `PIXIO_API_KEY` | yes | — | Bearer token (`pxio_live_...`) |
| `PIXIO_BASE_URL` | no | `https://beta.pixio.myapps.ai/api/v1` | Pin/override for API moves off beta |
| `PIXIO_MAX_CREDITS_PER_JOB` | no | `60` | Per-job cap |
| `PIXIO_SESSION_BUDGET` | no | `300` | Per-process cumulative cap |
| `PIXIO_DEFAULT_TIMEOUT_S` | no | `180` | Default `wait` timeout |
| `PIXIO_DOWNLOAD_DIR` | no | `~/pixio-outputs` | Default `download_output` destination |
| `PIXIO_LOG_LEVEL` | no | `INFO` | stderr JSON-lines logging |

Claude Desktop / Claude Code registration:

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

## 9. Non-functional requirements

Polling backoff 2s → 10s with jitter. Idempotent GETs retried 3x with exponential backoff; `POST /generate` never auto-retried. On account concurrency limit, surface `CONCURRENCY` immediately — no internal queueing in v1. Tool overhead (excluding provider time) under 300ms. Logs to stderr as JSON lines: job lifecycle at INFO, never request media payloads, never the API key.

## 10. Phasing

**v1 (build target):** tools 1–9, guardrails, error taxonomy, stdio transport, README with Claude Desktop config.
**v1.1:** `upload_asset`, `optimize_prompt`, `delete_generation`.
**v2:** workflows, asset CRUD, streamable HTTP, persisted spend ledger.

## 11. Acceptance criteria

1. From a clean environment with only `PIXIO_API_KEY` set, `list_models(type="text-to-image")` returns results in under 2 seconds.
2. End-to-end: Flux Schnell prompt → PNG on local disk, with credits spent and balance reported, in at most 4 tool calls including discovery.
3. A job estimated above `PIXIO_MAX_CREDITS_PER_JOB` is blocked with `BUDGET_EXCEEDED` and a message stating the estimate, the cap, and the `confirm=true` override; with `confirm=true` it proceeds.
4. `generate` with a 5s timeout on a video model returns `TIMEOUT_PENDING` + id; a later `wait_for_generation(id)` completes the same job.
5. A local path passed inside `generate` params is rejected with a `VALIDATION` error naming the offending field and pointing to `upload_media`, before any credits are spent.
6. `upload_media` with a local PNG returns a `pixiomedia.nyc3.digitaloceanspaces.com` URL usable directly as an `image_url` param.
7. The API key never appears in logs or tool output (verified by grep across a full test session).
8. A `failed` generation surfaces `GENERATION_FAILED` with the provider's reason string.

## 12. Risks and open questions

1. **Beta API stability.** Endpoints may move or change shape. Mitigation: `PIXIO_BASE_URL` override plus contract tests generated against `/openapi.json` in CI.
2. **Estimate coverage.** Unknown whether `/generations/estimate` works for all 554 models. Fallback: catalog-listed credit cost from `/models`.
3. **Output URL retention.** Retention window on DigitalOcean Spaces outputs is undocumented. Mitigation: `download_output` promptly; treat URLs as ephemeral.
4. **DELETE semantics.** Whether `DELETE /generations/{id}` cancels an in-flight job or only deletes the record is unverified — test before exposing as "cancel."
5. **Concurrency limit value.** The account in-flight limit is undocumented; surface it from error responses rather than assuming.
6. **No webhooks.** Polling only; long video jobs occupy agent turns unless callers use `wait=false`.

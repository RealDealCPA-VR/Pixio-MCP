# pixio-mcp — Build Contract (v1)

This is the binding design contract for all builder/test/validator agents. Read this AND `pixio-mcp-prd.md` before writing code. Where the PRD and this contract disagree, this contract wins (it encodes live-API ground truth probed 2026-07-11).

## Repo layout & file ownership

```
pixio-mcp-prd.md          (spec — read only)
CONTRACTS.md              (this file — read only)
pyproject.toml            (scaffold — integrator may edit deps)
src/pixio_mcp/
  __init__.py             (scaffold: __version__ = "0.1.0")
  runtime.py              (scaffold — read only, DI pattern below)
  config.py               (B1)
  errors.py               (B1)
  client.py               (B2)
  budget.py               (B3)
  cache.py                (B3)
  pathguard.py            (B6 — local-path detection helper)
  tools/__init__.py       (scaffold)
  tools/catalog.py        (B4)
  tools/credits.py        (B4)
  tools/media.py          (B5)
  tools/generation.py     (B6)
  server.py               (INTEGRATOR ONLY)
tests/
  conftest.py             (T1)
  test_config.py, test_errors.py, test_budget.py, test_cache.py, test_client.py   (T1)
  test_catalog.py, test_credits.py, test_media.py  (T2)
  test_generation.py, test_pathguard.py, test_redaction.py, test_acceptance.py    (T3)
  test_server.py          (T3)
README.md                 (B7)
```

**Rule: write ONLY your owned files.** Do not run `uv sync`, `pytest`, or any build command during the build wave — the integrator does that. Do not create extra top-level files.

## Coding standards

- Python 3.11+ (dev machine runs 3.13), full type hints, `from __future__ import annotations`.
- Everything async (`httpx.AsyncClient`); tools are `async def`.
- **NEVER write to stdout** — stdio transport uses stdout for the MCP protocol. All logging via `logging` to **stderr** as JSON lines (config in `config.py:setup_logging(level)`; one `logging.StreamHandler(sys.stderr)` with a JSON formatter: `{"ts","level","logger","msg", **extra}`).
- The API key must never appear in logs, exceptions, repr()s, or tool results. `Settings.__repr__` must redact it.
- No third-party deps beyond: `mcp` (FastMCP, official SDK), `httpx`. Tests: `pytest`, `pytest-asyncio` (asyncio_mode=auto in pyproject), `anyio` comes with httpx/mcp.
- Docstrings on every MCP tool function are the tool descriptions LLM callers see — write them carefully (state the 3-call contract: list_models → get_model_params → generate).

## Dependency injection — `runtime.py` (already scaffolded, do not edit)

```python
@dataclass
class Runtime:
    settings: Settings
    client: PixioClient
    budget: BudgetGuard
    catalog_cache: TTLCache

init_runtime(rt: Runtime) -> None
get_runtime() -> Runtime          # raises RuntimeError("runtime not initialized")
reset_runtime() -> None           # for tests
```

Tool functions call `get_runtime()` at call time (NOT import time). Tests inject fakes via `init_runtime`.

## Error taxonomy — `errors.py` (B1)

```python
class ErrorCode(str, Enum):
    AUTH = "AUTH"                              # 401 / missing key
    INSUFFICIENT_CREDITS = "INSUFFICIENT_CREDITS"  # 402
    VALIDATION = "VALIDATION"                  # bad/missing param; local path in generate
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"        # guardrail refusal
    CONCURRENCY = "CONCURRENCY"                # 429 account in-flight limit
    GENERATION_FAILED = "GENERATION_FAILED"    # terminal failed status
    TIMEOUT_PENDING = "TIMEOUT_PENDING"        # wait timed out; job still running
    NOT_FOUND = "NOT_FOUND"                    # 404 / unknown model or generation
    UPSTREAM_ERROR = "UPSTREAM_ERROR"          # 5xx / network / unparseable

class PixioError(Exception):
    def __init__(self, code: ErrorCode, message: str, details: dict | None = None): ...
    def to_dict(self) -> dict  # {"error": {"code": ..., "message": ..., "details": {...}}}
```

**Every MCP tool returns a JSON-serializable dict.** On failure, tools catch `PixioError` and return `err.to_dict()` — never raise through to FastMCP. A shared decorator/helper `tool_guard` in `errors.py` wraps tool bodies: catches `PixioError` → `to_dict()`; catches any other exception → `UPSTREAM_ERROR` dict with `type(exc).__name__` (message sanitized — never embed the API key).

## Config — `config.py` (B1)

```python
@dataclass
class Settings:
    api_key: str                     # "" if unset — AUTH error raised at call time, not boot
    base_url: str = "https://beta.pixio.myapps.ai/api/v1"
    max_credits_per_job: int = 60    # PIXIO_MAX_CREDITS_PER_JOB
    session_budget: int = 300        # PIXIO_SESSION_BUDGET
    default_timeout_s: int = 180     # PIXIO_DEFAULT_TIMEOUT_S
    download_dir: Path = ~/pixio-outputs (expanded)   # PIXIO_DOWNLOAD_DIR
    log_level: str = "INFO"          # PIXIO_LOG_LEVEL
    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings  # env injectable for tests
```

Invalid int env values → raise `PixioError(VALIDATION, ...)` naming the var. `base_url` accepts with/without trailing slash and with/without `/api/v1` suffix (normalize: strip trailing `/`; if it doesn't end in `/api/v1`, append it).

## HTTP layer — `client.py` (B2)

```python
class PixioClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None)
    # transport injectable for MockTransport tests. Timeout: httpx.Timeout(30.0, connect=10.0).
    async def aclose(self) -> None

    async def get_models(self) -> list[dict]                 # unwraps {"models":[...]}
    async def get_params(self, model_id: str) -> dict        # verbatim {"model":..., "params":[...]}
    async def estimate(self, model_id: str, params: dict) -> dict   # POST /generations/estimate
    async def generate(self, model_id: str, params: dict) -> str    # returns contentId; NEVER retried
    async def get_generation(self, generation_id: str) -> dict      # verbatim
    async def get_credits(self) -> dict                      # verbatim
    async def get_ledger(self, limit: int = 10) -> list[dict]  # entries[:limit]
    async def upload_file(self, path: Path) -> str           # multipart POST /media → url
    async def upload_url(self, url: str) -> str              # JSON {"url":...} POST /media → url
    async def download(self, url: str, dest: Path) -> int    # streaming GET (follow_redirects=True, no auth header to non-Pixio hosts) → bytes written
```

Retry policy: idempotent GETs and `estimate` retried 3x on network errors and 5xx (backoff 0.5s/1s/2s). `POST /generate` NEVER auto-retried (spend safety). Uploads: single retry on connect error only (never on HTTP error).

HTTP→error mapping (in one place, `_raise_for_response`):
- Parse body JSON; Pixio errors come as `{"error": "..."}"` or `{"error": {...}}` or `{"error": "...", "message": "..."}` — always surface the body text in `message` (the platform hides useful detail in 400 bodies).
- 401 → AUTH. 402 → INSUFFICIENT_CREDITS with `details={availableCredits, requiredCredits, shortfall}` when present.
- 404, or body containing "model not found" → NOT_FOUND.
- 429, or body containing "concurrency limit" → CONCURRENCY with `details={concurrencyLimit,...}` when present. (NOTE: the live gateway returns the concurrency error with HTTP 429; body sample below.)
- Other 4xx → VALIDATION (message = body text; if "Missing required parameter: X" pass through verbatim).
- 5xx / network / JSON-decode failure → UPSTREAM_ERROR.
- Missing/empty api_key → raise AUTH *before* any request ("PIXIO_API_KEY is not set").

Logging: INFO on request start/finish `{method, path, status, elapsed_ms}` — path only, never headers, never params (may contain prompts — log param *keys* only at DEBUG), never the key.

## Budget guard — `budget.py` (B3)

```python
class BudgetGuard:
    def __init__(self, max_per_job: int, session_budget: int)
    @property def session_spent(self) -> int
    def check(self, estimated: int, confirm: bool) -> None
    # raises PixioError(BUDGET_EXCEEDED) if estimated > max_per_job OR session_spent + estimated > session_budget,
    # UNLESS confirm=True. Message must state: the estimate, which cap was hit, cap value, and
    # "pass confirm=true to override". details={"estimated_credits","per_job_cap","session_budget","session_spent"}.
    def record_submit(self, generation_id: str, estimated: int) -> None
    # adds estimated to session_spent and remembers the per-id amount
    def record_actual(self, generation_id: str, actual: int) -> None
    # reconciles: session_spent += (actual - previously recorded for this id); updates per-id amount.
    # Unknown id → += actual. Idempotent: calling twice with the same actual changes nothing.
```

`check` with `confirm=True` never raises. Both caps checked; report whichever tripped (per-job first). Spent totals clamp at >= 0.

## TTL cache — `cache.py` (B3)

```python
class TTLCache:
    def __init__(self, ttl_s: float = 600.0, clock: Callable[[], float] = time.monotonic)
    def get(self, key: str) -> Any | None      # None if missing/expired
    def put(self, key: str, value: Any) -> None
    def clear(self) -> None
```

## Path guard — `pathguard.py` (B6)

`find_local_paths(params: dict) -> list[tuple[str, str]]` — recursive walk (dicts/lists), returns `(dotted.field.path, offending_value)` for every string that looks like a local filesystem reference: starts with `~`, `./`, `../`, `file://`, a Windows drive (`X:\` or `X:/`), UNC `\\`, or exists on disk (`os.path.exists` on strings < 500 chars that don't start with `http://`/`https://`/`data:`). http(s)/data URLs are always allowed.

## Tools — exact MCP surface

All tools return dicts. Common job-result shape (from `generate`, `wait_for_generation`, `get_generation`):

```python
{
  "generation_id": str,
  "status": "succeeded" | "failed" | "processing" | ...,
  "output_urls": [str, ...],        # unique, ordered: outputUrl first, then outputs{} values that are http(s) urls
  "outputs": dict,                  # raw outputs object from API (may be {})
  "model_id": str,
  "credits_spent": int | None,      # creditsCost when present
  "remaining_balance": int | None,  # from GET /credits (total) — fetched after terminal status; None on processing
  "elapsed_s": float,               # time spent in this tool call
  "error": str | None,              # provider reason when failed
}
```

### tools/catalog.py (B4)
- `list_models(type: str | None = None, query: str | None = None, limit: int = 50, offset: int = 0) -> dict`
  Fetch catalog via client (cache key "models", 10-min TTL through `runtime.catalog_cache`). Filter: `type` exact match on model `type`; `query` case-insensitive substring over id+name+description. Returns `{"models": [{id, name, type, credits, company, description}], "total_matching": int, "returned": int, "offset": int}`. Truncate each description to 200 chars. limit clamped 1..200.
- `get_model_params(model_id: str) -> dict` — verbatim `/params` passthrough. NOT_FOUND surfaces cleanly. Note in docstring: treat `options[].value` as the allowed values for select params, send select values as STRINGS, and that some params marked optional-with-default still must be sent (send all listed params at their defaults on first attempt).

### tools/credits.py (B4)
- `estimate_cost(model_id: str, params: dict) -> dict` → `{"model_id", "estimated_credits": int, "source": "estimate"}` from `estimatedCost`. If the estimate endpoint errors (any PixioError except AUTH/INSUFFICIENT_CREDITS), fall back to catalog `credits` → `"source": "catalog"`. If both fail → `{"estimated_credits": None, "source": "unknown"}` plus a `"warning"` string.
- Shared helper (used by tools/generation.py too): `async def resolve_estimate(model_id: str, params: dict) -> tuple[int | None, str, str | None]` returning `(estimated_credits, source, warning)` — module-level public function in tools/credits.py implementing the fallback chain above.
- `get_credits(include_ledger_tail: bool = False, ledger_limit: int = 10) -> dict` → `{"total", "recurring": {...}, "permanent"}` (+ `"ledger_tail": [...]` when requested).

### tools/media.py (B5)
- `upload_media(source: str) -> dict` — if `source` is http(s) URL → `client.upload_url`; else treat as local path (expanduser; must exist and be a file, else VALIDATION). Returns `{"url": str, "source_kind": "local_file"|"remote_url", "file_name": str, "size_bytes": int | None}`. The returned URL is permanent/public (pixiomedia.nyc3.digitaloceanspaces.com) — say so in the docstring.
- `download_output(generation_id: str, dest_dir: str | None = None) -> dict` — GET generation; if status != succeeded → VALIDATION error stating current status (mention wait_for_generation if processing). Download every URL in output_urls to `dest_dir or settings.download_dir` (mkdir parents). Filenames: `{generation_id[:8]}-{index}{ext}` where ext from URL path or Content-Type (image/png→.png, image/jpeg→.jpg, video/mp4→.mp4, audio/mpeg→.mp3, fallback .bin). Returns `{"generation_id", "files": [abs paths], "dest_dir"}`.

### tools/generation.py (B6)
- `generate(model_id: str, params: dict, wait: bool = True, timeout_s: int | None = None, confirm: bool = False) -> dict`
  1. `find_local_paths(params)` → any hit → VALIDATION: "params.<field> looks like a local file path; run upload_media first — generate accepts URLs only", details lists all offending fields. **Before any credits are spent.**
  2. Estimate via `estimate_cost` logic (shared helper import from tools/credits.py is fine). `budget.check(estimated or 0, confirm)`. Unknown estimate (None) → treat as 0 but include warning in result.
  3. `client.generate(model_id, params)` → contentId. (Client injects `providerId: "pixio"` in the POST body.)
  4. `budget.record` the estimate immediately after submission (adjust to actual creditsCost when terminal status observed: record delta).
  5. `wait=False` → return processing-shaped result immediately (`status: "processing"`, `credits_spent: None`, include `"estimated_credits"`).
  6. `wait=True` → poll loop (shared `_poll(generation_id, timeout_s)`): interval starts 2.0s, ×1.5 each iteration, cap 10.0s, ±20% jitter. Terminal: `succeeded` / `failed`. On `failed` → GENERATION_FAILED error dict, `details={"generation_id", "provider_reason": error}`. On timeout → TIMEOUT_PENDING error dict, `details={"generation_id", "timeout_s", "hint": "call wait_for_generation(generation_id) to resume"}`.
  7. Result: job-result shape above; `timeout_s` defaults to `settings.default_timeout_s`.
- `get_generation(generation_id: str) -> dict` — single GET, job-result shape (no balance fetch unless terminal), `elapsed_s` for the call.
- `wait_for_generation(generation_id: str, timeout_s: int | None = None) -> dict` — same poll loop + result/error semantics as generate's wait phase. Also updates budget actuals when terminal creditsCost present (track already-recorded ids in a module-level set on the Runtime? — keep it simple: `BudgetGuard.record_actual(generation_id, actual)` dedupes by id internally; B3 add `self._recorded: dict[str, int]`).

### server.py (INTEGRATOR)
- `mcp = FastMCP("pixio")`; `@mcp.tool()` for the 9 tools (thin wrappers importing from tools/*, preserving signatures + docstrings — simplest: `mcp.tool()(list_models)` etc.).
- `main()`: `Settings.from_env()` → `setup_logging` → build `Runtime` → `init_runtime` → `mcp.run()` (stdio). Missing API key logs a WARNING to stderr but the server still boots (tools return AUTH errors).
- `[project.scripts] pixio-mcp = "pixio_mcp.server:main"`.

## Live API ground truth (probed 2026-07-11 — trust this over guesses)

Base `https://beta.pixio.myapps.ai/api/v1`. Auth `Authorization: Bearer pxio_live_...`.

- `GET /models` → `{"models": [{id, providerId, name, description, type, credits, company, inputs: [...]}]}`. 554 models. Types include: text-to-image, image-to-image, image-to-video, text-to-video, video-to-video, text-to-audio, ...
- `GET /params?modelId=...` → `{"model": {...}, "params": [{name, type, label, required, defaultValue, placeholder?, options?: [{value, label}]}]}`. Select options live under `options[].value` (there is NO `.values` array).
- `POST /generations/estimate` body `{providerId:"pixio", modelId, params}` → `{"success": true, "modelId", "currency": "credits", "baseCost": 1, "estimatedCost": 1}`. Use `estimatedCost`.
- `POST /generate` body `{providerId:"pixio", modelId, params}` → `{"success", "message", "contentId", "providerId", "modelId"}`. **The id field is `contentId`, NOT `id`.**
- `GET /generations/{id}` → `{"id", "status", "type", "providerId", "modelId", "params", "outputUrl", "outputs": {imageUrl?/videoUrl?/thumbnailUrl?...}, "assetVariants", "error", "creditsCost", "createdAt", "updatedAt", "billedAt"}`. (This endpoint calls the same value `id`.) Statuses: `processing` → `succeeded` | `failed`.
- `GET /credits` → `{"accountId", "total", "recurring": {current, quota, lastTopOffAt}, "permanent"}`.
- `GET /credits/ledger` → `{"entries": [{id, reason, deltaRecurring, deltaPermanent, sourceId, createdAt}]}`.
- `POST /media` (any media) / `POST /images` (images only): multipart `file=@...` OR JSON `{"url": "..."}` mirror → `{"url": "https://pixiomedia.nyc3.digitaloceanspaces.com/uploads/..."}` — permanent public URL, no signed query string.
- Error bodies: `{"error": "..."}` string, sometimes with extra fields: 402 → `{"error":"Insufficient credits","availableCredits","requiredCredits","shortfall"}`; 429 → `{"error":"This account has reached its API concurrency limit of 3...","generationId","status","concurrencyLimit"}`; invalid media → `{"error":"invalid_media_url","message":"..."}`; unknown model → `{"error":"Pixio API model not found"}`.
- Account concurrency: shared account-wide (1 in-flight default, 3 Maker). No cancel API (`DELETE /generations/{id}` → 405). No list-generations endpoint.
- Quirks callers must be told about via docstrings (server stays schema-agnostic, do NOT enforce): select values must be sent as strings (`"5"` not `5`); some optional-with-default params are actually required by the gateway (send all listed params at defaults on first try); generation `outputUrl` may be a signed URL that expires ~1h — download promptly.
- `pixio/flux-1/schnell` = 1 credit, text-to-image (cheap live-test model).

## Test requirements (T1–T3)

- Offline only: `httpx.MockTransport` injected via `PixioClient(settings, transport=...)`; `Settings.from_env(env={...})` — never read real env in unit tests, never hit the network.
- `tests/conftest.py` (T1) provides EXACTLY these fixtures (T2/T3 code against them):

```python
TEST_KEY = "pxio_live_TESTSECRET123"   # sentinel scanned for in redaction tests

class MockAPI:
    """Programmable fake Pixio gateway for httpx.MockTransport."""
    requests: list[httpx.Request]           # every request seen, in order
    def __init__(self): ...                 # installs DEFAULT routes (below)
    @property
    def transport(self) -> httpx.MockTransport
    def on(self, method: str, path_suffix: str, response: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> None
    # method upper-case; path_suffix matched against the END of request path, e.g. "/generations/estimate".
    # Later .on() registrations override earlier/default ones. Callables receive the request.

# DEFAULT routes (all 200 unless stated): GET /models → {"models": [flux-schnell(1c, text-to-image),
# nano-banana-edit(4c, image-to-image), kling-master(295c, image-to-video)]};
# GET /params → flux-schnell params (prompt string + image_size select with options[].value);
# POST /generations/estimate → {"success":true,"modelId":...,"currency":"credits","baseCost":1,"estimatedCost":1};
# POST /generate → {"success":true,"message":"ok","contentId":"gen-123",...};
# GET /generations/gen-123 → succeeded, outputUrl https://cdn.example/out.png, outputs {imageUrl: same}, creditsCost 1;
# GET /credits → {"accountId":"acc","total":1000,"recurring":{"current":1000,"quota":15000,"lastTopOffAt":"..."},"permanent":0};
# GET /credits/ledger → {"entries":[...2 entries...]};
# POST /media → {"url":"https://pixiomedia.nyc3.digitaloceanspaces.com/uploads/test.png"};
# GET https://cdn.example/out.png → 200 PNG magic bytes b"\x89PNG\r\n\x1a\n" + padding, content-type image/png.

@pytest.fixture
def mock_api() -> MockAPI

@pytest.fixture
def settings() -> Settings        # api_key=TEST_KEY, defaults otherwise (caps 60/300), download_dir=tmp_path/"outputs"

@pytest.fixture
def runtime(settings, mock_api) -> Runtime
# PixioClient(settings, transport=mock_api.transport), BudgetGuard(60, 300), TTLCache(600),
# init_runtime(...); yields Runtime; teardown: await client.aclose() best-effort + reset_runtime()
```

- pytest config in pyproject: `asyncio_mode = "auto"`.
- Must cover (acceptance-criteria mapping — mark with comments `# AC-n`):
  - AC3: estimate over per-job cap → BUDGET_EXCEEDED dict with estimate+cap+confirm hint; same call `confirm=True` → proceeds (mock generate succeeds).
  - AC4: wait timeout → TIMEOUT_PENDING with generation_id; `wait_for_generation(id)` later completes against mock.
  - AC5: local path (Windows `C:\x.png`, posix `./x.png`, `~`, UNC, file://) inside nested params → VALIDATION naming field, and MockTransport proves NO /generate request was made.
  - AC7: full mock session then assert the key string never appears in captured logs (caplog) nor in any tool result dict (json.dumps scan).
  - AC8: failed generation → GENERATION_FAILED with provider reason.
  - Client: 5xx GET retried 3x then UPSTREAM_ERROR; POST /generate 5xx NOT retried (exactly 1 request captured); 401/402/404/429 mappings; hidden-400-body message surfaced.
  - Budget: per-job trip, session trip, confirm override, record accumulation, record_actual dedupe.
  - Cache: hit, expiry via fake clock, list_models uses cache (1 upstream call for 2 invocations).
  - Catalog filters: type, query, limit/offset clamping.
  - Media: URL mirror vs local multipart branch; missing file VALIDATION; download_output on processing → VALIDATION; filename ext inference.
  - Config: defaults, env overrides, bad int → VALIDATION, base_url normalization, repr redaction.

## Live smoke plan (validators only — NOT unit tests)

Env: real `PIXIO_API_KEY`. Keep `PIXIO_SESSION_BUDGET=25` for the smoke process. Sequence: list_models(type=text-to-image, query=flux) → get_model_params(pixio/flux-1/schnell) → generate(prompt, wait=true) → download_output → verify PNG/JPEG magic bytes on disk. Budget-block test: estimate_cost on an expensive model + generate WITHOUT confirm → expect BUDGET_EXCEEDED (no spend). TIMEOUT_PENDING test: generate(schnell, timeout_s=1) → expect TIMEOUT_PENDING or fast success; if pending → wait_for_generation resumes. Total spend target ≤ 5 credits.

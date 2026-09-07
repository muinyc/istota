# Native brain — operator runbook

Istota has three model-invocation backends behind one protocol:

- **`claude_code`** (default) — wraps the headless `claude -p` CLI subprocess. Battle-tested; delegates the agentic loop, tool use, and context management to Claude Code.
- **`native`** — istota's own in-process agent loop against an OpenAI-compatible provider. Gives istota direct control over the loop, tool execution, context compaction, and model selection.
- **`tmux_claude`** — drives the interactive `claude` TUI in a detached tmux session (keeps traffic on subscription billing), with a launch circuit breaker. Set `[brain] fallback = "claude_code"` to reroute an unavailable tmux primary. Configured under `[brain.tmux]`; see `config.example.toml` for the full block.

All coexist permanently and are switchable per instance or per task. Switching does not touch executor orchestration (memory, skills, sandbox, deferred writes) — only which `Brain` implementation runs.

This runbook covers the `native` backend.

## Enabling the native brain

Instance-wide:

```toml
[brain]
kind = "native"

[brain.native]
provider = "openai_compat"                 # only provider currently
model = "claude-sonnet-4-6"                # explicit id — openai_compat has no aliasing
effort = ""                                # default reasoning effort (see below)
base_url = "https://api.anthropic.com/v1"  # any OpenAI-compatible endpoint
max_turns = 100                            # hard cap on assistant turns per task
max_tokens = 16384                         # per-completion output cap
# prompt_caching                           # omit to derive from base_url (see below)
```

The API key never goes in the TOML file. Set it via the env override:

```
ISTOTA_BRAIN_NATIVE_API_KEY=sk-...
```

(loaded from the systemd `EnvironmentFile=`, direnv, or `.env`).

### Model id format

- `openai_compat` needs an **explicit** model id (e.g. `claude-sonnet-4-6`). It does not understand role aliases (`smart`) or Claude-CLI short names (`opus`).
- The `claude_code` brain (default) keeps Claude Code's aliasing — `opus` resolves to the latest Opus. The native brain does not; map role names with `[models.aliases]` if you want them.

### OpenRouter

`openai_compat` targets any OpenAI chat-completions endpoint (`src/istota/llm/openai_compat.py`), so OpenRouter is just a `base_url` + `model` + key:

```toml
[brain]
kind = "native"

[brain.native]
provider = "openai_compat"
base_url = "https://openrouter.ai/api/v1"
model = "anthropic/claude-sonnet-4"   # OpenRouter ids are slash-namespaced: <vendor>/<model>
prompt_caching = true                 # REQUIRED for OpenRouter — see below
```

```
ISTOTA_BRAIN_NATIVE_API_KEY=sk-or-v1-...
```

Two things to get right:

- **Model id.** OpenRouter uses slash-namespaced ids (`anthropic/claude-sonnet-4`, `openai/gpt-4o`, `google/gemini-2.5-pro`). Because `openai_compat` does no aliasing (above), paste the id exactly as OpenRouter lists it on its models page — a bare `claude-sonnet-4-6` will not resolve there.
- **Prompt caching must be explicit.** The auto-default only turns caching on when the base_url contains `api.anthropic.com` (`make_provider` in `src/istota/llm/__init__.py`). For an `openrouter.ai` base_url it defaults **off**, so set `prompt_caching = true` (Ansible: `istota_brain_native_prompt_caching: true`) to get `cache_control` breakpoints on caching-capable models routed through OpenRouter.

## Ansible deployment

The role renders the `[brain]` block from inventory variables. The `[brain.native]` and `[brain.source_type_overrides]` tables are only written when `istota_brain_kind` is `native`, `istota_brain_fallback` is `native`, or `istota_brain_source_type_overrides` is non-empty, so existing deployments stay byte-identical until you opt in. After templating, `files/validate_config.py` parses the rendered config and gates the scheduler restart, so a malformed brain block fails the play instead of the running daemon.

Instance-wide native brain:

```yaml
istota_brain_kind: "native"
istota_brain_native_provider: "openai_compat"
istota_brain_native_model: "claude-sonnet-4-6"
istota_brain_native_base_url: "https://api.anthropic.com/v1"
istota_brain_native_effort: ""                    # default reasoning effort (thinking models only)
# istota_brain_native_prompt_caching             # "" (default) derives from base_url; set true/false to force
istota_brain_native_api_key: "{{ vault_native_api_key }}"   # → ISTOTA_BRAIN_NATIVE_API_KEY
```

Gradual rollout (keep the default brain, move background work to native):

```yaml
istota_brain_kind: "claude_code"
istota_brain_native_model: "claude-sonnet-4-6"
istota_brain_native_api_key: "{{ vault_native_api_key }}"
istota_brain_source_type_overrides:
  scheduled: native
  heartbeat: native
```

The full variable set is documented in `deploy/ansible/defaults/main.yml`: `istota_brain_native_{provider,model,effort,base_url,extra_headers,context_window,max_turns,max_tokens,model_catalog_fetch,model_catalog_cache_ttl_hours,prompt_caching,bash_spill_full_output,turn_budget_nudge,turn_budget_nudge_early_percent,turn_budget_nudge_remaining,soft_deadline_percent,api_key}`, the `istota_brain_native_web_fetch_*` family, and `istota_brain_source_type_overrides`. `istota_brain_native_prompt_caching` defaults to `""` (derive from `base_url`); set it to `true`/`false` only to force.

!!! warning "The Ansible defaults are not the code defaults"
    Several Ansible variables ship opinionated values rather than mirroring the dataclass:

    | Variable | Ansible default | Code default |
    |---|---|---|
    | `istota_brain_native_model` | `z-ai/glm-5.2` | `""` |
    | `istota_brain_native_base_url` | `https://openrouter.ai/api/v1` | `https://api.anthropic.com/v1` |
    | `istota_brain_native_effort` | `medium` | `""` |
    | `istota_brain_native_max_tokens` | `32000` | `16384` |
    | `istota_brain_fallback_cooldown_seconds` | `3600` | `900` |
    | `istota_brain_fallback` | `claude_code` when `istota_brain_kind` is `tmux_claude`, `""` otherwise | `""` |

    The base_url one has a consequence worth spelling out: because a stock Ansible deploy points at OpenRouter, the "prompt caching defaults off for a non-Anthropic base_url" note above applies to it. Set `istota_brain_native_prompt_caching: true` if you want caching there.

## `[brain.native.web_fetch]`

The native harness ships its own daemon-side `WebFetch` tool. It runs in the daemon's network namespace, so it is *not* gated by the sandbox CONNECT allowlist — but it is credential-free (no cookies, `trust_env=False`) and SSRF-hardened: every resolved IP is validated against a private/reserved blocklist on each request and each redirect hop, the connection is pinned to the validated IP to close DNS rebinding, and it is GET/text-only with size and time caps. Fetched content is wrapped in an untrusted-content delimiter.

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Master switch; false omits the tool entirely |
| `allow_http` | `false` | Permit cleartext `http://` |
| `timeout_seconds` | `20` | Total wall-clock per fetch |
| `max_bytes` | `5000000` | Response body cap (streamed) |
| `max_content_chars` | `100000` | Extracted-text cap returned to the model |
| `max_redirects` | `5` | Redirect hops before giving up |
| `require_url_provenance` | `false` | Only fetch URLs that appeared in the task prompt — for sensitive deployments. The corpus is the prompt, never a prior tool result, so this also blocks a WebSearch-then-read chain |
| `admin_only` | `false` | Withhold the tool from non-admins. The tool was admin-only until ISSUE-449; the fields in this table are what bound where a fetch may go, and they bound it the same way for every user. Set this where a deployment wants who-scoping back — it decides whether the tool is registered, so unlike everything else here it never reaches the fetch itself |
| `allow_hosts` | `[]` | If non-empty, a host allowlist (suffix match) |
| `block_hosts` | `[]` | Always-denied hosts (suffix match) |

## Availability fallback

`[brain]` carries a failover mechanism independent of which brain is primary:

| Setting | Default | Description |
|---|---|---|
| `fallback` | `""` | Brain to rerun a request on when the primary is unavailable |
| `fallback_on_transient` | `true` | Also reroute a persistent `transient_api_error` |
| `fallback_cooldown_seconds` | `900` | Skip an unavailable primary at most this long before retrying it; a usage limit against a Claude subscription instead ends at the quota's own reset (floor 60s). 0 disables |

The same availability breaker is what the nightly sleep cycle consults before deciding to run at all. Pin `istota_brain_native_model` whenever native is either primary *or* fallback — an empty model id 400s on failover, which is the worst moment to discover it.

Key handling:

- `istota_brain_native_api_key` is **never** written to `config.toml`. With `istota_use_environment_file: true` (the default) it's rendered into the systemd `EnvironmentFile` as `ISTOTA_BRAIN_NATIVE_API_KEY`; vault it.
- **Per-user keys** go through the existing `istota_user_secrets` mechanism (the `native_brain` service is in the connected-service schema, flagged `cli_only` so it's operator-provisioned only — not exposed in the web UI), and overlay the instance key for that user's tasks:

  ```yaml
  istota_user_secrets:
    alice:
      - { service: native_brain, key: api_key, value: "{{ vault_alice_native_key }}" }
  ```

- `istota_brain_native_extra_headers` is rendered as a `[brain.native.extra_headers]` sub-table (a TOML inline table would be mis-emitted by the JSON filter), so header names with dots or dashes (`anthropic-beta`) are safe.

## Gradual rollout: per-source-type routing

Rather than flipping the whole instance at once, route specific task types to the native brain while everything else stays on `claude_code`. This is the recommended rollout path: move low-risk background work first, keep interactive talk/email on the proven backend, watch for regressions, then widen.

```toml
[brain]
kind = "claude_code"            # default for everything not listed below

[brain.source_type_overrides]
scheduled = "native"            # cron jobs
heartbeat = "native"            # health checks
```

`source_type` values match the task's origin: `talk`, `email`, `briefing`, `scheduled`, `heartbeat`, `subtask`, `cli`, `istota_file`. A routing typo (unknown brain kind) is logged and ignored — the task falls back to the instance default rather than failing. Each routed task logs one INFO line (`brain routing: task … -> kind=native`).

## Local development

Bubblewrap is Linux-only, so on a Mac dev box run with the sandbox off. Keep a gitignored `config/config.dev.toml` (copy `config/config.dev.toml.example`):

```toml
[brain]
kind = "native"
[brain.native]
provider = "openai_compat"
model = "claude-sonnet-4-6"
base_url = "https://api.anthropic.com/v1"

[security]
sandbox_enabled = false        # bwrap is Linux-only
skill_proxy_enabled = false    # simplifies the inner loop

[users.dev]
display_name = "Dev"
```

> Sandbox correctness cannot be validated on the Mac. "Works locally" means "logic is correct," not "isolation is correct" — check isolation on a Linux box or in the Docker image.

### Dev tiers

**Standalone loop runner** (`scripts/native_repl.py`) — runs one prompt through a `NativeBrain` with no executor/scheduler/Talk/DB. Tools operate in a throwaway temp dir. Prints the streamed events, the `BrainResult`, and `TaskUsage` (so cost is visible).

```bash
# Offline, deterministic — a scripted mock provider drives the loop.
uv run python scripts/native_repl.py --provider mock \
    --script tests/native/fixtures/two_tool_turn.json "write and read a file"

# Replay a recorded SSE session through the real parser (no credits).
uv run python scripts/native_repl.py --provider replay \
    --fixture tests/native/fixtures/text_completion.jsonl --tools "" "summarize this repo"

# Live, against whatever the dev config points at (needs a key).
uv run python scripts/native_repl.py -c config/config.dev.toml --provider live "..."
```

**Recorded-SSE replay** — `ReplayProvider` feeds committed JSONL SSE fixtures through the real provider parser (CI default, offline). `RecordingProvider` regenerates fixtures from the live API (`ISTOTA_NATIVE_RECORD=1` + a real key), run rarely.

**Full CLI task path** — point the existing `istota task` CLI at the dev config:

```bash
uv run istota init -c config/config.dev.toml
uv run istota task "read README and summarize it" -u dev -x -c config/config.dev.toml
```

Zero-cost live loop: point `[brain.native]` at a local Ollama model (`base_url = "http://localhost:11434/v1"`). Quality is lower — small models loop and mis-call tools, which is itself useful for exercising the loop detector and JSON repair — but it validates the whole stack offline.

### Shadow compare

Before flipping a task type to native, run the same prompt through both brains and diff the output:

```bash
uv run python scripts/brain_shadow.py -c config/config.dev.toml \
    "read README and summarize it in one sentence"
```

It diffs result text (similarity + unified diff), tool-call sequence, and native `TaskUsage`. Exact parity is not expected — the brains manage context differently and expose different tool schemas — but outcomes should be equivalent. Large text divergence or wildly different tool sequences are the signal to investigate.

## Operational notes

- **Cost telemetry.** Every brain attempt writes a row to `task_usage` (plus one `task_usage_models` row per model where the brain reports a split), and a greppable `brain_usage …` INFO line so a figure survives a failed database write. Cost prefers the provider's own reported figure (OpenRouter returns real charged cost); otherwise it falls back to the model catalog's per-mtok prices — which, for an OpenRouter deployment, are the live-fetched real prices, and otherwise 0.0. That distinction is recorded rather than lost: a row's `cost_basis` is `api` only when every accumulated turn reported a cost, and `estimated` when the catalog was used, because the catalog prices an unknown model at zero and a fabricated 0.0 must not be read as real spend. Native rows carry `totals_source = 'derived'` and NULL context columns — the native loop does not track per-request prompt sizes. `claude_code` is no longer the opaque one: it reports a full breakdown, a per-model split with each model's context window, and a cost figure, all captured off the stream.
- **Per-user API keys.** Beyond the instance-wide `[brain.native] api_key` / `ISTOTA_BRAIN_NATIVE_API_KEY`, each user can have their own provider key in the encrypted secrets table: `istota secret ensure -u <user> -s native_brain -k api_key -v <key>`. This is operator-provisioned only (CLI/Ansible) — it's deliberately not in the web UI, since it overrides only the key and not the provider/model/base_url, so a self-serve knob would imply more than it delivers. The per-user key overlays the instance key for that user's tasks.
- **Reasoning effort.** `[brain.native] effort` (`low` / `medium` / `high` / `xhigh` / `max`, default empty) sets a default reasoning budget; per-task overrides (e.g. `!model opus:high`, `[models.aliases]`) win. It is sent as the OpenAI-compatible `reasoning_effort` field **only** when the target model is thinking-capable (`supports_thinking`, resolved from the live-fetched OpenRouter catalog or a `model_overrides` entry) — for a non-reasoning endpoint it is dropped silently so the request never 400s. `xhigh` and `max` fold to `high` on the wire (the compat field exposes no finer knob); the original tier still tracks on the task row. Extended-thinking output is parsed but excluded from the visible result.
- **Prompt caching.** `[brain.native] prompt_caching` adds `cache_control` breakpoints covering the tool definitions, the system message, the first user message, and a rolling breakpoint on the latest message each turn (up to Anthropic's 4-breakpoint cap), which is what produces cross-turn cache hits. **The default is derived from `base_url`:** on for `api.anthropic.com`, off for any other endpoint. Set it explicitly to force either way — a plain-OpenAI, LM Studio, Ollama, or vLLM endpoint that doesn't understand the extension needs `prompt_caching = false`. A per-task cache hit-rate line is logged at task end (`native cache hit_rate=… read=… input=…`).
- **Context-overflow recovery.** If a turn exceeds the context window mid-task, the native brain force-compacts the accumulated transcript and continues from the summary instead of failing — up to two recovery attempts, sharing the task's wall-clock deadline. The proactive compaction hook (`prepare_next_turn`) is the first line of defense; this is the reactive safety net beneath it.
- **Image tool results.** A tool result carrying image content renders as a follow-up `role:"user"` block on vision-capable models (`supports_vision`); on a no-vision model the image is dropped with a text note so the request still validates.
- **Model ids.** `openai_compat` needs explicit ids and does not translate Anthropic aliases — `opus` is sent verbatim, not turned into `claude-opus-5` (that mapping is the `claude_code` brain's, not the native brain's). Map role names per deployment with `[models.aliases]` if you want `fast`/`general`/`smart` under native.
- **Cancellation / `!stop`.** Works on both brains. The native brain bridges the scheduler's cancel poll into an `asyncio.Event` threaded through the loop, tools, and retry backoff. A failing cancel poll (e.g. transient SQLite lock) is tolerated rather than silently disabling `!stop`.
- **Task timeout.** The native loop runs under a wall-clock deadline of `scheduler.task_timeout_minutes` (`istota_scheduler_task_timeout_minutes`, 60 on an Ansible deploy; 30 is the in-code default). On expiry it signals abort (killing any in-flight bash subprocess at the next poll), waits a short grace, then hard-cancels, and returns `stop_reason="timeout"`. This matches `claude_code` and prevents a runaway loop from outliving the scheduler's stuck-task reclaim (which would otherwise double-execute the task). `max_turns` is a second, coarser backstop, and it does **not** take the same split: `istota_brain_native_max_turns` is 100 on an Ansible deploy, the same as the in-code default. It was 200 for a while, on the argument that the cap is a loop backstop rather than a budget, and `52a136a1` put it back — at 200 on a slow fallback brain the wall clock arrives long before the turn budget, so the run ends on the stop that discards the model's work rather than the one that delivers it (ISSUE-373). It stays at 100 until the two backstops are sized against each other. `0` disables the cap entirely and leaves the clock as the only backstop.
- **Context management.** The native brain owns compaction (runs in `prepare_next_turn`, file-operation aware across cycles). `claude_code` delegates it to Claude Code. The two are independent.
- **Sandboxing.** `claude_code` runs the whole subprocess inside bwrap. The native brain runs the loop in-process and sandboxes each tool execution per-call (the loop itself never runs user-controlled code). Validate the per-tool sandbox on Linux, not on the Mac.

## Rollback

Set `[brain] kind = "claude_code"` (or remove the `source_type_overrides` entry) and restart the scheduler. `ClaudeCodeBrain` is never removed — rollback is a one-line config change.

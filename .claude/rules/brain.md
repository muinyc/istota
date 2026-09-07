---
paths:
  - "src/istota/brain/**"
  - "src/istota/agent/**"
  - "src/istota/llm/**"
  - "src/istota/session/**"
---

# Brain Module (`src/istota/brain/`)

Pluggable model-invocation backend. The executor composes the prompt, the env
and the sandbox config and hands a `BrainRequest` to a `Brain` implementation.
Brains own the call to the model and stream parsing; everything else (memory,
skills, sandboxing, deferred DB writes, result composition, malformed-output
detection) stays in the executor.

**The prompt is not one string.** It arrives on three channels with different
authority: `prompt` is the task material and is the model's user turn,
`composed_system_prompt_path` names Istota's own standing instructions and is
sent with system authority, and `custom_system_prompt_path` is the operator's
own file with each backend's existing override semantics. The split is what
keeps a brain that compacts its own message history from compacting away the
instructions that define the assistant (ISSUE-375). A direct text-only caller
supplies only the first and is unaffected.

## Layout
```
brain/
├── __init__.py     # Brain protocol re-exports + make_brain factory
├── _types.py       # BrainRequest, BrainResult, BrainConfig, Brain Protocol
├── _events.py      # StreamEvent types + Claude Code stream-json parser
├── _aliases.py     # CANONICAL_ROLES, EFFORT_LEVELS, split_effort, is_portable_alias
├── _roles.py       # Global operator alias-override state (provider-agnostic)
├── claude_code.py  # ClaudeCodeBrain — wraps `claude` CLI subprocess +
│                   # owns the Anthropic model namespace (canonical IDs,
│                   # DEFAULT_ALIASES, resolver methods).
│                   # Also exports build_claude_cli_flags() — the shared
│                   # model/effort/tool/system-prompt flag builder both the
│                   # headless and tmux paths use.
├── native.py       # NativeBrain — in-process agent loop (see below)
└── tmux_claude.py  # TmuxClaudeBrain — drives the interactive `claude` TUI in
                    # a detached tmux session (subscription billing). Composes
                    # ClaudeCodeBrain for model resolution; see below.
```

`stream_parser.py` at the package root is a backward-compat shim that
re-exports from `brain._events` for tests and a few internal callers.

## Brain protocol
```python
class Brain(Protocol):
    # The namespace this brain resolves role/alias names in. Operators key a
    # per-namespace role override on it. "anthropic" (claude_code + tmux_claude,
    # by delegation) | "openai_compat" (native).
    model_namespace: str

    def execute(self, req: BrainRequest) -> BrainResult: ...

    # Each brain owns its own model namespace. Consumers never reach into
    # a brain module's tables — they go through make_brain(config.brain)
    # and call these methods.
    def resolve_alias(self, alias: str) -> tuple[str | None, str | None] | None: ...
    def resolve_model_name(self, name: str | None) -> str: ...
    def list_aliases(self) -> list[tuple[str, str | None, str | None]]: ...

    # This brain's own configured default, unresolved. The model a request
    # that pins none runs on; empty = the backend's own idea of a default.
    # On the protocol because the question is per-brain and the answer used
    # to be deployment-wide (ISSUE-418).
    @property
    def default_model(self) -> str: ...
    @property
    def default_effort(self) -> str: ...
```

## Per-brain model defaults (ISSUE-418)

Each brain's default model and effort live in **its own** config block:
`[brain.claude_code] model` / `effort`, `[brain.tmux] model` / `effort`,
`[brain.native] model` / `effort`. The executor sends a genuine *task* pin or
nothing, and the brain applies its own default — the `or` chain in each brain
is the single place a default is applied.

Before this, `claude_code`'s defaults were the **top-level** `model` / `effort`,
a vestige of there having been one brain. Sitting at the root they read as
deployment-wide and the executor treated them as one, filling every request
whatever brain was about to run. `NativeBrain` already had the right shape
(`req.model or self._config.model`) and that `or` was simply never reached, so a
room pinned to `native` with `[brain.native] model = "z-ai/glm-5.3-flash"` ran
`claude-opus-5` against the OpenRouter endpoint — billed per token as
`cost_basis=api`, and a hard failure rather than a wrong bill anywhere the
endpoint does not happen to serve Anthropic ids. The operator could not avoid
it: the top-level key was the only way to set Claude Code's model, so setting it
correctly for one brain necessarily mis-set it for the other.

**The retired top-level keys still load** and are migrated by
`config._apply_legacy_brain_defaults` onto `[brain.claude_code]` **and**
`[brain.tmux]`, with a warning, filling only a block that set nothing itself.
Both, because those two share the `anthropic` namespace and the same `claude`
binary, so the value is equally valid in either and a `tmux_claude` deployment
would otherwise lose its model silently on upgrade. **Never onto
`[brain.native]`** — a name written in the Anthropic vocabulary cannot carry to
an `openai_compat` endpoint, which is the defect itself. Both deployment
generators do the same migration a step earlier, so a host that never edits its
variables gets the new keys without a warning at every boot:
`render-config.sh` reads `ISTOTA_BRAIN_CLAUDE_CODE_MODEL` /
`ISTOTA_BRAIN_TMUX_MODEL` falling back to `ISTOTA_MODEL`, and the Ansible role's
`istota_brain_claude_code_model` / `istota_brain_tmux_model` default from
`istota_model`.

`Brain.with_defaults(req)` is where a brain applies its own default, and it is
**idempotent** — a request whose model is already set takes neither branch, so a
caller may apply it before `execute` without double-counting. Effort precedence
inside it is **request > the block's own `effort` > the effort carried by the
alias the block's `model` resolves through**. The block's explicit key outranks
the alias's because the operator wrote it beside that model, and getting this
backwards makes the key *unreachable* whenever `model` names an effort-carrying
alias — which also breaks the migration's one promise, since the old path
resolved the model with `resolve_model_name` (which strips the modifier) and
took the top-level `effort` verbatim.

A deployment that reaches `native` while `[brain.native] model` is empty and the
retired top-level key was set is warned about once at load
(`config._warn_native_lost_its_only_model`). That shape used to work — the
executor substituted the top-level value into every request, which is the defect
— and now sends an empty model, which most endpoints reject. A warning rather
than a refusal: a failed config load takes the whole daemon down, and native may
be only the fallback there.

`brain.model_namespace_for_kind(kind)` answers "what namespace does this kind
read aliases in" as a **lookup** rather than a construction (ISSUE-417).
`model_namespace` is a class attribute, and building a brain to read one is not
free: `TmuxClaudeBrain.__init__` shells out to the installed `claude` and warns
on a version mismatch, so `web_app._brain_catalogue` — which asked once per
known kind on every catalogue fetch — put a `tmux_brain cli_version_mismatch`
WARNING in the operator's log every time a room-settings modal opened. Four
sites collapse onto it: that catalogue's `brain_namespaces`,
`commands._model_namespace`, the executor's fallback crossing rule, and
`scheduler_deferred._inherited_model`, which asks whether a parent's model pin
can travel onto a `subtask` row (ISSUE-421). It
returns `None` for an unbuildable kind, and every caller must read that as **not
established** rather than as "the same namespace". The separate question — can
this deployment *build* the kind — is still a construction, and
`_brain_catalogue` asks it only for the kinds on the operator's allowlist, since
offering an unbuildable one gives a room that fails every turn.

`brain.configured_default_model_effort(brain_config)` is the same answer as a
**lookup rather than a construction**, for the callers that only report the
default — the scheduler's log-channel line and the admin dashboard. Building a
brain to ask costs a `claude` CLI version probe per task on the tmux kind and a
provider client on native. It returns the value unresolved, since resolving an
alias needs the brain's own table.

## Model identity (single source of truth)

Every model ID in the codebase resolves through the active brain. There are two
layers plus the orthogonal `:effort` modifier.

**The `:effort` modifier** (`brain/_aliases.py`): effort is an axis orthogonal to
model choice, appended to *any* reference as `<base>:<effort>` where `<effort>` ∈
`EFFORT_LEVELS` (`low|medium|high|xhigh|max`). `split_effort(raw) -> (base,
effort|None)` peels it (via `rpartition(":")`, only when the suffix is a known
effort level and the base is non-empty; an OpenRouter `provider/model` slug's `/`
is untouched). Every brain's `resolve_alias` / `resolve_model_name` calls it
first. This replaced the hand-maintained model×effort cross-product (`opus-high`,
`opus-xhigh`, …) — those forms no longer resolve; `opus:high` is the only
spelling.

The two resolution layers, top to bottom:

1. **Operator alias overrides** (`brain/_roles.py`, global) — **per-namespace**.
   An override is stored `name -> namespace -> RoleTarget`, where
   `RoleTarget(model, effort=None)` carries an optional effort. The namespace
   key is a brain's `model_namespace` (`"anthropic"` / `"openai_compat"`) or the
   reserved `"*"` for a *legacy flat* value. Each brain resolves in its own
   namespace and a value written for one namespace can never leak onto another
   brain's wire. `set_alias_overrides(...)` (called once at config-load)
   normalizes a bare string → `{"*": RoleTarget(str)}`, `{ns: "str"}`, and
   `{ns: {model, effort}}`, and strips the reserved `portable = true` sibling key
   into a separate `_portable_names` set (`get_portable_alias_names()`).
   `get_alias_override_target(name, namespace)` precedence: per-namespace value >
   legacy `"*"` > None.
2. **Shipped defaults — the unified `DEFAULT_ALIASES`** (per-brain, e.g.
   `claude_code.DEFAULT_ALIASES`): one table mapping each base alias name →
   `(model_id, default_effort)` in that brain's namespace. Holds the portable
   tiers (`fast`/`general`/`smart`, the `CANONICAL_ROLES`) AND the provider
   shortcuts (`opus`/`sonnet`/`haiku`/`default`) together, base names only. This
   is the code floor the operator's `[models.aliases]` overlays. It replaced the
   old split `MODEL_ALIASES` + `DEFAULT_ROLE_TARGETS`.

`Brain.resolve_alias` (per brain): `split_effort` → resolve the base
(override → `DEFAULT_ALIASES` → canonical `claude-*` id passthrough → `None`) →
merge effort (the `:effort` suffix wins over the entry's own default effort). An
override target is itself resolved through the brain's `DEFAULT_ALIASES`, and an
explicit `RoleTarget.effort` wins over the target's alias-derived effort. Returns
`(model_id, effort) | None`. `Brain.resolve_model_name` collapses any name to a
canonical ID (effort stripped); `Brain.list_aliases` exposes the merged table
(tiers first, then shortcuts, then custom) for `!models` and `!help`.

**Config surface** (`[models.aliases]`) — three forms:
```toml
# Legacy flat (namespace-agnostic, stored under "*"):
[models.aliases]
smart = "opus:high"

# Per-namespace (define once, correct on every brain family):
[models.aliases.smart]
anthropic     = "opus:high"                                          # CLI brains
openai_compat = { model = "anthropic/claude-opus-4.8", effort = "high" }  # native
[models.aliases.deep]
anthropic     = "opus:max"
openai_compat = "anthropic/claude-opus-4.8"
portable      = true                                                # a cross-brain custom tier
```
An alias uses one form (TOML: a key can't be both a string and a table). A
per-namespace table missing the active brain's key falls to that brain's code
floor. `ModelsConfig.aliases` holds the **raw** parsed structure
(`dict[str, str | dict]`); normalization into `RoleTarget`s lives only in
`set_alias_overrides`. Config-load validation is namespace-aware: `anthropic`
entries validate against `claude_code` via `Brain.validate_alias_override`, a flat
`"*"` against the active brain, `openai_compat` against native (no alias table →
no warnings); the reserved `portable` key is skipped; warnings only, never fails
load. **Hard rename:** the old `[models.roles]` key is no longer read — a stale
one present logs a one-time migration WARNING (detection only).

ClaudeCodeBrain pins to versioned IDs, base names only:
- `OPUS = "claude-opus-5"` (current default Opus)
- `SONNET = "claude-sonnet-5"`
- `HAIKU = "claude-haiku-4-5"`

`OPUS_46` / `OPUS_47` and their effort-variant aliases were deleted — a
prior-version pin is the canonical id plus the modifier (`claude-opus-4-7:high`),
which resolves via the `claude-*` passthrough in `resolve_alias`.

Convention: bare alias names (`opus`, `sonnet`, `haiku`) always resolve to the
*current latest* version constant. Bumping `OPUS = "claude-opus-5-0"` ripples
through every consumer + alias automatically — a model release is one constant
edit, no effort variants to enumerate.

`Config.advisor_model` (top-level TOML `advisor_model`, `[brain.advisor_model]`
does NOT exist — it lives beside `model`/`effort`, not under `[brain]`) resolves
through this same table via `resolve_model_name`, which drops any `:effort`
modifier — the CLI's `--advisor` flag takes no effort. Only meaningful for the
anthropic namespace (`claude_code` / `tmux_claude`); `NativeBrain` ignores it
entirely, since the advisor is an Anthropic Messages beta tool with no wire
over `openai_compat`. See `.claude/rules/executor.md` § Brain invocation for
how the executor resolves and drops it per task.

Adding a new brain: implement the four Brain methods (`execute`,
`resolve_alias`, `resolve_model_name`, `list_aliases`, `validate_alias_override`),
set a `model_namespace` class attribute (the key operators use in
`[models.aliases.<name>]`; reuse `"anthropic"` / `"openai_compat"` if you share a
family, else a new label), and ship your own canonical-ID constants and
`DEFAULT_ALIASES`. Read overrides via
`get_alias_override_target(name, self.model_namespace)`; apply `split_effort`
first. Operator overrides plug in for free via `_roles.py`.

## BrainRequest fields
| Field | Notes |
|---|---|
| `prompt: str` | The **user half** — task material only: retrieved memory, knowledge facts, playbooks, conversation and confirmation history, the request itself and its attachment list. Sent as the model's user turn (stdin on the headless CLI path, the pane injection on tmux, the initial `UserMessage` on the native path), and the only half native compaction may summarize. It used to be the whole composed prompt; Istota's standing instructions now travel on `composed_system_prompt_path` instead of being prefixed onto this. Two consumers read it as "everything the model was shown" and both now read the narrower half by decision: `NativeBrain._extract_urls` builds the `require_url_provenance` corpus from it (a URL named only in a persona or a tool description is not user-provided provenance), and `build_image_prompt` prepends the image `Read` directive to it (which keeps that directive leading the user message rather than trailing eight kilobytes of tool documentation). |
| `allowed_tools: list[str]` | From `executor.build_allowed_tools()`. For ClaudeCodeBrain / TmuxClaudeBrain this is now effectively a **non-empty = give the model tools** signal (they run with `--dangerously-skip-permissions`, not an allowlist); the specific names only matter to NativeBrain, which filters its in-process tool set by them. Empty list = text-only invocation: ClaudeCodeBrain emits no tool flags and no skip-permissions (sleep-cycle path). |
| `cwd: Path` | Subprocess working dir (`config.temp_dir`) |
| `env: dict[str,str]` | Per-task env (already credential-stripped if proxy enabled). The one thing that split does **not** take out is the Claude runtime credential, which `build_clean_env` sets for every task whatever brain runs it and which no skill manifest declares — `NativeBrain` removes it on both its ways into the tool server — the `hello` frame and the spawn env (see `CLAUDE_RUNTIME_ENV_VARS` below), so the field as the two CLI brains read it still carries the token they authenticate with. Mutable and shared: `ClaudeCodeBrain` writes `IS_SANDBOX` / `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` onto this dict in place, and `_run_fallback` hands the same object to the fallback brain, so anything filtering it must copy. |
| `timeout_seconds: int` | `config.scheduler.task_timeout_minutes * 60` |
| `model: str` | The **task's own pin, or empty** — never a deployment default (ISSUE-418). An empty value reaches the brain, which fills in its own configured default (`[brain.claude_code] model`, `[brain.tmux] model`, `[brain.native] model`). The executor used to substitute the top-level `config.model` here, which was claude_code's own default living at the root and was therefore applied to every brain, making each brain's own `or` unreachable. |
| `effort: str` | The task's own pin, or empty, on the same rule. A task pinning a *model* with no effort carries no effort at all, at either layer: an effort chosen for one model need not be valid on another. |
| `advisor: str` | Anthropic-namespace brains only; `""` = no advisor. Set only by the executor, only when `config.advisor_model` is configured and the task carries no model pin (advisor-model spec). `ClaudeCodeBrain` / `TmuxClaudeBrain` emit `--advisor <value>` when both this and `allowed_tools` are non-empty, and otherwise set `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` in the child env so a host's `~/.claude/settings.json` `advisorModel` can't run one Istota didn't ask for. `NativeBrain` ignores it. |
| `custom_system_prompt_path: Path \| None` | The **operator's** own file (`config/system-prompt.md` under `custom_system_prompt`), and the older of the two system channels. Optional in the strict sense: a configured path that no longer exists is omitted rather than failing the attempt, which is the behaviour every deployment has today. Each backend keeps its own override position for it — `ClaudeCodeBrain` passes `--system-prompt-file`, which *replaces* the CLI's default harness prompt; `NativeBrain` appends it after its built-in coding block. |
| `composed_system_prompt_path: Path \| None` | **Istota's** own composed standing instructions — the system half of `executor.ComposedPrompt`, written by the executor to `system_prompt.txt` in the task's control directory (`{temp_dir}/.control/{user_id}/task_<id>/`, daemon-owned and writable by no task): identity, execution constraints, emissaries, persona, accessible resources, tool descriptions, rules, response guidelines, the skills changelog and the eager skill bodies. A third channel rather than a reuse of the row above, because the two have different owners and different contracts. `None` means this caller has no Istota-composed standing instructions, which is the default and what every direct text-only caller passes (the sleep cycle, shared-block synthesis, health OCR and explanation, the code reviewer, conversation triage), so their behaviour is unchanged. A non-`None` value is **required** input and a brain must not quietly ignore it because the file disappeared: the Claude CLI path emits its flag and lets the CLI refuse to start, and the native path reads the file with no `exists()` branch, the existing brain error boundary turning either into an ordinary failed `BrainResult`. Silent omission would run the task with the user half alone — no persona, no rules, no tool descriptions — which is ISSUE-375 recreated by a filesystem race. **Must be absolute**, and the producer is what guarantees it: `NativeBrain` opens it in the daemon process while the CLI opens it inside the sandbox, against two different working directories, and a reroute carries one value between them. Carried across a reroute by `_run_fallback`'s `dataclasses.replace`, which names it nowhere. |
| `streaming: bool` | True when `on_progress` callback is supplied |
| `on_progress: Callable[[StreamEvent], None] \| None` | Per-event callback. Widened `StreamEvent` union (task-event-streaming spec): `ToolUseEvent` (carries a real `tool_call_id`) \| `TextEvent` \| `TextDeltaEvent` (per-token incremental answer text — NativeBrain per provider `TextDelta`, ClaudeCodeBrain via the CLI's `--include-partial-messages` `text_delta` frames) \| `ResultEvent` \| `ContextManagementEvent` \| `ToolEndEvent` (NativeBrain only — `success` + loop-measured `duration_ms`) \| `ToolProgressEvent` (NativeBrain only) \| `ThinkingEvent` (whole reasoning block) \| `ThinkingDeltaEvent` (incremental reasoning — NativeBrain `reasoning` deltas, ClaudeCodeBrain `thinking_delta` partials). The executor's `executor_stream.TaskStreamAdapter.on_event` maps these to `TaskEvent`s via `EventWriter` (`istota/events.py`): `TextDeltaEvent` → coalesced `text_delta` on stream surfaces (web/repl), dropped on push surfaces; `ThinkingDeltaEvent`/`ThinkingEvent` → coalesced `thinking`, stream surfaces only. A loop-based brain MUST dispatch this callback off its event loop (NativeBrain's `run_in_executor` hop) so the synchronous Talk/log subscribers' `asyncio.run` calls don't collide (ISSUE-111 generalized). Both brains stay surface-agnostic — they emit both per-token deltas *and* whole-block `TextEvent`/`ThinkingEvent`s; the executor dedupes deltas-vs-whole-block per surface (stream: keep deltas, drop the redundant whole block; push: drop deltas, forward intermediate `TextEvent`s as `progress_text`, drop thinking). NativeBrain additionally suppresses the **final** turn's `TextEvent` (its text becomes the result); if the final turn carries no text the held block is released as progress instead, since it is no longer the answer. |
| `cancel_check: Callable[[], bool] \| None` | Polled between events; True → kill subprocess, return `cancelled` |
| `on_pid: Callable[[int], None] \| None` | Called once with subprocess PID after spawn. `NativeBrain` calls it too now, with the outer bwrap pid of its tool server — it used to call it never, so every native task carried `worker_pid` 0 and neither `!stop`, the web cancel endpoint nor `host_pressure.read_sandbox_shm` could reach it |
| `sandbox_wrap: Callable[[list[str]], list[str]] \| None` | Wraps raw cmd (e.g. with bwrap); no-op if not provided. The **CLAUDE** profile (`executor.SandboxProfile`): the namespace it builds carries the `claude` CLI's own runtime state — `~/.local/bin`, `~/.local/share/claude`, `~/.local/state/claude`, the `~/.claude` tmpfs base with `.credentials.json`, `settings.json`, `projects`/`debug`/`todos` through it — plus the `custom_system_prompt_path` bind, because the process being wrapped *is* that CLI. Read by `ClaudeCodeBrain` and `TmuxClaudeBrain` only. The **directory** holding `composed_system_prompt_path` is bound read-only under **both** profiles instead, through `build_bwrap_cmd`'s `extra_ro_binds`: it lands after every other bind, so the whole control directory is readable and not writable inside the namespace. That covers every bwrap-wrapped child, including the native `Bash` tool, and covers nothing reaching `ToolEnv` — which is what `fs_write_denied_roots` below is for. |
| `native_sandbox_wrap: Callable[[list[str]], list[str]] \| None` | The same sandbox under the **NATIVE** profile: istota's own code is the outer process, so no Claude runtime block, no credential, and no *operator* system-prompt bind — `custom_system_prompt_path` is bound under the CLAUDE profile alone, because it is there for the CLI to open. The composed system file is a different case and is bound under both, as the row above says. Read by `NativeBrain`, and applied **once per attempt** to the argv of the tool server that holds all six of its tools (see "The tool server" below) rather than per Bash call. **Two fields rather than one field plus a profile argument**, and structurally so: `executor._run_fallback` reroutes an attempt with `dataclasses.replace(req, model=…, effort=…, advisor=…, is_fallback=True)`, which names neither — so a single field would carry the Claude profile into NativeBrain on the shipped `claude_code → native` reroute, handing the model's Bash tool the credential the split exists to withhold (ISSUE-389). Two names each carry across harmlessly, because each brain reads only its own. `tests/test_brain_types.py` holds that. |
| `fs_read_roots: list[Path] \| None` / `fs_write_roots: list[Path] \| None` | NativeBrain-only file-tool path allowlist (NB-1). Populated by the executor (`native_fs_roots`) only under effective sandboxing; other brains ignore them (bwrap already confines their tools). `None` = unconfined (dev / no bwrap). |
| `fs_write_denied_roots: list[Path]` | RO carve-outs nested inside a write root — what bwrap gets by re-binding a subdirectory `--ro-bind` after its parent's RW bind, and what containment alone cannot express. Up to two entries, and which ones depends on the shape: the task's control directory `{temp_dir}/.control/{user_id}/task_<id>`, which holds the file `composed_system_prompt_path` names, always, since the executor seeds it outside its confinement branch; plus `{user_temp_dir}/.developer` on a confined deployment, since that one is appended by `native_fs_roots` — without it a native task rewrites the standing instructions it is running under with one `Write` call, since the file tools reach `ToolEnv` and enter no mount namespace. Note the different empty semantics from the pair above: `[]`, not `None`, because a deny set has no unconfined meaning to signal. Enforced on the write path only (the directory stays readable) and ahead of `ToolEnv`'s unconfined early return — which is what makes the control entry worth anything on the shapes with no bwrap behind it, and why the executor seeds it whether or not `native_fs_confinement_active`. Both entries name a **directory** and `_in_denied` compares realpaths with `is_relative_to`, so every file under one is covered and a framework file added later needs no new entry. Under confinement the control directory is on `fs_read_roots` as well, because it is a sibling of `user_temp_dir` rather than a child and is therefore inside no write root — without that a task could not open its own prepared image attachment. |
| `result_file: Path \| None` | claude_code-specific fallback file path |
| `images: list[ImageInput]` | The task's prepared image attachments, as `(path, media_type, display_name)` — never bytes. Built by `executor.prepare_image_attachments` (`istota/image_attachments.py`) and passed to every request the executor makes; the other eight construction sites leave it empty on purpose, including the three health OCR paths, which run their own vision prompt. `path` is resolved and normalized, `media_type` is derived from what Pillow decoded and the format the rewrite chose, and each brain converts at the last moment so nothing large reaches a task row or a log line. See "Image attachments" below for what each brain does with it. Preserved across the executor's fallback copy for free — that copy is `dataclasses.replace`, which carries every unnamed field |

## BrainResult fields
| Field | Notes |
|---|---|
| `success: bool` | Final success/failure |
| `result_text: str` | Final response text |
| `actions_taken: str \| None` | JSON-encoded list of tool-use descriptions |
| `execution_trace: str \| None` | JSON-encoded `[{type:"tool"\|"text"\|"cm_boundary", ...}]`. A `tool` entry carries an optional `raw` = the verbatim Bash command (`_tool_invocation`), threaded by all three brains for playbook command extraction (ISSUE-174) |
| `stop_reason: str` | `completed` / `cancelled` / `timeout` / `oom` / `terminated` / `transient_api_error` / `usage_limit` / `error` / `not_found` / `fallback`. `usage_limit` = a subscription/quota/billing limit (a persistent "brain unavailable" condition the executor reroutes to the configured fallback brain — see "Brain fallback" below). `terminated` = the subprocess was killed by a signal other than SIGKILL — see "Signal deaths" below. |
| `usage: BrainUsage | None` | Per-attempt token/cost telemetry, normalized across brains (`istota.usage`). **Retyped from `TaskUsage`** — the two vocabularies differ and the difference is load-bearing: `TaskUsage.input_tokens` is OpenAI-compat `prompt_tokens`, *inclusive* of cache reads (and `native._log_cache_telemetry` depends on that), while `BrainUsage.billed_input_tokens` excludes them, matching Anthropic's convention. `from_task_usage` reconciles the two at the boundary and labels the result `totals_source='derived'`; `session/usage.py` keeps its shape and that function still runs on the raw `TaskUsage`, before conversion. Set on **every** return, success or failure — tokens are spent either way. `TmuxClaudeBrain` leaves it `None`: it drives the interactive TUI and reconstructs events from a JSONL transcript, so there is no result frame to read, and a synthetic zero would drag every average. |
| `effort_used: str` | The effort the attempt actually ran with. Same contract as `model_used` and added for the same reason (ISSUE-418): a brain fills its own configured default onto a `dataclasses.replace` copy, so the executor's `req.effort` stops describing the attempt and `task_usage.effort` held the empty string for every unpinned task — which `!usage --by effort` reads. Stamped at each brain's single `execute` seam, beside `brain_kind`, rather than at the ~20 return sites below it. Empty = the brain has none to report, and the executor falls back to the requested effort. |
| `brain_kind: str` | Which brain produced this result, for the usage row. Set by the brain on the way out rather than threaded from the executor's construction site, so it stays correct on the fallback path for free — there the executor's own variable no longer describes the result it holds. One of `KNOWN_BRAIN_KINDS`; empty for `tmux_claude`. |

## ClaudeCodeBrain
Wraps the `claude` CLI subprocess. Owns:

1. **Command construction** — `claude -p - --disallowedTools Agent Workflow
   --dangerously-skip-permissions`, plus optional `--model`, `--effort`,
   `--system-prompt-file`, `--append-system-prompt-file`, and an
   `--output-format` that depends on the mode:
   `stream-json --verbose --include-partial-messages` when streaming,
   `json --verbose` otherwise (see §11 — `--verbose` is what makes the
   non-streaming shape predictable across CLI versions, and with it the
   `init` frame that carries `apiKeySource`). `--include-partial-messages` makes the CLI emit
   answer / reasoning text token-by-token as `stream_event` frames *before* the
   whole `assistant` block lands — without it the final response would arrive as
   one block and dump all at once on stream surfaces. There is **no
   `--allowedTools` allowlist**: the run is non-interactive (a per-tool
   permission prompt can't be answered in `-p` mode and would auto-deny), so it
   relies on `--dangerously-skip-permissions` for the model's full default
   toolset, with the bwrap sandbox + network proxy as the security boundary (the
   same posture the tmux brain uses; `build_claude_cli_flags` is shared). `Agent`
   and `Workflow` stay explicitly denied — deny rules win even under
   skip-permissions — so Istota keeps orchestrating through its own skills, not
   Claude Code's multi-agent fan-out (whose dozens-of-subagents cost we don't
   want a task reaching for unprompted; the old allowlist implicitly excluded
   `Workflow`, so dropping it required denying `Workflow` explicitly again).
   Text-only invocations (empty `allowed_tools`, e.g. the sleep cycle)
   get neither tool flags nor skip-permissions, so they can't reach a tool. As
   root (the Docker container-as-sandbox case) `execute()` sets `IS_SANDBOX=1`
   for tool-bearing tasks, since `claude` refuses skip-permissions as root
   otherwise (`_is_root`, shared with the tmux brain).

   **The two system-prompt flags are not interchangeable.** The operator file
   keeps `--system-prompt-file`, which *replaces* the CLI's default harness
   prompt, and keeps its `exists()` gate. Istota's composed half is emitted as
   `--append-system-prompt-file` with no `exists()` check at all: replacement
   would discard the harness prompt on the default deployment, where no
   operator file is configured, and an omission on a missing file would run the
   task with no persona, no rules and no tool descriptions. The CLI's own
   behaviour is fail-closed here rather than inferred — on `ENOENT` it prints
   `Error: Append system prompt file not found: <path>` and exits without
   running, and the only conflicts it enforces are `--system-prompt` against
   `--system-prompt-file` and `--append-system-prompt` against
   `--append-system-prompt-file`, so passing the operator file and the composed
   file together is legal. That was read out of the 2.1.241 bundle by grepping
   the binary for its own error string, because the obvious probe cannot fail:
   `claude` exits zero on any unrecognised option under both `--version` and
   `--help`, and the flag is undocumented in `--help` besides.

2. **Sandbox wrap** — calls `req.sandbox_wrap(cmd)` if provided so the
   executor's bwrap configuration applies without the brain knowing about
   bubblewrap.
3. **Subprocess** — `Popen` (streaming) or `subprocess.run` (simple),
   prompt via stdin (avoids E2BIG on large prompts), stderr drained on
   a background thread to prevent deadlock (streaming only; `communicate()`
   drains both pipes on the simple path). The **streaming** spawn passes
   `start_new_session=True` so the CLI leads its own process group and every
   kill path can take its bash grandchildren with it (ISSUE-257 — a
   `pytest -n auto` run outlived a bare `process.kill()` and finished on a
   saturated host). Two consequences worth knowing: the pid handed to `on_pid`
   is now a group leader, which is what lets `!stop` and the web cancel
   endpoint reach the group; and the CLI has left the daemon's process group,
   so under the local `istota serve` shape (no cgroup) a Ctrl-C reaches the
   daemon but not an in-flight task's `claude`, which then runs to its own
   timeout. Under systemd this is covered — `KillMode=mixed` SIGKILLs the whole
   cgroup after `TimeoutStopSec`.

   The **simple** path still spawns via `subprocess.run`, so its timeout still
   kills only the direct child and orphans the tree — the deferred half of
   ISSUE-257. Narrower than the streaming path was: `_execute_simple_once`
   never calls `req.on_pid`, so no `worker_pid` is recorded and neither cancel
   endpoint reaches it at all. Fixing it means spawning via `Popen` so the
   group can be killed, and roughly ninety tests across six files patch
   `subprocess.run` to keep the brain from spawning, so those move first.
4. **Stream parsing** — line-by-line via `make_stream_parser()` from
   `_events.py`, dispatching ResultEvent → final result, ToolUseEvent /
   TextEvent → trace + on_progress, ContextManagementEvent → `cm_boundary`
   marker in trace. The `stream_event` partial frames parse into
   `TextDeltaEvent` / `ThinkingDeltaEvent` and go to `on_progress` only (never
   the trace); the trailing whole-block `TextEvent` / `ThinkingEvent` still
   records the trace and is deduped against the deltas executor-side.
5. **Cancellation** — polls `req.cancel_check()` between events; final
   re-check after subprocess exit catches SIGTERM-style external kills.
   The in-loop kill goes through `process_group.kill_process_group`, not
   `process.kill()`.
6. **Timeout** — `threading.Timer` kills the process group after
   `req.timeout_seconds` (same helper); result tagged `stop_reason="timeout"`.
   Both kill sites skip a process that has already been reaped
   (`process.returncode is None`): the timer can still fire during the two 5s
   thread joins that follow `process.wait()`, and a raw pid carries none of the
   protection `Popen.send_signal` gave — the number may by then belong to
   someone else, whose group would be killed.
7. **Signal deaths** — a negative returncode means the subprocess died on
   signal `-rc` (`_signal_result`, both exec paths, checked after the
   cancellation/timeout branches so `!stop` still reports as a cancellation).
   `-9` keeps its OOM wording + `stop_reason="oom"` (SIGKILL is the OOM
   killer's and systemd-oomd's signature); every other signal returns
   `"Claude Code was terminated by <NAME> (signal N)"` with
   `stop_reason="terminated"`, a WARNING, and the execution trace attached.
   Before ISSUE-191 only `-9` was recognized and every other signal fell to the
   generic stream-parse catch-all ("Stream parsing failed (rc=-15, N lines)").
   `is_signal_termination(text)` is the shared marker predicate the scheduler
   classifies on (the executor drops `stop_reason` at its return boundary, so
   the scheduler reads failure *text* — same as OOM and cancellation).
8. **API retry** — wraps single-attempt execution in a 3-attempt loop when
   `is_transient_api_error()` matches (every 5xx, plus 408/425/429). The
   delay is the provider's own `Retry-After` where it supplied one, capped
   at `RETRY_AFTER_MAX_SECONDS`, else `API_RETRY_DELAY_SECONDS`.
   Retries do NOT count against the task's `attempt_count`.
9. **Result fallback** — prefers ResultEvent > result_file > stderr.
10. **Usage capture** — off the same stream, into `BrainResult.usage`. Totals and
    the per-model split come from the terminal frame's `modelUsage`, **not**
    `result.usage`: measured on a two-turn run, `modelUsage` reproduces
    `total_cost_usd` exactly while `result.usage` is 533 input and 14 output
    tokens short, because it covers only the main agent's conversation and not
    the CLI's own out-of-band calls. Totalling from `result.usage` therefore
    under-reports spend *and* breaks the invariant that a parent's totals equal
    the sum of its children.

    Per-request context measures come from `stream_event`/`message_delta`
    frames, one per API request, carrying the final usage for that request.
    Deliberately not the `assistant` frames: `parse_stream_line` returns one
    event per line and that branch already ends in a ladder returning a
    `ToolUseEvent` / `TextEvent` / `ThinkingEvent`, so emitting usage there
    would consume the return slot and drop the tool event — costing a tool chip
    on the live surface, an `actions_taken` entry and the `execution_trace`
    entry the sleep cycle reads for playbooks. `tests/test_stream_parser_usage.py`
    carries the regression guard. `message_delta` is also better data: once per
    request, no `message.id` dedup, and the true output count rather than the
    per-content-block snapshot an `assistant` frame carries.

    Sub-agent frames (`parent_tool_use_id` set on the wrapper) and compaction
    replays are excluded from the context measures and counted instead, so the
    peak means *this* agent's peak and a replay does not inflate the request
    count. Neither `RequestUsageEvent` nor `RateLimitEvent` is forwarded to
    `req.on_progress` or the execution trace — the executor fans progress out to
    live surfaces, and an accounting frame in a user's chat is a bug.

    `cost_basis` comes from the `init` frame's `apiKeySource`, and an
    unrecognized spelling is `unknown` rather than guessed into `api` — a
    subscription's list-price equivalent must never render as spend. Only the
    final in-brain retry attempt's usage is captured (a documented limitation),
    but a retry that exhausts its attempts still records that attempt rather
    than nothing. Both retry loops carry that attempt's usage onto the results
    they build themselves (`last_usage`), so an exhausted ladder or a cancel
    during the backoff still writes a row.

11. **Non-streaming usage capture** — the simple path gets
    `--output-format json --verbose` (no partials) and parses its usage
    out of stdout rather than off a stream, which is what measures the daemon's
    eight task-less origins. **What `json` emits on its own is
    CLI-version-dependent and both shapes are live** (ISSUE-271): 2.1.227 emits
    a JSON *array* of the same frames the streaming path produces, 2.1.238
    emits the bare terminal `result` frame as a single object.
    `_parse_simple_json_output` reads either, wrapping the object as a
    one-element frame list so the array loop is the only implementation.

    `--verbose` is what makes the shape predictable: measured against both
    deployed versions, 2.1.227 and 2.1.239 each emit the array *with* the
    `system`/`init` frame when it is passed. Without it the newer CLI drops
    that frame, and the `cost_basis` degradation below stopped being a rare
    fallback and became every row this path wrote — `sleep_cycle`,
    `code_review` and `shared_blocks` all landing on `unknown` while carrying
    real reported cost, split off from the identically-credentialled `task`
    rows for no reason visible to a reader of the dashboard.

    An object counts as the terminal frame **only** when its `type` is
    `result` — several daemon callers ask the model for a JSON answer, so
    `{`-leading stdout is not on its own evidence of an envelope. Anything
    matching neither shape returns `(None, None)` and the caller keeps raw
    stdout as the answer: that fallback is load-bearing, not defensive, since
    roughly ninety tests across six files patch `subprocess.run` with
    plain-text stdout and a CLI ignoring the flag behaves identically. Output
    that *did* come from the CLI (a known frame `type`, or an envelope-only key
    like `modelUsage`) but carries no terminal frame logs one WARNING — the
    silent fallback is why ISSUE-271 survived three weeks, reading as success
    at every layer above.

    The single-object shape carries no `init` frame, so two fields degrade —
    reachable now only on a CLI that ignores `--verbose`, not in a current
    deployment. `cost_basis` is `unknown` (deliberate — inferring it from
    config is exactly the guess `cost_basis_from_api_key_source` refuses) and
    `model_hint` is
    empty, so `usage.model` comes from `modelUsage`'s dominant child and a
    costed frame with no children lands model-less. Totals and cost are
    unaffected; `modelUsage` is present in both shapes. There are no
    `message_delta` frames on this path either way, so these runs carry totals
    and NULL context columns.

`_compose_full_result()` does NOT live in the brain — both brains will
produce `(result_text, execution_trace)` and the executor reconciles them.

## Image attachments

`BrainRequest.images` arrives as paths and media types, and each brain owns the
conversion to its own provider's shape. That split is what keeps base64 out of
the task row and out of every log line, and keeps the executor from learning a
wire format. What the brains share is the rule underneath: **a model must never
be left to infer that it saw an image.** Every path that cannot deliver the
pixels names the image and says why, because "attached" alone is not evidence
of sight and silence is what produced the confident blind answer this whole
change exists to prevent (ISSUE-366).

**NativeBrain** builds the first user message in `_initial_user_content`: one
`TextContent` with `req.prompt` — the user half, since the standing
instructions reach the model on `AgentContext.system_prompt` instead — then one
`ImageContent` per image, text first,
which is the order OpenRouter's image-understanding guide documents.
`OpenAICompatibleProvider._message_to_wire` already renders those as
`data:<media_type>;base64,<data>` URLs, so the provider layer needed no change.
Encoding happens immediately before the first call, so the base64 lives exactly
as long as the request. Three refusals, each per image and none of them fatal to
the rest: the resolved model's `supports_vision` is false (no file is read at
all — reading bytes to discard them is pure cost — and every image gets
`_NO_VISION_NOTICE`, plus one operator WARNING naming the model, since
`supports_vision` defaults false and a direct-Anthropic base URL fetches no
catalog); the file vanished or is unreadable (`_UNREADABLE_NOTICE`, naming the
exception class only); or it outgrew `_MAX_IMAGE_BYTES` (6 MiB). That last bound
is asserted here rather than inherited from preparation because this is a
*second* read of a file under the user temp dir, which bwrap binds read-write
into that user's own sandboxes — another task of theirs can replace it between
the two reads. The constant is restated rather than imported (importing
`image_attachments` from a brain closes a cycle through `brain/__init__.py`) and
`tests/native/test_input_images.py` holds the two equal.

**Compaction must not delete the images in silence** (`session/compaction.py`).
The image-bearing message is at index 0 and `find_cut_point` walks back from the
newest, so an ordinary cut takes it — and `_serialize_for_summary` handles only
`TextContent` and `tool_call`, so the summarizer would never learn an image had
existed. Two halves, and they are deliberately exclusive rather than belt-and-
braces. `plan_image_pin` returns `(pin, summary_input)`: the pin is a small
`UserMessage` holding `_PIN_LABEL` plus the first image-bearing message's
blocks, prepended ahead of the summary, and `summary_input` is the same history
with exactly those blocks removed — so the summary's `[image <name> — no longer
in context]` notice is written only over blocks that really did go. Leaving them
in both places would write a durable summary saying an image was lost at the
moment it was being carried over, and that text is updated forward on every
later cycle. The pin is refused when it would take more than half the
`keep_recent_tokens` budget (`_PIN_TOKEN_SHARE`), because a pin that swallows
the tail budget makes `find_cut_point` return 0 and compaction a permanent
no-op; the loss notice carries the fact instead. The pin is what keeps the
capability, the notice is the floor.

**ClaudeCodeBrain and TmuxClaudeBrain** have no image block to send, so their
vision path is Claude Code's own `Read` tool, which returns visual content
rather than bytes. `build_image_prompt` prepends one of two sections to
`req.prompt`, and a request with no images is returned byte-identical:

- with tools, `IMAGE_DIRECTIVE_HEADER` plus the resolved absolute path of each
  image, requiring one `Read` per image before any answer or other action, and
  requiring a failed `Read` to be reported rather than guessed around. The
  wording follows `health/ocr._build_vision_prompt`, which shipped the same
  instruction first;
- with `allowed_tools=[]`, `IMAGE_OMITTED_HEADER` and the basenames. An empty
  tool list is a caller's policy decision (the sleep cycle, the health OCR
  paths), never a gap to fill: the tool set is not enabled implicitly, which is
  the split `health/ocr.py` already settled with its own
  `allowed_tools=["Read"] if read_path` line — where the same value also
  supplies `fs_read_roots` and the document bound into the request's
  `sandbox_wrap`, so the tool grant and its confinement travel together
  (ISSUE-395, ISSUE-397).

The tmux brain puts the same section in `prompt.txt` via `prompt_file_text`,
ahead of the original request, because it submits one buffer per run and a
second paste would be a second turn. Both brains name only basenames in a
notice and only resolved paths in a directive.

**The directive is checked rather than trusted.** The audit lives in the
executor (`unread_images`, `.claude/rules/executor.md`): it counts `Read` calls
in the recorded execution trace and appends a note naming any image that was
never opened. A recorded `ToolUseEvent` is a fact about what the model did,
which is a different thing from grading its prose.

**An image-payload rejection re-issues once, with a notice.**
`ClaudeCodeBrain._execute` applies the image section, runs one attempt, and on
`is_image_payload_rejection` re-issues with `images=[]` and
`build_withdrawn_image_prompt`, which names every withdrawn image and tells the
model not to imply it saw them. Not a silent strip: a blind retry can produce a
confident answer that lost sight, which is the original defect by another route.
Four bounds on that second call. It is skipped when the result was a success (an
answer *quoting* a provider error would otherwise cost a paid call and replace
the user's answer); it is skipped when `result.work_committed` is set, the same
veto `_is_retryable` applies for the same reason — a first-call 413 never
reached the model and arrives with the flag clear, while a 413 later in a run is
the accumulated context and is a reroute rather than a re-issue; `req.result_file`
is unlinked first, or the re-issue can deliver the text the *images* produced
under a prompt saying they were withdrawn; and the timeout is what remains of
the original budget, floored at `_MIN_REISSUE_SECONDS`, since two full attempts
under one `timeout_seconds` would hold a worker for twice its configured bound.
Once only — a rejected re-issue falls through to the ordinary classification.

## API error helpers
| Function | Purpose |
|---|---|
| `parse_api_error(text) -> dict \| None` | status_code/message/request_id from `API Error: NNN {json}` **or** the bodyless `API Error: NNN <text>` the CLI also emits (ISSUE-212 — matching only the JSON form meant a bare `API Error: 529 Overloaded` parsed as nothing: not transient, not retried, not a fallback trigger). Tail stops at the newline |
| `is_transient_api_error(text) -> bool` | True for a capacity status (**every 5xx**, plus 408/425/429 — see `_status_is_transient` below) **or** a network-level failure (connection reset / timeout / DNS / `ECONNRESET`-class errno). The network branch is gated on an `API Error` marker (or an unambiguous errno) because this predicate also runs against arbitrary tmux pane text; an explicit status is authoritative, so a 400 quoting "connection reset" stays permanent (NB-13a) |
| `is_permanent_api_error(text) -> bool` | True for a request-shaped failure: `PERMANENT_STATUS_CODES` (`400/401/403/404/405/413/414/422`), context-length, content-filter, `invalid_request_error`-class bodies. A transient status wins over request-shaped body text |
| `api_error_stop_reason(text) -> str \| None` | The single classifier every execution path uses: `usage_limit` > `error` (permanent) > `transient_api_error` > `None` (not a provider error). `_failure_stop_reason` is `api_error_stop_reason(text) or "error"` |
| `_status_is_transient(status) -> bool` | The live transient rule: **every** 5xx, plus 408/425/429. `TRANSIENT_STATUS_CODES` is kept as documentation of the common cases but is no longer the gate — enumerating was a latent second copy of this bug (a Cloudflare-fronted provider emits 520-526, none of which were listed, so each would have dead-ended exactly as 529 did) |
| `is_api_error_banner(text) -> bool` | True iff the text *is* a bare API-error banner — anchored at the start (past ≤8 chars of decoration) and length-gated, mirroring `is_usage_limit_banner`. `claude -p` reports a provider failure as a **success** result frame with the error as the whole answer, which is how a raw `API Error: 529 Overloaded` reached the user as the final reply; strict so a genuine answer *discussing* an earlier API error isn't converted into a retry + a paid fallback call |
| `parse_retry_after(text) -> float \| None` | The provider's requested wait, capped at `RETRY_AFTER_MAX_SECONDS` (60s) and treating ≤0 as absent. Both retry loops use it in place of the fixed delay; the cap exists so a worker is never parked on the provider's word for an hour when the task's own retry ladder / the fallback could take over |
| `is_usage_limit_error(text) -> bool` | True if the text carries a subscription/quota/billing usage-limit signal (keyword set + an "exceeded…limit" regex). Provider-agnostic (works on CLI output, tmux transcript/pane text, and native error bodies). Checked *before* the transient predicate at every call site so a quota 429 classifies as `usage_limit`, not a retry. |
| `is_image_payload_rejection(text, has_images) -> bool` | True iff the provider is refusing *this* request's images: a 413, or a 400 whose diagnostic names an image (`image`, `media_type`, `attachment`). The 400 arm needs that test and the 413 arm does not — `exceeds`, `too large` and `maximum` are also the vocabulary of a context-length complaint, and matching them buys a second paid run plus a notice blaming images that were not the problem. Checked ahead of the classification above, which puts both statuses in `PERMANENT_STATUS_CODES` and would fail an otherwise valid text task with no answer and no fallback |

`parse_api_error`, `is_transient_api_error` and `is_usage_limit_error` are
re-exported from `executor` for `scheduler.py` and tests; the newer helpers are
imported from `brain.claude_code` directly (nothing needs a back-compat
re-export). Canonical home is `brain/claude_code.py` for all of them.

**Consumers must pick the right strictness.** `parse_api_error` answers "does
this text contain a provider status code" — fine for *formatting* an
already-known failure, wrong for *deciding* something is a failure.
`scheduler`'s masquerading-success guard and `_is_policy_refusal` both discard a
completed answer, so both key on `is_api_error_banner`; widening the parser
without moving them was the ISSUE-212 fix's own near-miss (an answer summarising
yesterday's 529 would have been failed and retried three times).

**Retry vs reroute.** `BrainResult.work_committed` marks a failure whose run
already reached the model and may have executed tools — set by every
success-frame reclassification, since the CLI ran to completion. `_is_retryable`
vetoes the in-brain retry on it, so those failures are reroute-only and a task
that wrote files or sent mail before the provider fell over doesn't repeat that
work three times. The backoff itself is slept in `_RETRY_SLEEP_SLICE_SECONDS`
slices with a `cancel_check` poll between them, so `!stop` lands during a
(now possibly 60s) provider-requested wait rather than after it.

## Configuration
```toml
[brain]
kind = "claude_code"  # "claude_code" | "native" | "tmux_claude"
# Availability failover (see "Brain fallback" below). "" = none.
fallback = "native"               # brain kind to fall back to when primary unavailable
fallback_on_transient = true      # also reroute a persistent transient_api_error (default on)
fallback_cooldown_seconds = 900   # skip an unavailable primary this long; 0 disables stickiness
# Brain kinds a room may pin for itself. Empty (the default) = none may.
# See "Per-room brain selection" below.
room_selectable = ["claude_code", "native"]

[brain.native]         # only used when kind = "native" (or routed-to/fallen-back-to below)
provider = "openai_compat"
model = "claude-sonnet-4-6"
effort = ""            # default reasoning effort; capability-gated on supports_thinking
base_url = "https://api.anthropic.com/v1"
# prompt_caching       # omit to derive from base_url (on for api.anthropic.com); set true/false to force
# api_key via ISTOTA_BRAIN_NATIVE_API_KEY env override (kept out of TOML)

[brain.native.web_fetch]  # daemon-side WebFetch tool (native-only). Safe defaults.
enabled = true            # false omits the tool entirely
allow_http = false        # permit cleartext http:// (off = HTTPS-only, matches CONNECT-only posture)
timeout_seconds = 20.0    # total wall-clock per fetch
max_bytes = 5_000_000     # response body byte cap (streamed)
max_content_chars = 100_000  # extracted-text cap returned to the model
max_redirects = 5
allowed_ports = [80, 443]
# allow_hosts = []        # if non-empty, a suffix-match allowlist (default-open by design)
# block_hosts = []        # always-denied hosts (suffix match)
# extra_blocked_cidrs = []  # operator additions to the private/reserved IP blocklist
require_url_provenance = false  # only fetch URLs seen in the task prompt (blocks model-fabricated
                                #   URLs, and blocks a WebSearch-then-read chain — the corpus is
                                #   the user half of the prompt, never a tool result)
admin_only = false              # true withholds the tool from non-admins (the pre-ISSUE-449 rule).
                                #   Read by build_allowed_tools, not by the tool: the one field
                                #   here with no WebFetchPolicy counterpart

[brain.native.session_log]  # per-attempt JSONL transcript (native-only). See "Session logs" below.
enabled = true              # false = no writer, no file, no directory, no cost — AND no sweep,
                            #   since step 7b is gated on this too, so transcripts already on
                            #   disk are then kept for ever and `doctor` SKIPs. Remove them by hand
dir = ""                    # "" resolves to {db_path.parent}/logs. A value here is used AS GIVEN
                            #   and is TRUSTED — no containment rule bounds it, and the sweep
                            #   unlinks *.jsonl under every first-level subdirectory of it
retention_days = 14         # age rule (privacy). 0 = keep for ever by age; the ceiling still applies
max_total_gb = 2.0          # size ceiling (disk) across EVERY user summed, 0.5 floor.
                            #   0 = no ceiling; the age rule still applies. Both at 0 (or
                            #   enabled = false) means nothing is ever deleted
max_content_chars = 32768   # per text/thinking block, head+tail. 0 here means NO CAP
max_args_chars = 8192       # per tool-call arguments object. 0 = no cap
include_thinking = true     # reasoning traces in the written log (independent of the read verb)

[brain.tmux]           # only used when kind = "tmux_claude". All defaulted —
                       # an empty/absent block reproduces the prototype exactly.
fallback_trip_threshold = 5       # consecutive launch failures before the circuit opens
fallback_cooldown_seconds = 300   # how long the circuit stays open
ready_timeout_seconds = 30        # REPL-ready deadline
tmux_command_timeout = 10         # per-tmux-subprocess timeout
cli_version_pin = "2.1.168"       # supported claude CLI; mismatch logs a WARNING
# ready_markers / trust_markers / theme_markers / bypass_warning_marker /
# bypass_accept_marker / error_markers / usage_limit_markers — pane-substring
# heuristics; override on a CLI reword. usage_limit_markers (checked before
# error_markers) classify a pane limit hit as stop_reason=usage_limit → fallback.

[brain.source_type_overrides]   # per-source-type routing (gradual rollout)
scheduled = "native"
heartbeat = "native"
```

## Per-room brain selection

A room carries a standing brain default the way it already carries `model` and
`effort`. Two nullable columns hold it: `rooms.brain`, the standing choice, and
`tasks.brain`, the answer for one task. `record_inbound` copies the first onto
the second at task creation, in the same block that fills `model` and `effort`
and under the same `room_surface` guard — so Talk and web, and deliberately not
email, which joins a room's transcript without being a room surface
(`.claude/rules/transport.md`).

**A model pin carries the namespace it was written in, and that is a recorded
fact rather than an inference** (ISSUE-420). `rooms.model_namespace` is written
by whichever producer set `rooms.model`, from the brain it actually resolved the
alias against, and `record_inbound` freezes it onto `tasks.model_namespace`
beside the model; `executor._pin_origin_namespace` prefers it to every
inference. Nothing on the row could replace it, which is the point: reading the
origin off `tasks.brain` is right for a pin written *while* that kind was
admitted (ISSUE-417's case) and wrong for one written after the operator dropped
the kind from `room_selectable`, because `brain_for_room` then refuses the pin
and resolves the alias in the lane's namespace instead. The two writes leave
identical rows and want opposite answers. Reading it off the *lane* is wrong for
a third case, ISSUE-421(c): `rooms.model` is one column shared by every bound
surface, written against the writing surface's lane and read against the inbound
one. Recording it settles all three. NULL means "not recorded" — every row
written before the column — and the old inference answers those, so the upgrade
moved nothing. Only the model's own writers touch it: `!room effort` leaves it
alone, and clearing the model clears it.

A task column as well as a room column, for the reason `model` and `effort`
have both: every site that resolves the brain already holds the task row, so
the resolution stays a pure function of that row rather than a second read of
the rooms table on a hot path; an edit to the room must not change a task
already running; and retry and subtask inheritance then come free
(`_create_retry_task` and the deferred subtask writer copy the column). A `source_type` of `subtask` would otherwise
take `source_type_overrides["subtask"]` and could silently differ from its
parent.

The `model` half is not free in the same way, because a stored name is
namespaced by the lane it was written in rather than by the row. With `brain`
set the column carries the namespace down with it and the name travels; with it
NULL the child would read its own lane for a name the parent's lane produced, so
the subtask writer carries the pin only where the two lanes read the same
vocabulary and drops it otherwise, leaving the child on the routed brain's own
default (ISSUE-421, `scheduler_deferred._inherited_model`).

**Resolution order**, highest first:

```
tasks.brain  >  [brain.source_type_overrides][source_type]  >  [brain] kind
```

`resolve_brain_kind(source_type, brain_config, override=None)` is where all
three meet. The room sits *above* the source-type layer because the two answer
different questions: `source_type_overrides` is an operator's gradual-rollout
knob keyed on a lane, a room's brain is an explicit human choice about one
conversation, and an explicit pick a lane rule silently overrode would be
indistinguishable from a bug.

**Two producers write `tasks.brain`, and the allowlist bounds both.** A room's
pin is one (`rooms.brain`, filled at `record_inbound` under the `room_surface`
guard); a scheduled job's is the other (`scheduled_jobs.brain`, a CRON.md
`[[jobs]] brain` field passed through by `check_scheduled_jobs`, ISSUE-419).
`room_selectable` is therefore narrower as a name than as a setting: the
question it answers is which kinds may be pinned from outside the operator's
own config, and `resolve_brain_kind` applies it to any `override` it is handed
without knowing where the value came from. That is why there is no
`job_selectable` — a second list means either a second gate or teaching this
function to branch on a provenance it has no reason to know, and an operator
who sets one and forgets the other gets a silent no-op. The naming cost is
recorded rather than paid; renaming the key touches about fifty occurrences
across ten source files plus the Ansible template and `render-config.sh`.

The job pin's own gate is a different one and sits earlier: CRON.md is
model-writable through the `schedules` skill, so `cron_loader.fj_brain_or_none`
drops the field at sync for a non-admin and keeps the rest of the job. Gating
at sync rather than at dispatch is what keeps the row and the `!cron` listing
from showing a pin the author was not allowed to write. It answers *who* may
pin and deliberately not *what*, so an admin's unlisted kind is stored here and
falls through at dispatch, and the listing shows it — read that line as the
sync gate's answer rather than as a statement about which brain will run.

An override is admitted only when it names a buildable kind **and** one the
operator listed in `[brain] room_selectable`. Those are two separate refusal
branches, each a WARNING and a fallthrough to the source-type layer, never a
failed task — the same contract an unknown `source_type_overrides` target
already has. The allowlist branch is what makes shortening the list take effect
at the next dispatch with nothing having to rewrite stored rows: the column
keeps its value, the operator may restore the list, and `!brain` says the room
is set to a kind that is no longer offered and names what it is running
instead.

**Each refusal is logged once per process, not once per call** (ISSUE-422).
Both branches above and the unknown-`source_type_overrides`-target branch go
through `_refusal_is_unreported(arm, kind, source_type)`, because every one of
them is a static fact about a stored row and the operator's config: nothing
changes between calls, so the second line said nothing the first had not and
the sequence ran for as long as the misconfiguration lasted — about 1440 lines
a day for a `* * * * *` cron job with a refused pin, times however many times
one task resolves its brain. `arm` is in the key rather than being decoration:
a refused pin falls *through* to the source-type layer, so one call can refuse
the same name twice for two different reasons with two different remedies, and
keying on the name alone silences the second. Two bounds sit on the key set,
and neither implies the other — `pinned` comes off `scheduled_jobs.brain`, a
plain string field CRON.md can write, so the entry count is capped
(`_WARNED_REFUSAL_CAP`) against one durable entry per attacker-chosen value and
each axis is truncated (`_REFUSAL_SHOWN_CHARS`) against a bounded number of
unbounded ones. The same truncation bounds the value as it is *logged*, since
256 unbounded lines fill a disk as surely as 1440 short ones. Reaching the cap
logs one line saying further refusals are suppressed, rather than going quiet in
a way that reads as the refusals having stopped; with three buildable kinds and
eleven shipped source types the legitimate ceiling is far below it. That budget
is **shared across the three arms**, so a flood on one silences a condition
first seen on another — accepted, since the refusal itself is unaffected and
only the line goes. The latch is also process-lifetime and keyed on nothing
about the config, so it **outlives a SIGHUP reload**: a kind added to
`room_selectable`, reloaded, and removed again refuses silently for the rest of
that process. Same shape as `config._RO_PATH_CONTROL_TREE_WARNED`, which is
likewise never cleared across `load_config` calls. No lock: the
scheduler resolves brains on worker threads and the two outcomes of a race are
one duplicate line or a cap overshot by a few, which is the trade
`scheduler._warn_once` already makes. `tests/conftest.py` clears the set between
tests, since a process-global latch otherwise decides whether a test asserting
on one of these warnings passes by who ran first.

The same change made "never wedges a task" true of the argument *types*, which
it was not. A non-string `source_type` raised `AttributeError` on `.strip()`
and an unhashable `source_type_overrides` target raised `TypeError` from the
membership test, both out of a function `execute_task` calls unguarded.
Neither is reachable through `load_config`, whose hook stringifies that mapping
on both sides — which is why it went unnoticed — but `reachable_brain_kinds`
already reads the same mapping behind a `try` for exactly this reason. Both
reads go through `_as_text` now. That is a different helper from `_shown` on
purpose: `_shown` bounds the value, and a bounded string is right for a dedup
key and a log line and wrong for the override *lookup*, where it would let an
over-long `source_type` match a lane it does not name.

**The allowlist is empty by default and is a gate rather than a preference.**
Brain kind decides which process holds the agent loop, which credentials that
process carries, which `SandboxProfile` is built and what tool set is
registered — so it changes an enforcement posture, and a change to one should
not arrive switched on by an upgrade. Be exact about which posture, because the
obvious answer is out of date: since ISSUE-389 the native brain's six core
tools run in a per-attempt bwrap namespace of their own (`tool_server`, under
`SandboxProfile.NATIVE`), so tool *execution* is no longer the difference.
`native_fs_roots` is the error-message layer above that namespace, and the only
confinement there is on macOS, the standalone install and the shipped Docker
stack — where the CLI brains are equally unconfined, so it is not a
native-versus-CLI delta there either. Writing a room's brain is admin-gated on top of the list; reading is
not, since every member of a shared room is entitled to know what their turns
run under. `room_selectable_kinds(brain_config)` is the allowlist intersected
with the kinds `make_brain` can build, so a name that is not one is offered to
nobody. `config._validate_room_selectable` warns about it once per load and
leaves the value on the field: `resolve_brain_kind`'s own refusal cannot cover
this case, since that fires when a room *pins* a kind and a name the picker
never offered is a name no room can pin — the "typo that did nothing" shape,
where the operator sees a feature that does not work and no reason why.

**The live delta is egress, and a shared room is what made it reachable.** A
room's brain applies to every member's turns, admin or not — the fill is
unconditional on sender — so an admin pinning a room to `native` decides the
posture for a non-admin who chose nothing. The native brain's `WebFetch` runs
in the **daemon's** network namespace, outside the CONNECT allowlist, where the
same user's CLI-brain task has `--unshare-net` plus that allowlist; and the
tool-server namespace above does not cover it, since it is a daemon-side tool
rather than one of the six.

`build_allowed_tools` used to answer that by withholding `WebFetch` from a
non-admin. It no longer does (ISSUE-449): the answer is
`[brain.native.web_fetch]`'s own egress policy — `allow_hosts`, `block_hosts`,
`extra_blocked_cidrs`, `allowed_ports`, `allow_http`, the built-in
private/reserved blocklist and `require_url_provenance` — which binds every
caller identically, where the identity gate bound nobody's destinations and
merely decided who got a tool at all. What it cost was concrete and the reason
the issue was filed: reading a web page, which is about the most ordinary thing
a user asks for, did nothing for a non-admin and said nothing about why. The
gate survives as `admin_only`, off by default, and it is still read
**unconditionally rather than only for a pinned room** — the asymmetry predates
per-room selection and exists on any native-default deployment, and a rule
scoped to pinned rooms would leave a non-admin *more* egress on the deployment
default than in a room somebody pinned. `WebSearch` was never in this argument
and stays for everyone: it runs at the provider and returns titles and URLs,
granting this host no egress at all.

The other decision worth not relitigating is that `require_url_provenance` is
**not** turned on for non-admins as a middle ground. Two reasons, and the first
is mechanical: the corpus is built from the user half of the prompt alone, so a
WebSearch-then-read chain — the exact flow the issue describes — fails it, and
the middle ground would trade a silent absence for a frequent refusal. The
second is that it would make one deployment-wide setting mean two different
things depending on who was asking, which is the shape `config_mapper` exists to
stop.

### A pinned room has no failover

An admitted override returns `replace(brain_config, kind=…, fallback="")`, so
`effective_fallback_kind` answers None and the failover machinery collapses to
a plain primary call. The room named *this* brain; a task that cannot run on it
fails with the primary's own `stop_reason` rather than answering from a
different model — and rather than `FALLBACK_EXHAUSTED_MARKER`'s "both brains
are down" wording, which would be false, since only one was tried.

**A pinned cron job takes the same rule, and it is a different trade rather
than the same one.** Nobody is watching, so the argument for keeping it is
stronger, not weaker: a job answering from a model nobody chose, unattended and
on a schedule, is worse than one that fails where it can be seen. And it is
seen — the task's retry ladder exhausts at 1, 4 and 16 minutes,
`consecutive_failures` climbs, the job auto-disables at five and
`notification_resolvers/cron_job.py` raises it in the inbox. `!cron enable` is
the remedy. Kept rather than softened, and documented rather than assumed,
because the reasoning above is about a person reading a room.

One rule rather than three patches. Left alone, a routed kind inherits
`fallback` and the asymmetries are arbitrary in both directions: a
`native`-pinned room on a deployment whose fallback is `native` would have none
while every other room did, and a room pinned to the deployment's own fallback
kind would rerun a failed attempt through the identical brain. With no fallback
there is nothing to collide with, which is also why `room_selectable_kinds`
needs no exclusion rule for the fallback kind and why `reachable_brain_kinds`
folds failover over the base kind and the `source_type_overrides` targets and
**not** over `room_selectable`.

The decision is made at the moment of admission rather than inferred
downstream, and it has to be: a room pinned to the kind that is already the
instance default resolves to a config equal to the unrouted one, so "was an
override admitted" is not a question the executor can ask of a `BrainConfig`.
Pinning the default kind therefore still counts as pinning. The alternative is
a rule with an exception, which is harder to explain and no less surprising; the
surprise is removed by saying it instead, in the `!brain` set reply and in the
failover line every `!brain` read carries.

**What a pinned room does not lose is the breaker and the alert.** The
breaker-open block in `executor` is deliberately not gated on a fallback being
configured (ISSUE-362) — the breaker is a shared signal the direct callers read
through `primary_brain_unavailable` — and `_fire_fallback_alert` fires there
with `fallback_kind=None`. Only `_skip_primary` is gated on a fallback
existing. So through an outage a pinned room's task makes one doomed primary
attempt rather than being skipped straight to a substitute that does not exist,
the operator is alerted, the availability record is written, and the first task
after the provider recovers succeeds. The cooldown holds back the direct
callers, not the room's tasks. This is the same shape any deployment running
the shipped `fallback = ""` already has. `!brain default` restores the
inherited brain and its failover.

### The model-namespace rule

`rooms.model` holds a canonical id resolved in one brain's namespace, and the
room does not store the alias that produced it — so an id that crossed from
`anthropic` (`claude_code`, `tmux_claude`) to `openai_compat` (`native`) is not
re-resolvable, only unrunnable. Two halves, and the first is worth nothing
without the second:

1. **A brain change across a namespace clears `rooms.model` and `rooms.effort`
   as a unit**, and the reply or the PATCH response says what it dropped.
   `commands._clear_pin_across_namespaces` is the one implementation; the web
   PATCH calls it rather than restating the rule. A move within a namespace
   keeps the pin, which is correct — the same id runs under both. An
   undeterminable namespace clears, which is the opposite of this module's
   usual "behave as before" and is right here: leaving a pin whose portability
   is unknown is the failure being prevented. Effort alone survives a change,
   since a semantic rung is portable; it goes only when the model goes, because
   the two were written as a pair.
2. **Every writer of `rooms.model`, and every surface that offers a model name,
   resolves through the room's brain** — `commands.brain_for_room(config, conn,
   room_token, source_type)` on the command and Talk paths,
   `web_app._brain_for_room_token` on the web ones. Without this the next
   `!room model sonnet` writes an anthropic id back into a native room and
   undoes the clear permanently. Seven surfaces over six call expressions: the
   `!model` prefix on Talk (`transport/talk/inbound.py`) and on web
   (`chat_send_message`), `!room model`, `_known_room_models` — which gates the
   web PATCH — `/chat/commands`, which takes an optional `room_id` for this and
   nothing else, and then `!models` and `!help`, which share
   `commands._ctx_brain`. A surface added through that helper costs no new call
   and makes the count eight with nothing going red, so read the helper's
   callers rather than the number. The composer's own autocomplete still asks unscoped, so its
   suggestions can be stale; a pick it produces is refused server-side against
   the room's brain rather than run.

`brain_for_room` returns a `BrainConfig`, so a caller that needs to resolve an
alias composes `make_brain(brain_for_room(...))`. It never raises: the whole
call including `resolve_brain_kind` is inside the guard, because `rooms.brain`
is TEXT in a dynamically typed store and `.strip()` on a non-string would
otherwise escape into the Talk poll loop. The per-user native API-key overlay
the executor applies is deliberately not applied here — this path resolves
alias names and reaches no provider.

### Surfaces

`!brain` has three forms. Bare, it reports the chain — the room's own setting,
the rule for this lane, the instance default, and a failover line — and is
ungated. `!brain <kind>` sets it, admin-only, bounded by the allowlist, naming
both consequences. `!brain default` clears it; `default` is checked ahead of
the allowlist, since clearing is a narrowing and emptying `room_selectable` is
the documented way to switch the feature off, which is exactly how a room ends
up holding a pin nothing honours. `!room` gains a read-only brain line and no
setter — one writer, for the "several ways to name the same destination" reason
that has produced three room bugs in this codebase already.

`PATCH /api/chat/rooms/{id}` is the second writer, on the key-presence contract
`model` and `effort` already use, with the admin gate keyed on the key's
presence so a clear is gated too. `brain` and `model` can arrive in one body,
which the settings modal makes the common case: `model` is applied first, then
the brain, then the namespace rule against the pin this request just wrote. See
`.claude/rules/web-chat.md`.

`doctor` reads `reachable_brain_kinds(brain_config)` rather than
`config.brain.kind`, so `runtime.model_cli` and `runtime.tmux` run on a
deployment that can only reach those kinds through a room, and
`runtime.native_brain` is new — `make_brain("native")` constructs a defaulted
dataclass and asserts no credential, so "buildable" was never "runnable" and an
allowlisted `native` could otherwise give a room where every turn fails at the
provider with nothing naming it. Config only, no database: a
`SELECT DISTINCT brain FROM rooms` would make two pure checks
database-dependent and would still answer wrongly for a room nobody has used
yet. The cost is stated rather than hidden — allowlisting a kind gets that
kind's checks whether or not a room selected it, which is the safe direction.

`!steer`'s gate reads the task's own `brain` rather than `config.brain.kind`,
so a refusal names the brain that room actually runs. Better in the static case
and not a one-way improvement: a task whose primary fell back mid-attempt runs
a brain the column does not name, so the gate can still accept a note that
reaches nothing. That state is reachable today for the same reason and this
neither creates nor fixes it — a pinned room can no longer get into it at all,
since it has no failover.

`task_usage.brain_kind` is written per attempt and is already a `!usage` group
key, so the "By brain" split becomes a per-room readout with no reporting work.

## Brain fallback (availability failover)

When the primary brain is **unavailable**, the executor reruns the same attempt
(no new DB row, no `attempt_count` increment) through a configured fallback
brain. Generalizes the old hardcoded tmux→claude_code rerun; wired at the
executor level (brains have no `Config` for the operator alert; the
same-attempt rerun already lives there). Three cooperating pieces:

- **Unavailability classification.** Each brain classifies "I am unavailable"
  into a `stop_reason`. `usage_limit` (new, shared `is_usage_limit_error`
  detector) covers subscription/quota/billing exhaustion on all three brains:
  ClaudeCodeBrain wires it into both exec paths (before the transient check —
  a quota 429 is not retried); NativeBrain's `_classify_native_error` maps a
  quota/billing error body → `usage_limit`, a plain overload/rate-limit →
  `transient_api_error`; TmuxClaudeBrain detects it in the transcript/Stop-payload
  body (`_build_result`) and via `usage_limit_markers` pane match in
  `_wait_for_completion`, guarded so it never feeds the launch `_CircuitBreaker`
  or tmux's own headless fallback.

- **Portable alias layer** (`brain/_aliases.py`). `CANONICAL_ROLES =
  ("fast","general","smart")` is the single source of truth (every brain's
  `DEFAULT_ALIASES` tier keys import it); a contract test asserts every brain
  resolves every canonical role. `is_portable_alias(name, portable_names)` (with
  `split_effort` applied first, so `smart:low` reads portable) decides whether a
  requested model name is a portable *intent* (a canonical tier, or a custom
  alias the operator flagged `portable = true`) that re-resolves in the fallback
  namespace, or a non-portable pin (shortcut `opus`, canonical `claude-opus-5`)
  that can't cross the boundary. The executor computes `portable_names` via
  `config_alias_portable_names(config)` (`CANONICAL_ROLES` ∪ declared-portable).

- **Availability breaker + routing** (`brain/_fallback.py`, wired in
  `executor.py`). See the trigger/cooldown sets and the executor path in
  `.claude/rules/executor.md` "Brain fallback". `PrimaryAvailabilityBreaker` is a
  process-global, thread-safe breaker keyed by primary kind — distinct from
  `tmux_claude._BREAKER` (which governs tmux launch fast-fail); the two compose.
  `effective_fallback_kind(brain_config)` is the configured `[brain] fallback`
  or None — explicit config only, no implicit target for any kind, and None also
  where the configured value equals *this* config's `kind`, since rerunning the
  same brain cannot help. That last test is here rather than only at config load
  because `brain_config` may be a **routed** config: `resolve_brain_kind` returns
  `replace(brain_config, kind=target)` and the routed config inherits `fallback`,
  so `kind = "claude_code"` + `source_type_overrides = {scheduled = "tmux_claude"}`
  + `fallback = "claude_code"` is a self-fallback for an interactive task and a
  real target for a scheduled one. A routed config is also how a **room-pinned**
  brain gets no failover at all: `resolve_brain_kind` clears `fallback` on the
  admission path, so nothing here needs a condition of its own — see "Per-room
  brain selection" above for why that is one rule rather than three. A
  `tmux_claude` primary used to resolve to `claude_code` there with nothing
  configured; that shim predated the generalized `fallback` key and was removed
  in ISSUE-362, because it left no value of `fallback` meaning "no failover" on
  a tmux deployment and made both of `_validate_brain_fallback`'s "disabling
  fallback" warnings false — blanking the field is what activated it.

**Trigger set** (reroute this attempt): `{usage_limit, not_found, fallback}` +
`transient_api_error` iff `fallback_on_transient` (**on by default** since
ISSUE-212 — a capacity error that survived the primary's own
`API_RETRY_MAX_ATTEMPTS` is precisely what the fallback exists to absorb, and the
alternative is handing the user a raw provider error). **Cooldown set** (open the
breaker → skip the primary on subsequent tasks for `fallback_cooldown_seconds`):
`{usage_limit, not_found}` only — `fallback` is excluded so tmux keeps being
probed per-task (its own breaker decides when to stop). **Never fallback:**
`oom` / `timeout` / `cancelled` / `error` (task-level outcomes, flow through the
normal path). Config keys: `[brain] fallback` / `fallback_on_transient` /
`fallback_cooldown_seconds`; `_validate_brain_fallback` (config load) neutralizes
an unknown kind with one WARNING, and a self-fallback — now "the only kind this
deployment runs", not the bare `fallback == kind` string comparison, so the
routed shape above survives — with another. It also logs one INFO line, once per
process, where `tmux_claude` runs (as `kind` or as an override target) with no
fallback: that pairing was unconfigurable before ISSUE-362, so an upgrade drops
failover silently otherwise, and `load_config` runs in every skill-CLI spawn, so
the notice is process-scoped rather than per call. Single fallback level only;
if the fallback is also unavailable the task fails/retries normally. On a dropped
non-portable pin the successful reply gets a one-line italic model note.

**The cooldown is a deadline, not a duration** (ISSUE-374). `fallback_cooldown_seconds` is the *ceiling*; where the reason is `usage_limit` and the primary is a subscription brain (`claude_code` / `tmux_claude`), the window ends at the quota's own reset instead. `open_primary_breaker` is the one place that decides it, so the in-memory breaker and `brain_availability`'s `expires_at` cannot describe two different windows — the executor's task path and `report_brain_result` both go through it. The reset comes from `subscription_usage.cached_reset_seconds`, which reads the deployment-wide **disk cache only**: no fetch, no credential resolution, no socket on the path a failing task is standing on, which it can afford because `resets_at` is absolute and the cache reader recomputes the countdown against now. `soonest_reset_seconds` takes the earliest *future* window and ignores which one hit its limit — a `stop_reason` does not say, and the asymmetry decides it: too short costs one failed primary attempt and reopens the breaker, too long runs every task in the remainder of the window on a different model. That is the observed failure — a limit hit eleven minutes before the reset held every task on the fallback brain for the remaining forty-nine, with the primary idle and available. Clamped in both directions inside `open`'s lock: never past `opened_at + fallback_cooldown_seconds`, so a wrong reset cannot pin the deployment to its fallback, and never below `MIN_COOLDOWN_SECONDS` (60, itself capped by the cooldown), so a reset seconds away does not produce a breaker that does nothing. `not_found` is excluded — a quota reset says nothing about a missing binary — as is a `native` primary, whose provider has its own quota on its own clock. No cache, a disabled `subscription_usage` or a window that has already reset all fall back to the flat cooldown. A repeat failure inside an open window still never moves the deadline, a later `until` included.

### Direct-caller availability (ISSUE-181)

The sleep cycle (`memory/sleep_cycle.py:_run_sleep_cycle_brain`) and shared-block
synthesis (`briefings/shared_blocks.py:_run_section_brain`) call the primary
brain **directly** (`make_brain(config.brain).execute(req)`), not through the
executor's fallback-wrapped path — so "pause on fallback" reduces to "detect
primary-brain unavailability and skip." Two Config-free helpers in
`brain/_fallback.py` give them the same breaker signal the executor arms:

- **`primary_brain_unavailable(brain_config) -> (available, reason)`** —
  consult before each call (or before a batch). Returns `(False, "unavailable")`
  when the breaker is open for the primary kind, so a degraded primary doesn't
  grind through every channel/block. Honours `fallback_cooldown_seconds` (`0` =
  every caller probes, matching the executor).
- **`report_brain_result(brain_result, brain_config) -> reason | None`** — feed
  a direct caller's `BrainResult` back into the shared breaker. Opens it
  (returns the `stop_reason` only on the closed→open transition → caller arms
  exactly one operator alert) on `usage_limit`/`not_found`; closes it on success.
  Mirrors the executor's task path, so the breaker is a **single shared signal
  across all brain callers** — whichever path first hits the limit opens it and
  alerts; the others see it open and skip silently.

The sleep cycle short-circuits both passes at the top (`check_sleep_cycles` /
`check_channel_sleep_cycles`) and re-checks per-iteration so a mid-pass failure
stops the remaining channels (the four-identical-errors-in-six-seconds pattern
from ISSUE-181). Shared-block synthesis skips the gather+brain and keeps
last-known-good content; one operator alert fires when the breaker opens.
`structured` shared blocks never touch the brain, so they still generate when
degraded. The breaker cooldown gates the next scheduled run, so neither
re-attempts every cycle while the primary stays down; a bounded "still down"
heartbeat re-alerts once per cooldown window (org-monthly limit) until an admin
raises it, then the next probe succeeds and closes the breaker.

These six sites (plus the executor and conversation-context triage) are also
the nine `BrainRequest` construction sites the advisor-model spec enumerates.
All six build their env from `executor.build_model_cli_env` (ISSUE-395 — they
used to pass `dict(os.environ)`, handing the model the master Fernet key, the
Nextcloud app password and every service token).

**Three of them are sandboxed and the rest are not, and the split is the tool
grant.** The OCR extractors pass `executor.build_daemon_sandbox(config,
user_id, extra_ro_binds=[document])` as `sandbox_wrap` (ISSUE-397); the others
grant no tool and stay unwrapped. That split is not cosmetic: a
non-empty `allowed_tools` is what makes `build_claude_cli_flags` add
`--dangerously-skip-permissions` with no `--allowedTools` allowlist at all, so
the CLI gets its whole default toolset — and both Claude brains ignore
`fs_read_roots`, taking their filesystem boundary from bubblewrap alone. Until
that wrap went in, an OCR extraction ran `Bash` and `Write` host-side as the
daemon user on the default deployment.

(The roster below is spelled "six" here and in `.claude/rules/executor.md` and
names seven modules. The count is wrong in both; the roster is what to read.)

The ones that stay unwrapped — unlike the executor, whose sandbox only
RO-binds the host's `~/.claude/settings.json` — read the daemon user's **real**
settings file directly. Any Claude Code setting that changes model behaviour
(`advisorModel` is the first one Istota has taken a position on) is inherited
there unless a brain neutralises it structurally; see
`.claude/rules/executor.md` § Environment Variable Mapping and "Model identity"
below.

### Fallback-compatibility posture registry (ISSUE-181, Problem 3)

`brain/_postures.py` declares, for every scheduled/automatic brain-calling
task, one of three postures for what happens when the primary is unavailable:

- **skip** — non-essential tasks (sleep cycle, shared-block synthesis, location
  discovery) that can wait. Don't run against a degraded brain; resume when the
  primary recovers. Implemented via the breaker helpers above.
- **pin** — essential tasks that must produce a real answer and shouldn't ride
  the fallback (briefings — ISSUE-180; scheduled `prompt` jobs via per-job
  `model`).
- **fail_clean** — interactive-but-automatic callers (health OCR, biomarker
  explainer) whose failure should be visible ("couldn't generate — brain
  unavailable") rather than a silent stub.

The registry is a declared, discoverable data structure (`TASK_POSTURES`,
`task_postures_by_name()`) — each entry carries its call site + notes — so the
policy is auditable in one place rather than scattered as ad-hoc per-task
logic. A task not listed routes through the executor's fallback wrapper and
needs no separate posture. ISSUE-180 (briefings pin/fail-clean) is the inverse
face of this policy; together they define the essentialness/skip-pin-fail-clean
contract in both directions.

NativeBrain pi-parity capabilities (over `openai_compat`, the sole transport):
- **Final-turn answer (ISSUE-211).** `result_text` is `final_turn_text` — the
  text of the turn the run actually ended on. It used to be
  `last_assistant_text` (the last turn that happened to carry *any* text), so a
  tool-only or empty final turn shipped an earlier turn's between-tool-calls
  narration verbatim as the answer. An empty final turn now leaves `result_text`
  empty and `session.result._ensure_final_answer` surfaces "the turn ended
  without a final response" instead. Both values are tracked, because the
  **abnormal-stop paths deliberately keep the old behaviour**: a
  `_TRUNCATION_MARKERS` hit (NB-15) or a `_PARTIAL_ANSWER_STOP_REASONS` stop
  (`max_turns` / `loop_detected`, ISSUE-187) delivers the text *with a marker
  saying it is incomplete*, so falling back to `last_assistant_text` there is
  honest — and without it a capped run whose last turn was tool-only would ship
  a bare marker and drop the partial work, while a `max_tokens` turn after a
  real answer would flip a success into a retried failure.
- **Trace document order (ISSUE-211).** The agent loop runs a turn's tools
  *before* emitting its `turn_end` (`agent/loop.py`: `_execute_tool_batch` then
  `turn_end`), so appending tool entries as they fired recorded them **ahead of
  the text the model wrote first** — every native trace was inverted, which is
  measurable: 100% of native traces in production start with a `tool` entry
  against a 53/46 split for the CLI brains. The brain now buffers a turn's tool
  entries (`pending_tools`) and flushes them after that turn's text at
  `turn_end`, with a post-loop flush for a run torn off mid-turn. This matters
  beyond cosmetics: the finality rule in `session/result.py` reads "text after
  the last tool call" as the final message, so an inverted trace made narration
  look like the answer, and the web transcript's render groups showed tools
  ahead of the narration that preceded them.
- **Reasoning effort.** `req.effort or native.effort` → the OpenAI-compat
  `reasoning_effort` field, gated on `get_model_info(model).supports_thinking`
  (dropped + DEBUG-logged for non-reasoning endpoints). `xhigh`/`max` fold to
  `high` at the wire (provider-side `_REASONING_EFFORT_WIRE`); the raw tier stays
  on the task row. Extended-thinking deltas (`reasoning_content` / `reasoning`)
  parse into a `ThinkingContent` block excluded from `result_text`.
- **Prompt caching.** `_apply_cache_breakpoints` marks up to 4 `cache_control`
  breakpoints — tool defs (last tool), system, first user, and a rolling
  breakpoint on the last message each turn (the cross-turn-hit win).
  `make_provider` defaults caching ON for `api.anthropic.com` and OFF elsewhere
  unless `prompt_caching_explicit` (set when the TOML key is present). Usage
  captures `cache_creation_input_tokens` → `Usage.cache_write_tokens`; a per-task
  `native cache hit_rate=…` line logs at task end.
- **Cost source.** `TaskUsage.cost_usd` prefers the provider's own reported
  cost over catalog pricing. OpenRouter returns real per-request cost (markup
  included) in the trailing usage chunk when the request carries
  `"usage": {"include": true}` — `openai_compat` sends that param scoped to
  `openrouter.ai` base URLs (other endpoints may 400 on it) and parses the
  top-level `usage.cost` via `_parse_reported_cost` (finite / non-negative /
  not bool-or-string; `NaN`/`Infinity` from `json.loads` are dropped so one bad
  turn can't poison the task total). `Usage.cost_usd` is three-state: `None` →
  `TaskUsage.add` computes from the catalog (`price_usage`), a number → used
  verbatim, `0.0` → a real free turn (respected). The native loop accumulates
  usage on `total_tokens > 0 or cost_usd is not None`, so a costed zero-token
  turn isn't dropped. Non-OpenRouter endpoints are unchanged (no request param;
  catalog pricing).
- **Model catalog (config-first + live OpenRouter enrichment, ISSUE-182).** Per-
  model metadata (`context_window`, `supports_thinking`/`supports_vision`, prices)
  resolves through `llm.catalog.get_model_info` — a pure, synchronous three-layer
  chain: operator `[brain.native.model_overrides]` (partial, merged on top) >
  live-fetched OpenRouter catalog (`_FETCHED`) > conservative `_DEFAULT`
  (`context_window=200_000`, zero price). **There is no bundled catalog file** —
  `model_catalog.json` was deleted. When `base_url` contains `openrouter.ai` and
  `[brain.native] model_catalog_fetch` is on, `NativeBrain._ensure_fetched_catalog`
  (called once at the top of the async run) fetches OpenRouter's public
  `GET /models` list, parses it (`llm.openrouter_catalog.parse_openrouter_models`
  — per-token USD → per-mtok, `input_modalities`→vision, `supported_parameters`
  `reasoning`→thinking), and installs it via `catalog.set_fetched_catalog`. The
  fetch is lazy, disk-cached (`{db_path.parent}/openrouter_models.json`, TTL
  `model_catalog_cache_ttl_hours`, parsed-fields not raw payload so upstream drift
  can't poison a read), and **never fatal**: fresh cache → live fetch → stale
  cache → 200k default. A process-global lock + `_CATALOG_FETCHED_AT` guard mean
  at most one fetch per process per TTL (no worker-thread stampede). A
  non-OpenRouter native endpoint (local vLLM/Ollama, direct Anthropic we don't
  run) is never fetched — it sets `context_window` (or a `model_overrides` entry)
  as the documented contract, else it gets the 200k default (overflow is
  recoverable; premature compaction is merely wasteful).
- **Overflow recovery.** A mid-task context-length error triggers a bounded
  (≤2) force-compact + `run_agent_loop_continue`, sharing the wall-clock deadline
  via `_run_loop_once`. `_build_recovery_context` force-compacts (aggressive
  `_aggressive_cut` fallback when `find_cut_point` returns 0) and appends a
  synthetic user nudge when the tail ends on an assistant message.
- **Image tool results.** `_tool_image_followup` renders an image-bearing tool
  result as a follow-up `role:"user"` block on vision models
  (`render_tool_images` = `supports_vision`); a no-vision model gets a text note.
- **The Claude runtime credential does not reach a native tool subprocess**
  (ISSUE-390). `executor.build_clean_env` copies `CLAUDE_CODE_OAUTH_TOKEN` out
  of the daemon's environment into every task's env, unconditionally and for
  every brain, and no skill manifest declares the name — so neither
  `derive_credential_set` nor `derive_proxy_only_set` splits it out to the skill
  proxy, and it arrived in `ToolEnv.subprocess_env` and then in the Bash child.
  Nothing on this path reads it (the provider key comes from
  `NativeBrainConfig`), so its only effect was that `echo
  "$CLAUDE_CODE_OAUTH_TOKEN"` came back as a `ToolResultMessage` addressed to
  whatever provider native is pointed at — the same provider-boundary crossing
  ISSUE-389 describes for the mounted `~/.claude/.credentials.json`, by the
  other mechanism. `claude_runtime_env.CLAUDE_RUNTIME_ENV_VARS` names what a
  task env carries only because the outer process is the `claude` CLI, and
  `without_claude_runtime_env` takes it back out. **Three call sites, not one**:
  `_hello_payload`, for what a Bash child is handed; `_start_tool_server`, for
  what the tool-server process itself carries, since a Bash child runs at the
  same uid in the same PID namespace and reads its parent's
  `/proc/<pid>/environ` — so stripping only the frame leaves the token
  reachable; and `execute_task`'s `proxy_base_env`, which is what every
  host-side skill CLI gets — the model
  reaches those through the same Bash tool, and they run unsandboxed as the
  daemon user. That third site had one reader after all, and the claim that it
  did not is what ISSUE-409 corrected: `code_review` spawns the `claude` binary
  per reviewer, so from ISSUE-390 every review on a subscription deployment came
  back `skipped / review_failed` about a second after it started. The strip
  stands and the exception is scoped rather than lifted —
  `executor.skill_model_credentials` copies the credential into the proxy's
  per-skill map for the skills in `SKILL_MODEL_CALLERS` alone, a copy rather
  than a third split because `ClaudeCodeBrain` needs the same value in the
  model's own env, and `_PROXY_LOOKUP_BLOCKED` keeps it out of the
  `credential-fetch` allowlist, which is a union anything holding the socket can
  read from.
  Four things about the shape are deliberate. It is a **name list, not a
  `CLAUDE_*` prefix rule**, because a prefix would also swallow an operator's
  own `passthrough_env_vars` entry; the drift that buys is covered by a guard in
  `tests/test_security.py` asserting over the *keys* `build_clean_env` produces
  (a value-based check sees only an untransformed copy and misses a rename, and
  `PATH` is already transformed in that same function). It **copies rather than
  mutates**, because the mapping is `req.env`: `ClaudeCodeBrain` hands it to the
  CLI and writes to it in place, and `_run_fallback` carries it across a reroute
  with `dataclasses.replace` without rebuilding it. It keeps `{}` and `None`
  **distinct** — `ToolEnv` reads `None` as "inherit the parent environment", and
  the parent is the daemon, whose environment is where the token came from, so
  `or None` goes on the input and a fully-stripped env must never collapse into
  inheritance. And the strip happens **at these seams rather than where the env
  is built**, which looks like the tidier place and is the trap: the env is
  assembled some six hundred lines before `_brain_config.kind` is known, and a
  per-brain-kind decision made there would strip the credential from a `native
  -> claude_code` fallback and leave the CLI unauthenticated on the Ansible
  shape, where that token is the credential. Inert where the variable is unset,
  including a deployment authenticating the CLI by credentials file alone.
  **The property is about this one credential, and that is not an arbitrary
  scope.** The skill credentials — `NC_PASS`, the mail passwords, the forge
  tokens — are already removed from the model's env by `_split_credential_env`,
  gated on `security.skill_proxy_enabled`, which defaults on. The Claude token
  is the one name that gating never reaches: `build_clean_env` sets it
  unconditionally and no manifest declares it, so `derive_credential_set` cannot
  see it, and it therefore survived on the *sandboxed* production shape where
  every other credential was already gone. Two neighbouring shapes look like the
  same bug and are not. With the proxy off, `setup_wizard` also sets
  `sandbox_enabled = false`, so nothing is confined and there is no boundary for
  an env var to cross — the task runs as the user and can read `config.toml`
  directly, which makes stripping decorative. With the proxy off *and* a sandbox
  on, credentials do land inside a real boundary; `load_config` warns on that
  pairing today, and whether the warning says enough is ISSUE-393.
- **Bash `exclude_from_context`.** The Bash tool takes an optional
  `exclude_from_context` boolean: the full output still streams to the user via
  `on_update`, but the model gets a short `[output shown to user; N bytes
  omitted from context]` stub instead of the body — for noisy commands the model
  doesn't need to reason over. Failure markers (`[exit code: N]` /
  `[command aborted]` / `[command timed out …]`) are appended to the stub so a
  failure still surfaces even when the body is omitted.
- **Bash runs under `pipefail`.** The argv is built by `shell_exec.shell_argv`,
  so it is `bash -o pipefail -c` rather than `bash -c` — the counterpart of
  ISSUE-307 on the shell the native brain actually uses. `[exit code: N]` is a
  claim about whether the command worked, and without the option a pipeline
  reported its *last* stage, so `pytest … | tail -3` came back clean on a suite
  that failed. It is the bare name rather than a probed absolute path because
  the argv is handed to `native_sandbox_wrap`: bubblewrap binds `/usr` and need not
  reproduce the host's `/bin` symlink, so PATH resolution inside the namespace
  is what has always worked here. `exit 141` is SIGPIPE (`| head`, `| grep -q`
  closing the pipe early) and carries `shell_exec.SIGPIPE_NOTE` after the
  marker, since a bare 141 reads as a failure and the command was correct. The
  second cost has no marker and is documented rather than detected: a non-final
  stage exiting non-zero to *report* something (`grep` with no match) now
  colours the pipeline.
- **So do the two CLI brains, by a different route (ISSUE-321).** A
  `ClaudeCodeBrain` or `TmuxClaudeBrain` task runs its commands through the
  Claude Code CLI's *own* Bash tool, which builds `bash -c 'source
  <shell-snapshot> && eval <cmd>'` in a process istota launches and does not
  instrument — so `shell_argv` cannot reach it and that shell started with the
  option off, on the surface where the great majority of tool calls happen. The
  environment is the only lever that does: `executor.build_clean_env` sets
  `SHELLOPTS=pipefail` (`shell_exec.pipefail_env`), which bash reads at startup
  and which survives the sourced snapshot — measured; the snapshot restores
  functions, aliases and PATH and touches no `set -o` option. `SHELLOPTS`
  rather than `BASH_ENV` because it carries option *names* and cannot name a
  file to source, so it opens no exec inlet; see `.claude/rules/executor.md`
  under `build_clean_env` for the full comparison. Being inherited rather than
  an argv flag, it also reaches a pipeline inside a nested `bash script.sh`,
  which `-o pipefail` does not — the two brains therefore agree on an identical
  command string, which is what ISSUE-307 wanted when it left this alone.

NativeBrain hardening (2026-07-18 audit, NB-1…NB-24 — see the audit doc in the
project notes for the full list):
- **File-tool confinement (NB-1).** The in-process file tools run outside bwrap,
  so `ToolEnv` enforces a symlink-resolved read/write path allowlist. The
  executor computes the same user-data roots bwrap would bind
  (`executor.native_fs_roots`) and passes them via `BrainRequest.fs_read_roots`/
  `fs_write_roots`, active only when `native_fs_confinement_active(config)`
  (`sandbox_enabled` + bwrap available) — matching the claude_code boundary.
  Other brains ignore the fields (bwrap already confines their tools).
  `fs_write_denied_roots` carries the RO carve-outs bwrap gets by re-binding a
  subdirectory `--ro-bind` after its parent's RW bind — containment alone can't
  express a hole inside a root. Up to two entries: the task's control
  directory `{temp_dir}/.control/{user_id}/task_<id>`, which holds Istota's
  own standing instructions — a `Write` would otherwise rewrite them under
  the running task — along with every other per-task file the daemon
  authors, on every shape; and `{user_temp_dir}/.developer`, which holds
  the credential helpers and where a writable copy is a credential-interception
  path, on a confined one. See the `fs_write_denied_roots` row above. Denied is checked before allowed, on the write
  path only, so the directory stays readable.
- **Model resolution (NB-3).** Built-in role aliases (`fast`/`general`/`smart`)
  resolve to `native.model` unless remapped via `[models.aliases]`; provider
  shortcuts (`opus`/`sonnet`/`haiku`) pass through untranslated. A `:effort`
  modifier still applies (`split_effort`). Per-model capability/window overrides
  via `[brain.native.model_overrides]` (NB-4).
- **Wire integrity (NB-2/15).** The `openai_compat` SSE parser surfaces
  mid-stream `{"error":…}` frames and EOF-without-`[DONE]`/`finish_reason` as
  `StreamError` (not a false clean `StreamDone`); `content_filter` is preserved
  and a `max_tokens`/`content_filter` final answer gets a visible marker. OpenAI's
  own o-series/gpt-5 use `max_completion_tokens` (NB-12).
- **`stop_reason` vocabulary (NB-18).** `BrainResult.stop_reason` is normalized
  to the documented set (`completed`/`cancelled`/`timeout`/`oom`/
  `transient_api_error`/`error`/`not_found`); the loop's raw `max_turns`/
  `loop_detected` map to `completed` with an informative message (no empty
  success). The agent loop's own `agent_end.stop_reason` is unchanged.
- **Robustness.** Adjacency-based loop-pair detection (NB-5), hook-exception
  containment in both execution modes (NB-8), off-loop cancel poll (NB-9), Bash
  process-group kill + chunked reads + `try/finally` reap (NB-6/7/11), overflow-
  recovery input bounding + retrying-provider + empty-summary fail (NB-10),
  window-relative compaction sizing (NB-14), per-task httpx client close (NB-17).

Native-brain coding enhancements (2026-07-20, `Specs/Done/native-brain-coding-enhancements.md`)
— native-path-only; the `claude_code`/`tmux_claude` brains take their prompt +
tools from the CLI and are byte-unchanged:
- **Fuzzy, multi-edit Edit tool.** `session/tools/edit_engine.py` (pure logic
  ported from pi's `edit-diff.ts`) backs `make_edit_tool`. Matching is
  exact-first then a bounded fuzzy fallback (Unicode NFKC, trailing-whitespace
  strip, smart-quotes/dashes/exotic-spaces → ASCII); it does **not** tolerate
  indentation/internal-whitespace reflow. An optional `edits[]` array applies
  several disjoint edits in one call (uniqueness + overlap enforced); the legacy
  `old_string`/`new_string`/`replace_all` shape is retained (`replace_all` stays
  exact-only — fuzzy+replace_all is disallowed). A `prepare_arguments` shim
  coerces `edits`-as-JSON-string and legacy→one-element-`edits`. Reads/writes
  **raw bytes** (not `read_text`) to preserve CRLF/BOM through the edit. When any
  edit is fuzzy the batch matches in normalized space but writes via
  `apply_replacements_preserving_unchanged_lines` so untouched lines keep their
  exact bytes. Failure messages are actionable (not-found / duplicate / overlap /
  empty / no-op).
- **System prompt composition.** `native._system_prompt_parts` builds the system
  prompt from up to three parts, in this order: the module-level
  `CODING_SYSTEM_PROMPT` (generic coding hygiene: read-before-edit, prefer Edit
  over Write, batch multi-site edits into one `edits[]` call, keep `old_string`
  minimal-but-unique, verify with tests) **only when `req.allowed_tools` is
  non-empty**; then Istota's `composed_system_prompt_path` when it is set; then
  the operator's `custom_system_prompt_path` when it is configured and present.
  The operator file stays last so its existing final-override position is
  preserved. The composed part is **not** gated on `allowed_tools` —
  `allowed_tools=[]` suppresses only `CODING_SYSTEM_PROMPT`, so a text-only
  invocation (sleep cycle) still keeps an empty prompt because it supplies no
  composed path, while a future tool-less executor task would still get Istota's
  standing instructions. `req.prompt` remains the initial user content; the
  system prompt lives on `AgentContext.system_prompt`, which compaction never
  touches, and that is what makes the standing instructions survive a cut.
- **Parallel tool execution.** `native.py` sets `tool_execution="parallel"`, so
  independent read-only tools (Read/Grep/Glob/WebFetch) run concurrently; the
  loop's existing guards still serialize any batch containing a mutation
  (Write/Edit/Bash are `execution_mode="sequential"`) or two calls to the same
  path (`_has_path_overlap`), and results append in call order.
- **Truncated-tool-call guard.** In `agent/loop.py`, a tool-call assistant
  message with `stop_reason == "max_tokens"` (the provider's map of
  `finish_reason="length"`) is **not executed** — `_truncated_tool_results`
  synthesizes an is-error result per pending call (keeping tool_call/result
  pairing valid) and the loop lets the model re-issue. Mirrors pi's guard.
- **Recovery hints in truncated output.** Read's tail note names the concrete
  `offset=` to continue from; Grep's head-limit note says how to see more; Bash
  spills full over-cap output to a task-scoped temp file (lazily, under
  `ToolEnv.deferred_dir` = `ISTOTA_DEFERRED_DIR`, fallback system temp) and names
  it in the result (`… [output truncated at N bytes; full output: PATH]`) instead
  of silently dropping the tail. `_SpillWriter` is best-effort (degrades to
  cap-only on I/O error) and skipped when `exclude_from_context` is set. Knob:
  `[brain.native] bash_spill_full_output` (default true).
- **Grep context + literal.** `-C`/`context` (integer) adds surrounding context
  lines in `content` mode (ripgrep `path:lineno:` match / `path-lineno-` context
  rendering, `--` between non-adjacent groups); `literal` (`re.escape`) matches a
  plain string. Pure-Python, no ripgrep dependency.

### Turn-budget awareness nudge (ISSUE-187 defect 3)

The `max_turns` cap is a hard safety net the model can't see, so a long
explorative task routinely gets capped mid-plan (the incident: a Lisbon-apartment
search capped at turn 80 on *"let me move to Otodom and OLX next"* — mid-plan,
non-empty narration delivered verbatim as the answer). Defects 1–2 (the masking:
`max_turns`/`loop_detected` collapsed to `completed`; the truncation marker gated
on an empty result) shipped in `6e4cd4e` and made the cap *visible when hit*. This
is defect 3 — making the model *pace itself* so it's hit less often, and so a
capped run produces a deliberate partial deliverable.

Native-only (the CLI brains take prompt + budget from `claude`). Two layered
mechanisms behind the hard cap, both gated on `[brain.native] turn_budget_nudge`
(default on) + a set `max_turns` + a **tool-bearing** task (empty `allowed_tools`,
e.g. the sleep cycle, is untouched):

- **(B) Threshold reminder — the primary mechanism.** As the run nears the cap
  the loop injects an environment notice so the budget surfaces only when
  actionable (short/common tasks never see it — zero anchoring, zero overhead).
  `_pick_turn_budget_nudge(turns, max_turns, early_percent, remaining_levels,
  fired)` counts assistant turns from the loop's `new_messages` accumulator
  (monotonic across compaction — matches `_max_turns_stop` exactly, so a
  threshold never re-fires after a context shrink), and returns the most urgent
  *unfired* crossed threshold. It fires **once** at `turn_budget_nudge_early_percent`
  of the cap (a ~halfway "keep it in mind" reminder), then **once each** as
  absolute steps-remaining crosses each value in `turn_budget_nudge_remaining`
  (default `[15, 5]`, escalating urgency). Each threshold fires at most once
  (`fired` set); when several cross on the same turn (a tiny cap) the most urgent
  wins and the overtaken ones are marked fired so they can't fire stale later.
  `_turn_budget_nudge_message(remaining, phase)` frames the notice as a
  **shrinking** resource ("~N steps remaining", anchoring-resistant), leading with
  absolute remaining, never an upfront allotment.
- **The ladder runs against whichever budget is scarcer (ISSUE-373).** Turns are
  not what ends a slow run. With the shipped numbers the ladder fires at turns
  50, 85 and 95 of a 100-turn cap; which of those a run reaches depends entirely
  on how long a turn takes, which is a property of the brain and something none
  of the three limits knew anything about. At 40s/turn a 60-minute clock lands
  near turn 90 and the last notice is unreachable; at 60s/turn it lands near
  turn 60 and only the halfway reminder ever fires. `_turns_left_by_clock`
  converts the time budget into a turn budget from the rolling **median** of the
  last `_LATENCY_WINDOW` (5) turn latencies — measured turn-end to turn-end, so
  tool execution counts, since what the estimate answers is how long the next
  *step* takes — and refuses to answer below `_LATENCY_SAMPLES_MIN` (3) samples,
  because one slow first turn is a cold connection rather than a pace. The
  median rather than the mean because turn latency is heavy-tailed: one `npm
  install` is minutes where its neighbours are seconds, and a mean lets that one
  sample set the budget. Four 10s turns and one 400s build average to 88s, which
  collapses a 100-turn cap to 30 and spends the whole ladder in a single turn —
  and since a crossed threshold is marked fired, nothing fires when the pace
  recovers and the genuine crossing arrives. The median only moves once most of
  the window is really slow, which is the condition the estimate describes.
  `_pick_turn_budget_nudge` then takes `budget = min(max_turns, turns +
  turns_left_by_clock)` and runs the whole ladder, the early reminder's
  percentage included, against that. One collapsed budget rather than two
  parallel ladders: each threshold still fires once whichever resource crossed
  it, the `fired` keys are unchanged, and the number the model reads is always
  the steps it actually has. The horizon is the **soft** deadline where one is
  set — estimating to the hard one would promise steps the loop has already
  decided not to take. No estimate (no deadline, too few samples) leaves the
  ladder byte-identical to what it was.
- **(A) Upfront pacing line — optional flavoring, NON-numeric.**
  `_extract_system_prompt` appends one non-numeric line to the coding-system-prompt
  block ("produce the best deliverable you can rather than leaving the work
  mid-stream") when the nudge is on + tools present + a cap is set. Stating the
  numeric cap up front would anchor it as a target and *compound* the sprawl on
  the exact tasks that hit the cap, so the line carries no number. Compaction-safe
  (the system prompt lives outside `ctx.messages`).

**Injection mechanism.** The nudge rides the `prepare_next_turn` closure (which
already receives `(ctx, new_messages)` every turn — no new loop API). The
threshold logic lives in the `_next_budget_nudge` helper so it doesn't tangle with
the compaction path; the closure combines them (nudge appends to the compacted
list, or to a copy of `ctx.messages` on a non-compaction turn). Injecting via the
returned `PrepareNextTurnResult(messages=…)` puts the notice in `ctx.messages`
only — **not** `new_messages` — so it's invisible to the execution trace and the
turn count, purely model-facing, exactly like the compaction-summary injection.

**Wire role.** The notice is wire-role *user* (the LLM layer has no
mid-conversation system role, and Anthropic rejects one). The `_TURN_BUDGET_FRAME`
carries the "environment metadata, not a new user instruction" semantics
explicitly ("Automatic system notice — not from the user: …") — the mirror of the
`_STEER_FRAME`'s "the user sent this" framing. Between thresholds a compaction may
fold a prior notice into the summary; the count-from-`new_messages` +
fire-each-threshold-once design keeps re-fire correct, and the gap is bounded
until the next threshold. The layered posture: optional non-numeric turn-1 line →
threshold nudge (~50% / ≤15 / ≤5, or their wall-clock equivalents) → the soft
deadline → hard `max_turns` cap → unmasked `stop_reason` + marker (defects 1–2).

### The soft deadline (ISSUE-373)

Three limits govern a native run — `max_turns`, the wall clock from
`scheduler.task_timeout_minutes`, and the nudge ladder — and all three are
constants chosen against a brain that answers in a couple of seconds. On a slow
fallback the clock arrives first, and *which* stop wins is what matters:
`max_turns` and `loop_detected` deliver the model's partial work under a marker,
while the wall-clock timeout throws all of it away. So a slow brain does not just
make a run longer, it moves the terminating stop from the one that salvages the
work to the one that discards it.

`_soft_deadline_stop` is a third stop condition, checked at a turn boundary like
the other two, firing at `[brain.native] soft_deadline_percent` (default 90) of
the task timeout. Its `soft_timeout` is in `_PARTIAL_ANSWER_STOP_REASONS`, so
`_build_result` delivers the last text-bearing turn under a marker and the task
succeeds — the same treatment `max_turns` gets, and for the same reason: the loop
chose to end a still-coherent run at a boundary of its own.

**It may only stop a turn that called tools, and it is gated on
`req.allowed_tools`.** The loop evaluates stop conditions after *every*
`turn_end`, the final text-only one included, with its own natural exit one check
away — so an unguarded condition labels a *finished* run `soft_timeout` and
appends a marker saying it ran out of time, which is the opposite of what
happened. There is also nothing to rescue there: this stop exists to salvage work
from a run that would have continued, and a run that stopped on its own has
already delivered. The `allowed_tools` gate is the same one `budget_nudge_on`
carries, and for the same reason — the native brain's text-only direct callers
(the sleep cycle, shared briefing blocks, health OCR, conversation triage) parse
structured output, and prose appended to their JSON breaks them.

**A soft stop with nothing to save is the hard clock, not a success.**
`_build_result`'s partial-answer arm makes the marker the whole text and returns
`success=True`, which for `max_turns` and `loop_detected` is right — both name a
pathology a retry would only repeat, and the comment there says so. `soft_timeout`
names *slowness*, which a retry on a fresh budget can legitimately clear, and
before ISSUE-373 that exact run reached the hard clock and got up to
`max_attempts` retries. So a `soft_timeout` whose run produced no text at all
returns the `timeout` shape instead: `success=False`, the same fixed string, no
`partial_text`. Nothing is lost — the condition being tested is that there was
no work to preserve.

**A cancel outranks all three stop conditions.** The loop checks `abort` at the
top of its inner iteration while stop conditions run at the bottom, and
`_stream_assistant_response` catches only an abort landing mid-*stream* — so an
abort set during **tool execution** reaches the stop conditions first, and any of
them firing there converts the cancel into its own reason. All three return
`success=True`, so the scheduler marks the task `completed`, posts the marker to
the room as the answer after `!stop` has already said it stopped, indexes the
turn into memory, and replays the run's deferred ops (`_drain_deferred_ops` gates
on success alone). Each condition therefore declines while `abort.is_set()`,
letting the loop's own check end the run as `aborted` on the next iteration. The
hazard predates the soft deadline for the other two, but the soft deadline widens
it from one specific turn to the last 10% of every task's budget — which is
exactly where a long run a user wants to stop lives, and tool execution is where
that run spends most of its wall clock.

`_PARTIAL_ANSWER_STOP_REASONS` is **derived** from `_PARTIAL_ANSWER_MARKERS`
rather than declared beside it: `_build_result` subscripts that table unguarded,
so a fourth stop added to a hand-maintained frozenset alone would raise
`KeyError` on the result-construction path and turn a salvageable run into an
exception.

The hard deadline is not replaced, and the remaining 10% is what it still covers:
a turn that hangs is cut mid-stream and there is no boundary to stop at. That is
also why `timeout` stays *outside* the partial-answer set — it names a torn turn,
not a chosen stop — while carrying its own `partial_text` (below) so the work
survives either way. `soft_deadline_percent` of 0 or ≥ 100 turns it off and the
hard clock is the only backstop again.

### Partial work on a stop that discards the answer (ISSUE-372)

`BrainResult.partial_text` is the model's last text-bearing turn, carried out of
a run that ended on `timeout` or `cancelled`. Those two stops returned a fixed
string — `"Task execution timed out after N minutes"`, `"Cancelled by user"` —
and dropped everything the model had written; the observed case was 29 minutes
and 48,516 output tokens of investigation delivered as three words.

The inversion is worth naming: the two stops that fire automatically preserved
the work, and the two a person is most likely to see destroyed it, on exactly the
runs long enough for the work to be worth something.

It is a separate field rather than an addition to `result_text` because the
executor drops `stop_reason` and the scheduler dispatches on `result_text` by
string match — `result == "Cancelled by user"` is an **exact-equality** match in
three places, so appending would send a cancelled task back through the retry
ladder. `result_text` therefore stays byte-identical on both paths and every
existing match is untouched; the persistence and delivery paths opt in by naming
the new field (see `.claude/rules/executor.md` and `.claude/rules/scheduler.md`).

Both brains populate it, or the vocabulary splits: `NativeBrain` from the same
`last_assistant_text` the backstop paths use, `ClaudeCodeBrain` from the last
`TextEvent` its streaming path already appends to the trace. That path also
stopped dropping `actions_taken` and `execution_trace` on its cancel and timeout
returns, which is the ISSUE-183 fix it had never received.

### The tool server (native-only)

`NativeBrain` no longer runs the model's tool calls in the daemon. It spawns
`python -m istota.tool_server` once per task attempt, through
`build_bwrap_cmd(..., profile=NATIVE)`, places it in the task cgroup from
`preexec_fn`, records its pid via `req.on_pid`, and returns six proxy tools
onto it (`session/tools/remote.py`). `WebFetch` stays in the daemon.

**Why, in one paragraph.** The five file tools ran on daemon worker threads
confined by `ToolEnv`'s symlink-resolved root allowlist — a second filesystem
policy written in Python, which every bwrap bind change had to be copied into,
whose check and open were separate syscalls so an ancestor could be swapped
between them, and under which `Grep`/`Glob` walked the daemon's own filesystem
view and filtered afterwards. `Bash` was contained, but by a *fresh namespace
per call* carrying the Claude CLI's runtime block — `~/.claude/.credentials.json`
included, read-only, which stops the token being rewritten and not read
(ISSUE-389). One namespace per attempt fixes both at once and is what
`ClaudeCodeBrain` has always had.

**What it changes that is visible.** A hostile path is *absent* rather than
refused. The ancestor-swap race stops mattering, because the host path is not in
the namespace to reach. Everything `Bash` forks is in the task cgroup, placed
before it could fork. `/tmp` and a backgrounded process now live for the attempt
instead of dying with each call — real, observable, and the same as the CLI
brains. Bash is also faster: one namespace setup per attempt rather than one per
call. And `worker_pid` names a real long-lived process for the first time.

**The transport** is an inherited `AF_UNIX`/`SOCK_STREAM` socketpair whose
number is passed in argv with `pass_fds`. Nothing nameable, so nothing to
replace and no peer to authenticate; `close_fds` keeps it out of every Bash
child. Framing and the eight messages are `tool_server_protocol.py`.

**Failure is not degradation.** A dead server, a `fatal`, or a malformed frame
errors every in-flight call (so nothing raises into the loop), sets the loop's
abort, and fails the attempt with a message naming the tool server — checked
*ahead* of `timed_out` and of the loop's own `aborted`, because the abort this
sets would otherwise be reported as "Cancelled by user", a success-shaped
terminal state the scheduler neither retries nor reports. Degrading each call to
an error result instead would let the model narrate around a broken sandbox and
answer confidently.

**No enable/disable flag**, deliberately: a flag choosing between in-process and
out-of-process tools would keep two tool-execution paths alive for ever, which
is the thing being removed. `build_bwrap_cmd` already returns the command
unchanged where bwrap is unavailable, so macOS, the standalone install and a
Docker stack without the two container settings run the server unsandboxed with
`ToolEnv` doing exactly what it did — which is also what lets the default suite
exercise this seam on every developer machine. Rollback is a revert.

**Text-only invocations spawn nothing.** Empty `allowed_tools` (the sleep cycle,
health OCR, briefing synthesis, conversation triage) returns before the spawn,
so none of those pays for a namespace.

### Native WebFetch tool (daemon-side, SSRF-hardened)

The native harness's only web-reaching tool is Bash, which runs sandboxed behind
`--unshare-net` + the tight CONNECT-proxy allowlist — so it can't fetch an
arbitrary page. `session/tools/web_fetch.py` (`make_web_fetch_tool`) adds a
`WebFetch` tool that runs **in the daemon process** (host netns), so it is not
gated by the CONNECT allowlist. It is `build_default_tools`-registered
(native-only) iff `env.web_fetch` is set and enabled; `NativeBrain._build_tools`
maps `[brain.native.web_fetch]` (`WebFetchConfig`) → `session.tools.WebFetchPolicy`
onto `ToolEnv.web_fetch` (`_web_fetch_policy()`), and the tool passes the
`allowed_tools` filter iff `executor.build_allowed_tools` listed `WebFetch` —
which it does **for every user**, unless the operator set
`[brain.native.web_fetch] admin_only` (ISSUE-449). The tool does reach the
network from the daemon's namespace outside the CONNECT allowlist, while that
same user's task under a CLI brain has `--unshare-net` plus the allowlist, and a
shared room an admin pinned to native puts a non-admin's turns on the native
path without their choosing it — so the asymmetry the old gate named is real.
What it did not do was bound it: identity decides *who* fetches, and the block's
other fields already decide *where* anyone may fetch, for everybody. So the
gate became an operator setting rather than the shipped rule. `admin_only` is
the one field in `WebFetchConfig` with no counterpart on `WebFetchPolicy`,
deliberately: it decides whether the tool is registered, so it can never reach
the object performing a fetch. `build_prompt`'s Tools section is scoped by the
same flag, or the prompt would name a tool that is not registered — and where
the flag withholds it and no browser service is up, that section now *says* so
rather than dropping the page-reading line, which is the silence that made the
old gate hard to live with. **The prompt's withheld predicate asks the routing
question and `build_allowed_tools` deliberately does not**, so the two disagree
on one shape and that is the correct answer rather than drift: `admin_only`
only ever removes the daemon-side tool, and a `claude_code` or `tmux_claude`
task keeps the CLI's own `WebFetch` whatever the list says — so telling that
user they have no fetch tool would assert an absence that is not there, which is
the same defect as naming a tool that is not registered, pointing the other way.
It is also what keeps `enabled = false` from being blamed on administrators.
Empty `allowed_tools` (text-only, e.g. sleep cycle) still yields no tools.

Because it runs in the daemon netns (bypassing the CONNECT boundary), its
hardening carries the whole load:
- **Credential-free**: own `httpx.AsyncClient` with `trust_env=False` (no ambient
  proxy/auth), no cookies (cleared per hop), fixed User-Agent; never sees secret
  env. GET-only, text-only.
- **SSRF-hardened** (`_ip_is_public`): every resolved destination IP is validated
  against a private/loopback/link-local/CGNAT/benchmarking/reserved/multicast
  blocklist (IPv4 + IPv6, with IPv4-mapped-IPv6 unwrapping) on the initial request
  **and every redirect hop**, failing closed if *any* resolved IP is non-public.
  The connection is **pinned to the validated IP** (custom Host header + TLS SNI
  extension) so there's no getaddrinfo→connect DNS-rebinding TOCTOU. Manual
  redirect handling (`follow_redirects=False`) re-validates each hop, and refuses
  an https→http downgrade when `allow_http` is off.
- **Capped**: streamed body cap (`max_bytes`), extracted-text cap
  (`max_content_chars`), redirect cap, total wall-clock `timeout_seconds`, honors
  the `abort` event. HTML→text via a stdlib `html.parser` extractor (no new dep);
  text/JSON/XML returned as-is; binary content returns a short `[non-text …]` note.
- **Untrusted framing**: content is wrapped in `[UNTRUSTED WEB CONTENT …]` with a
  `Fetched: <final-url> (HTTP <status>, <mime>)` provenance header. Because a core
  tool doesn't drive `companion_skills`, the executor folds `untrusted_input` into
  the **eager** skill set when a task routes to the native brain with WebFetch
  enabled and not withheld by `admin_only` (`_native_web_fetch_enabled`), so its
  inbound-handling guidance reaches the prompt exactly where the tool does. That
  predicate used to short-circuit on `is_admin` alone, which was right only
  while the tool was withheld on that same axis.
- **Residual**: model-driven exfiltration via a GET query string is not
  eliminated (a GET is a canonical exfil channel), but it's the same bounded
  residual the `browse` skill already carries. `require_url_provenance` (default
  off) tightens it for sensitive deployments; the corpus is threaded onto
  `ToolEnv.web_fetch_url_corpus` only when the knob is on. **The corpus is
  `_extract_urls(req.prompt)` and nothing else** — the user half of the prompt,
  which folds in prior conversation context, and never a prior tool result, so
  a WebSearch-then-read chain fails provenance. Written down because the
  neighbouring prose has said "task/prior tool output" since the knob shipped
  and the implementation has never done the second half; it is also what
  settled ISSUE-449 against making the knob a non-admin default, which would
  have turned the flow that issue was about into a refusal rather than a
  silence.

### Session logs (native-only, `session/session_log.py`)

Nothing persisted the conversation the native brain holds with the model.
`tasks.execution_trace` carries tool *labels* and no tool output at all,
`task_events` carries a capped payload and only when streaming is on, and the
control directory's `prompt.txt` is the input rather than the run — so a native
task that produced a wrong answer could not be reconstructed. `ClaudeCodeBrain`
never had that problem: the `claude` CLI writes its own session JSONL and
`build_bwrap_cmd` binds `projects`/`debug`/`todos` read-write so those
transcripts survive sandbox exit, so the asymmetry was accidental rather than
designed. Note that is the *opposite* mount direction from the posture below —
the CLI's transcripts are bound **into** the sandbox on purpose; istota's are
bound nowhere. The
format adapts pi's session store — the same prior art `agent/types.py` already
cites for `prepareNextTurn` — to istota's unit of work.

**One file per task *attempt*,** at
`{session_log_dir}/{user_id}/{timestamp}_task-{task_id}-{attempt}.jsonl`, `0600`
behind a `0700` directory, opened `O_EXCL` so a colliding name (the fallback
brain rerouting to native within one attempt) is renamed with a short
`session_id` suffix rather than overwritten. **`attempt` is 1-based**, matching
`task_usage.attempt_seq`; it is `task.attempt_count + 1`, since that column
counts *prior* attempts. A retry re-executes the prompt with a fresh message
list, so two attempts are two runs and merging them would produce a transcript
that never existed. Records are linear — no `id`/`parentId`, because a task
attempt has no user at the keyboard and resume is a non-goal. Adding the two
fields later is a `FORMAT_VERSION` bump the reader can absorb.

**`is_fallback` in the header is what pairs a transcript with its spend**
(ISSUE-378). A reroute after a fresh primary failure runs two brains and writes
two `task_usage` rows — `attempt_seq` 1 and 2, keyed apart on that table's own
`is_fallback` column — against a single log `attempt`, because a reroute
deliberately increments neither `attempt` nor `task.attempt_count` (the
breaker-cooldown reroute writes one row, still flagged). The file name cannot
say which run it holds: a run of either brain is filed as
`task-{id}-{attempt}`, and nothing in the name says which brain wrote it. On
the shipped `claude_code` → `native` shape that is one transcript against two
spend rows, and it read as the first of them; the `session_id` suffix is a
same-millisecond collision guard and is not what distinguishes the two, since
the name's timestamp already does. The
field is set on the *request* — `BrainRequest.is_fallback`, written by
`executor._run_fallback` and by nothing else — rather than derived at the
header, because the brain has no way to know it was the substitute. It is set
there and not passed in for the same reason `_ran_fallback` is set at the call
site: every path reaching `_run_fallback` is a fallback run, including the
breaker-cooldown one where the primary is never called and there is no primary
result to infer from.

Written on **every** run, `false` included, so the absence of the key means a
log from before the field existed rather than a primary run — and
`session_log_read.summarize` keeps that as a tri-state (`True` / `False` /
`None`, via `_as_bool_or_none`; a non-boolean claim reads as `None` too, since
the header is file content). `istota session show` prints `fallback=` only when
the file answers. **No `FORMAT_VERSION` bump**: an added optional key is
absorbable in both directions — an old reader ignores it, a new one handles its
absence — which is the bar that constant states.

What the field gives is an exact join within one attempt and an *ordinal* one
across a retried task: the log carries `attempt` where `task_usage` carries
`attempt_seq`, and the two are different counters, so a task that failed over on
several attempts is paired by ordering both sides rather than by a shared key.
Carrying `attempt_seq` in the header outright is the open half of ISSUE-378 and
was left undecided.

One JSON object per line, every record carrying `type` and `ts`:

| `type` | Where it comes from | What it says |
|---|---|---|
| `session` | line 1, `open(header)` | `v`, `session_id`, the identity six (`task_id`, `attempt`, `user_id`, `source_type`, `conversation_token`, `is_group_chat`), `is_fallback`, plus the brain/provider/model/effort/limits header |
| `context` | line 2, once | system prompt, its source, tool *names*, and `tools_schema_sha256` over the sorted schema JSON |
| `message` | `message_end` | the serialized `llm.types` message — user, assistant (with thinking, tool calls, usage, stop reason) or tool result |
| `compaction` | both compaction paths | `trigger` = `proactive` or `overflow`, `summary`, `tokens_before`, `cut_index`, `messages_dropped`, `image_pinned`, `details`, `recovery_index` |
| `steer` | `_get_steering_messages` | the drained `!steer` text |
| `nudge` | `_next_budget_nudge` | `phase`, `remaining`, `turns`, `max_turns` |
| `error` | the `except` clauses in `_execute_sync` | `kind`, `message`, `traceback` |
| `result` | last line, on any run that produced a `BrainResult` | `success`, `stop_reason`, uncapped `result_text`, `model_used`, `duration_ms`, `usage`, `turns`, `compactions`, `truncated_records` |
| `serialization_error` | any record that would not serialize | the record type and the error, so one bad object costs that record and not the session |

Both `compaction` triggers write the same field set, with the fields the other
path cannot fill set to `null`, so a reader never has to know which fired
before it knows which fields exist. `error` and `result` are both written where
both apply: `error` says what went wrong, `result` says what the task was told.
A `BaseException` out of the loop gets the `error` record and no `result`, which
is what a run that produced no result should look like.

**Wiring is one branch plus the record kinds below, all in `brain/native.py`** —
`agent/loop.py` and `agent/events.py` are not modified. `emit` gains
`elif event.type == "message_end": log.message(event.message)`, and that is the
whole message path: the loop emits `message_end` for every message it *appends*,
in the order it appended them, across user, assistant and tool-result roles
alike, including the `run_agent_loop_continue` pass after an overflow recovery
(measured with a probe, not read). **Do not also hook `turn_end`** — it
re-carries the same `AssistantMessage` and would double-write every assistant
turn. The others: `open(header)` once the model and effort resolve, so the
header can carry them; `context(...)` once the `AgentContext` is built;
`compaction(...)` from `prepare_next_turn` and from the overflow loop, the
latter *before* the no-summary check so a failed recovery records
`summary: null` and explains the failure; `steer`; `nudge`; `error`; and
`result(...)` once at the end of `_execute_sync` with `close()` in the `finally`.
`result` is written at one site rather than at each `return BrainResult(...)`
because three of those returns are inside a `@staticmethod` with no writer in
scope, and one site downstream of all of them also covers a fifth return added
later.

**What the loop appends is not everything the model sees.** Compaction
*replaces* `ctx.messages` wholesale and the loop emits nothing for a
replacement, so the compaction summary, a pinned image and the `_RECOVERY_NUDGE`
user message have no `message` record. The `compaction` record stands for the
first two — which is why it carries the summary text, `image_pinned` and the
drop count rather than a bare marker — and the recovery nudge is a fixed
constant that is recorded nowhere. A turn-budget nudge is the same shape: it
rides `prepare_next_turn`'s returned list into `ctx.messages` only, never into
`new_messages`, which is why it gets a record of its own. A `steer` record means
*drained*, not delivered: the loop drains at the end of a turn and injects at
the top of the next, so an abort in that window is the pre-existing lost-steer
case, now visible rather than silent.

**The header's redaction rule lives in `native.py`, not in the writer.**
`SessionLogWriter.open` copies the caller's mapping through minus `type`, `v`
and `ts`, so keeping `api_key` and `extra_headers` out of it and reducing
`base_url` to `base_url_host` (an operator can put a token in a URL path)
belongs to whoever builds the mapping. `prompt_caching` records the *provider's*
resolved answer rather than the config tri-state, because `make_provider` reads
`None` as "on for `api.anthropic.com`" and the config field would write `null`
for every run on the default deployment that cached. `system_prompt_source` is
derived from the same walk that builds the string, not from
`custom_system_prompt_path` being set: that file is appended to the built-in
block rather than replacing it, and a configured path that does not exist
contributes nothing. Values are `builtin`, `composed` and the operator file's
absolute path, joined with `+` in composition order — `builtin+composed` on an
ordinary task, `builtin+composed+/etc/istota/system-prompt.md` with an operator
file, `empty` for a direct text-only call. The composed part reports the stable
label rather than its path, since that path carries a task id and an attempt
and would make every record's source string unique.

**Two caps keep the artifact bounded.** An `ImageContent` is never written as
bytes — it serializes as `{media_type, display_name, bytes, sha256}`, where
`bytes` is the decoded length and the hash still identifies two records as the
same image. Text is capped per content block at `max_content_chars`, **head and
tail** rather than head alone, because a truncated build log's tail is where the
error is; the block keeps `truncated: true` and `chars_total`. Tool-call
arguments have their own `max_args_chars` and become a marker object over it,
since a truncated *fragment* of a JSON object is worse than an honest marker.
The content cap also reaches three strings outside the blocks it was written
for — the `context` record's system prompt, a `steer`'s text, and both halves of
an `error` — because each arrives from somewhere the writer does not control.
`result_text` is the one deliberate exemption, the same reasoning that put
`result` in `events._UNCAPPED_EVENT_KINDS`. On both caps, `0` means **no cap**,
not "off" as it does on `retention_days` and `max_total_gb`.

**The writer never raises and never nags.** A task must not fail because a log
could not be written, so every public method is wrapped; the first failure logs
one warning, disables the writer and closes the handle, and every later call is
a no-op — a full disk must not produce one warning per tool call for the rest of
the day. `SessionLogWriter(root=None)` is the disabled writer, which is how
`enabled = false` costs nothing and why there is no `if self._log is not None`
at the call sites. `make_session_log` returns it on **three** conditions, not
one: the feature is off, `task_id <= 0`, or `user_id` is empty. The last two are
the same case in practice — a direct brain call is not a task attempt and has
nothing to name a file after — so the heartbeat's synthetic `id=0` task and
every non-task caller (sleep cycle, health OCR, briefings, code review, context,
the REPL) get no transcript. Records are flushed and never `fsync`ed: a daemon that dies
loses what the OS had buffered, which beats an `fsync` per tool result on the
loop's hot path. pi makes the same trade.

**Where it lives, and what actually protects it.** The directory
(`resolve_session_log_dir(db_path, cfg.dir)`, with `dir = ""`) defaults to
`{db_path.parent}/logs` — local disk, beside `istota.db`, `modules/` and
`backups/`, never the FUSE mount and never `user_temp_dir` (which is bound
read-write into every sandbox as `ISTOTA_DEFERRED_DIR`, so a transcript there
would hand every task the full history of every previous task for that user, and
the ability to rewrite the record of what it did). That resolves to
`/srv/app/{namespace}/data/logs` on the Ansible shape and **`/data/db/logs`** on
Docker, because `render-config.sh` writes `db_path = "/data/db/istota.db"`; the
plausible-looking `/data/logs` is a *sibling* of `db_path.parent` and therefore
outside the tmpfs mask, which inverts the property while looking correct.

**The boundary is that nothing binds the directory into any sandbox; the
database mask is defence in depth behind it.** `build_bwrap_cmd` masks
`db_path.parent` and `module_db_root()` last, so where a sandbox is built at all
and `_mask_dir` does not refuse, the logs are behind that mask for free. Three
shipped shapes, three different answers, and only the first is unconditional:

- **Ansible** — masked. `db_path.parent` is `{istota_home}/data`, which contains
  no protected path, and bwrap runs unasked as an ordinary user.
- **Docker** — masked *only if the operator added the two container settings*.
  The path is right (`/data/db/logs` is under `/data/db`), but the shipped
  `docker/docker-compose.yml` grants neither `seccomp:unconfined` nor
  `systempaths=unconfined`, so the bwrap probe fails and `build_bwrap_cmd` hands
  every command back unwrapped — no sandbox, therefore no mask, and
  `native_fs_roots` is not applied either since `native_fs_confinement_active`
  is `effective_sandboxing`. `.claude/rules/testbed.md` and
  `docs/deployment/docker.md` state the same thing about the databases; the
  transcripts inherit it.
- **Standalone** — `_mask_dir` *refuses*. `setup_wizard` puts `db_path`, the
  workspace and the temp dir all under `~/.istota`, so `mask_shadowed_by` is
  non-empty and nothing is masked. The logs are unbound rather than masked,
  joining `modules/` in that condition rather than creating it.

`mask_shadowed_by` and `mask_protected_paths` were lifted out of `_mask_dir`'s
closure in `executor.py` precisely so `doctor`'s `runtime.session_log_dir` asks
the sandbox's own question instead of a copy of it; "is the directory under
`db_path.parent`" answers yes on the standalone shape and would report the
property holding while the directory sat outside every mask. **That covers the
path axis, and the availability one is asked beside it** (ISSUE-381).
`_session_log_mask_finding` used to gate on `security.sandbox_enabled`, so on a
shipped Docker stack — flag on, bwrap unavailable — the check reported `OK` and
no mask finding while nothing was masked at all. It now asks
`executor.effective_sandboxing` and **reports both counts** rather than the
first: the two conditions are independent, the standalone install fails both at
once, and an operator who reads one reason and fixes it would otherwise be told
nothing about the second. Availability leads the joined reason, because whether
a mask exists outranks where it would land.

Three things about that arm are easy to get wrong and each was got wrong first.
It **may not spawn** under `run_checks(probe=False)`, and `effective_sandboxing`
consults the bwrap capability probe — so it asks `effective_sandboxing_if_known`
there instead, which reads the process memo `_bwrap_available` fills and returns
`None` rather than probing. The memo is usually warm where it matters, since the
daemon probes at start-up in `_log_startup_status`, and reporting "not probed"
while `_bwrap_checked` holds the answer would be a statement about the world
that is wrong. An answer it genuinely cannot get is a **third state**, not a
pass: `_session_log_sandbox_availability` returns `True | False | None`, because
returning `True` on the unobtainable path reinstated ISSUE-381 in miniature — a
protection asserted by a function that had just failed to check it. And an
unestablished answer takes a **different finding prefix** (`_MASK_UNKNOWN`
rather than `_MASK_EXPOSED`): composed under the old fixed prefix it read "the
logs are unbound rather than masked — [it] was not probed", asserting the
exposure in one clause and disclaiming it in the next, on a deployment whose
mask was fine. This is the one check in the module that will not report `OK`
under `probe=False`, against a convention where `_binary_status` and
`check_tmux` return `OK` and `check_subscription_usage` returns `SKIP`; the
subject here is a boundary, and "fine" from a run that did not look is the
defect wearing a flag.

**And "nothing binds it" is a default rather than a guard.** No code in
`build_bwrap_cmd` knows about this directory; what keeps it out is that
`security.sandbox_ro_paths` defaults to `[]` and is bound verbatim when it is
not — the same setting whose old `/srv/app` default is how every database got
exposed once before. On the standalone shape there is a second route, because
`nextcloud_mount_path` and `db_path.parent` are the same directory there: a
`user_resources` row with `resource_path = "logs"` resolves to exactly the
transcript directory, which both `build_bwrap_cmd` and `native_fs_roots` bind as
a per-resource mount. `tests/test_sandbox.py::TestSessionLogContainment` pins the
default shapes — no `sandbox_ro_paths`, no user resources — so widen either and
nothing goes red.

**An operator-set `dir` is trusted, the way `security.sandbox_cache_dir` is.**
There is no containment rule bounding it against an ancestor, and that is a
decision rather than an omission: the root is the whole input, it comes from the
operator's config file, no model-supplied name is resolved against it, and there
is no ancestor to bound against that would not refuse both a reasonable
`/var/log/istota` and the relative value the resolver is required to honour as
written. What that costs is stated where an operator meets it
(`config.example.toml`, `SessionLogConfig.dir`, `sweep_session_logs`): the sweep
treats every first-level subdirectory of whatever `dir` resolves to as a user's
and unlinks `*.jsonl` at any depth beneath it. Values naming no directory of
their own (`/`, `.`, `..`, `a/..`) and null bytes are refused; a relative value
is used as given, so write an absolute path — the daemon, the sweep and an
`istota` command run from a shell need not share a working directory.

**Retention is two independent rules under an `or` gate**, at
`run_cleanup_checks` step 7b, and neither implies the other. `retention_days`
(default 14) bounds how long a transcript is retrievable, which is a privacy
question; it is deliberately longer than `task_retention_days = 7`, since the
log's value is that it outlives the task row. `max_total_gb` (default 2.0,
clamped to a 0.5 floor) bounds how much disk a burst of long agentic tasks can
take from the filesystem `istota.db` and every module DB are writing to, which
is an availability question — `sandbox_cache_sweeper` wrote the reasoning first:
a rule phrased in days either keeps everything or throws away something minutes
old, because the growth arrives in bursts rather than at a rate. The gate is
`or` between the two rules because setting one to `0` must not silently disable
the other.

**The whole gate is `enabled and (retention_days > 0 or max_total_gb > 0)`, and
the `enabled` conjunct is the half worth stating out loud.** Step 7b is the
sweep's only caller, so `enabled = false` stops new transcripts *and* stops
anything ever deleting the ones already written — while `check_session_log_dir`
returns `SKIP` on the same flag, so `doctor` goes quiet about the directory at
the same moment. An operator who switches the feature off for privacy reasons
keeps every transcript already on disk, indefinitely and unreported; the
directory has to be removed by hand. Setting both limits to `0` has the same
effect with the feature still on. Whether the sweep should run regardless of
`enabled` is a live question — the step's own comment already makes the
analogous argument for brain kind, that a deployment which switched away from
native still sweeps what native left behind.

The ceiling is **deployment-wide across every user**, because the thing being
protected is a filesystem and a filesystem has no per-user quota; under a
per-user ceiling the real limit is `users x ceiling`, a number that appears
nowhere in the config. Eviction is **largest-user-first, then oldest within that
user**. Plain global oldest-first is the obvious rule and it inverts the
outcome: the globally oldest files belong to the *quietest* users precisely
because they are quiet, so one noisy user would clear everyone else's history to
make room for their own fresh output. A file stamped inside `LIVE_WINDOW_SECONDS`
(one hour) is never evicted, and a tree that cannot get under the ceiling
without touching those reports `still_over` and stops rather than looping.
Measurement is `st_blocks * 512`, du-style; directory inodes are deliberately
not counted, since a per-user directory is overhead no eviction can reclaim.
The sweep writes its outcome to the reserved `_session_log_sweep` KV namespace,
which is what lets `doctor` warn that the ceiling — not `retention_days` — is
what is actually binding, and that the configured window is therefore not the
window in force.

**Reading it back** goes through `session/session_log_read.py`, which is where
the parsing rules live so its two consumers cannot drift into disagreeing about
what a transcript says. A file whose line 1 is not a `session` record is
unreadable and is reported that way rather than rendered; a malformed line in
the middle is skipped and *counted*; a trailing line with no newline is a live
write rather than damage. Operators get `istota session list|show|tail|stats`.
A running task gets `istota-skill tasks transcript <id>` — host-side through the
skill proxy, never a bind, scoped to `ISTOTA_USER_ID` with no admin override,
with its own attempt excluded (an earlier attempt of the same task is the useful
case), thinking off unless asked for, every tool result and error framed in
`[UNTRUSTED TRANSCRIPT CONTENT …]`, and `--grep` matched **literally** via
`re.escape` rather than as a regex, because the pattern is model-written and the
scan runs in the daemon's namespace with a task waiting on it. A missing,
swept or non-native transcript is `{"available": false, "reason": …}` at exit 0,
not an error. See `.claude/rules/skills.md` for the verb.

Config lives at `[brain.native.session_log]` (`SessionLogConfig` on
`NativeBrainConfig`): `enabled`, `dir`, `retention_days`, `max_total_gb`,
`max_content_chars`, `max_args_chars`, `include_thinking`. **The Docker shape
gets none of these as environment knobs, deliberately** — a Docker deployment
runs the shipped defaults. Adding one means adding it to *both*
`docker/istota/render-config.sh` and `docker/docker-compose.yml`; a variable the
generator reads and compose never passes is silently ignored, which is the
`ISTOTA_EMAIL_AUTHSERV_ID` defect class. Ansible templates no block either.
Native-only by decision: the CLI brains already get a transcript from `claude`,
and unifying the two formats is separate work with an argument on both sides.

`Config.brain: BrainConfig` follows the dataclass-with-defaults convention.
`source_type_overrides` maps a task's `source_type` to a brain kind, overriding
`kind` for matching tasks — the gradual-rollout knob (cron/heartbeat on native,
interactive on claude_code). `brain.resolve_brain_kind(source_type, brain_config)`
returns the routed `BrainConfig` (same object when no override applies; unknown
target kinds are logged and ignored so a routing typo never wedges a task). The
executor calls it per task: `make_brain(resolve_brain_kind(task.source_type, config.brain))`.

## TmuxClaudeBrain (`brain/tmux_claude.py`)

Drives the **interactive** `claude` TUI in a detached tmux session instead of the
headless `claude -p` subprocess. Same `claude` binary, same `CLAUDE_CODE_OAUTH_TOKEN`
auth — so it keeps traffic on subscription usage limits rather than the metered
Agent-SDK credit `claude -p` draws from after 2026-06-15. Model resolution is
delegated wholesale to a composed `ClaudeCodeBrain` (same Anthropic namespace);
only `execute` is genuinely new. Selected with `brain.kind = "tmux_claude"` (a
**full instance switch** — every source type, interactive chat included, routes
through it; `claude_code` stays the constructible *fallback* kind, not a parallel
route).

**Mechanism per attempt.** Per-session workdir under `ISTOTA_DEFERRED_DIR`
(`.tmux-<session>/`) holds a per-session `CLAUDE_CONFIG_DIR` (`config/`), the Stop
sentinel (`stop.json`), the early sentinel (`started.json`), and the prompt file.
`settings.json` in the config dir declares a `Stop` hook (`cat > stop.json` — its
stdin payload carries `transcript_path` + `last_assistant_message`) plus
`UserPromptSubmit`/`SessionStart` hooks (`cat > started.json` — early
transcript-path signal for streaming). `_seed_onboarding` also pre-writes a
per-session `.claude.json` (`theme`, `hasCompletedOnboarding`,
`bypassPermissionsModeAccepted`, per-project trust keys) so the fresh config dir
doesn't re-trigger first-run onboarding. A detached `tmux new-session -e K=V`
passes `req.env` + `CLAUDE_CONFIG_DIR` into the pane (the detached-session env
gotcha: the OAuth token must reach the pane); under uid 0 the brain also sets
`IS_SANDBOX=1` so the TUI accepts `--dangerously-skip-permissions` as root (the
container-as-sandbox case — left unset on a non-root deploy where the flag is
allowed without it). `claude` is launched sandbox-wrapped (`req.sandbox_wrap` —
bwrap wraps the *claude* process, never tmux, so no nesting). `_wait_ready`
scripts past the first-run theme picker, the workspace-trust dialog, and the
Bypass-Permissions warning as a version-tolerant safety net; the prompt is
buffer-pasted, submitted, and the submit is confirmed (`_turn_started`) before
`_wait_for_completion` polls; the Stop hook fires → sentinel → parse the
transcript JSONL → `BrainResult`. Result text prefers the Stop payload's
`last_assistant_message`; the trace is reconstructed from the transcript
(`parse_transcript`, settled via `_transcript_has_final_turn`). When the
payload omits the message, `parse_transcript` synthesizes the answer from the
last `end_turn` turn, falling back to the last text-bearing turn **that issued
no tool calls** — a turn that went on to call a tool was narrating, and
promoting that is ISSUE-211. The host needs
`tmux` on `PATH` (a missing binary → `not_found` → headless fallback); the Docker
image installs it.

**Production hardening** (`Specs/Done/claude-tmux-production-readiness.md`):

- **Per-session hook isolation (§2).** Each session's hook lives in its own
  `CLAUDE_CONFIG_DIR`, not a shared project `.claude/` — so two concurrent
  same-user tasks can't clobber a shared `settings.json` and cross-fire each
  other's Stop sentinel. The whole workdir (config dir included) is `rmtree`d in
  `finally`; a one-shot best-effort cleanup removes any legacy `base_dir/.claude`
  a prior prototype left.
- **Fail-fast completion (§3).** `_wait_for_completion` is multi-signal:
  sentinel→`done`, cancel→`cancelled`, an `error_markers` pane match→`error`
  (fail fast, classified for transient retry), dead pane→`error`, else continue
  to the hard timeout with a one-shot `tmux_stall` warning at the halfway mark.
- **Transient-API retry parity (§3).** An error-marker pane is run through
  `is_transient_api_error` (reused from `claude_code`); a transient match retries
  a **fresh session** up to `API_RETRY_MAX_ATTEMPTS` (3), `API_RETRY_DELAY_SECONDS`
  (5) apart, **not** counting against the task's `attempt_count` — identical
  contract to `ClaudeCodeBrain`.
- **Provider-error classification parity (ISSUE-212).** `_build_result` runs the
  transcript body through the shared `_success_frame_stop_reason`, not just
  `is_usage_limit_banner` — on the subscription brain a capacity banner
  delivered as the final assistant message is exactly what the fallback exists
  to absorb, and left alone it was handed to the user verbatim as the answer.
  `_wait_for_completion`'s error branch likewise returns
  `stop_reason="transient_api_error"` when its pane match is retryable, instead
  of a bare `error` that matched no fallback trigger and dead-ended once the
  in-brain session retries were spent.
- **`stop_reason="fallback"` + circuit breaker (§4).** A launch-level failure
  (REPL never ready, markers never matched, missing tmux→`not_found`) returns
  `fallback`/`not_found`; the executor reruns that *same attempt* once through a
  `claude_code` brain (no new DB row, no attempt increment) so the instance keeps
  completing (at metered cost) instead of failing en masse. A process-global
  `_CircuitBreaker` opens after `fallback_trip_threshold` consecutive launch
  failures: `execute` short-circuits straight to `fallback` for
  `fallback_cooldown_seconds` without trying tmux, logs `circuit_open`, and arms
  one operator alert (the executor fires it via `consume_circuit_open_alert()` →
  `notifications.send_notification(purpose="alert")`, since the brain has no
  `Config`). Any tmux success resets it; per-process state, reset on daemon
  restart (also when a fixed CLI version lands). This tmux launch alert is
  **preserved** by the generalized fallback path (see "Brain fallback" above +
  `.claude/rules/executor.md`): `fallback`/`not_found` are in the general trigger
  set (so the executor reruns through the effective fallback = `claude_code`), but
  `fallback` is *not* in the availability-breaker cooldown set, so tmux keeps being
  probed per-task and its own `_CircuitBreaker` (+ this alert) still governs the
  eventual skip. A tmux `usage_limit` (see the classification bullet above) routes
  through the *configured* fallback instead and never feeds this launch breaker.
- **Live streaming recovery (§10).** On stream-eligible tasks
  (`req.streaming and req.on_progress`) a background `_TranscriptTailer` tails the
  transcript JSONL *during* the turn and forwards each new `tool_use`/`text`/
  `thinking` block to `on_progress` as it lands (dedup by tool id + block index),
  instead of only whole-turn at Stop. The Stop-time parse stays **authoritative**
  for the persisted result/trace — the tailer is progress-only, so a missed or
  double-emitted block can't corrupt the result (`_build_result(forward_progress=
  tailer is None)` avoids double emission). Tailer exceptions are caught, never
  propagated. Token-level animation (Tier 2) stays a documented stretch, gated on
  a partial-flush probe. The brain can't distinguish push (Talk) from stream
  (web/repl) surfaces — `req` carries no surface — so the tailer runs whenever
  `streaming`; push consumers coalesce the incremental events identically.
- **Observability (§7).** One structured INFO line per attempt on logger
  `istota.brain.tmux_claude`: `tmux_brain session=… outcome=… ready_ms=… wait_ms=…
  dialogs=… tools=… retries=…`. Ready/error/stall events log at WARNING/ERROR with
  a (length-capped) pane snapshot.

**Interactive-TUI launch hardening** (surfaced during the live docker rollout):

- **First-run onboarding.** The per-session `CLAUDE_CONFIG_DIR` is empty each
  task, so the interactive TUI would re-run onboarding (theme picker → trust →
  bypass) every time. `_seed_onboarding` writes a per-session `.claude.json` with
  the onboarding-skip keys so the gauntlet is skipped. `_wait_ready` still scripts
  past the theme picker (`theme_markers`, a dark option pre-selected → bare
  `Enter`) as a safety net if a CLI version renames a seeded key.
- **Root containers.** When the process runs as uid 0 (`_is_root`), the brain
  sets `IS_SANDBOX=1` in the pane env so `claude` allows
  `--dangerously-skip-permissions` (it refuses it as root otherwise). Accurate in
  a container where the container itself is the isolation boundary and bwrap is
  off. Non-root deploys leave it unset. The Docker image installs `tmux` (without
  it every task would `not_found` → fall back to headless).
- **Race-proof prompt submission.** A large prompt arrives as a bracketed paste
  the TUI collapses to a `[Pasted text]` placeholder; an `Enter` sent before the
  paste is ingested gets absorbed, leaving the prompt unsent (the turn then hangs
  to the hard timeout). `_inject_prompt` pastes, settles, sends `Enter`, then
  confirms a turn actually started (`_turn_started` — the `UserPromptSubmit` hook
  fired, or the transcript file appeared) and only resends `Enter` if it didn't,
  up to `_SUBMIT_MAX_ATTEMPTS` — never a blind resend that could append a stray
  empty `Enter`. Every tmux path (interactive tasks + background sleep-cycle / OCR
  / explainer calls) goes through this.

**`[brain.tmux]` config** (`TmuxBrainConfig`, all defaulted to the prototype's
hardcoded values, so an empty/absent block is behavioral parity):
`fallback_trip_threshold` (5), `fallback_cooldown_seconds` (300),
`ready_timeout_seconds` (30), `tmux_command_timeout` (10), `cli_version_pin`
("2.1.168" — readiness/dialog markers are pinned to a CLI version; a reword is a
config hotfix via the marker lists), `ready_markers`, `trust_markers`,
`theme_markers`, `bypass_warning_marker`, `bypass_accept_marker`, `error_markers`.

**Known gaps / live-only gates** (the spec's Stage 1/6 prod-host probes — they
can't run off-Linux/off-bwrap):
- `CLAUDE_CONFIG_DIR` hook discovery *under bwrap* is the §2 primary mechanism
  (assumed working — cwd-independent). The documented fallback if it doesn't is a
  per-session bwrap `--chdir` (a localized executor change behind the kind).
- Interactive-TUI flag support: `build_claude_cli_flags(req, unsupported=…)` drops
  any flag the TUI rejects and warns once. `_TMUX_UNSUPPORTED_FLAGS` is empty by
  default (the prototype passed `--effort`/`--system-prompt-file`); populate if a
  CLI version starts rejecting one. `--append-system-prompt-file` must **not** go
  in there as a convenience: it carries Istota's composed standing instructions,
  so dropping it on this backend alone would run tmux tasks with no persona, no
  rules and no tool descriptions while the headless path kept them. A measured
  parser rejection in the interactive TUI is a release blocker.
- Early-path hook reliability + the partial-flush streaming ceiling, and live
  network isolation (`--unshare-net` + CONNECT bridge) — validated on the prod
  host, not in unit tests.

## Adding a new brain
1. Create `brain/<name>.py` with a class implementing `Brain.execute()`.
2. Add the kind string to `make_brain()` in `brain/__init__.py`.
3. Extend `BrainConfig` (or add a nested config dataclass) for new knobs.
4. Update `_build_network_allowlist()` in `executor.py` if the brain calls
   a new external host (e.g. `openrouter.ai:443`).
5. Tests: instantiate the brain, mock its transport (HTTP / subprocess),
   verify it produces correct `BrainResult` shapes for the standard cases
   (success, transient retry, cancel, timeout, oom, malformed output).

## Task Event Streaming

One persistent, typed event stream per task feeds every output surface. The executor adapts the brain's (widened) `StreamEvent` union into `TaskEvent`s via an `EventWriter` (`events.py`), which persists them to the `task_events` table (WAL, shared scheduler ⇄ web) and notifies in-process subscribers. Event kinds: `task_started`, `tool_start`, `tool_end` (NativeBrain only — carries loop-measured `duration_ms`), `tool_progress` (NativeBrain, SSE only), `progress_text`, `text_delta` (stream surfaces only — incremental answer text coalesced by the executor at ~250ms/120char/boundary; pruned on the terminal path once the canonical `result` lands, so steady state retains zero. **Narration gate (a substance classifier):** a text run streams nothing until it crosses `scheduler.stream_text_gate_chars` (default 280) without an intervening tool call. At a tool boundary the two cases split (`executor_stream.TaskStreamAdapter.settle_at_tool_boundary`): a short lead-in ("Let me check…") that stayed under the gate is _dropped_ (it never streamed, so it can't flash in the answer area); a SUBSTANTIAL block that crossed the gate is _kept_ — its unflushed tail is flushed so the full block reaches the stream surface, where the web client renders it as its own prose block (analysis the model wrote, then acted on, is content — not throwaway narration). The gate is thus not an answer-vs-narration split: the final answer (after the last tool) always streams, and a short _final_ answer that never crosses the gate still arrives whole via `result`. The earlier 250ms-timer flush _raced_ the tool boundary and leaked narration permanently; the gate has no time-flush while held. Tune against the `stream_gate:` logs the executor emits per flush/discard. `0` disables the gate), `context_management`, `brain_fallback` (emitted by `execute_task` at the moment it reroutes to the fallback brain, *before* that brain runs — a fallback used to be silent on every stream surface, so a task sat on its `task_started` ack verb for as long as the primary's failure plus the whole fallback run took, and a dropped non-portable pin meant the visible answer came from a different model than the room was configured for with nothing saying so until `done` (ISSUE-278). Payload carries `primary` / `reason` / `fallback` / `model` / `dropped_pin` plus a rendered `text`; the sentence is composed once in `executor.fallback_notice_text` so the web transcript and the REPL cannot word it differently. `model` is what the fallback was *asked* for and is empty exactly when `dropped_pin` is set — the model that actually ran is not known until the run returns, which is what `done` carries. The reroute is treated as a stream boundary like a tool call: `flush_thinking` + `settle_at_tool_boundary` run before the emit, or the failed brain's unflushed tail opens the fallback's answer. **Live-only:** the row survives pruning and an SSE resume, but history rebuilds a finished turn from `execution_trace`, so a reload shows no notice — the durable record of a model substitution stays the italic `_append_model_note` line on a dropped pin), `confirmation`, `result`, `error`, `cancelled`, `done`. Consumers: `TalkEventSubscriber` (edits the ack message in place), `LogChannelSubscriber` (accumulating edit), `PushNotificationSubscriber` (ntfy on long tasks) are in-process subscribers; the web SSE endpoint (`/istota/api/chat/tasks/{id}/stream`), the snapshot endpoint (`…/events`), and the admin endpoint (`/api/admin/tasks/{id}/events`) poll the table directly — the table is the bus, no IPC. **Retry continuity:** the event log is kept across retry-eligible failures (it is _not_ wiped). `set_task_pending_retry` leaves the rows in place, the retry branch emits a `progress_text` "⏳ Attempt failed — retrying in N min…" notice, and the next attempt's `EventWriter` resumes `seq` from `db.get_max_task_event_seq` so it stays monotonic (no UNIQUE(task_id, seq) collision) and a watching web client's resume cursor stays valid — it sees the notice and the next attempt's events instead of a silent spinner. The live view therefore accumulates across attempts (attempt 1's tools, the notice, attempt 2's tools); history reconstruction is unaffected (it reads `execution_trace`, the final attempt's). **Terminal backstop:** the SSE + snapshot endpoints (`web_app._synthetic_terminal_events`) synthesize a terminal frame from the task row — numbered above the client's cursor — whenever a task is terminal in the DB but has no `done` deliverable to that client (a crash that skipped `finish()`, or any future log-reset path). A terminal task always yields a terminal frame. `seq` is monotonic per task, assigned by the writer; events are hand-deleted only in `cleanup_old_tasks` (the `ON DELETE CASCADE` clause is decorative — `PRAGMA foreign_keys` is unset). The brain owns dispatching the executor callback off any event loop (NativeBrain's `run_in_executor` hop), keeping the synchronous subscribers' `asyncio.run` calls safe (ISSUE-111 generalized). Config under `[scheduler]`: `progress_show_tool_use`, `progress_show_text`, `event_log_enabled`, `stream_text_gate_chars`, `push_notification_threshold_seconds`, `push_notification_sources`.

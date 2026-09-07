# Executor

The executor (`executor.py`) is responsible for assembling prompts, building the per-task environment, and orchestrating a pluggable Brain implementation. The Brain owns model invocation (subprocess or HTTP), stream parsing, and transient-API retry; the executor stays focused on per-task orchestration. See [brain](brain.md) for the protocol and the bundled `ClaudeCodeBrain`.

## Prompt assembly

`build_prompt()` returns a `ComposedPrompt` — a frozen dataclass with two strings, `system` and `user`. The split is by authority rather than by size: the question is whether a layer has to remain verbatim for the life of the task. Standing instructions go to `system` and reach the model outside the message history; task material goes to `user`, which is the model's first turn and the only half a native compaction may summarize.

**System half** — standing instructions:

1. **Header**: role definition, user_id, current datetime, task_id, conversation_token, source, output target, the per-user email address, a line stating the database is reachable only through skill CLIs — the path itself is deliberately not in the prompt — and the task's privileges
2. **Emissaries**: constitutional principles from `config/emissaries.md` (skipped for briefings)
3. **Persona**: user workspace `PERSONA.md` overrides `config/persona.md` (skipped for briefings)
4. **Workspace layout**: one static line describing the workspace, plus any CalDAV-discovered calendars. The Resources sunset replaced the enumerated Folders / TODO Files / Notes / Reminders sections with that single line
5. **Tools**: available tools documentation (file access, browser, CalDAV, email). No `sqlite3` bullet — the databases are masked out of the sandbox and reached only through skill CLIs
6. **Rules**: resource restrictions, confirmation flow, subtask creation, output format
7. **Guidelines**: channel-specific formatting from `config/guidelines/{source_type}.md`
8. **Skills changelog**: "what's new" if skills updated since last interaction
9. **Skills documentation**: concatenated skill `.md` files, selectively loaded

**User half** — task material:

1. **User memory**: `USER.md` content (skipped for briefings)
2. **Knowledge graph facts**: relevance-filtered entity-relationship triples from `knowledge_facts` table, capped by `max_knowledge_facts` (skipped for briefings)
3. **Channel memory**: `CHANNEL.md` content (when `conversation_token` is set)
4. **Dated memories**: last N days of extracted memories (via `auto_load_dated_days`)
5. **Recalled memories**: BM25 search results (when `auto_recall` is enabled)
6. **Learned playbooks**: `_recall_playbooks()` BM25/vector hits over `source_type="playbook"` (when `playbooks.enabled`; skipped for automated / `skip_memory` tasks)
7. **Conversation context**: previous messages (selected by the context module)
8. **Confirmation context**: previous bot output for confirmed actions — interpolated after the conversation context, immediately before the request
9. **Request**: the actual prompt text + file attachments

Before the split all of this travelled as one string in the model's first user turn. The native brain's first compaction therefore replaced Istota's identity, rules, tool surface and skill bodies with a model-written summary, and the task carried on for hours without them (ISSUE-375). Two consequences of the classification are worth knowing. The workspace layout and the four time lines are in the system half despite reading as task facts, because rules 1, 7, 8 and 9 name them and those rules survive compaction — an instruction pointing at deleted material is the same bug in a smaller frame. And `## Response format` now permanently precedes `## User's request`, where it used to follow it; the guidelines are instructions and travel with the instructions.

A dry run prints both halves under fixed `===== SYSTEM =====` and `===== USER =====` labels and returns before writing anything. A real run writes the user half to `prompt.txt` and the system half to `system_prompt.txt`, both in the task's control directory — `{temp_dir}/.control/{user_id}/task_{id}/`, created by the executor at `0700` and writable by no task.

## Brain invocation

Once the prompt and env are built, the executor composes a `BrainRequest` and calls `make_brain(...).execute(req)` on the brain the task resolves to — `resolve_brain_kind(task.source_type, config.brain, override=task.brain)`, which is the room's standing pick, then the operator's `[brain.source_type_overrides]` entry, then `[brain] kind`. The request bundles the prompt — the **user half only** — allowed tools, working directory (`config.temp_dir`), env, timeout (`task_timeout_minutes * 60`), model/effort overrides, `composed_system_prompt_path` (the system file just written), an optional custom system prompt path (when `custom_system_prompt` is enabled), and the callbacks the brain needs: `on_progress`, `cancel_check`, `on_pid`, and `sandbox_wrap` (a closure that wraps the brain's raw cmd in bubblewrap when the sandbox is enabled — the brain itself stays sandbox-agnostic).

The two system paths are separate channels with different owners. `composed_system_prompt_path` is Istota's own, generated per task, and **required**: a brain must not skip it because the file went missing, because that would run the task with the user half alone. `custom_system_prompt_path` is the operator's, and stays optional — a configured file that no longer exists is omitted rather than failing the attempt. Each backend keeps its own override position: `NativeBrain` composes built-in coding block, then Istota's file, then the operator's, into `AgentContext.system_prompt`, which compaction never touches; the Claude Code backends pass the operator file with `--system-prompt-file` (which replaces the CLI's default harness prompt) and Istota's file with `--append-system-prompt-file` (which does not). A direct text-only brain caller — the sleep cycle, shared-block synthesis, health OCR, the code reviewer — passes no composed path at all and is unchanged.

The control directory is guarded twice, because two tool families reach it by different routes. `build_bwrap_cmd` binds it read-only after every other bind, which covers every sandboxed child including the native `Bash` tool. And `native_fs_roots` returns it in both `read_only` and `write_denied`: the native file tools run against `ToolEnv` and enter no mount namespace, so without the deny root a native task could rewrite the instructions it is running under with one `Write` call, while without the read root it could not open its own prepared image attachment, since the directory is a sibling of the task temp directory and therefore inside no write root. The deny root is seeded whether or not the sandbox is active, since the unsandboxed shapes are the ones with no bind behind it. Only the task's own directory is bound, so no task reaches another task's files.

The brain returns a `BrainResult` carrying `(success, result_text, actions_taken, execution_trace, stop_reason)`. The executor then runs result composition (see below) and downstream cleanup (malformed-output detection, deferred file processing).

`ClaudeCodeBrain`, the default brain, builds and invokes:

```
claude -p - --dangerously-skip-permissions --disallowedTools Agent Workflow \
  --output-format stream-json --verbose --include-partial-messages
```

(The three streaming flags are appended only when the executor asked for streaming.)

with optional `--model`, `--effort`, `--system-prompt-file` and `--append-system-prompt-file` flags. `--system-prompt-file` names `config/system-prompt.md`, which the CLI opens itself, from inside the sandbox — so `build_bwrap_cmd` binds that one file read-only. It is the only config-directory file the sandbox sees; everything else in there (emissaries, persona, guidelines, skill bodies) reaches the model as prompt text the daemon read, and `config.toml` stays out. `--append-system-prompt-file` names Istota's composed system half, which the CLI also opens from inside the sandbox and which is bound read-only the same way. The append form rather than the replace form, because replacing would discard Claude Code's default harness prompt on the default deployment, where no operator file is configured. Tool-bearing tasks run with `--dangerously-skip-permissions` and no `--allowedTools` allowlist — the security boundary is the bwrap sandbox + network proxy + clean env, not an interactive permission prompt. See [brain](brain.md) for the full implementation.

## Environment variables

The executor builds a minimal, clean environment for the subprocess. `build_clean_env()` starts with PATH, HOME, USER, LOGNAME, PYTHONUNBUFFERED, and configured passthrough vars (`LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`), plus `CLAUDE_CODE_OAUTH_TOKEN`, and `ISTOTA_ADMINS_FILE` / `ISTOTA_CONFIG_PATH` where those are set — the last so a subprocess resolves the same config the daemon loaded rather than searching afresh.

Three shell-startup names — `BASH_ENV`, `SHELLOPTS`, `BASHOPTS` — are filtered out of the inherited environment by this builder *and* by `build_stripped_env`, and then `build_clean_env` alone applies `SHELLOPTS=pipefail` **last**, from `shell_exec.pipefail_env()`. Bash imports that at startup, which is the only lever that reaches the shells istota does not spawn: a `claude_code` or `tmux_claude` task runs its commands through the Claude Code CLI's own Bash tool, and that shell started with `pipefail` off and reported a pipeline's last stage (ISSUE-321). Filtering first is what stops an inherited value being trusted; `SHELLOPTS` rather than `BASH_ENV` because it names options and cannot name a file to source. The two `build_stripped_env` paths — cron `command` jobs and heartbeat shell commands — get `pipefail` from `shell_exec.shell_argv`'s own `bash -o pipefail -c` instead, flag depth only, because those commands are operator-authored.

The main env vars the executor injects directly are the core identity ones (`ISTOTA_TASK_ID`, `ISTOTA_USER_ID`, `ISTOTA_CONVERSATION_TOKEN`, `ISTOTA_DEFERRED_DIR`, `ISTOTA_SKILL_PROXY_SOCK`, `ISTOTA_BOT_DIR_NAME`, `ISTOTA_EXPERIMENTAL_FEATURES`, plus `ISTOTA_SANDBOXED` when bwrap is in effect), a few path/runtime vars (`NEXTCLOUD_MOUNT_PATH`, `BROWSER_API_URL`, `BROWSER_VNC_URL`), the package-cache placement (`UV_CACHE_DIR`, `npm_config_cache`, `XDG_CACHE_HOME`, and `HF_HOME` pinned *back* to `$HOME/.cache/huggingface` so moving XDG does not orphan the pre-warmed model bind) when a cache root resolves under effective sandboxing, and, when devbox is enabled, the `ISTOTA_DEVBOX_*` set.

Database paths (`ISTOTA_DB_PATH`, `HEALTH_DB_PATH`, `LOCATION_DB_PATH`) go into the skill proxy's environment instead and never reach the subprocess. `ISTOTA_TASK_ATTEMPT` rides in that same bucket without being a path: it is the floor `tasks transcript` uses to withhold the transcript the calling run is still writing, so a copy the model held would be a floor the model could raise.

Everything else — Nextcloud / CalDAV / IMAP / SMTP credentials, service tokens, per-user secrets — is **manifest-derived**. Each skill's `skill.md` frontmatter declares its env vars in the `env:` block; `build_skill_env()` walks the loaded skill index and resolves each `EnvSpec` against the task's `EnvContext`. This replaces the hardcoded credential-injection block in `execute_task` that used to duplicate the same wiring across the executor, the proxy strip-set, and the auth map.

`EnvSpec` sources: `config` (dotted config path with `when` guard), `template_file` (auto-create from template), `secret` (per-user encrypted secret), `user_id` (the task's user id, for skills that scope by it), `setup_env` (skill-defined hook in `__init__.py:setup_env(ctx)` — used by `developer` for `DEVELOPER_REPOS_DIR`, which it derives per task as `{repos_dir}/{user_id}` and is the sole producer of, for the git credential helper, for the `gh` / `glab` wrappers it writes into the task's `.developer` directory, and for the `.developer/exec-shims` that route builds into the devbox; and by `google_workspace` for its OAuth token). The resource-backed sources (`resource`, `resource_json`, `user_resource_config`) and the `gate_user_has_resource` flag were removed in the Resources sunset — no bundled skill used them.

One pre-resolution gate filters out specs that shouldn't fire:

- `gate_has_discovered_calendars: true` — only resolve when CalDAV discovery returned at least one calendar

CalDAV discovery is itself a best-effort step: `discover_calendars_for_task(task, config)` returns `[]` when CalDAV is unconfigured / unreachable / the user owns no calendars. The same helper is reused by the scheduler's two subprocess paths (`_execute_skill_task`, `_execute_command_task`) so the gate fires consistently across LLM, skill-task, and command-task dispatch.

See [environment variables reference](../reference/environment-variables.md) for the full mapping and [credentials](../configuration/credentials.md) for the two-tier credential architecture (global vs per-user).

## Credential proxy and authorization

When the skill proxy is enabled (default), credential vars are split out of Claude's environment via `_split_credential_env(env, credential_set)` and routed through a Unix socket proxy. The credential set is itself manifest-derived — `derive_credential_set(skill_index)` returns every env var declared with `sensitive: true` across all skills.

Authorization is decoupled from skill selection. `derive_authorized_skills(selected, skill_index, ctx)` returns the union of selected skills plus any skill whose sensitive `EnvSpec`s actually resolve under the task's context. So a user with Karakeep configured can always reach `KARAKEEP_API_KEY`, even if keyword selection missed the bookmarks skill on a given prompt. Critical correctness note: the auth-side resolution passes `fallbacks_disabled=True` so an instance-wide `EnvironmentFile` value cannot fan a global secret out to per-user auto-authorization.

`derive_skill_credential_map(authorized, skill_index)` builds the per-skill map the proxy uses to scope credential injection — a skill CLI invocation only ever sees credentials its own manifest declared. `derive_lookup_allowlist(authorized, skill_index)` is the union the proxy will respond to over `credential-fetch`, with `_PROXY_LOOKUP_BLOCKED = {"ISTOTA_SECRET_KEY"}` subtracted as a defense-in-depth hard reject so a buggy `setup_env` hook can't expose the master Fernet key over the lookup channel.

See [security](../deployment/security.md#authorization-model) for the full model and rejection logging.

## Streaming events

The brain emits `StreamEvent`s (defined in `src/istota/brain/_events.py`) which `executor_stream.TaskStreamAdapter.on_event` maps to typed `TaskEvent`s and writes to the `task_events` log via `EventWriter.emit()` (`src/istota/events.py`). There is no scheduler-side progress callback and no `italicize` flag — the log is the bus. In-process consumers (`TalkEventSubscriber`, `LogChannelSubscriber`, `PushNotificationSubscriber`) and the web SSE endpoint read from it.

The `TaskStreamAdapter.on_event` mapping:

- **ToolUseEvent** -> `tool_start` (gated by `progress_show_tool_use`); NativeBrain also emits `ToolEndEvent` -> `tool_end` and `ToolProgressEvent` -> `tool_progress`
- **TextEvent** -> `progress_text` (gated by `progress_show_text`); per-token `TextDeltaEvent` -> coalesced `text_delta` on stream surfaces only
- **ThinkingEvent / ThinkingDeltaEvent** -> coalesced `thinking` (stream surfaces only)
- **ResultEvent** — final result (surfaces as `BrainResult.result_text`)
- **ContextManagementEvent** -> `context_management`, and a `cm_boundary` marker in the trace

The full `TaskEvent` kind set: `task_started`, `tool_start`, `tool_end`, `tool_progress`, `progress_text`, `text_delta`, `thinking`, `context_management`, `confirmation`, `result`, `error`, `cancelled`, `done`. The scheduler emits the terminal frames (`confirmation` / `result` / `cancelled` / `error` + `done`) and calls `writer.finish()`.

Cancellation is polled between events via the `cancel_check` callback, which calls `db.is_task_cancelled()`. The brain kills its subprocess and returns `stop_reason="cancelled"` when the check returns True.

## Result composition

The result goes through `_compose_full_result()`, which has two narrowly-scoped mechanisms sharing a `_last_substantial_region()` walker. Both mechanisms **replace** `result_text` outright — they never prepend or glue recovered text in front of the model's final output. (The one path that synthesizes text instead of choosing between candidates is `_ensure_final_answer`, described below, and only when there is no answer at all to protect.)

**Mechanism A — CM-aware (ISSUE-026):** When any `cm_boundary` events exist in the trace, segments the trace at those boundaries and returns the last region whose text is at least 200 chars (`_CM_SEGMENT_MIN_CHARS`). Always runs when CM events are present, including for automated tasks (scheduled / briefing / heartbeat). Falls back to `result_text` if no segment qualifies.

**Mechanism B — terse-recovery (ISSUE-025):** Segments the trace by both `tool` and `cm_boundary` events and returns the last region of at least 500 chars (`_TRAILING_REGION_MIN_CHARS`). Gated by **both** `_is_automated_task(task)` returning False **and** `_is_terse(result_text)` returning True (text shorter than 150 chars or matching a short reference regex like "see above" / "done" / "ok"). Structured-output tasks and substantial results bypass this mechanism. Skipped when CM events exist (Mechanism A wins).

**The finality rule (ISSUE-211):** the channel guidelines promise the model that text written between tool calls streams as a progress indicator and is not the saved reply, so a text region followed by a tool call is mid-turn narration by construction — the model kept working after writing it. Both mechanisms therefore restrict recovery to the region after the last `tool` entry (`trailing_only`), so only the model's final message is eligible. The exception is an explicit back-reference ("see above" / "done"), where the model itself says the answer is earlier — that is the ISSUE-025 case, so reaching back honours it rather than guessing. Note this deliberately revokes the earlier property that a tool is not a CM-mode delimiter.

**`_ensure_final_answer`** is the tail of both paths. When `result_text` is empty and nothing was recovered, it adopts any text after the last tool call however short (the size floors exist to protect a non-empty result, and there is none), and otherwise returns "The turn ended without a final response." with the last earlier region appended under a label — so the work stays visible without being passed off as the answer. Automated tasks are exempt because their output is parsed rather than read. Callers that interpret a result use `is_no_final_answer()` to tell composer-synthesized text apart from something the model wrote; the scheduler's confirmation gate and memory indexing both do.

Every override emits a single `compose_full_result: mechanism=… task_id=… original_chars=… recovered_chars=…` INFO log so the 500-char floor can be calibrated against production data. The `no_final_answer` path shares the prefix but logs `partial_chars=…` in place of the original/recovered pair.

Result priority: ResultEvent > result file > stderr > fallback error.

## API retry logic

Transient API errors are retried inside the brain up to 3 times, and those retries don't count against task attempts. The rule is **every 5xx, plus 408, 425 and 429** (`_status_is_transient`) — enumerating the common codes was itself a bug, since a Cloudflare-fronted provider emits 520–526 and none of those were on the list. `TRANSIENT_STATUS_CODES` survives as documentation of the common cases and is no longer the gate.

Two error shapes are parsed: `API Error: (\d{3}) (\{.*\})` first, then the bodyless `API Error: 529 Overloaded` form the CLI also emits. The delay is the provider's own `Retry-After` where it supplied one (capped at `RETRY_AFTER_MAX_SECONDS`, 60 s), otherwise `API_RETRY_DELAY_SECONDS` (5 s) — a default rather than a floor.

The helpers (`parse_api_error`, `is_transient_api_error`, `is_permanent_api_error`, `api_error_stop_reason`, `is_api_error_banner`, `parse_retry_after`, `API_RETRY_*`) live in `src/istota/brain/claude_code.py`; the first two are re-exported from `executor.py` for `scheduler.py` and tests. Pick the right strictness: `parse_api_error` answers "does this text contain a status code", which is fine for formatting a known failure and wrong for deciding something *is* one — a caller that would discard a completed answer keys on `is_api_error_banner` instead.

## Output validation

`detect_malformed_result()` checks for leaked tool-call XML in the output:

- **Strict mode** (Talk): any `</parameter>`, `</invoke>`, `<thinking>` outside code fences is flagged
- **Lenient mode** (other targets): only flags when the entire output is syntax fragments (< 20 chars of real content)

Malformed results are reclassified as failures and retried.

## Security functions

| Function | Purpose |
|---|---|
| `build_clean_env()` | Minimal env for Claude subprocess |
| `build_stripped_env()` | `os.environ` minus anything containing `PASSWORD`, `SECRET`, `TOKEN`, `API_KEY`, `APP_PASSWORD`, `NC_PASS`, or `PRIVATE_KEY` in its name. Substring match — no preserve list (`ISTOTA_SECRET_KEY` is stripped). For heartbeat/cron commands. |
| `build_model_cli_env()` | `build_clean_env()` plus an inherited `ANTHROPIC_API_KEY`. Used by the daemon-side `claude` spawns that send a prompt without going through a `BrainRequest`: conversation-context triage and the `!check` / self-check execution test. `build_clean_env()` already carries `CLAUDE_CODE_OAUTH_TOKEN`, so both auth shapes reach the CLI while the rest of the daemon environment does not. |
| `build_allowed_tools()` | Returns `["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]`. The CLI brains no longer pass this as an `--allowedTools` allowlist (they run `--dangerously-skip-permissions`); the list survives as NativeBrain's in-process tool filter and the non-empty/empty signal for tool-bearing vs text-only tasks. `WebSearch` runs server-side (titles + URLs only), so page reads are steered to the `browse` skill. Nothing here is scoped by identity unless `[brain.native.web_fetch] admin_only` is set, in which case `WebFetch` is dropped for a non-admin (ISSUE-449). |
| `_split_credential_env()` | Separates vars out of the model's env for proxy routing. Called twice — once with the credential set, once with the proxy-only set |
| `derive_credential_set()` | Sensitive env-var names across all skill manifests (replaces `_PROXY_CREDENTIAL_VARS`) |
| `derive_proxy_only_set()` | The second bucket: `ISTOTA_DB_PATH` plus manifest `proxy_only: true` vars (`HEALTH_DB_PATH`, `LOCATION_DB_PATH`). Routed to the proxy without credential semantics — not secrets, just paths the model has no business holding |
| `derive_authorized_skills()` | Selected skills ∪ skills whose sensitive `EnvSpec`s resolve under this task's context. Takes `hook_env` so a credential produced by a `setup_env` hook (the `google_workspace` case) can authorize its own skill |
| `derive_skill_credential_map()` | Per-skill credential map used by the proxy (replaces `_build_skill_credential_map`) |
| `derive_lookup_allowlist()` | Vars the proxy will respond to over `credential-fetch`, minus `_PROXY_LOOKUP_BLOCKED` |
| `discover_calendars_for_task()` | Best-effort CalDAV discovery; returns `[]` on any failure. Reused across LLM and subprocess dispatch paths |
| `build_bwrap_cmd()` | Builds bubblewrap sandbox command wrapper |
| `custom_system_prompt_path()` | `config/system-prompt.md` as an absolute path when `custom_system_prompt` is on — one source for both the `BrainRequest` field and the sandbox bind |
| `_build_network_allowlist()` | Builds host:port allowlist for CONNECT proxy |

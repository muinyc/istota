---
paths:
  - "src/istota/executor.py"
---

# Executor Internals

## `execute_task()`
```python
def execute_task(
    task: db.Task, config: Config, user_resources: list[db.UserResource],
    dry_run: bool = False, use_context: bool = True,
    conn: "db.sqlite3.Connection | None" = None,
    event_writer: "events.EventWriter | None" = None,
) -> tuple[bool, str, str | None, str | None]:
```
The old `on_progress: Callable[[str], None]` parameter is gone (task-event-streaming spec). The scheduler builds an `EventWriter`, subscribes consumers, and passes it; the executor emits `task_started` then adapts the brain's widened `StreamEvent` stream to `TaskEvent`s via `executor_stream.TaskStreamAdapter`, one instance per task, whose `on_event` calls `event_writer.emit(...)`. `None` on dry-run / CLI paths.
Returns `(success, result_text, actions_taken_json, execution_trace_json)`. `actions_taken` is a JSON array of tool use descriptions from streaming execution, or `None` for simple/dry-run/error paths. `execution_trace` is a JSON array of interleaved `{"type": "tool", "text": "..."}` and `{"type": "text", "text": "..."}` events, or `None`. A `tool` entry additionally carries `"raw": "<verbatim command>"` for Bash calls (the literal command, untruncated; `_tool_invocation` in `agent/events.py`) so the sleep cycle can distil playbooks that quote the real invocation rather than the paraphrased description (ISSUE-174 fix 1). Additive — consumers that read `text`/`type` are unaffected.

### Flow
1. **Setup temp dir**: `config.temp_dir / task.user_id`, plus the task's own control directory `config.temp_dir / .control / task.user_id / task_{id}` (`ensure_task_control_dir`, 0700 at all three levels). An unusable control directory fails the task by **return**, not by raise — `process_one_task` handles no exception from `execute_task`
1b. **Deferred briefing prompt** (ISSUE-143): when `task.source_type == "briefing"` and `task.briefing_name` is set, `build_deferred_briefing_prompt(task, config)` resolves the live briefing config + timezone and builds the full prompt (`build_briefing_prompt`'s slow news/yfinance/FinViz/IMAP fetch) here, in the worker — `task.prompt` is replaced. This keeps the slow network I/O off the scheduler dispatch thread (the scheduler creates briefing tasks with only the identity + a placeholder). Build failure / unresolvable briefing → keeps the placeholder, never fails the task.
1c. **Prepare attachments** (image-attachment-vision spec): `_pre_transcribe_attachments()` for audio, then `prepare_image_attachments()` for images — both before skill selection and before `build_prompt`, so selection, assembly and the model all see the same orientation, the same paths and the same OCR text. See "Image attachments" below.
2. **Merge resources**: DB resources + config resources → `db.UserResource` list
3. **Load skills**: `load_skill_index()` → `select_skills()` (deterministic matching, the only selection pass) produces the **eager** set → `load_skills(eager)` for the body + `eligible_skill_names(exclude = selected ∪ ⋃ exclude_skills_of_selected)` for the **menu** (the full eligible catalogue) → `build_disclosure_index(menu)` → `skills_index`. Always on, single-axis (selected ⇒ eager, else eligible ⇒ menu); no `progressive_disclosure` flag, no eager/lazy partition. The menu replaced the removed LLM Pass 2; the executor logs `skills: eager=N menu=M`.
4. **Skills changelog**: fingerprint compare, interactive only
5. **Context loading**: skip for scheduled/briefing
6. **User memory**: `read_user_memory_v2()`, skip for briefings
7. **Channel memory**: `read_channel_memory()`, only if `conversation_token`
8. **CalDAV discovery**: `get_calendars_for_user()`
8b. **Dated memories**: `read_dated_memories()`, skip for briefings, controlled by `auto_load_dated_days`
8c. **Memory recall**: `_recall_memories()`, BM25 search using `retrieval_query`, skip for briefings
8d. **Knowledge facts**: load from `knowledge_graph`, relevance-filtered by `retrieval_query`, capped by `max_knowledge_facts`
8d2. **Playbook recall**: `_recall_playbooks()`, BM25/vector over `source_type="playbook"` using `retrieval_query`, gated on `playbooks.enabled`, skipped for automated/`skip_memory` tasks (Part B). On a hit it `os.utime`s each recalled playbook file so retention keys on last-*use*, not last-write (ISSUE-174 Concern 3)
8e. **Memory cap**: `_apply_memory_cap()`, truncates recalled → knowledge facts → dated → playbooks if `max_memory_chars` exceeded (playbooks truncated last — most protected; returns a 6-tuple)
9. **Confirmation context**: load from `task.confirmation_prompt` if confirmed task
10. **Build prompt**: `build_prompt()` returns a `ComposedPrompt` — standing instructions in `.system`, task material in `.user`. Includes `confirmation_context` when set
11. **Dry run check**: return both halves through `render_composed_prompt()`, under fixed `===== SYSTEM =====` / `===== USER =====` labels. No prompt file is written and no brain request is built
12. **Write prompt files**: into the control directory from step 1 — the user half to `prompt.txt`, the system half to `system_prompt.txt`, both opened `O_NOFOLLOW` at mode `0600`; see "The two prompt files" below for why
13. **Build env**: `task_env.build_task_runtime()` returns a `TaskRuntime` — the env (see the var table below), the two proxy objects, their socket paths, the sandbox's read-only bind list and `authorized_skills`. Credential vars split via `_split_credential_env()`, twice, proxy-only first. Both context managers come back *constructed and not entered*: the `ExitStack` at step 15 is what enters them, because they must be live across the primary call, a reroute and the fallback call. The three orderings inside that function are load-bearing and its docstring is where they are written down
14. **Build BrainRequest**: prompt (the **user half only**) + `composed_system_prompt_path` (the system file) + allowed_tools + env + model/effort + the two sandbox-wrap closures (one per `SandboxProfile`) + on_progress/cancel_check/on_pid callbacks + `images` (the prepared attachments)
15. **Execute**: `make_brain(resolve_brain_kind(task.source_type, config.brain, override=task.brain)).execute(req)` — the room's pin, then the source-type rule, then `[brain] kind`; see `.claude/rules/brain.md`
16. **Compose result**: `_compose_full_result(result, trace)` reconciles result-text vs trace (CM-aware + terse-result recovery)
16b. **Image notes**: `_append_vision_dropped_note` when the brain that actually ran cannot see, `unread_images` + `_append_unread_images_note` when a CLI brain skipped a `Read` the directive required
17. **Update fingerprint**: on success, interactive only

## `build_prompt()`
```python
def build_prompt(
    task: db.Task, user_resources: list[db.UserResource], config: Config,
    skills_doc: str | None = None, conversation_context: str | None = None,
    user_memory: str | None = None, discovered_calendars: list[tuple[str, str, bool]] | None = None,
    user_email_addresses: list[str] | None = None, dated_memories: str | None = None,
    channel_memory: str | None = None, skills_changelog: str | None = None,
    is_admin: bool = True, emissaries: str | None = None,
    source_type: str | None = None, output_target: str | None = None,
    recalled_memories: str | None = None,
    playbooks: str | None = None,
    skip_persona: bool = False,
    cli_skills_text: str | None = None,
    skills_index: str | None = None,
    confirmation_context: str | None = None,
    knowledge_facts: str | None = None,
    conn: "db.sqlite3.Connection | None" = None,
    effective_prompt: str | None = None,
    attachment_status: "dict[str, str] | None" = None,
) -> ComposedPrompt:
```

`effective_prompt` is what `## User's request` renders — the typed request plus any audio transcript plus the rendered OCR context. It is a parameter rather than a mutation of `task.prompt` because the same string has to reach skill selection and the three retrieval passes as well, and a field read in five places is how those drift apart. `None` falls back to `task.prompt`, which is what every caller outside `execute_task` passes. `attachment_status` maps an attachment path to the one-phrase status rendered after it; see "Image attachments" below.

### The two prompt halves

`build_prompt` splits by **authority**, not by size (ISSUE-375). The question a layer is sorted on is whether it has to remain verbatim for the life of the task. Standing instructions go to `.system` and reach the model outside anything native compaction can reach; task material goes to `.user`, which is what a compaction summary carries forward instead. Before the split, every layer travelled as one string in the model's first *user* turn, so `NativeBrain`'s first compaction replaced Istota's identity, rules, tool surface and skill bodies with a model-written summary and the task ran on for hours without them.

**No line in the system half may point at material in the user half.** The old single string was written as one document and referred to itself throughout — "as listed above", "below", "at the top of this prompt". Each of those is a dangling pointer after the split, and worse than dangling after the first compaction: a surviving instruction naming deleted material is ISSUE-375 in miniature. Four were live. Three are answered by classification — rule 1's "as listed above" (accessible resources), rules 7 and 8's `Today's date` / `Current time` / `User timezone`, and rule 9's `Current UTC` are all in the system half beside the rules that name them, despite reading as task facts. The fourth could not be, because its referent belongs in the user half: the group-conversation line dropped the word "below". `tests/test_prompt_split.py` asserts each pairing rather than either half alone, so a later reclassification of one side fails instead of passing quietly.

The split also raises several interpolated scalars from a user message to a system one, so bot name, user id, source, output target, per-user email, conversation token and the rendered timezone all go through `_one_line()` before they are rendered into a header. That is structural sanitation and not instruction sanitation: persona, emissaries, guidelines, changelog and skill overlays stay multiline, because their structure *is* lines.

**System half** (standing instructions):
1. Header: role, user_id, datetime, task_id, conversation_token, source, output target, per-user email, a database line that names no path, and privileges (the file is masked out of the sandbox; naming it would point at nothing). Kept whole rather than split at the paragraph — the block is small and fixed for the task
2. Emissaries: `config/emissaries.md` constitutional principles (skipped for briefings)
3. Persona: user workspace `PERSONA.md` overrides `config/persona.md` (skipped for briefings or `skip_persona`)
4. Workspace layout: one static line, plus CalDAV-discovered calendars. The Resources sunset replaced the enumerated Folders / TODO Files / Notes / Reminders sections with that single line. Here rather than in the user half because rule 1 names it and because the file-tool descriptions are written in its vocabulary
5. Tools: file access, browser, CalDAV, email, then `skills_index` ("Available skills (load on demand)" — the menu catalogue) when the menu is non-empty. The **file-access framing is storage-backend-aware** (storage-agnostic-vocabulary spec): it renders in one of three modes keyed on `config.storage_backend` — Nextcloud-via-mount, Nextcloud-via-rclone, or local — and the folders header + attachments prose follow the same switch. Local mode adds a bullet clarifying the workspace is the *managed* area, not the limit of what an unsandboxed local bot can read (fixes the "I can only see the Nextcloud mount" false claim). Server/Nextcloud prompts are byte-unchanged. The executor is the single home of storage framing; skill bodies are storage-neutral and reference paths through the `{workspace}` / `{storage}` placeholders (see below)
6. Rules: resource restrictions, confirmation, subtasks, output
7. Guidelines: `config/guidelines/{source_type}.md`
8. Skills changelog
9. Skills doc (eager skills only — the menu skills are surfaced by the index in step 5, not inlined), including per-user skill overlays

**User half** (task material):
1. User memory: USER.md (skipped for briefings)
1b. Knowledge facts: relevance-filtered KG triples (skipped for briefings)
2. Channel memory: CHANNEL.md
3. Dated memories: auto-loaded from `memories/YYYY-MM-DD.md` (configurable via `auto_load_dated_days`)
3b. Recalled memories: BM25 search results (when `auto_recall` enabled)
3c. Learned Playbooks: `_recall_playbooks` BM25/vector hits over `source_type="playbook"` (when `playbooks.enabled`; skipped for automated/`skip_memory` tasks)
4. Context: previous messages
4b. Confirmation context: previous bot output for confirmed actions — interpolated after the context section, immediately before the request
5. Request: `effective_prompt` — the typed request, then any audio transcript, then the OCR section framed as untrusted text — followed by the attachment list, each line carrying its own location label and vision status

The one ordering change a reader will notice: `## Response format` used to sit *after* `## User's request` and now permanently precedes it, because the guidelines are instructions and travel with the instructions. No source's guidelines depend on following the request text.

### The two prompt files

A real run writes both halves into the task's **control directory**, `{config.temp_dir}/.control/{user_id}/task_{id}/`, before the request is built and never conditionally — a request naming a file that was not written is the fail-closed contract firing on our own bug. The user half is `prompt.txt`, because it is still the exact text sent on stdin, injected into the tmux pane, or used as the native initial user message. The system half is `system_prompt.txt`, and that path is what `BrainRequest.composed_system_prompt_path` names. `briefing_meta.json` and the prepared image renditions under `attachments/` are in the same directory, which holds every per-task file the daemon authors and nothing the model writes. `task_{id}_result.txt` stays in the per-user temp dir, because the model writes it from inside the sandbox.

**The directory is what the guards name, and it is a sibling of the per-user temp dir rather than a child of one.** `config.temp_dir` is bound at no path, so nothing model-writable is an ancestor and there is no window between the daemon's `mkdir` and bwrap's `mount` for a symlink swap — which is what rules out the `.developer` shape here, since `.developer` survives that window only because the repos bind lands after it and buries a swap (ISSUE-320). `get_task_control_dir` refuses an empty `user_id`, a non-`str` one, one that escapes the root under the same containment equality `get_user_repos_dir` uses, and one casefold-equal to `.control` itself. `ensure_task_control_dir` creates all three levels at `0700`, re-asserts the mode on an existing directory, opens each with `O_NOFOLLOW | O_DIRECTORY` and refuses a level that is not a directory the daemon owns.

The composed path is absolute with its *directory* resolved and its *filename* not. `control_dir` arrives resolved, which is what makes it the in-namespace path — `_ro_bind` uses the string it is handed as the destination — and leaving the last component alone is what keeps the `O_NOFOLLOW` open meaningful, since `.resolve()` would follow a planted symlink and hand `O_NOFOLLOW` an ordinary file to inspect. That flag is belt-and-braces now rather than the guard, since nothing can plant anything in a directory no task can write; it stays because a guard dropped on the strength of a property held somewhere else is the one nobody notices the loss of.

Two guards, for two tool families, and both are needed:

- `_extra_ro_binds` carries the directory into `build_bwrap_cmd`, whose `extra_ro_binds` are applied after every other bind, so the later read-only bind wins over anything beneath it. It covers every bwrap-wrapped child, including the native `Bash` tool, and it is emitted under both `SandboxProfile.CLAUDE` and `SandboxProfile.NATIVE` — not for the native image pipeline, which base64-encodes in the daemon, but because both backends put the prepared attachment path into the prompt's `Attached files:` section and a model that decides to `Read` one has to find it. Unlike the `custom_system_prompt_path` bind two hundred lines below, it needs no mask caveat: the database masks are the last mount operations and would shadow anything beneath them, but `mask_protected_paths` names `config.temp_dir`, so a mask that would cover this directory is refused outright rather than emitted. The config directory is in no such list, which is why that bind carries the caveat and this one does not.
- `native_fs_roots` returns the same directory in **both** `read_only` and `write_denied`, because the two are enforced under different conditions and neither covers the other's case. `read_roots = None` means *unconfined* and makes both root lists inert, so the deny entry — checked ahead of that early return — is the only guard on macOS, the standalone install and the shipped Docker stack, which is why `execute_task` seeds `_fs_write_denied_roots` with it outside the confinement branch. Under confinement the read entry is what makes the directory *readable*: it is inside no write root, and without it a task could not open its own prepared image attachment on a path its own prompt named. `_in_denied` compares realpaths with `is_relative_to`, so the directory entry covers every file nested under it and no per-file entry is needed. Without the deny root a native task rewrites its own standing instructions with one `Write` call, and a `native -> claude_code` reroute then reads the rewrite, since `dataclasses.replace` keeps the path.

**What is closed, and what is not.** Both cross-task cases are gone. Only the task's own directory is bound, never `{temp_dir}/.control/{user_id}`, so a concurrent task of the same user can neither overwrite another task's standing instructions nor read its assembled user half — and that half is the material one, since it carries retrieved memory, knowledge facts, playbooks, conversation history and the request. Under the flat layout every later task of that user could read it for the length of `scheduler.temp_file_retention_days`. Two routes could put a wider path back and one is guarded: `security.sandbox_ro_paths` is bound verbatim, and `load_config` now warns on an entry overlapping the control tree in either direction, latched per process per entry and gated on the requested `sandbox_enabled`; a `user_resources` row is `mount / resource_path`, bounded by `nextcloud_mount_path` and nothing else, which no shipped shape puts above `temp_dir` and which `doctor.runtime.task_control_dir` reports rather than refuses. The deferred-op files keep the original symlink exposure and stay in the per-user temp dir, model-authored by design. And nothing deletes the control directory per task: `cleanup_old_temp_files` owns it, because the scheduler reads `briefing_meta.json` after `execute_task` has returned and a per-task cleanup would delete the file before its consumer ran, inside a bare `except Exception` that logs nothing. The accepted cost is a directory inode per task for about two retention windows rather than one, since unlinking the files updates the directory's mtime and the sweep's `rmdir` arm is age-gated too.

## Image attachments

`prepare_image_attachments()` (`istota/image_attachments.py`) runs before skill selection and before assembly, and never raises: every failure is a bounded model-facing notice plus a metadata-only log line, because a corrupt image or a missing Tesseract must not fail a task whose text request is usable. It returns `attachments` (the original order, with normalized paths substituted for accepted images), `images` (the `ImageInput` list for `BrainRequest`) and `ocr_blocks` (one result or notice per candidate). `task.attachments` is updated in memory only — nothing writes it back, so a retry regenerates renditions and OCR rather than stacking a second copy of either. The module owns the gates, the two renditions and the OCR budget; the rest of this section is what the executor owns.

**One enriched request, two renderings, five consumers.** The same content — the typed request plus any audio transcript plus the OCR text — reaches skill selection, prompt assembly and the three retrieval passes, and none of them reads `task.prompt` for it. The first two get `effective_prompt`, which carries the OCR framed as untrusted text; the three retrieval passes get `retrieval_query`, which carries the same OCR unframed, for the reason below. An attachment is deliberately supplied input, so its text earns recall the way any other input does; the audio transcript already reached those passes.

**The retrieval passes get the OCR text unframed** (`image_attachments.ocr_query_text`), not the rendered section the other two get. `memory.search` joins every whitespace token of a query with an implicit AND and the recall path passes no `allow_or_fallback`, so the rendered section's untrusted-content preamble and per-image headings would add sixty-odd terms present in no stored chunk and return zero rows for every task carrying an image — silently, since an AND miss is not an error. Framing is for the model, which must see it; an index has no use for it.

**The audio transcript still lands on `task.prompt` and OCR text never does.** That assignment survives for one consumer outside this function: `scheduler.py` indexes `task.prompt` into conversation memory after `execute_task` returns, and an audio-only send arrives carrying the transport's stand-in "Process the attached file(s)", so dropping it would index the stand-in instead of what the user said, for every voice message. Nothing *inside* `execute_task` reads the mutated field any more, so the implicit contract is gone even though the assignment stays. OCR text is kept off it because a retry re-runs preparation and would otherwise stack the same block.

**`untrusted_input` is added to the eager set explicitly** when there are prepared images or image notices, using the same pattern as the native WebFetch gate. File-type selection already pulls it in as `transcribe`'s companion; the explicit add is defence against a future metadata change, not a second mechanism.

**Paths.** Every path in `attachments`, in `ImageInput.path` and in the Claude Code directive is `Path.resolve()`d, because `build_bwrap_cmd`'s `_bind` uses the resolved source as the in-namespace destination — on a deployment whose `temp_dir` sits behind a symlink, an unresolved path names a file that does not exist inside the sandbox, and a mandatory `Read` of it fails every time. An image whose resolved source lies under none of `image_bind_roots(config, task, user_temp_dir, control_dir)` is copied into `{control_dir}/attachments/` even when it needs no resize: the scheduler's nc-data fallback hands out `/mnt/nc-data/<user>/files/Talk/<name>`, which `build_bwrap_cmd` binds nowhere. `bind_roots` is passed only under `effective_sandboxing(config)`; without a namespace there is nothing to be outside of, and copying would replace the user's own path with a temp one for every image on the standalone shape.

**Status, per line.** `image_attachment_status()` marks a prepared image `VISION_PREPARED` and an omitted one with its reason. It stops there deliberately: assembly runs several hundred lines before the brain is constructed, so `vision supplied` would be a claim of sight that a non-vision native model, an availability-breaker skip-primary or an in-attempt reroute each falsify. The layer that can be right says the rest in the same prompt — the CLI brains' `Read` directive, the native brain's image blocks or named omissions. `_attachment_line` also moved the location label from a whole-list `any(att.startswith("/") …)` predicate onto each line, since normalizing more images makes a mixed list ordinary and that predicate presented a workspace-relative PDF as a local path.

**Two after-the-fact notes on the result.** `unread_images(req.images, trace)` counts `Read` calls in the execution trace and `_append_unread_images_note` names any image the model never opened, so a CLI brain's vision claim rests on a recorded tool call rather than on prompt compliance. It errs toward silence in both directions and its docstring says how: the trace entry comes from the `tool_use` block at call time, so a *failed* `Read` reads as done (the tool result reaches the same model, and the directive requires it to report the failure), and the entry carries a basename, so an unrelated file of the same name satisfies it. `brain_delivers_vision(kind, model)` plus `_append_vision_dropped_note` cover the other side: an answer written by a model with no declared vision support says so in the result, since a prompt-side notice is read by the model and only the result is read by the user. `None` from that predicate means "cannot tell" and produces no note — asserting an answer was written blind when it may not have been is the same class of false statement.

## Environment Variable Mapping

| Resource/System | Env Var | Source |
|---|---|---|
| Core | `ISTOTA_TASK_ID` | `str(task.id)` |
| Core | `ISTOTA_TASK_ATTEMPT` | `str(task.attempt_count + 1)` — which attempt of that task this process is running, 1-based to match the session log's file name. Bound **once** as `task_attempt` and used twice, here and as `BrainRequest.attempt`, since the exclusion below is an equality between the two and the direction they would drift is the permissive one. Set beside `ISTOTA_TASK_ID` by all three task paths (here, `scheduler._execute_skill_task`, `scheduler._execute_command_task`); `tasks transcript` reads it to exclude the log it is writing, and where the id names *that* task and the attempt is missing or unusable it excludes every attempt of it rather than answering (an id naming a different task needs no attempt and is unaffected). It used to derive the number from `attempt_count` per call, which the liveness reaper bumps to release a task it has decided is stuck — a decision that is wrong whenever the worker is slow rather than gone, and the row-derived floor then named the *next* worker's attempt while the live file sat below it (ISSUE-377). In `_EXECUTOR_PROXY_ONLY_VARS` beside `ISTOTA_DB_PATH`, so the model never holds it: it is the floor's authority, and `skill_client._run_direct` re-execs with the inherited environment on a proxy-off deployment. |
| Core | `ISTOTA_USER_ID` | `task.user_id` |
| Core | `ISTOTA_DB_PATH` | `str(config.db_path)` — set for **every** user, then split out of Claude's env into the skill proxy's `base_env` (`derive_proxy_only_set`, which also carries `ISTOTA_TASK_ATTEMPT`). It never reaches the sandbox. `scheduler._execute_command_task` / `_execute_skill_task` / `heartbeat` set it unconditionally and unsplit; those paths are unsandboxed by design. |
| Core | `HEALTH_DB_PATH`, `LOCATION_DB_PATH` | Manifest-declared `proxy_only: true` — same routing, no credential semantics. |
| Core | `ISTOTA_SANDBOXED` | `"1"` when `skill_proxy_enabled and effective_sandboxing(config)` — the proxy conjunct matters, since the marker means "the socket is how you run a skill" and with the proxy off there is no socket. Sandbox env only (added *after* the proxy's base env is snapshotted). `skill_client._run_direct` refuses when it is set. |

**No database is reachable from the sandbox.** `build_bwrap_cmd` ends with `--tmpfs` masks over `config.db_path.parent` and `config.module_db_root()` — the **last** mount operations, since bwrap applies argv in order. Each mask is followed by `--remount-ro` on the same path (`_bwrap_supports_remount_ro()`, probed like `--disable-userns`): a writable mask lets `sqlite3 {db_dir}/istota.db` *create* a zero-byte file and answer `no such table`, which reads as a corrupt database rather than a boundary and litters the directory for the rest of the task. A `--remount-ro` is the one thing allowed to follow a mask, since it can only take permissions away. Read-only also makes a mask *nested inside* another fatal (bwrap `mkdir`s the second mountpoint on the first mask's tmpfs, gets EROFS, exits before running anything), so `_mask_dir` skips any candidate an earlier mask already covers — including the case where the outer mask was refused, which the old caller-side check treated as covered. `--disable-userns` now ships with the `--unshare-user` bwrap requires alongside it; without it bwrap exits 1, which is why both the probe and the flag were inert from the day they were added. Nothing binds the framework DB or its `-wal`/`-shm` any more (the admin `--ro-bind` and `security.sandbox_admin_db_write` are both gone), and `native_fs_roots` dropped its matching admin read root.

The masks exist rather than a narrower "don't bind it" because not binding it is what the code already did, and it did not hold. `module_data_dir` defaults under `{db_path.parent}`, the reference deployment puts that under `istota_home`, and `sandbox_ro_paths` defaulted to the `/srv/app` containing it — so one RO bind naming no database exposed the framework DB with live sidecars, every user's module DB, the local backups and the browser profile, to admin and non-admin alike. The word "modules" appeared nowhere near the bind code, which is why two reviews of this area missed it. `sandbox_ro_paths` now defaults to `[]` **and is parsed from TOML at all** (it never was — the advertised knob was inert), but the masks are what makes the property independent of that.

The boundary is therefore two things, in order: the skill CLIs scope by `ISTOTA_USER_ID`, and the files are not there. Reaching them requires the proxy, which is why it is started unconditionally now — the old `if credential_env:` gate let a task with no secrets fall through to `skill_client._run_direct`, running the skill module inside the sandbox. `ISTOTA_SANDBOXED` makes that path fail closed instead.

Not covered by any of this: a deployment where bwrap is unavailable (Docker without `CAP_SYS_ADMIN` — the probe fails and the sandbox is silently skipped) or the standalone local install, which ships `sandbox_enabled = false` + `skill_proxy_enabled = false` by design. Both run the model with the daemon user's own filesystem access; the masks are a server-shape property.
| Core | `ISTOTA_CONVERSATION_TOKEN` | `task.conversation_token` |
| Core | `ISTOTA_DEFERRED_DIR` | `str(user_temp_dir)` — always set, for deferred DB writes |
| Core | `ISTOTA_EXPERIMENTAL_FEATURES` | CSV of `config.experimental.features`. Read by `experimental.enabled_features_from_env()` and `@requires_feature`. Propagated by every subprocess builder: `executor.execute_task` (LLM path), `scheduler._execute_skill_task`, `scheduler._execute_command_task`, `heartbeat._check_shell_command`. Not credential-flavored — passes through the skill proxy and `build_stripped_env` untouched. |
| Core | `ISTOTA_SKILL_PROXY_SOCK` | Skill proxy socket path (if proxy enabled) |
| Nextcloud | `NC_URL`, `NC_USER`, `NC_PASS` | `config.nextcloud.*` |
| Nextcloud | `NEXTCLOUD_MOUNT_PATH` | `str(config.nextcloud_mount_path)` |
| CalDAV | `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` | `config.caldav_*` |
| Browser | `BROWSER_API_URL`, `BROWSER_VNC_URL` | `config.browser.*` (if enabled) |
| Devbox | `ISTOTA_DEVBOX_CONTAINER`, `ISTOTA_DEVBOX_DOCKER_CLI`, `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES` | `config.devbox.*` (set unconditionally when `config.devbox.enabled`, no selection gate). Container name defaults to `f"{container_prefix}{task.user_id}"`. **No socket path of any kind is exported, and `build_bwrap_cmd` binds no Docker socket and no `docker` binary.** The Docker-API allowlist proxy that used to be bound at `/var/run/docker.sock` in every sandbox is retired with its only consumer; the devbox skill's one remaining Docker verb, `reset`, runs host-side in the CLI's own process. `/usr` is `--ro-bind`ed unconditionally, so `/usr/bin/docker` is still *in* the namespace on any host with the client installed — the guarantee is that no socket is bound at any path and no `DOCKER_HOST` is exported, so any `docker` a task finds fails at connect. `ISTOTA_DEVBOX_EXEC_TIMEOUT` went with the 300-second default it carried (the transport imposes none; the task's budget governs). `ISTOTA_DEVBOX_EXEC_SOCKET` does not exist and must not be added, for the reason `config.devbox.docker_socket` was kept out before it was deleted (ISSUE-284): this environment is the model's, so a socket path named here is one the model can replace, and a replaced socket answers `ok` and a fabricated exit 0. The skill CLI reads its socket from config, host-side. |
| Email | `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM` | `config.email.*` (`SMTP_FROM` is plus-addressed: `bot+user_id@domain`) |
| Email | `IMAP_HOST/PORT/USER/PASSWORD` | `config.email.*` |
| Karakeep | `KARAKEEP_BASE_URL`, `KARAKEEP_API_KEY` | From resource config `extra` |
| Monarch | `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN` | From the encrypted `secrets` table (cookie-pair auth). The legacy `MONARCH_EMAIL` / `MONARCH_PASSWORD` / `MONARCH_SESSION_TOKEN` were removed when the API switched to Django CSRF auth on `/graphql` — the cookie pair is the only credential *stored*. It is mintable server-side: `POST /api/money/monarch/login` takes email/password (plus an MFA code or an emailed OTP) transiently, signs in at the endpoint Monarch's own web app uses and with the client version its `version.json` reports, and persists only the resulting pair. Those inputs never reach a task env. |
| Money | `MONEY_USER` | The istota user_id (in-process facade; config resolved from the per-user money DB via `resolve_for_user`). `MONEY_CONFIG` is gone — there is no standalone money config path. |
| Feeds | `FEEDS_USER` | From the user's `feeds` resource (in-process; defaults to istota user_id) |
| Location | `LOCATION_DB_PATH` | `istota.location.resolve_for_user(user_id, config).db_path` via the location skill's `setup_env` hook. Per-user `{workspace}/location/data/location.db`. Skill subcommands needing the framework geocode caches (`reverse_geocode`, `day_summary`) open a second conn to `ISTOTA_DB_PATH`. |
| Developer | `DEVELOPER_REPOS_DIR` | `{config.developer.repos_dir}/{user_id}` via the developer skill's `setup_env` hook (if enabled, and the task is an admin's). The per-user subtree, matching the bind — never the shared root. |
| Developer | `GITLAB_URL` | `config.developer.gitlab_url` (if enabled) |
| Developer | `GITLAB_DEFAULT_NAMESPACE` | `config.developer.gitlab_default_namespace` (if enabled + set) |
| Developer | `GITLAB_REVIEWER` | `config.developer.gitlab_reviewer` (if enabled + set) — the reviewer's GitLab username |
| Developer | `GITHUB_URL` | `config.developer.github_url` (if enabled) |
| Developer | `GITHUB_DEFAULT_OWNER` | `config.developer.github_default_owner` (if enabled + set) |
| Developer | `GITHUB_REVIEWER` | `config.developer.github_reviewer` (if enabled + set) |
| Developer | `DEVELOPER_AUTHOR_CREDIT` | `config.developer.author_credit` (if enabled + set) |
| Developer | `GIT_CONFIG_*` | Git credential helpers for HTTPS auth (if enabled + token set) |
| Developer | `ISTOTA_PATH_PREPEND` | `{user_temp_dir}/.developer`, written by the developer `setup_env` hook when a forge token is configured, **plus `{user_temp_dir}/.developer/exec-shims` on a deployment with a devbox** (`[devbox] enabled` plus `developer.enabled` and a `repos_dir` — the derivation that replaced the retired `[developer.container] backend` key) — the two halves are independent (a deployment routing builds into the devbox need not have a forge configured), and where both are present `.developer` comes first, so a forge wrapper wins any name collision with a shim. The executor folds them onto the brain's `PATH` and **strips the variable**, so `gh` and `glab` resolve to the wrappers (`src/istota/forge_cli.py`) and the configured `shim_commands` resolve to the exec shims. A separate directory rather than the forge wrappers' own, so a shim whose command left the list can be removed without deciding which files are shims from their contents. The shim half is gated on **configuration alone** — `developer.enabled`, a non-empty `repos_dir`, and `[devbox] enabled` — never on selection: `developer` is a menu skill with no `always_include` and no `source_types`, so it reaches `selected_skills` only through sticky skills, which is the *second* turn of a conversation. A selection gate would leave the shims absent on a fresh "work on repo X" and the build would run host-side and 403 at the CONNECT proxy, reading as flakiness. The security half — binding the socket — is gated separately, on `authorized_skills`, which is a set the hook cannot see because hooks are dispatched *before* authorization by design. Deliberately absent from the `proxy_base_env` snapshot: a skill CLI runs host-side and must not pick up the task's wrappers. |

The sandbox RO-binds the host's real `~/.claude/settings.json` back into the
tmpfs'd `~/.claude` (`build_bwrap_cmd`), and the six direct brain callers
(`.claude/rules/brain.md` § Direct-caller availability) pass the daemon's own
environment through unsandboxed — so any Claude Code setting that changes
model behaviour is inherited on both paths unless something explicitly
neutralises it. The advisor tool (`advisorModel` in settings) is the first one
Istota takes a position on: `ClaudeCodeBrain` / `TmuxClaudeBrain` set
`CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` in the child env whenever the request
won't itself emit `--advisor`, closing the inherited channel on every
`BrainRequest` path at once (advisor-model spec, Stage 1).

## Brain invocation
The executor no longer spawns `claude` directly — it composes a `BrainRequest`
and hands it to a brain. The brain owns command construction, sandboxing (via
the supplied `sandbox_wrap` callback), subprocess/HTTP, stream parsing, and
transient-API retries. Three ship; details in `.claude/rules/brain.md`.

**Which brain is not `config.brain`.** It is
`resolve_brain_kind(task.source_type, config.brain, override=task.brain)`, so a
room's standing pick (frozen onto `tasks.brain` at creation) beats an operator's
`[brain.source_type_overrides]` entry, which beats `[brain] kind`. Returned
unchanged as the same object when neither applies, which is the common case and
what the no-routing check reads. An admitted room pin also clears `fallback` on
the returned config, so the failover machinery collapses to a plain primary call
for that task — see `.claude/rules/brain.md` § Per-room brain selection. Three
sites here resolve it, and each is handed `task.brain` rather than re-reading
the room.

Per-task BrainRequest fields the executor populates:
- `prompt`, `allowed_tools` (from `build_allowed_tools`), `cwd=config.temp_dir`,
  `env` (built per task), `timeout_seconds=config.scheduler.task_timeout_minutes * 60`
- `model = task.model or ""`, `effort = task.effort or ""` — the task's own pin or nothing, never a deployment default (ISSUE-418). The brain fills its own from `[brain.<kind>] model` / `effort`. `_resolve_effort` keeps its one rule: a task pinning a model with no effort carries none, since an effort chosen for one model need not be valid on another
- `advisor = brain.resolve_model_name(_resolve_advisor(task, config))` when
  `brain.model_namespace == "anthropic"`, else `""`. `_resolve_advisor` returns
  `config.advisor_model` unless `task.model` is set (a per-task model pin drops
  the advisor: the CLI's fatal advisor-gate check is "does the *main* model
  support the advisor tool at all," which is pin-dependent — a genuine
  capability mismatch between two otherwise-advisor-capable models only warns
  and the task still completes, mirroring `_resolve_effort`'s pin-drop rule
  one severity up). `_run_fallback` carries `advisor` across an
  anthropic→anthropic reroute and drops it on anthropic→native (advisor-model
  spec, Stage 3).
- `custom_system_prompt_path = config/system-prompt.md` when `custom_system_prompt = true`
- `streaming = event_writer is not None`
- `on_progress = stream.on_event`: `executor_stream.TaskStreamAdapter`, which maps the widened `StreamEvent`
  union to `TaskEvent`s on the `EventWriter` — `ToolUseEvent`→`tool_start`,
  `ToolEndEvent`→`tool_end`, `ToolProgressEvent`→`tool_progress`,
  `TextEvent`→`progress_text`, `ContextManagementEvent`→`context_management`.
  `tool_*` gated on `progress_show_tool_use`, `progress_text` on
  `progress_show_text`; `tool_progress` always emitted (SSE only)
- `cancel_check`: closure that polls `db.is_task_cancelled()`
- `on_pid`: closure that calls `db.update_task_pid()` for `!stop` support
- `sandbox_wrap` / `native_sandbox_wrap`: two closures over `build_bwrap_cmd(...)`
  so the brain can wrap its raw cmd without knowing anything about bwrap; both
  no-ops when the sandbox is disabled. Same plan, different `SandboxProfile`:
  `CLAUDE` carries the `claude` CLI's runtime state and credential because the
  process being wrapped is that CLI, `NATIVE` carries neither because it is
  istota's own code. Two fields rather than one plus a profile argument, because
  `_run_fallback`'s `dataclasses.replace` names neither and a single field would
  hand the Claude namespace to NativeBrain on a reroute (ISSUE-389)
- `result_file = {user_temp_dir}/task_{task_id}_result.txt`

After `brain.execute()` returns, the executor:
1. On a **failure** carrying `BrainResult.partial_text` (ISSUE-372), sets
   `task.partial_result` — the same post-run hand-off `model_used` uses, and for
   the same reason: `result_text` is what the scheduler dispatches on (by exact
   equality for a cancel), so the partial answer cannot travel in it, and
   widening the four-tuple would touch every caller for a value only the
   scheduler reads. Nothing is set on success — a successful run's answer *is*
   `result`, and a second candidate for one column is a bug.
2. Calls `_compose_full_result(result_text, trace)` on success to reconcile
   the final ResultEvent text against substantial intermediate text blocks
   (CM-aware + terse-result recovery — same logic both brains will need).
3. On a dropped-pin fallback (see below), appends the visible model note
   **after** composition.
4. Updates the user skills fingerprint when interactive task succeeded.
5. Returns `(success, result, actions_taken_json, execution_trace_json)` —
   shape unchanged from before the refactor.

## Brain fallback (availability failover)
Generalizes the old hardcoded tmux→claude_code in-attempt rerun. The
`brain.execute(req)` call happens inside `run_with_failover`, which reruns the
*same attempt* (no new DB row, no `attempt_count` increment) through a
configured fallback brain when the primary is unavailable. Kept executor-level:
brains have no `Config` for the operator alert, and the rerun/breaker already
live here.

`run_with_failover(brain, req, *, config, brain_config, task, stream,
event_writer) -> FailoverOutcome` sits at module scope beside its helpers rather
than inside `execute_task`, which is where the loop used to be — 6000 lines from
the code it belongs with. `FailoverOutcome` is the block's existing output set
written down: `result`, `primary_usage_result`, `ran_fallback`, `usage_effort`,
`dropped_pin`, `primary_kind`, `fallback_kind`, each read by `execute_task`
after the call. `ran_fallback` is not derivable from `primary_usage_result` —
on the breaker-cooldown path the fallback runs with no primary call at all, so
there is nothing to hold. **The `ExitStack` stays in `execute_task`** and wraps
this call: the skill and network proxies must be live across the primary call,
the reroute and the fallback call alike.

`_failover_notice(stream, event_writer, reason, *, primary_kind,
fallback_kind)` is where the stream and the failover meet, and it is why
`run_with_failover` takes a `TaskStreamAdapter`. A reroute is a stream boundary
exactly like a tool call, so it settles the buffers — `flush_thinking()` then
`settle_at_tool_boundary()`, in that order — before emitting the banner, or an
unflushed primary tail opens the fallback's answer. Two asymmetries: the settle
runs even when `emit_once` dedupes the banner away (it is about the daemon's
own buffers, not the sentence), and the gate is the **writer**, not the stream:
with no `event_writer` the function returns `None` and `_run_fallback` skips the
hook entirely. Nothing is lost there, because `TaskStreamAdapter.on_event`
returns at its own `event_writer is None` before either buffer is appended to,
so a writerless stream has buffered nothing to settle.

- `_fallback_kind = effective_fallback_kind(brain_config)` (`brain/_fallback.py`);
  `_cooldown = config.brain.fallback_cooldown_seconds`; `_breaker =
  get_availability_breaker()` (process-global `PrimaryAvailabilityBreaker`).
- **Stickiness:** when the breaker `should_skip(primary_kind, cooldown)`, the
  primary is skipped entirely and the task goes straight to the fallback.
- **Trigger set** `{usage_limit, not_found, fallback}` (+ `transient_api_error`
  iff `fallback_on_transient`, **on by default** since ISSUE-212): on a matching `brain_result.stop_reason` with a
  fallback configured, the executor reruns via `_run_fallback`. **Cooldown set**
  `{usage_limit, not_found}` opens the breaker through
  `open_primary_breaker(primary_kind, cooldown, stop_reason, config=config)`,
  which returns True once → `_fire_fallback_alert`, one operator alert. That
  helper also publishes the availability record, so the executor no longer calls
  `record_unavailable` itself: the two used to be handed `_cooldown`
  independently, and the window is now a deadline off the quota's reset
  (ISSUE-374), which only one of them can compute. `fallback` is excluded from
  the cooldown set (tmux keeps being probed per-task); the tmux launch alert
  (`consume_circuit_open_alert`) is still fired for a `tmux_claude` primary.
  A successful primary run (breaker armed) calls `record_success` to close it.
- `_run_fallback(config, brain_config, fallback_kind, task, req)` →
  `(BrainResult | None, dropped_pin)`. Builds the fallback brain
  (`dataclasses.replace(brain_config, kind=fallback_kind)`, overlaying the
  per-user native key when `native`), resolves model/effort via
  `_resolve_crossing_model_effort`, and reruns `dataclasses.replace(req,
  model=…, effort=…)`, passing the result through `_mark_if_exhausted`. A construction failure returns `None` (keep the primary
  result); an unexpected `execute` exception becomes a failed `BrainResult`.
- `_resolve_crossing_model_effort(task, config, target_brain, effort, *, origin_namespace)` →
  `(model, effort, dropped_pin)`. Empty requested model → fallback's own default
  (no note). **A move within one namespace is not a crossing**: where
  `origin_namespace` matches the target brain's, the pin is used exactly as it
  arrived and nothing is reported (ISSUE-417). This function used to cross
  unconditionally, so a `claude_code -> tmux_claude` fallback — the same
  `claude` binary, the same `anthropic` namespace — dropped a valid
  `claude-opus-5` *and* put a "your pin was dropped" note in front of the user.
  `origin_namespace` is where the name was *written*, which on this path is the
  primary brain's, and it comes from `brain.model_namespace_for_kind` rather
  than a construction. `_pin_origin_namespace` is what answers it for the
  **primary** path, and since ISSUE-419 an unpinned task's answer is
  `resolve_brain_kind`'s — the lane's own brain, not `[brain] kind` — which is
  the namespace every producer resolves in. So on that path the rule is inert
  by construction for an unpinned task and survives only for a pinned one; read
  that function's docstring before changing either side, since it names the
  three producers that do not meet the premise. `None` is "not established" and never compares equal, so
  an unresolvable origin drops the pin — the direction
  `commands._clear_pin_across_namespaces` already takes. Otherwise,
  `is_portable_alias(raw, config_alias_portable_names(config))` → re-resolve the
  intent in the fallback namespace **via `fallback_brain.resolve_alias(raw)`**, so
  both the model *and its effort* are the fallback namespace's own (a customized
  `smart` falling back claude_code→native lands on a valid openai_compat slug +
  effort, not the anthropic value — the role-tier-cross-brain-standardization
  fix); falls back to `resolve_model_name(raw)` defensively if the pair is empty
  (no note either way). Non-portable pin → fallback default + `dropped_pin = raw`
  (INFO log + visible note).
- `_append_model_note(result_text, dropped_pin, primary_kind, actual_model)` —
  pure string→string, appended after `_compose_full_result` and only on success;
  a single italic line naming the dropped pin and the model actually used
  (`actual_model` = the persisted `model_used`). Delivers uniformly across
  surfaces (it's part of `result_text`).

- `_mark_if_exhausted(fb_result)` — when the *fallback* also failed for an
  availability reason (`{usage_limit, fallback, transient_api_error}` — `not_found`
  is excluded: a missing fallback binary is an operator misconfiguration, and
  "try again shortly" would be false),
  prefixes `FALLBACK_EXHAUSTED_MARKER` (`"[brain-fallback-exhausted]"`) onto its
  `result_text`. `scheduler._format_error_for_user` checks that marker first and
  says "both my primary and backup brains are unavailable" instead of echoing a
  raw provider error at the user (ISSUE-212). A marker rather than a formatted
  sentence because `execute_task`'s return contract is a plain string — the
  scheduler owns the user-facing wording, and the underlying cause stays in the
  text for the logs. A *task-level* fallback failure (`timeout` / `oom` /
  `cancelled`) is deliberately not marked: it isn't an availability problem.
  Two consumers, because only one of them is Talk: `_format_error_for_user`
  (the Talk push path) and `scheduler._error_event_message`, which the
  terminal `error` **task event** goes through — stream surfaces (web chat,
  REPL) render that payload directly as the turn body and never touch the
  Talk formatter, so without it the marker and the raw provider text would
  reach the user there. It reworders only provider-availability failures;
  every other failure keeps its original text (useful in the REPL), and
  `tasks.error` keeps the raw text either way.

No fallback configured (`fallback = ""`, any primary kind since ISSUE-362) skips
only the `_run_fallback` reroute. The rest still runs on a trigger stop_reason:
the availability breaker opens, `record_unavailable` writes the row, and both
operator alerts fire, each saying there is nothing to reroute to. That is
deliberate — the breaker is what the sleep cycle and shared-block generation read
through `primary_brain_unavailable`, and gating it on a fallback left a
fallback-less deployment with no signal at all that its primary had gone down.
`_skip_primary` stays gated on a fallback existing, so an open breaker never
skips a primary there is nothing to replace. See `.claude/rules/brain.md`
"Brain fallback" for the classification + portable-alias contract.

## Result composition (`_compose_full_result`)
Stays in the executor (not the brain) because it operates on the
brain-agnostic `(result_text, execution_trace)` pair. Two mechanisms
sharing one `_last_substantial_region()` walker; both **replace**
`result_text` outright — never prepend / glue:
1. **Mechanism A — CM-aware** (ISSUE-026): runs whenever any
   `cm_boundary` entries exist in the trace. Segments by `cm_boundary`,
   returns the last region ≥ `_CM_SEGMENT_MIN_CHARS` (200). Always runs
   for automated tasks too — scheduled tasks truncated mid-response by
   CM still get the fix. Falls back to `result_text` if no segment
   qualifies.
2. **Mechanism B — terse-recovery** (ISSUE-025): segments by both
   `tool` and `cm_boundary`, returns the last region
   ≥ `_TRAILING_REGION_MIN_CHARS` (500). Gated on
   `not _is_automated_task(task)` (source_type ∉ {scheduled, briefing}
   plus structural fallbacks `heartbeat_silent` / `scheduled_job_id`)
   AND `_is_terse(result_text)` (< 150 chars or matches a short
   reference regex like "see above" / "done" / "ok"). Skipped when CM
   events exist (Mechanism A wins) and when the recovered region is
   already a substring of `result_text`.

**The finality rule (ISSUE-211)** bounds both mechanisms. The channel
guidelines promise the model that text written between tool calls streams as
a progress indicator and is not the saved reply, so a text region followed by
a `tool` entry is mid-turn narration by construction — the model kept working
after writing it — and must never become the durable answer. Both mechanisms
therefore pass `trailing_only=True` to `_last_substantial_region`, which
slices the trace at the last `tool` entry before walking, so recovery only
ever sees the model's final message. The one exception is
`_is_back_reference(result_text)` (the `_TERSE_REFERENCE_RE` set — "see
above" / "done"): there the model itself says the answer is earlier, which is
exactly ISSUE-025, so reaching back honours it rather than guessing. Before
the rule, a `<150`-char genuine answer or an empty result promoted whatever
narration preceded the last tool call, and Mechanism A additionally glued
narration onto the answer (its `{"cm_boundary"}`-only delimiter set spans tool
calls). This deliberately revokes the earlier "a tool is NOT a CM-mode
delimiter" property; the cost is that a CM-split answer whose post-tool tail is
under the CM floor now keeps the truncated `result_text` instead of recovering.

`_ensure_final_answer(result_text, trace, task)` is the tail of both paths and
closes the abnormal-end case: when `result_text` is empty and nothing was
recovered, it does *not* fall through to narration. Any text after the last
tool call is adopted outright however short (the size floors exist to protect
a non-empty `result_text`, and there is none); otherwise it returns
`_NO_FINAL_ANSWER_NOTICE` — "The turn ended without a final response." — with
the last mid-turn region appended under a label, so the work stays visible
without being passed off as the answer. Automated tasks are exempt: a
briefing body is parsed as JSON and an empty result already flows to that
module's quiet retry, which prose would break. This is why the executor now
calls composition on `if success:` rather than `if success and trace:` — a
successful turn with no trace at all still must not deliver a blank reply.

Every override logs one INFO line
(`compose_full_result: mechanism=… task_id=… source_type=… original_chars=… recovered_chars=…`)
so the 500-char floor can be calibrated against real production data. The
`no_final_answer` path shares the prefix but logs `partial_chars=…` instead of
the original/recovered pair, so a field-keyed query needs both shapes.
The legacy Jaccard near-duplicate gluing path is gone; `_text_similarity`
remains in the source as a dead helper but is no longer called.

## API retry constants (re-exported from brain.claude_code)
- The live transient rule is **every 5xx**, plus `408`/`425`/`429` (`_status_is_transient`). `TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}` is kept as documentation of the common cases and is **not** the gate — enumerating was itself the ISSUE-212 bug class
- `PERMANENT_STATUS_CODES = {400, 401, 403, 404, 405, 413, 414, 422}` — no retry,
  no fallback attempt (retrying or paying for a fallback call that would fail
  identically buys nothing)
- `API_RETRY_MAX_ATTEMPTS = 3`
- `API_RETRY_DELAY_SECONDS = 5` — the default when the provider named no wait, not a floor; superseded per attempt
  by `parse_retry_after(text)` when the provider supplied a `Retry-After`,
  capped at `RETRY_AFTER_MAX_SECONDS = 60`
- Patterns: `API Error: (\d{3}) (\{.*\})` first, then the bodyless
  `API Error:?\s+(\d{3})\b[ \t]*([^\n]*)`
- Retries do NOT count against task attempts
- `parse_api_error`, `is_transient_api_error` re-exported from `executor`
  for `scheduler.py` and tests; canonical home is `brain/claude_code.py`.
  `is_permanent_api_error` / `api_error_stop_reason` / `is_api_error_banner` /
  `parse_retry_after` are new and are imported from `brain.claude_code` directly
  (nothing needs a back-compat re-export).

## Key Constants
- Background task types excluded from context: `["scheduled", "briefing"]`
- Task control directory: `{config.temp_dir}/.control/{user_id}/task_{task_id}` (`CONTROL_DIR_NAME = ".control"`), 0700 at all three levels, 0600 files
- Prompt file (user half): `{control_dir}/prompt.txt`
- Composed system prompt file (system half): `{control_dir}/system_prompt.txt`
- Briefing block metadata: `{control_dir}/briefing_meta.json`, read and unlinked by the scheduler after `execute_task` returns
- Prepared image renditions: `{control_dir}/attachments/`
- Result file: `{user_temp_dir}/task_{task_id}_result.txt` — the model writes it, so it stays in the writable directory

## Security Functions
| Function | Purpose |
|---|---|
| `build_clean_env(config)` | Minimal env for Claude subprocess (PATH, HOME, PYTHONUNBUFFERED + `USER`/`LOGNAME` + passthrough vars). `USER`/`LOGNAME` are process-identity basics (not secrets) that the macOS Keychain lookup needs — without them the `claude` CLI's login-Keychain OAuth read fails and every task reports "Not logged in" on a standalone mac; harmless on Linux where the credential is a file under `HOME`. Deliberately does **not** set the cache variables: `proxy_base_env` is built from its output and reaches every host-side skill CLI. Those live in `execute_task`, after that snapshot. Sets `SHELLOPTS=pipefail` (`shell_exec.pipefail_env`) as its last act, so every bash below a model subprocess starts with the option on (ISSUE-321). Here rather than in a brain because the defect is not one brain's: a `ClaudeCodeBrain` or `TmuxClaudeBrain` task runs its commands through the *CLI's own* Bash tool — `bash -c 'source <shell-snapshot> && eval <cmd>'`, in a process istota launches and does not instrument — so `shell_exec.shell_argv`, which fixes the shells istota spawns itself, cannot reach it, and the environment is the only lever that can. `NativeBrain` already passes `-o pipefail` on its own argv and gains only depth, which is what keeps the two brains answering an identical command string identically; that divergence was the stated reason ISSUE-307 left this site alone. **`SHELLOPTS` rather than `BASH_ENV`**, which also works and was also measured surviving the snapshot: `BASH_ENV` names a *file* bash sources before every non-interactive shell — arbitrary code execution before every command the model runs, needing that file to exist, to be bound into the sandbox and to stay unwritable by the model, three things that each fail silently since bash ignores an unreadable one without a word. `_SHELL_STARTUP_ENV_VARS` strips it for that reason — and **the review of this change found that the strip did not cover this function**: it had exactly one use, inside `build_stripped_env`, while `build_clean_env`'s passthrough loop was an unfiltered `env[key] = os.environ[key]`, so an operator listing `BASH_ENV` in `passthrough_env_vars` forwarded it to every model subprocess. That loop now filters the same set (and the set gained `SHELLOPTS` and `BASHOPTS`, which are the same import-at-startup mechanism and would let an inherited `xtrace` echo injected credential values into every cron job's captured output). `SHELLOPTS` carries option *names*: `pipefail:$(touch /tmp/x)` is rejected as an invalid option name rather than evaluated. Strip first, set second — the guarantee is that no *inherited* value survives, not that the variable is absent. Applied after the passthrough loop, so a `passthrough_env_vars` entry of the same name is dropped rather than honoured. `set +o pipefail` inside a command is the per-command escape hatch and the only one: `SHELLOPTS` is readonly inside bash so it cannot be unset, and there is deliberately no config switch, matching ISSUE-307 and `shell_argv`, neither of which shipped one. **Reaches bash and nothing else**, so a `#!/bin/sh` script gets the option on macOS (bash in sh-mode imports the variable) and not on Debian (dash has no `pipefail`) — do not write either into a doc as the rule. It also reaches the host-side skill CLIs through the `proxy_base_env` snapshot in `execute_task`, which is the reach this row's cache-variable note exists to warn about; it is inert there, since the value is a fixed option name with no file behind it and nothing under `src/` passes `shell=True`. |
| `resolve_sandbox_cache_dir(config, user_id)` | This user's package-cache directory, created, or `None`. One predicate for the RW bind, the cache environment in `execute_task`, and `native_fs_roots`, so they cannot disagree. **Two shapes.** With `developer.enabled` and `developer.repos_dir` set it is *derived*, not configured: `{repos_dir}/{user_id}/.package-caches`, inside the subtree the repos bind covers, which is the only shape where uv hardlinks a wheel into a venv instead of copying it (`link(2)` compares mounts, not devices). `security.sandbox_cache_dir` is not consulted at all on that branch. Without them it is `{security.sandbox_cache_dir}/{user_id}` — the fallback, for a deployment running the sandbox without the developer skill, where nothing binds an ancestor and a venv pays the copy as it always did. Per user in both, because uv trusts its unpacked wheels on read, so a shared cache is a cross-user code path. **The containment assertion is the layout in one line**: the directory must resolve to exactly the path the layout names, on both branches. On the derived branch the cache's parent is bound read-write into the task's own sandbox, so a symlink at `.package-caches` would otherwise be created, `chmod 0700`-ed and bound by the daemon — ISSUE-319 back through a name. The mode goes on through an `O_NOFOLLOW` fd, since `mkdir(exist_ok=True)` and `os.chmod` both re-traverse by name. **The window after that check is real, was reachable, and is closed by the branch gate rather than by the resolver** (ISSUE-320, `tests/linux/test_sandbox_cache_dir.py::TestTheCacheBindSymlinkRace`): `_bind` resolves in Python and the kernel walks the name again at bwrap's `mount`, so a symlink planted in between *is* followed — confirmed binding another user's subtree read-write on the shape with no covering bind. The derivation is therefore gated on `sandbox_cache_is_derived`, which is the repos bind's whole condition (`is_admin and developer.enabled and developer.repos_dir`) rather than the `developer.enabled` half it used to be: wherever the cache's parent is model-writable, the covering bind is emitted after the cache bind and buries the swap. Before that, a **non-admin** derived a cache inside `{repos_dir}/{user_id}` — which the devbox mounts read-write into their own container, with no admin gate on that mount or on the exec-socket bind reaching it — and took the cache bind with nothing above it. `native_fs_roots` closes the same window differently, because it has no mounts and so nothing to bury with: it does not add the derived cache as a write root at all (it is inside the repos write root already, and `ToolEnv` realpaths every root at construction). The `--disable-userns` flag was never what closed any of this — bwrap mounts in its own namespace, so the host directory is not a mountpoint and `rename` on it succeeds while the bind is live. Returned **as written**, not resolved, so a cache under a symlinked `repos_dir` lands on the same mount the repos bind does — otherwise `link(2)` returns EXDEV and every worktree pays for a full copy, silently. **Never raises**; every rejection falls open to the pre-ISSUE-305 behaviour, and the branch selection is inside the `try` because `build_bwrap_cmd` reaches this per Bash call under NativeBrain. Rejects a relative path, a root that is not an existing writable directory, anything under a database directory (checked here, since `_validate_workspace_dir` skips a relative `db_path`), the rest of `_validate_workspace_dir`'s blocklist, and anything at or above a path the sandbox already mounts — see `_sandbox_bind_targets`. The protection checks run against the cache's *parent* on both branches, which is conservative in the only direction that matters and has one consequence with no escape hatch left: a `developer.repos_dir` overlapping the source tree, the mount, a database directory or a `$HOME` dotfile directory loses its disk cache on every task, and the fix is to move `repos_dir`. Warns once per process per distinct refusal. |
| `_sandbox_bind_targets(config)` | What `build_bwrap_cmd` mounts, that a cache must not be mounted *above*. bwrap applies argv in order, so the late cache bind would cover an earlier mount whose destination is beneath it: `$HOME/.cache` over the read-only huggingface bind, `config.temp_dir` over every workspace and the `.developer` credential helpers, `$HOME/.local` over the `claude` binary, `developer.repos_dir` over every user's subtree at once. `_mask_protected` solves the same problem for the masks; this is its counterpart. Equal-or-ancestor, not overlap. It answers **one direction only** — what the cache can swallow, never what can swallow the cache, and inferring the second from the first is what kept ISSUE-319 invisible for a release. The `repos_dir` entry is reachable on exactly one shape, and that is worth naming because the obvious reading is that the derivation made it dead. The derived branch is gated on the **triple** `sandbox_cache_is_derived` names (`is_admin and developer.enabled and developer.repos_dir`) while this list appends on `repos_dir` alone, so a deployment with the skill switched off and the path still set — or a non-admin — reads `security.sandbox_cache_dir` *and* carries the entry. That is the sandbox-without-developer deployment the fallback branch exists for, and a `sandbox_cache_dir` at or above `repos_dir` there would cover every user's subtree at once. The docstring used to say the entry cannot fire and now states that shape instead. |
| `without_claude_runtime_env(env)` | `env` minus `claude_runtime_env.CLAUDE_RUNTIME_ENV_VARS` — what a task env carries only because the outer process is the `claude` CLI (`CLAUDE_CODE_OAUTH_TOKEN` today). `build_clean_env` sets it for every task whatever brain will run it, since two of the three brains authenticate with it; `NativeBrain` does not, so on that path it has no reader and only a route out — `echo "$CLAUDE_CODE_OAUTH_TOKEN"` in a Bash call returns as a `ToolResultMessage` addressed to whatever provider native is pointed at (ISSUE-390). The environment counterpart of the Claude-only *mount* block ISSUE-389 put behind `SandboxProfile.CLAUDE`; mounts and environment are separate mechanisms and that split never addressed the second. **Three call sites**, and the third is the one with the subtlest reasoning: `NativeBrain._hello_payload` for what a Bash child is handed, `NativeBrain._start_tool_server` for what the tool-server process itself carries (a Bash child runs at the same uid in the same PID namespace and reads its parent's `/proc/<pid>/environ`, so the frame strip alone leaves the token reachable), and `proxy_base_env` below for the host-side skill CLIs — the model reaches those through the same Bash tool, they run unsandboxed as the daemon user, and the variable is in no manifest so neither `derive_credential_set` nor `derive_proxy_only_set` removes it — with one reader the original reasoning missed, `code_review`, which spawns the `claude` binary per reviewer and from ISSUE-390 failed every review; `skill_model_credentials` / `SKILL_MODEL_CALLERS` below is the scoped exception, and the strip itself is unchanged. **Copies, never mutates**: the argument is `req.env`, which `ClaudeCodeBrain` writes to in place and which `_run_fallback` carries across a reroute with `dataclasses.replace` without rebuilding, so an in-place strip would unauthenticate the CLI on a `native -> claude_code` fallback. **`None` and `{}` stay distinct**, because `ToolEnv.subprocess_env` reads `None` as "inherit the parent environment" and the parent is the daemon, whose environment is the token's source — the caller puts `or None` on the *input*. Lives in a stdlib-only leaf rather than here because `executor` imports `.brain`, so `brain/native.py` cannot import this module at all. It is **not** a general credential filter, and the scope is deliberate: the skill credentials are already removed by `_split_credential_env` (gated on `skill_proxy_enabled`, default on), while this token is the one name that gating never reaches — set unconditionally, declared in no manifest — so it survived on the sandboxed shape where the others were gone. The proxy-off shapes are ISSUE-393, not this. |
| `skill_model_credentials(*sources)` | `SKILL_MODEL_CREDENTIAL_VARS` (the Claude token plus `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`), read from the first source that has each one, for the skill CLIs named in `SKILL_MODEL_CALLERS` — `code_review` today, the only skill under `src/istota/skills/` that imports a brain. It spawns `claude -p` per reviewer, so `proxy_base_env`'s strip left it unauthenticated and every review came back `skipped / review_failed` about a second in (ISSUE-409). **A copy, never a split**: `_split_credential_env` *moves* a name from the model's env to the proxy's, and this value has to be in both, since `ClaudeCodeBrain` and `TmuxClaudeBrain` authenticate the task's own brain with it — splitting it out would unauthenticate the task in order to fix the skill. A **name list rather than a manifest flag** because both manifest routes are worse: `sensitive: true` in `skill.md` puts it in `derive_credential_set`, which is index-wide and drives exactly that split, and a `daemon_env` source would let any skill claim any variable in the daemon's environment. Two sources because `build_clean_env` puts the token in the task env and puts the two API-key names nowhere, so those come from the daemon's own — which is also why the API-key shape was broken a change *earlier* than the subscription one. **Injection only, never lookup**: `task_env` adds it to the per-skill `skill_credential_map`, while `_PROXY_LOOKUP_BLOCKED` keeps it out of `derive_lookup_allowlist`, whose result is a union that anything holding the socket — the model included — can `credential-fetch` by name. Deliberately not the rest of `_MODEL_CLI_*`: those are *reachability*, handled by the two entries below (ISSUE-410). This set is what a CLI needs to **authenticate**, and it stays its own set because the empty-value rule differs — an empty credential is not a credential, while an empty `NO_PROXY` is meaningful. |
| `skill_cli_tls_env(*present)` / `skill_model_reachability(*present)` | The ISSUE-410 reachability pair, and **the axis they split on is the whole point**. `build_model_cli_env` tops its allowlist up from `os.environ`, which is the daemon's own for every daemon-side caller and is `proxy_base_env` for the one that is not — `code_review`, which the skill proxy spawns as a subprocess — so the loop was reading the wrong environment and finding nothing, and after ISSUE-409 gave it a credential a proxied, TLS-terminating or gateway deployment would authenticate and then fail at connect or at the handshake. Nothing in `build_model_cli_env` changed; what changed is what its environment has to offer. The axis is **not** "secret or not" — it is whether handing the value to a CLI that never asked for it can do harm, and that splits the names cleanly. A **trust store path cannot**: it only ever adds a CA, so a skill that needed it works and one that did not is unaffected, and the value is a path. Those are `SKILL_CLI_TLS_VARS`, shared with every host-side CLI through `proxy_base_env`; `CURL_CA_BUNDLE` joins the original four because `forge_cli._CARRY_EXACT` already answered this question that way, it is the name `curl` reads and the only one `requests` falls back to, and an operator who set just that name — the common single-variable choice — otherwise got nothing and a handshake error pointing nowhere near here. A **proxy URL redirects traffic**, which is the half the obvious reading misses: it is not merely inert for a CLI with no use for it, it captures that CLI's requests *including the ones aimed at this deployment's own services*. `browse` is the live case — its only outbound call is `BROWSER_API_URL`, `http://localhost:9223` by default, over httpx with `trust_env=True`, which honours `HTTP_PROXY` and does not special-case loopback — and `tests/support/env_isolation.py` records exactly this failure already happening in this repo, to nineteen loopback stub servers against a proxy that answered 405. A proxy URL is also not reliably a non-secret (`http://user:pass@egress:3128` is an ordinary shape, and `skill_proxy` returns a skill CLI's stderr to the model verbatim, which is what a connect failure echoes). So `SKILL_MODEL_REACHABILITY_VARS` — the proxy triple plus `ANTHROPIC_BASE_URL`, which no other skill reads and whose path can carry a key — goes through ISSUE-409's **per-skill** map to `SKILL_MODEL_CALLERS` alone, and `_PROXY_LOOKUP_BLOCKED` gains the same names so the union `credential-fetch` reads from does not. **Left undone deliberately, and not what ISSUE-410 filed**: `feeds` fetches external URLs and would want an egress proxy, which first requires exempting each deployment's own internal endpoints from it — a design question about per-skill network policy rather than about the reviewer's environment. Both halves are a **gap-filler, never an override**: `credential_env` is passed in beside `proxy_base_env`, so a name a manifest declared `sensitive`, which `_split_credential_env` just *moved* into the per-skill map, is not read back out of the daemon's environment. That is right and has a cost worth knowing, because it is not the scoping it looks like — `derive_credential_set` is index-wide, so **one** manifest declaring `SSL_CERT_FILE` sensitive removes it from `browse`, `feeds` and `code_review` at once. No shipped manifest names one today; `tests/test_task_env.py::TestTheReachabilityNames` carries the controls, including the one that turns red under the rejected design where the proxy triple is shared. Presence, not truthiness, for the reason `_MODEL_CLI_PROXY_VARS` gives, which is also what keeps `skill_model_reachability` separate from `skill_model_credentials` rather than folded into it. **No collision with a sandboxed task's CONNECT bridge**: that value is `exec env HTTPS_PROXY=…` applied by `sandbox_plan` inside the namespace at exec time, so it enters no env dict, and a host-side CLI is outside the namespace where the daemon's own proxy is the correct answer. All of it sits inside the `skill_proxy_enabled` branch, since `proxy_base_env` exists only there; with the proxy off, `skill_client._run_direct` re-execs with the model's own `build_clean_env`-derived environment, which carries none of these names. |
| `build_stripped_env()` | os.environ minus credential vars (PASSWORD/TOKEN/SECRET/API_KEY/NC_PASS/PRIVATE_KEY/APP_PASSWORD). For heartbeat/cron commands. Always-on. |
| `build_model_cli_env(config)` | `build_clean_env` plus the names a model call needs to *reach* the provider: the proxy triple (upper and lower case), the TLS-trust names, and the endpoint group (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`). Those extras are an ISSUE-395 regression fix rather than a widening — every caller runs in the daemon's own netns with no CONNECT bridge, so each inherited the daemon's proxy and CA settings by accident from `dict(os.environ)`, and narrowing without carrying them forward would strand a proxy-only or gateway deployment at the connect. Presence, not truthiness, since `NO_PROXY=` is meaningfully empty; an operator's `passthrough_env_vars` entry wins, being filled in only when absent. The env for **any** daemon-side model call that is not a task, whether a bare CLI spawn or a `BrainRequest`: the `!check` / self-check execution test, which spawns the CLI itself; conversation-context triage, which builds a `BrainRequest` around this env instead (`context._claude_cli_triage`, ISSUE-272) rather than spawning directly; and the six direct `BrainRequest` builders — the three OCR extractors, `health/explainer.py`, `memory/sleep_cycle.py`, `briefings/shared_blocks.py` and `skills/code_review`. `build_clean_env` already carries `CLAUDE_CODE_OAUTH_TOKEN`, so both auth shapes work while the master key, the Nextcloud app password and every service token stay out. Triage was the one prompt-bearing spawn with no `env=` at all and inherited `os.environ` wholesale (ISSUE-232), and the six builders did the same until ISSUE-395; use this for any new one — the rule, not the roster. (The two `claude --version` probes — `commands.py`, `brain/tmux_claude.py` — still inherit the daemon env; they send no prompt and read only a version string.) |
| `build_allowed_tools(is_admin, skill_names, *, web_fetch_admin_only=False)` | Returns `["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]`. `is_admin` decides nothing here unless the operator set `[brain.native.web_fetch] admin_only`, which drops `WebFetch` for a non-admin (ISSUE-449). The native brain builds its daemon-side `WebFetch` from this list and that tool reaches the network outside the CONNECT allowlist, where the same user under a CLI brain has `--unshare-net` plus the allowlist — an asymmetry that is bounded by the block's own egress policy (`allow_hosts` / `block_hosts` / `extra_blocked_cidrs` / `allowed_ports` / `allow_http` / `require_url_provenance`), which binds every caller, rather than by an identity gate, which bound nobody's destinations. Where the flag is set it is read unconditionally rather than only for a room-pinned brain, so a native-default deployment is covered too. `build_prompt`'s Tools section names the tool under the same flag, and states the withheld case rather than dropping the line. For ClaudeCodeBrain / TmuxClaudeBrain the *list contents* no longer reach the CLI — both run with `--dangerously-skip-permissions` (no `--allowedTools` allowlist), so the model gets its full default toolset and the bwrap sandbox + network proxy + clean env are the boundary. The list survives as (a) NativeBrain's in-process tool filter and (b) the non-empty/empty signal distinguishing a tool-bearing task from a text-only one. `Agent` + `Workflow` (the harness's multi-agent fan-out) stay denied via `--disallowedTools`. |
| `build_daemon_sandbox(config, user_id, *, extra_ro_binds=None)` / `daemon_work_dir(config, user_id)` | Bubblewrap for a model call with no task behind it (ISSUE-397). The OCR extractors build their own `BrainRequest` rather than going through `execute_task`, so none of the per-task plumbing runs for them — the sandbox included. That mattered because of the row above: a non-empty `allowed_tools` is what makes `build_claude_cli_flags` add `--dangerously-skip-permissions` with no allowlist, so a request asking for `Read` gets the CLI's whole default toolset, and both Claude brains ignore `fs_read_roots` and take their filesystem boundary from bubblewrap alone. Without a wrap that toolset ran host-side as the daemon user, on the default deployment, driven by a prompt whose input is an uploaded document. Returns the wrap and the `cwd` **together**, because bwrap chdirs into `work_dir` inside the namespace and a request naming a different directory would disagree with its own wrap. `SandboxProfile.CLAUDE` and a synthetic `db.Task(id=0, …)`, following `heartbeat.py`'s task-less `claude -p`; `conversation_token` stays empty, so no channel directory is bound. No `net_proxy_sock`, so no `--unshare-net` — these run in the daemon's own netns and have to reach the provider, which is the same posture `build_model_cli_env` above exists to keep working. **The document is bound by name** rather than left to the `{mount}/Users/{user_id}` bind: that covers a bloodwork panel's upload but not the encounter and immunization routes' temp copy, nor `python -m istota.health.ocr`'s arbitrary local file, and a wrap that hides the document is an outage rather than a boundary. Read-only, even inside the read-write `work_dir` bind — a later `--ro-bind` under an earlier `--bind` is what takes the write away. `daemon_work_dir` is the shared half, and the two upload routes read it too so the write and the bind cannot disagree about where a temp copy goes; its containment test is the same equality `get_user_repos_dir` uses, and **its fallback value is also the refusal signal** — an id that names no child hands back the shared root, and `build_daemon_sandbox` declines to wrap rather than bind every user's scratch space into one namespace. `wrap` is `None` in one further case and it is the *other* answer: `security.sandbox_enabled = false`, a deployment that confines no task at all, where this one runs too. A non-`None` wrap is not proof of a namespace either — on macOS and on the shipped Docker stack (which grants neither `seccomp:unconfined` nor `systempaths=unconfined`, ISSUE-381) the flag reads true, `build_bwrap_cmd` returns its argument unchanged and the closure is inert, exactly as it is for an ordinary task there. **`is_admin` is the caller's real one**, so an admin's OCR namespace also carries `{repos_dir}/{user_id}` read-write and the derived cache; passing `False` was considered and is unsafe, since `sandbox_cache_is_derived` reads `config.is_admin` itself and would derive the cache without the repos bind that buries a symlink swap — ISSUE-320 reopened. Narrowing this means narrowing both gates together. Never raises: a framework DB it cannot read costs the resource binds, not the wrap. |
| `derive_credential_set(skill_index)` | Every sensitive env-var name declared by any skill manifest. **Manifest-derived** — this replaced the hand-maintained `_PROXY_CREDENTIAL_VARS` frozenset, so adding a credential to a skill's `env:` block is the only step needed; there is no list to keep in step. |
| `derive_proxy_only_set(skill_index)` | Env vars routed to the proxy *without* credential semantics (no auto-authorization, no `credential-fetch` lookup, no per-skill scoping): the manifest `proxy_only: true` vars (`HEALTH_DB_PATH`, `LOCATION_DB_PATH`) plus `_EXECUTOR_PROXY_ONLY_VARS` (`ISTOTA_DB_PATH`, which is in no manifest — the executor sets it imperatively). These aren't secrets, so there is nothing to leak between skills; they are withheld because they name databases. |
| `derive_authorized_skills(selected_skills, skill_index, ctx, hook_env=None)` | Skills authorized for credential access this task: a skill qualifies if it was **selected**, or if **any** of its sensitive `EnvSpec`s resolves (the user has at least one of its credentials configured). `any`, not `all`, so a multi-provider skill (`developer` — GitLab *or* GitHub) authorizes when one provider is set up. Decoupled from skill selection, so a selection miss doesn't lock out a skill the user has clearly configured; the threat model is unchanged because only credentials the user supplied ever resolve. Replaced `_authorized_skills_from_credentials`. `hook_env` (the merged `dispatch_setup_env_hooks` output, which is why the executor now dispatches hooks *before* deriving authorization) is the auto-auth signal for a `source="setup_env"` credential — `_resolve_env_spec` returns `None` for that source by design, so without it such a skill can never auto-authorize: its var is sensitive, so it is stripped from Claude's env, and it is in no authorized skill's credential map, so the proxy never injects it back and the CLI runs unauthenticated. `google_workspace` was the live case — no eager selector (menu-only since keyword selection was removed), hook-sourced OAuth token, hence never authorized on any path. A hook value is per-user (derived from that user's stored token), unlike an EnvironmentFile `fallback_var`, so it is a sound signal. |
| `derive_skill_credential_map(authorized_skills, skill_index)` | Per-skill: the sensitive env vars its own manifest declares. The proxy scopes injection with it, so a skill CLI invocation only ever sees its own credentials. Replaced `_build_skill_credential_map`. |
| `derive_lookup_allowlist(authorized_skills, skill_index)` | Union of the credentials any authorized skill may fetch via `credential-fetch` — the path helper scripts use (the git credential helper, the `gh` / `glab` wrapper). Subtracts `_PROXY_LOOKUP_BLOCKED` as a hard reject (today `ISTOTA_SECRET_KEY`). Replaced `_allowed_credentials_for_skills`. |

## Skill Proxy Authorization Model

The proxy (`skill_proxy.py`) takes two distinct skill sets:
- `allowed_skills` (frozenset): all CLI skills (`cli: true`) — global whitelist used to reject typos / non-existent skill names.
- `authorized_skills` (frozenset): per-task subset returned by `derive_authorized_skills()`. Used purely for the informative-rejection error message returned to the client, and logged at proxy startup as `proxy_authorization task_id=… selected=… authorized=… …`.

The `skill_credential_map` (built from `authorized_skills` via `derive_skill_credential_map`) controls which credential env vars actually get injected for a given skill CLI invocation — that is the real enforcement boundary. Skill selection controls only which skill *docs* (eager bodies) go in the prompt; it no longer gates credential access.

Every proxy rejection emits a structured WARNING — `proxy_rejected task_id=… type=skill|credential … reason=unknown_skill|not_authorized|not_authorized_credential|credential_not_present`. Use these to count selection misses vs. real abuse attempts.

## Output Validation
| Function | Purpose |
|---|---|
| `detect_malformed_result(text, tool_count, ...)` | Validates model output for leaked tool-call XML. Strict mode (Talk): any `</parameter>`, `</invoke>`, `<thinking>` outside code fences is flagged. Lenient mode (other targets): only flags when entire output is syntax fragments (< 20 chars of real content). Malformed results are reclassified as failures and retried. |
| `_compose_full_result(result_text, execution_trace, task=None)` | Two replace-only mechanisms sharing `_last_substantial_region()`: (A) CM-aware — runs whenever `cm_boundary` events exist, returns last segment ≥ 200 chars; (B) terse-recovery — runs only on non-automated tasks with terse `result_text`, segments by `tool` + `cm_boundary`, returns last region ≥ 500 chars. Both bounded by the finality rule; both tail into `_ensure_final_answer`. See "Result composition" section above. Logs every override. |
| `_last_substantial_region(trace, delimiters, min_chars, *, trailing_only=False)` | Shared walker: groups text events into regions split by `delimiters`, returns the joined text of the last region whose length crosses `min_chars`. `trailing_only` first slices the trace at the last `tool` entry, so only the model's final message is eligible (ISSUE-211). |
| `_is_automated_task(task)`, `_is_terse(text)` | Gates for Mechanism B. Automated = source_type in `{scheduled, briefing}` or `heartbeat_silent` or `scheduled_job_id`. Terse = empty, < 150 chars, or matches short-reference regex. `_is_automated_task` additionally exempts a task from the `_ensure_final_answer` notice. |
| `_is_back_reference(text)`, `_ensure_final_answer(result_text, trace, task)` | The ISSUE-211 pair. `_is_back_reference` is the `_TERSE_REFERENCE_RE` match that licenses reaching back past a tool boundary. `_ensure_final_answer` guarantees a completed non-automated turn never delivers an empty reply or a promoted status fragment. |
| `is_no_final_answer(text)` | Public predicate: is this the composer's synthesized no-final-answer output rather than something the model wrote? Callers that *interpret* a result must check it — the scheduler's confirmation gate (a "should I proceed?" inside quoted mid-turn text is not a question awaiting an answer) and memory indexing (boilerplate from a broken turn has no recall value) both do. |

## Other Functions
| Function | Purpose |
|---|---|
| `parse_api_error()` | Extract status_code/message from error text |
| `is_transient_api_error()` | Check if error is retryable |
| `get_user_temp_dir()` | `config.temp_dir / user_id` |
| `get_task_control_dir(config, user_id, task_id)` | `{config.temp_dir}/.control/{user_id}/task_{task_id}`, with the ancestors resolved and the last component deliberately not. `None` for an empty or non-`str` `user_id`, one that escapes the root under the same containment equality `get_user_repos_dir` uses, or one casefold-equal to `.control`. `task_id` is coerced with `int()`, since `PurePath` does not collapse `..` and no caller's type is guaranteed once an id can come from a deferred-op JSON. Creates nothing, never raises |
| `ensure_task_control_dir(config, user_id, task_id)` | Creates that path, `0700` at each level with the mode re-asserted on an existing directory, each level opened `O_NOFOLLOW \| O_DIRECTORY` and refused if it is not a directory the daemon owns. Retries once from the top, because `cleanup_old_temp_files` walks `.control` too and can collect an empty level mid-`mkdir`. Raises `RuntimeError`; `execute_task` turns that into a returned failure. Idempotent, which is why `_build_module_briefing_prompt` calls it again rather than threading the path through two signatures |
| `_ensure_reply_parent_in_history()` | Force-include reply parent in context |
| `load_emissaries()` | Load constitutional principles (global only, not user-overridable) |
| `load_persona()` | Load persona (user workspace > global) |
| `load_channel_guidelines(config, source_type, user_id=None)` | Load guidelines/{source_type}.md, substituting `{BOT_NAME}`/`{BOT_DIR}`/`{user_id}`. `{user_id}` joined the set so web.md's file-handover link can name a concrete workspace path; skill bodies already substituted it. |
| `_split_credential_env()` | Split an env dict into (matched, rest). Called twice: once with the credential set, once with the proxy-only set. `proxy_base_env = {**env, **proxy_only_env}` is snapshotted *before* `ISTOTA_SANDBOXED` is added, so the host-side CLI isn't told it is sandboxed. |
| `_build_network_allowlist()` | Build host:port allowlist for CONNECT proxy |
| `build_bwrap_cmd()` | Build bubblewrap sandbox command wrapper. Binds the task's own developer subtree, `Path(developer.repos_dir) / task.user_id`, RW when it exists and the task is an admin's with the skill enabled — never the shared root, so no other admin's clones, worktrees, model-written git configs or package cache are in the namespace. Binds `resolve_sandbox_cache_dir(config, task.user_id)` RW when set, **before** the repos bind so that bind covers it (one mount, the hardlink property), before the database masks, and gated on neither admin nor the developer skill — any task running a package manager writes a cache, and without the bind that write lands on bwrap's root tmpfs (ISSUE-305). Binds the per-user exec socket **directory** `{[developer.container] exec_socket_dir}/{user_id}` RW when the derived backend is `devbox` and `"developer" in authorized_skills` — that second conjunct is the one `_build_network_allowlist` already uses to decide the package registries, so the exec socket is bound exactly where the registries are allowed. The directory rather than the socket file, because a server restart unlinks and recreates the inode; the per-user subdirectory rather than the parent, because the parent holds every user's socket. Binds **no** Docker socket and no `docker` CLI: the allowlist proxy and its bind are both gone, and unlike that proxy this bind cannot be ungated — an allowlist is safe to hand every task, an arbitrary-command channel into a permissive-egress container is not. `selected_skills` was a dead parameter and is now `authorized_skills`, which is the set that decides the bind. Takes a required keyword-only `profile` (`SandboxProfile.CLAUDE` / `.NATIVE`), which decides exactly two things and nothing else: the Claude runtime block (`~/.local/bin`, `~/.local/share/claude`, `~/.local/state/claude`, the `~/.claude` tmpfs with the credential, settings and session directories through it) and the `custom_system_prompt_path(config)` ro-bind — the file, never its directory. Both are there because the wrapped process *is* the `claude` CLI; `NATIVE` wraps istota's own code, which makes no model call from inside the namespace and reads the system prompt in the daemon. No default, so a forgotten profile is a `TypeError` rather than a silent grant of the credential (ISSUE-389). Everything else, including the ordering, is generic and identical under both. |
| `custom_system_prompt_path(config)` | `config/system-prompt.md` as an absolute path (`abspath`, not `resolve` — the bind lands at the name as written, which is also the name the CLI is handed) when `custom_system_prompt` is set, else `None`. One source for both the `BrainRequest` field and the bind. The config dir is otherwise absent from the sandbox and stays that way: it holds `config.toml`, and emissaries / persona / guidelines / skill bodies all reach the model as content the daemon read. This is one of the two files the *CLI* opens inside the namespace (the other is the task's own `{control_dir}/system_prompt.txt`, reached through the `extra_ro_binds` bind of its directory — see "The two prompt files" above) — which is why it silently depended on the `sandbox_ro_paths = ["/srv/app"]` default that also exposed the databases, and why narrowing that to `[]` made every task on a `custom_system_prompt` install exit with "System prompt file not found". Caveat: the DB masks run last, so a config dir sitting under `db_path.parent` would shadow the bind. |
| `effective_sandboxing(config)` | Whether the filesystem sandbox is actually in place: `sandbox_enabled` (what the operator asked for) **and** `_bwrap_available()` (what they got). The one name for a predicate four sites need — `native_fs_confinement_active`, `build_prompt`'s `db_masked`, the `ISTOTA_SANDBOXED` marker and the REPL `cwd` choice. Three of them spelled it out inline until ISSUE-308, two under comments calling it "effective sandboxing" with nothing of that name to point at; since one of them decides whether the prompt tells the model its databases are masked, a definition drifting between them would have the daemon making a false boundary claim. Consults the bwrap probe, which shells out once per process and caches — that is why prompt assembly touches `subprocess` at all. |
| `native_fs_confinement_active(config)` | Whether NativeBrain's in-process file tools should be path-confined — `effective_sandboxing(config)`, the same predicate the `cwd` choice uses (NB-1). |
| `native_fs_roots(config, task, is_admin, user_resources, user_temp_dir, workspace_dir=None, control_dir=None)` | The `(read_roots, write_roots, write_denied_roots)` for a native-brain task. **No longer the boundary**: the tools moved into `istota.tool_server`, one bwrap namespace per attempt, where a path outside the binds is *absent* rather than refused (ISSUE-389). What the roots still do is produce the error the model reads — "outside the allowed workspace" beats ENOENT — and on the unsandboxed shapes (macOS, standalone, Docker without the two container settings) they are once again the only confinement there is, which is why they must still mirror the binds exactly. **They are now a projection of the same `MountPlan` `build_bwrap_cmd` renders** (`sandbox_plan.project_fs_roots`) rather than a second hand-written derivation beside it, so a bind added to the plan reaches both consumers or neither — mirrors `build_bwrap_cmd`'s user-data binds (user temp dir, mount user/channel dirs RW, Talk RO, the task's own `{developer.repos_dir}/{user_id}` subtree rather than the root, per-resource). That projection is also what made ISSUE-402 a one-line fix: the mount's user directory is scoped by `user_scope.scoped_user_dir` at the join in `build_mount_plan`, so an id that names no child of `{mount}/Users` costs the bind and the write root together, and this function needs no guard of its own. Includes the per-user package-manager cache (`resolve_sandbox_cache_dir`) as a write root, mirroring the bwrap bind, which is gated on neither admin nor skill selection. **No DB root** — the admin read root went with the bwrap bind. No site/website write root — ISSUE-194 removed that primitive entirely; see `.claude/rules/config.md` under `SiteConfig`. The third element is the RO carve-outs containment cannot express. Two *named* entries, plus every read-only user-data mount nested inside an earlier read-write one, which `project_fs_roots` derives by containment rather than naming — that is what bwrap's ordering already does, and the projection used to disagree with it. The named two: `{user_temp_dir}/.developer`, nested inside a write root, matching the `--ro-bind` bwrap applies after binding its parent so the credential helpers can't be replaced; and `control_dir`, the task's own `{temp_dir}/.control/{user_id}/task_{id}`, matching the `extra_ro_binds` entry the same caller passes. `control_dir` is **also** appended to `read_only`, and it is the entry that is inside no write root — it is a sibling of `user_temp_dir`, not a child — so the read entry is what makes it openable at all under confinement, while the deny entry is what protects it when confinement is off. Both appended without an existence check, and the reason changed with the seam rather than going away: bwrap used to re-check per Bash call while this list was built once, so gating on existence left a `.developer` created mid-run writable here and read-only there. There is one bwrap build per attempt now, and both are decided at the same moment — but that moment is still *before* the developer skill's own writes on some paths, so an existence gate would reinstate the same split between what the namespace holds and what this list says about it. Threaded into `BrainRequest.fs_read_roots`/`fs_write_roots`/`fs_write_denied_roots` when confinement is active. **The control directory is not seeded here alone, and must not become so**: `execute_task` calls this function only under `native_fs_confinement_active`, so on macOS, the standalone install and the shipped Docker stack nothing here runs — and those are exactly the deployments with no bwrap re-bind behind them. The executor therefore puts `control_dir` on `fs_write_denied_roots` *outside* that branch as well, and `ToolEnv` enforces a deny root whether or not confinement is on, precisely so that seeding means something. This function returns it too, so the confined path has one list and no duplicate. Both entries name a directory and `_in_denied` compares realpaths with `is_relative_to`, so a framework file added under either later needs no new entry — which is the guard-shape change the control directory was for. The docstring records one gap this function does not close: a `user_resources` row is bounded by `nextcloud_mount_path` alone, so on a layout with `config.temp_dir` under the mount a row naming the control tree would bind it read-write. No shipped shape produces that layout, and `doctor.runtime.task_control_dir` reports it. |
| `_execute_simple()` | subprocess.run mode |
| `_execute_streaming()` | Retry wrapper for streaming |
| `execute_task_interactive()` | CLI interactive mode |

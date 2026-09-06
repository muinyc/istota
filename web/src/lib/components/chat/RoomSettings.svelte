<script lang="ts">
  import { untrack } from 'svelte';
  import type { ChatRoom, RoomPatch, SelectableBrain } from '$lib/api';
  import { Modal, Button, ConfirmDialog, Select, type SelectOption } from '$lib/components/ui';
  import { ROOM_COLORS, ROOM_COLOR_LABELS, roomColorVar } from '$lib/roomColors';
  import {
    getBaseModelChoices,
    getBrainNamespaces,
    getInheritedBrain,
    getSelectableBrains,
  } from '$lib/components/chat/autocomplete/providers';

  interface Props {
    open?: boolean;
    room: ChatRoom;
    onSave: (patch: RoomPatch) => void;
    onDelete: () => void;
    onPromote?: () => void;
    onClose: () => void;
  }

  let { open = $bindable(true), room, onSave, onDelete, onPromote, onClose }: Props = $props();

  // Model + effort defaults for this room (canonical values, shared Talk+web).
  // "" is the "instance default" sentinel (cleared on the backend as null).
  const EFFORT_OPTIONS: SelectOption[] = [
    { value: '', label: 'Default effort' },
    { value: 'low', label: 'low' },
    { value: 'medium', label: 'medium' },
    { value: 'high', label: 'high' },
    { value: 'xhigh', label: 'xhigh' },
    { value: 'max', label: 'max' },
  ];
  let modelOptions = $state<SelectOption[]>([{ value: '', label: 'Default model' }]);
  let modelValue = $state(untrack(() => room.model ?? ''));
  let effortValue = $state(untrack(() => room.effort ?? ''));

  // Base model choices (dedup + provider-alias-preferred labels) shared with
  // the room header badge, so the dropdown and the badge name a model the same.
  // Scoped to the brain that would have to run the model, so a room on another
  // model namespace is not offered ids it cannot run — which the PATCH would
  // reject anyway.
  //
  // That brain is the one **selected in this modal**, not the one the room
  // still holds (ISSUE-417). The room keeps its old brain until the save lands,
  // so keying on it offered the outgoing brain's models for a change the user
  // had already made — which is why the select used to be disabled on a
  // crossing change with "pick a new one after saving". Reading `brainValue`
  // inside the effect is what subscribes it, so switching the brain refetches.
  $effect(() => {
    // Both captured, and the result dropped if either has moved on: the fetch
    // is per room *and* per pending brain now, so a stale resolution would
    // otherwise paint one selection's aliases under another.
    const forRoom = room.id;
    const forBrain = brainValue;
    getBaseModelChoices(forRoom, forBrain || undefined).then((choices) => {
      if (room.id !== forRoom || brainValue !== forBrain) return;
      // Show the canonical model id in parens next to the alias, so the pick
      // is unambiguous (e.g. `opus (claude-opus-4-8)`).
      modelOptions = [
        { value: '', label: 'Default model' },
        ...choices.map((c) => ({ value: c.value, label: `${c.label} (${c.value})` })),
      ];
    });
  });

  // A model chosen for the previous brain cannot carry to a crossing one, and
  // the server would refuse it. Clearing it back to "Default model" as the list
  // changes is what keeps the select showing something it actually offers,
  // rather than a stale id rendered against a list that no longer holds it.
  $effect(() => {
    if (!crossesNamespace) return;
    untrack(() => {
      if (modelValue) modelValue = '';
      if (effortValue) effortValue = '';
    });
  });

  // The room's brain. The control exists only where the server offered kinds:
  // writing the pin is admin-only and the kinds are an operator allowlist, and
  // the endpoint collapses both conditions into an empty list, so emptiness is
  // the whole test rather than two the client could get out of step.
  let selectableBrains = $state<SelectableBrain[]>([]);
  let brainNamespaces = $state<Record<string, string>>({});
  let inheritedBrain = $state<SelectableBrain | null>(null);
  let brainValue = $state(untrack(() => room.brain ?? ''));
  $effect(() => {
    getSelectableBrains().then((brains) => (selectableBrains = brains));
    getBrainNamespaces().then((map) => (brainNamespaces = map));
    getInheritedBrain().then((b) => (inheritedBrain = b));
  });
  const showBrain = $derived(selectableBrains.length > 0);
  const brainOptions = $derived<SelectOption[]>([
    { value: '', label: 'Default brain' },
    ...selectableBrains.map((b) => ({ value: b.kind, label: b.label })),
  ]);
  const brainChanged = $derived(brainValue !== (room.brain ?? ''));

  // Whether saving this brain change would drop the room's model pin (D5
  // Rule 1), answered the way `commands._clear_pin_across_namespaces` answers
  // it rather than approximately.
  //
  // An empty kind is the *inherited* brain — the room pinning none on the way
  // in, or being cleared back to it on the way out — and the server resolves
  // that through `resolve_brain_kind` to a real namespace, so reading it as
  // unknown here would warn and lock on the two commonest changes there are.
  // `brain_namespaces` rather than `selectable_brains` for the same reason in
  // the other direction: a room can be pinned to a kind the operator has since
  // dropped from the allowlist, which the server still resolves and the picker
  // no longer lists. Unknown remains "not established", and never compares
  // equal, which is the direction the server takes for a kind it cannot build.
  function namespaceOf(kind: string): string | undefined {
    if (!kind) return inheritedBrain?.model_namespace;
    return brainNamespaces[kind];
  }
  const crossesNamespace = $derived.by(() => {
    if (!brainChanged) return false;
    const before = namespaceOf(room.brain ?? '');
    const after = namespaceOf(brainValue);
    return before === undefined || after === undefined || before !== after;
  });
  // There is deliberately no lock any more (ISSUE-417). The model and effort
  // selects used to be disabled while a namespace-crossing brain change was
  // pending, because the server applied `model` first and the brain change then
  // cleared it — so a model sent in the same body was written and dropped in
  // one request, and the caption told the user to come back after saving. The
  // server applies the brain first now and validates a model in the same body
  // against the brain being switched to, so both go in one save; the selects
  // stay live and the effect above repopulates them from the pending brain.

  // A room is on Talk when it originated there or has been promoted.
  const onTalk = $derived(room.origin === 'talk' || !!room.talk_token);
  // A *promoted* room keeps the control, relabelled (ISSUE-401). Its binding
  // can go stale — the Talk conversation deleted out from under it — and the
  // button is the only way back; hiding it once `talk_token` was set is what
  // made that state permanent from the app. The server decides whether
  // anything actually happens: it probes the bound conversation and refuses
  // unless Nextcloud says it is gone, so pressing this on a healthy room is
  // answered with "already connected" rather than a second Talk room.
  // Both keyed off one predicate: `origin` is optional in the type, and an
  // origin-less room with a talk_token otherwise passed canPromote while
  // failing isPromoted, mislabelling the button.
  const canPromote = $derived(room.origin !== 'talk');
  const isPromoted = $derived(canPromote && !!room.talk_token);
  // An imported (Talk-origin) room is hidden per-user, not destroyed — this
  // must match the backend's hide condition (`reg.origin == 'talk'`), NOT
  // `onTalk`: a promoted web room (origin='web' + talk_token) is still hard-
  // deleted, so it must read as a delete, not a hide.
  const isImported = $derived(room.origin === 'talk');
  let promoting = $state(false);
  async function handlePromote() {
    if (!onPromote || promoting) return;
    promoting = true;
    try {
      await onPromote();
    } finally {
      promoting = false;
    }
  }

  // Local edit state. Re-seeded whenever the modal is opened for a different
  // room so reusing one component instance across rooms never leaks state.
  let name = $state(untrack(() => room.name));
  // A stored name the palette no longer carries folds to "no colour", which is
  // what `roomColorVar` already does for the sidebar row. Seeded raw it would
  // match no radio, so the picker would read as unset while `colorChanged`
  // stayed false — a modal whose display disagrees with its own state, one
  // touch away from writing over a value with no path back to it.
  const paletteValue = (c: string | null | undefined) => (roomColorVar(c) ? c! : '');
  let colorValue = $state(untrack(() => paletteValue(room.color)));
  let showDeleteConfirm = $state(false);
  let copied = $state(false);
  let copyError = $state('');
  let lastRoomId = $state(untrack(() => room.id));

  $effect(() => {
    if (room.id !== lastRoomId) {
      lastRoomId = room.id;
      name = room.name;
      colorValue = paletteValue(room.color);
      modelValue = room.model ?? '';
      effortValue = room.effort ?? '';
      brainValue = room.brain ?? '';
      showDeleteConfirm = false;
      copied = false;
      copyError = '';
    }
  });

  const trimmed = $derived(name.trim());
  const nameChanged = $derived(trimmed.length > 0 && trimmed !== room.name);
  // Both are gated on the lock, so a model picked before the brain select was
  // touched is neither counted as a change nor sent: the server would write it
  // and clear it in the same request, which reads as the pick not having taken.
  const modelChanged = $derived(modelValue !== (room.model ?? ''));
  const effortChanged = $derived(effortValue !== (room.effort ?? ''));
  // Compared against the *folded* stored value, so a room holding a retired
  // name opens clean and its unrelated edits send no `color` key. Compared
  // against the raw one it would open dirty and a rename would silently clear
  // the stored value as a side effect. The row already renders untinted, so
  // the picker showing "no colour" is honest; picking one writes over it.
  const colorChanged = $derived(colorValue !== paletteValue(room.color));
  // Saveable when anything changed, and the name is never blanked.
  const canSave = $derived(
    trimmed.length > 0 &&
      (nameChanged || modelChanged || effortChanged || brainChanged || colorChanged),
  );

  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  async function copyToken() {
    copyError = '';
    try {
      await navigator.clipboard.writeText(room.token);
      copied = true;
      clearTimeout(copyTimer);
      copyTimer = setTimeout(() => (copied = false), 1500);
    } catch {
      copyError = 'Copy failed — select and copy manually.';
    }
  }

  function handleSave() {
    if (!canSave) return;
    // Send only what changed. A name-only rename must not re-send a model
    // the backend might now reject (e.g. one retired from the alias table),
    // which would 400 the whole PATCH; the backend leaves absent fields
    // untouched.
    const patch: RoomPatch = {};
    if (nameChanged) patch.name = trimmed;
    if (modelChanged) patch.model = modelValue || null;
    if (effortChanged) patch.effort = effortValue || null;
    if (brainChanged) patch.brain = brainValue || null;
    if (colorChanged) patch.color = colorValue || null;
    onSave(patch);
  }

  function handleOpenChange(next: boolean) {
    if (!next) onClose();
  }
</script>

<Modal bind:open title="Room settings" onOpenChange={handleOpenChange} width="380px">
  <label class="field">
    <span>Name</span>
    <input
      type="text"
      bind:value={name}
      maxlength="80"
      placeholder="Room name"
      onkeydown={(e) => {
        if (e.key === 'Enter') handleSave();
      }}
    />
  </label>

  <!-- A fixed palette rather than a colour input: one user-picked value cannot
	     read on both themes' --surface-raised, so the choice is among ours. Radios
	     rather than buttons, because that is what a single-choice set is — it
	     gives arrow-key movement and one tab stop for free (ISSUE-433). -->
  <fieldset class="field colors">
    <legend>Colour</legend>
    <div class="swatches">
      <label class="swatch none" class:selected={colorValue === ''} title="No colour">
        <input
          type="radio"
          name="room-color"
          value=""
          aria-label="No colour"
          bind:group={colorValue}
        />
        <span class="dot" aria-hidden="true"></span>
      </label>
      {#each ROOM_COLORS as c (c)}
        <label
          class="swatch"
          class:selected={colorValue === c}
          style:--swatch={roomColorVar(c)}
          title={ROOM_COLOR_LABELS[c]}
        >
          <input
            type="radio"
            name="room-color"
            value={c}
            aria-label={ROOM_COLOR_LABELS[c]}
            bind:group={colorValue}
          />
          <span class="dot" aria-hidden="true"></span>
        </label>
      {/each}
    </div>
    <p class="caption">Tints this room's row in the sidebar. Only you see it.</p>
  </fieldset>

  {#if showBrain}
    <div class="field">
      <span>Brain</span>
      <Select
        value={brainValue}
        options={brainOptions}
        onValueChange={(v) => (brainValue = v)}
        ariaLabel="Room brain"
        fullWidth
      />
      <p class="caption">
        Every turn in this room runs on this brain, and a room that names one does not fall back to
        another if it is unavailable.
      </p>
    </div>
  {/if}

  <div class="field">
    <span>Model</span>
    <Select
      value={modelValue}
      options={modelOptions}
      onValueChange={(v) => (modelValue = v)}
      ariaLabel="Room model default"
      fullWidth
    />
    {#if crossesNamespace}
      <p class="caption">
        Now listing the models the new brain can run — the one this room had reads model names
        differently and has been cleared. Pick a new one here, or leave it on
        <em>Default model</em> to use that brain's own.
      </p>
    {:else}
      <p class="caption">
        Applies to every message in this room, on both web and Nextcloud Talk. A
        <code>!model</code> prefix still overrides it for a single message.
      </p>
    {/if}
  </div>

  <div class="field">
    <span>Effort</span>
    <Select
      value={effortValue}
      options={EFFORT_OPTIONS}
      onValueChange={(v) => (effortValue = v)}
      ariaLabel="Room effort default"
      fullWidth
    />
  </div>

  <div class="field">
    <span>Room token</span>
    <div class="token-row">
      <input class="token" type="text" readonly value={room.token} />
      <button class="copy-btn" type="button" onclick={copyToken}>
        {copied ? 'Copied!' : 'Copy'}
      </button>
    </div>
    <p class="caption">Use this to link to or route output to this room.</p>
    {#if copyError}<p class="copy-error">{copyError}</p>{/if}
  </div>

  <div class="field">
    <span>Nextcloud Talk</span>
    {#if onTalk}
      <p class="caption talk-on">
        This room is also open in Nextcloud Talk — replies sync to your phone.
      </p>
    {/if}
    {#if canPromote && onPromote}
      <button class="talk-btn" type="button" disabled={promoting} onclick={handlePromote}>
        {#if promoting}
          {isPromoted ? 'Checking…' : 'Opening…'}
        {:else}
          {isPromoted ? 'Reconnect to Talk' : 'Also open in Talk'}
        {/if}
      </button>
      <p class="caption">
        {#if isPromoted}
          If the Talk conversation for this room was deleted, this creates a new one and points the
          room at it. Nothing changes while the existing conversation is still there.
        {:else}
          Creates a Nextcloud Talk conversation so this chat is reachable from the Talk apps.
        {/if}
      </p>
    {/if}
  </div>

  {#if isImported}
    <p class="caption hide-hint">
      Hiding only removes this room from your web chat list. The Nextcloud Talk conversation and its
      messages aren't deleted, and it reappears here if you post in it again.
    </p>
  {/if}

  {#snippet footer()}
    {#if isImported}
      <!-- A hide is reversible (re-engagement un-hides), so it's a one-click
			     action with no type-the-name confirm — unlike a real delete. -->
      <button class="delete-link" type="button" onclick={onDelete}> Hide </button>
    {:else}
      <button class="delete-link" type="button" onclick={() => (showDeleteConfirm = true)}>
        Delete
      </button>
    {/if}
    <Button variant="ghost" onclick={onClose}>Cancel</Button>
    <Button variant="primary" onclick={handleSave} disabled={!canSave}>Save</Button>
  {/snippet}
</Modal>

<ConfirmDialog
  bind:open={showDeleteConfirm}
  title="Delete room"
  message={`Permanently deletes "${room.name}" and all its messages. This cannot be undone.`}
  challenge={room.name}
  confirmLabel="Delete this room"
  onConfirm={onDelete}
/>

<style>
  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin-bottom: var(--space-3);
  }

  .field > span {
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  /* The palette row. A <fieldset> so the group has a real legend, styled back
	   to match the plain `.field > span` labels around it. */
  fieldset.colors {
    border: none;
    padding: 0;
    margin: 0 0 var(--space-3);
    min-width: 0;
  }

  fieldset.colors legend {
    padding: 0;
    font-size: var(--text-xs);
    color: var(--text-muted);
  }

  .swatches {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-1);
  }

  .swatch {
    position: relative;
    display: inline-flex;
    cursor: pointer;
    border-radius: var(--radius-pill);
  }

  /* The input carries the accessible name and the keyboard behaviour, so it
	   stays in the tree and is only made invisible — `display: none` would take
	   it out of the radio group's arrow-key navigation. */
  .swatch input {
    position: absolute;
    opacity: 0;
    inset: 0;
    margin: 0;
    cursor: pointer;
  }

  .swatch .dot {
    width: 1.15rem;
    height: 1.15rem;
    border-radius: var(--radius-pill);
    background: var(--swatch);
    /* Same reason as the sidebar dot: forced-colours substitutes a background,
		   which here would flatten all eight swatches to one colour and leave a
		   picker whose options are indistinguishable. The colour is the content. */
    forced-color-adjust: none;
    /* The ring is drawn outside the dot so selecting one does not resize it. */
    box-shadow: 0 0 0 2px var(--surface-card);
    transition: box-shadow var(--transition-fast);
  }

  .swatch.none .dot {
    background: transparent;
    border: 1px dashed var(--border-hover);
  }

  .swatch.selected .dot,
  .swatch input:focus-visible + .dot {
    box-shadow:
      0 0 0 2px var(--surface-card),
      0 0 0 3px var(--accent);
  }

  .field input[type='text'] {
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
  }

  .token-row {
    display: flex;
    gap: var(--space-2);
    align-items: stretch;
  }

  .token-row .token {
    flex: 1;
    font-family: var(--font-mono);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .copy-btn {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    padding: 0 var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
    white-space: nowrap;
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }
  .copy-btn:hover {
    background: var(--surface-raised);
    color: var(--text-primary);
  }

  /* Typography is the global .caption; only the <p> reset stays here. */
  p.caption {
    margin: 0.1rem 0 0;
  }

  .talk-on {
    color: var(--text-muted);
  }

  .hide-hint {
    margin: 0 0 var(--space-2);
  }

  .talk-btn {
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition:
      background var(--transition-fast),
      color var(--transition-fast);
  }
  .talk-btn:hover:not(:disabled) {
    background: var(--surface-raised);
  }
  .talk-btn:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .copy-error {
    font-size: var(--text-xs);
    color: var(--status-danger-fg);
    margin: 0.1rem 0 0;
  }

  .delete-link {
    margin-right: auto;
    background: none;
    border: none;
    color: var(--text-dim);
    font: inherit;
    font-size: var(--text-sm);
    cursor: pointer;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-pill);
    transition: color var(--transition-fast);
  }
  .delete-link:hover {
    color: var(--status-danger-fg);
  }
</style>

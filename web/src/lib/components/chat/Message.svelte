<script lang="ts">
  import { Copy, Star, Trash2, Reply, Mail, Info, Pencil, X } from 'lucide-svelte';
  import { chatFileUrl, type ExternalTurnDisplay } from '$lib/api';
  import { copyText } from '$lib/clipboard';
  import { renderMarkdown } from '$lib/markdown';
  import type { ChatMessage } from '$lib/stores/chat';
  import type { OutboundDraft } from '$lib/api';
  import { messageCopyText, renderGroups } from '$lib/stores/segments';
  import { online } from '$lib/stores/connectivity';
  import { Avatar, Button, IconButton } from '$lib/components/ui';
  import ActivityTrace from './ActivityTrace.svelte';
  import ConfirmationCard from './ConfirmationCard.svelte';
  import DraftCard from './DraftCard.svelte';
  import SearchResults from './SearchResults.svelte';

  /** One line's worth of a collapsed external turn, in characters. */
  const EXTERNAL_PREVIEW_CHARS = 160;

  let {
    message,
    continuation = false,
    userName = 'You',
    userId,
    userAvatar = null,
    botName = 'Istota',
    botAvatar = null,
    onConfirm,
    onReject,
    onToggleStar,
    onDelete,
    onReply,
    onJumpToMessage,
    onRetry,
    retryBusy = false,
    onQueueSend,
    onQueueEdit,
    onQueueRemove,
    onRoomClick,
    onJump,
    onImageOpen,
    drafts = [],
    draftActions,
    externalDisplay = 'collapsed',
    aggregate = false,
    active = false,
    touch = false,
  }: {
    message: ChatMessage;
    // True when this message continues a run from the same author, so the
    // avatar + author/time header is collapsed (Discord/Slack grouping).
    continuation?: boolean;
    userName?: string;
    // The viewer's own istota user id, which is what the avatar endpoint keys
    // on — not `userName`, which is a display handle. Absent while the server
    // has not confirmed the identity, and the row then falls back to the chip.
    userId?: string;
    // Content hashes for the two identities `/me` names, passed straight
    // through to `Avatar`: a string builds an immutable URL, `null` says there
    // is no picture and suppresses the request. `null` by default, so a caller
    // that knows nothing about avatars renders what it rendered before.
    userAvatar?: string | null;
    botName?: string;
    botAvatar?: string | null;
    onConfirm: (cid: number, taskId: number) => void;
    onReject: (cid: number, taskId: number) => void;
    // Star toggle for durable messages (rows carrying msgId). Absent → no
    // star affordance (e.g. surfaces that don't support starring).
    onToggleStar?: (cid: number) => void;
    // Delete a durable message. The handler owns the confirmation — this
    // component only offers the affordance. Absent → no delete affordance.
    onDelete?: (cid: number) => void;
    // Stage a reply citing this message. The handler owns the composer chip —
    // this component only offers the affordance. Absent → no reply affordance,
    // which is also how the aggregate panes stay read-only.
    onReply?: (cid: number) => void;
    // Follow this turn's own citation back to the message it names. Absent →
    // the quote block renders but doesn't click through.
    onJumpToMessage?: (msgId: number) => void;
    // Re-send a message whose send failed. Absent → the failure is reported
    // without an offer to retry it (read-only surfaces, aggregate views).
    onRetry?: (cid: number) => void;
    // True while the room has a turn in flight. Retry is refused then (the
    // store's `runTurn` is not re-entrant), so the button says so rather than
    // silently doing nothing.
    retryBusy?: boolean;
    // Send a message that is waiting to go out, now (ISSUE-238). Offered only
    // on a *held* row: an unheld one is going to drain on its own, and a second
    // way to fire it would race that. Absent → no Send affordance, which is how
    // read-only surfaces and the aggregate views stay read-only.
    onQueueSend?: (cid: number) => void;
    // Take a queued message back into the composer to edit it. The handler owns
    // the round trip — this component only offers the affordance.
    onQueueEdit?: (cid: number) => void;
    // Drop a queued message without sending it.
    onQueueRemove?: (cid: number) => void;
    // Aggregate views: click the message's room label to jump into that room.
    // Only rendered when both the handler and message.roomName are present.
    onRoomClick?: (token: string) => void;
    // Jump to a search result's conversation turn (room token + task id).
    // Passed to a search_results system row's cards; absent elsewhere.
    onJump?: (roomToken: string, taskId: number) => void;
    // Open an inline image in the page's lightbox: this message's images, in
    // document order, and the index of the one that was activated. Absent →
    // the images render and are not clickable, which is what a caller with no
    // `<Lightbox>` of its own gets. The renderer still announces an admitted
    // image as a button there — that markup is per-image and has no idea which
    // surface it landed on — so a surface that mounts one is the way to fix it,
    // not a flag on the renderer.
    onImageOpen?: (images: string[], index: number) => void;
    // Outbound mail this turn's task composed and the gate is holding. Placed
    // under the turn that produced it, which is where the drafted text and the
    // "this task also created a calendar event" summary are legible together.
    // A draft whose turn is not on screen — a task with no room, or one paged
    // out of view — renders in the page's own list instead, so nothing is lost
    // by this being per-turn. Empty (and the handlers absent) everywhere else.
    drafts?: OutboundDraft[];
    draftActions?: {
      approve: (id: number) => Promise<boolean> | boolean;
      discard: (id: number) => Promise<boolean> | boolean;
      edit: (id: number, body: string) => Promise<boolean> | boolean;
      refresh?: () => void;
    };
    // How much of a turn that arrived from outside the room this reader wants
    // inline. Applies to the body only — the header row and the origin marker
    // render at every setting, because a transcript holding a bot answer with
    // no question above it is what the inbound mirror was built to fix.
    externalDisplay?: ExternalTurnDisplay;
    // True in the cross-room views (All messages / Unread / Starred), where
    // the hover bar carries only the task number — model and timings are
    // room-level detail that belongs in the room view.
    aggregate?: boolean;
    // Touch surrogate for hover: the one row the user last tapped. A touch
    // device has no hover to reveal the metadata + star with, and leaning on
    // Safari's synthesized :hover left the affordances stuck on every row
    // ever tapped. The list owns this so exactly one row can be active.
    active?: boolean;
    // True once the page's last pointer was a finger. `@media (hover: hover)`
    // answers for the device, not the gesture — a touchscreen laptop or an iPad
    // with a trackpad reports hover, and a tap there still strands a synthesized
    // :hover. This mutes the hover reveal for as long as touch is what's in use.
    touch?: boolean;
  } = $props();

  const isUser = $derived(message.role === 'user');
  const isSystem = $derived(message.role === 'system');
  // A user row is not always the viewer's own words — a shared room has other
  // members, and an email mirrored into the room it continues was written by
  // whoever sent it. The server names them when it can; `userName` is the
  // fallback, and stays right for everything the viewer typed here.
  const author = $derived(isUser ? (message.author ?? userName) : botName);
  // A user row the *viewer* wrote. `message.author` is set only when the server
  // named somebody else — another room member, or the sender of a mirrored
  // email — so its absence on a user row is what identifies the viewer's own
  // words.
  //
  // Reading an *absence* as the viewer is only safe because the server refuses
  // to emit an empty one: `web_app._display_name_for` falls back to the user id
  // rather than to `''`, and says in its own docstring that it does so because
  // "an empty author would read as the viewer". The chat store folds `''` into
  // `undefined`, so without that guarantee this would paint the viewer's face
  // on somebody else's turn — a stronger claim than the wrong initial the same
  // gap used to produce.
  const ownTurn = $derived(isUser && !message.author);
  /* Whose picture the gutter asks for on a user row: the viewer's own id on
     their own turn, and the writer's on a co-member's. `undefined` on the bot
     row (the bot has no user id) and on an external sender's, whose name is an
     email address with no account behind it — `Avatar` answers a missing id
     with the chip and issues no request, which is the whole handling of that
     case. Never the viewer's id as a stand-in for a missing one: that paints
     the reader's face on somebody else's words. */
  const authorAvatarId = $derived(!isUser ? undefined : ownTurn ? userId : message.authorId);

  // System (!command) output goes through the safe markdown renderer; user text
  // is shown verbatim and the assistant body is rendered below.
  const bodyHtml = $derived(isSystem ? renderMarkdown(message.text) : '');

  // ---- External-origin turns -------------------------------------------------
  // A user row whose `origin` is set came from a surface this room does not live
  // on — today, mail mirrored into the thread it continues. Without a marker it
  // renders as an ordinary user bubble with an unfamiliar name in it: full body,
  // no provenance, nothing saying a stranger wrote it. Keyed on presence rather
  // than on a comparison, because the server only sends the field for a turn
  // that is genuinely from outside.
  const isExternal = $derived(isUser && !!message.origin);
  // The server's contract is "a surface that does not own rooms"
  // (`not surfaces.is_room_member(origin_surface)`), which resolves to email
  // today only because `TRANSCRIPT_SURFACE_FILTER` limits user rows to
  // web/talk/email. That coupling lives in two files with nothing enforcing it,
  // so the label is derived rather than hardcoded: if the filter ever widens, an
  // unfamiliar origin reads as "External message" instead of asserting an email
  // that never arrived.
  const isEmailOrigin = $derived(message.origin === 'email');
  const externalLabel = $derived(isEmailOrigin ? 'External email' : 'External message');
  // `hidden` withholds the body and nothing else. The row stays, because a bot
  // answer with no question above it was the defect the inbound mirror exists to
  // fix (ISSUE-136) — this setting is about how much of a stranger's text sits
  // in the transcript, not about whether the exchange happened.
  let bodyExpanded = $state(false);
  // The mode is tested rather than `bodyExpanded` being trusted on its own, so
  // `hidden` wins whatever the reader expanded earlier. Expanding under
  // `collapsed` and then arriving at `hidden` would otherwise leave the body on
  // screen with the toggle gone — stuck open in the one mode whose whole job is
  // to withhold it. Unreachable while the prop is only set at init, and one
  // config refresh away from not being.
  const externalBodyShown = $derived(
    !isExternal || externalDisplay === 'full' || (externalDisplay === 'collapsed' && bodyExpanded),
  );
  // Only `collapsed` offers expansion. `hidden` is a reader saying they do not
  // want the text inline at all, so a toggle there would be the setting asking
  // to be overruled on every message it applies to.
  const canExpandExternal = $derived(
    isExternal && externalDisplay === 'collapsed' && !!message.text.trim(),
  );
  // The one line a collapsed turn shows in place of the body: the first line
  // with anything on it, so a mail opening with a blank line or a quoted header
  // still previews as something. Capped so a single long paragraph — mail is
  // routinely one — cannot fill the row it is standing in for. Sliced by code
  // *point*, since a cut landing between an emoji's surrogates renders U+FFFD.
  const externalPreview = $derived.by(() => {
    if (!isExternal) return '';
    const line = message.text.split('\n').find((l) => l.trim()) ?? '';
    const chars = [...line.trim()];
    return chars.length > EXTERNAL_PREVIEW_CHARS
      ? `${chars.slice(0, EXTERNAL_PREVIEW_CHARS).join('')}…`
      : chars.join('');
  });

  // The turn's body is an ordered list of render groups (substantial prose +
  // activity chips), interleaved in the model's true block order. A substantial
  // intermediate text block — analysis the model wrote, then acted on — renders
  // as its own prominent prose group rather than vanishing into a tool-only
  // chip; short lead-in narration is dropped. The trailing text is always the
  // answer. See renderGroups for the rule.
  const groups = $derived(renderGroups(message));
  const toolCount = $derived(message.segments.filter((s) => s.kind === 'tool').length);
  // Index of the last activity group, so only the trailing chip pulses while
  // the message is still streaming.
  const lastActivityIdx = $derived.by(() => {
    for (let i = groups.length - 1; i >= 0; i--) if (groups[i].kind === 'activity') return i;
    return -1;
  });

  // Subtle per-message metadata, revealed on hover (bottom-right).
  const meta = $derived.by(() => {
    const parts: string[] = [];
    if (message.taskId) parts.push(`#${message.taskId}`);
    if (aggregate) return parts;
    // Drop a provider prefix (e.g. `anthropic/`) then a leading `claude-` for
    // a compact label; native/openrouter slugs keep their distinguishing tail.
    if (message.model) parts.push(message.model.replace(/^[^/]+\//, '').replace(/^claude-/, ''));
    if (typeof message.durationSeconds === 'number') parts.push(`${message.durationSeconds}s`);
    if (toolCount) parts.push(`${toolCount} tool${toolCount === 1 ? '' : 's'}`);
    return parts;
  });

  const time = $derived.by(() => {
    if (!message.createdAt) return '';
    const d = new Date(message.createdAt);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  });

  // Star affordance: durable messages only (msgId = the messages-store row),
  // and only when the surface passes a toggle handler.
  const starrable = $derived(typeof message.msgId === 'number' && !!onToggleStar);
  const showRoomChip = $derived(!!message.roomName && !!onRoomClick);

  // Under a finger the reveal is gated in the *markup*, not by opacity alone.
  // A control at zero opacity is still hit-testable, and the star sits at the
  // row's top-right: a tap that clipped it starred the message outright, and
  // because a tap on a button deliberately leaves the activation alone (see
  // tapActivation), the row never lit up either. What the user saw was a gold
  // star with no metadata beside it, on every row a thumb had brushed —
  // indistinguishable from the sticky-hover bug this was meant to have fixed,
  // and the tell was that only the star persisted while the metadata behaved.
  // A starred message keeps its star: that one is state, not an affordance.
  const revealed = $derived(!touch || active);
  const showMeta = $derived(revealed && meta.length > 0 && !message.streaming);
  const showStar = $derived(starrable && (revealed || !!message.starred));
  const hasActions = $derived(showStar || showMeta);

  // Copy is per *turn*, in a row under the message, alongside delete.
  //
  // It used to hang off each prose block, on the theory that you lift one
  // paragraph out of an answer rather than the whole thing. In practice the
  // opposite is true — a reply is usually taken whole — and the per-block
  // version put a second, differently-placed affordance on a surface that
  // already had one (the star), which is what made adding delete the moment to
  // collapse them. `messageCopyText` keeps the property the per-block version
  // was really protecting: activity chips are excluded, so a tool trace still
  // never reaches the clipboard.
  const copySource = $derived(messageCopyText(message));
  // Withheld while streaming: copying a half-written turn hands back half an
  // answer, and the row would sit under text that is still moving.
  const showCopy = $derived(!message.streaming && !!copySource.trim());
  // Delete needs a durable row — a live placeholder isn't stored yet — and a
  // handler willing to confirm it.
  const showDelete = $derived(typeof message.msgId === 'number' && !!onDelete);
  // Star appears twice on a turn, and the two are not redundant. The hover bar's
  // is the one that *persists* at rest on a starred row, which is what makes a
  // starred message legible without hovering it; this one is where the hand
  // already is once the row's actions are open, next to the other two things you
  // do to a whole turn. Same condition as the bar's, so they can't disagree
  // about whether the turn is starrable.
  const showRowStar = $derived(starrable);
  // Reply needs a durable id to cite — the same rule star and delete follow,
  // which correctly withholds it from optimistic rows and in-flight
  // placeholders — and somewhere to stage into. The aggregate panes have no
  // composer, so a staged reply there would have nowhere to go.
  const showReply = $derived(typeof message.msgId === 'number' && !!onReply && !aggregate);
  // The parent this turn cites. `deleted` is truthy-tested, never compared to
  // false: a citation staged in the composer carries no flag at all, and
  // treating absence as deleted would render every fresh reply muted.
  const cited = $derived(message.replyTo);
  const citedDeleted = $derived(!!cited?.deleted);
  const citedLabel = $derived(
    cited?.role === 'user' ? userName : cited?.role === 'assistant' ? botName : '',
  );
  const citedClickable = $derived(!!cited && !citedDeleted && !!onJumpToMessage);
  // ---- Send lifecycle (ISSUE-200) -------------------------------------------
  // A send that failed reports on the message that failed, not on an assistant
  // placeholder standing in for a reply that was never attempted.
  const sendFailed = $derived(message.sendState === 'failed');
  // Truthful state is set the moment the row exists; the store's grace timer
  // opens `showSending` only once the send is slow enough to be worth saying.
  const sendPending = $derived(message.sendState === 'sending' && !!message.showSending);
  // `retryable` is false where a retry would fail identically (an expired
  // session), and an offer that cannot work is worse than no offer.
  const showRetry = $derived(sendFailed && message.retryable !== false && !!onRetry);
  // ---- The send queue (ISSUE-238) -------------------------------------------
  // A message typed into a busy room: written and committed to, never POSTed.
  // Distinct from the *assistant* placeholder's `Queued…` progress line, which
  // means the opposite — POSTed already, waiting for its stream.
  const sendQueued = $derived(message.sendState === 'queued');
  // Held means the turn this was written against ended abnormally (Stop, an
  // error, a parked confirmation), so it will not drain on its own and the
  // user has to say so.
  const queueHeld = $derived(sendQueued && !!message.queueHeld);
  // Send is a held-row affordance only. An unheld entry drains by itself when
  // the running turn settles, and a button that fired it early would race that
  // drain for the one slot `runTurn` owns.
  const showQueueSend = $derived(queueHeld && !!onQueueSend);
  // What the row is waiting *for*, which for an unheld entry is the whole
  // difference between the two waits (ISSUE-202): a turn that will finish on
  // its own, or a connection that may not come back for a while. A held row
  // reads the same either way — what it is waiting for is the user.
  //
  // Both halves are needed and neither is enough. The reason is why the entry
  // is here; `online` is whether that is still what it is waiting on — once
  // the connection is back, the second and third of an offline batch are
  // waiting on the turn ahead of them, and pointing at a banner that is no
  // longer on screen would be the one reading the row cannot recover from.
  const queueWaitText = $derived(
    queueHeld
      ? 'Held — not sent'
      : message.queueReason === 'offline' && !$online
        ? 'Waiting for a connection'
        : 'Waiting to send',
  );

  // The row is in the layout whenever any of the three could be there, so
  // revealing it never reflows the transcript under the pointer. Withheld
  // entirely from a failed send: all three act on a durable turn, and this one
  // never became one — star and delete have no `msgId` to work with, and a lone
  // copy button would compete with the Retry that is the actual next move. A
  // queued row is withheld for the same reason and one more: it is not a turn
  // *yet*, so every one of them would be an action on something that has not
  // happened, competing with the Send / Edit / Remove that have.
  const hasRowActions = $derived(
    (showCopy || showRowStar || showReply || showDelete) && !sendFailed && !sendQueued,
  );

  // ---- Inline images ---------------------------------------------------------
  // A body renders through `{@html}`, so an inline image is markup the markdown
  // renderer wrote rather than an element this component can put a handler on.
  // The content column listens for the whole message instead, which is also
  // what makes the gallery message-scoped: `currentTarget` is the message, so
  // one click cannot page through a room's history.
  //
  // The keyboard half depends on the renderer emitting `role="button"` and
  // `tabindex="0"` on an image it admitted — there is nothing here that could
  // make a string of html focusable.

  /**
   * The image an event should open, or `null` if this event is not one.
   *
   * An image inside a link is refused, and that is the one refusal here that is
   * load-bearing rather than tidy: the anchor navigates, so opening a lightbox
   * over it means both happen at once. The renderer already withholds the
   * button affordance from that shape and marks it `md-image-linked` for the
   * cursor, but this asks `closest('a')` rather than reading that class,
   * because the question is whether an anchor is going to fire — a fact about
   * the DOM, true whatever the renderer labelled it.
   */
  function eligibleImage(target: EventTarget | null): HTMLImageElement | null {
    if (!(target instanceof HTMLImageElement)) return null;
    if (!target.classList.contains('md-image')) return null;
    if (target.closest('a')) return null;
    return target;
  }

  /**
   * Hand the message's images to the page's lightbox, positioned on this one.
   *
   * The list is the openable images only — a linked one is excluded on the same
   * grounds it is not clickable, which also keeps the index the caller gets
   * aligned with the list it gets.
   *
   * `getAttribute` rather than `img.src`: the lightbox draws into the same
   * document, so a relative URL resolves identically there, and the attribute
   * is the string the renderer wrote rather than one the browser rewrote.
   */
  function openImage(img: HTMLImageElement, scope: HTMLElement) {
    const images = [...scope.querySelectorAll<HTMLImageElement>('img.md-image')].filter(
      (el) => !el.closest('a'),
    );
    const index = images.indexOf(img);
    if (index < 0) return;
    onImageOpen?.(
      images.map((el) => el.getAttribute('src') ?? ''),
      index,
    );
  }

  function imageClick(e: MouseEvent & { currentTarget: HTMLElement }) {
    if (!onImageOpen) return;
    const img = eligibleImage(e.target);
    if (img) openImage(img, e.currentTarget);
  }

  function imageKeydown(e: KeyboardEvent & { currentTarget: HTMLElement }) {
    if (!onImageOpen) return;
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const img = eligibleImage(e.target);
    if (!img) return;
    // Space scrolls the transcript, and the image is announced as a button.
    // Claimed only once there is something to open, so an image with no handler
    // behind it keeps the page's own behaviour rather than swallowing the key.
    e.preventDefault();
    openImage(img, e.currentTarget);
  }
</script>

<!-- Turn-level actions, left-aligned under the message body. In the flow
     rather than absolutely positioned: it sits below the content it acts on,
     so it needs to take the space it occupies — the old per-block button was
     absolute because it overlapped the block's own bottom padding. `revealed`
     gates opacity only; the row keeps its box either way, so nothing shifts
     when it appears. -->
{#snippet turnActions()}
  <div class="turn-actions" class:revealed>
    {#if showCopy}
      <button
        class="turn-action"
        onclick={(e) => {
          void copyText(copySource, { label: 'Copied' });
          // Same reason as the star: a pointer click leaves the button
          // focused, and a focus ring that also reveals it is a second way for
          // it to sit there lit after the user has moved on. A keyboard
          // activation reports detail 0, so this can't take focus away from
          // keyboard use.
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label="Copy message"
        title="Copy"
        type="button"
      >
        <Copy size={15} />
      </button>
    {/if}
    {#if showRowStar}
      <!-- Between copy and delete: the row then reads left to right in
           ascending consequence, and the destructive button ends up at the end
           of the row rather than immediately beside the benign one. -->
      <button
        class="turn-action star"
        class:starred={message.starred}
        onclick={(e) => {
          onToggleStar?.(message.cid);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label={message.starred ? 'Unstar message' : 'Star message'}
        aria-pressed={message.starred ? 'true' : 'false'}
        title={message.starred ? 'Unstar' : 'Star'}
        type="button"
      >
        <Star size={15} fill={message.starred ? 'currentColor' : 'none'} />
      </button>
    {/if}
    {#if showReply}
      <!-- After star, before delete. The row reads left to right in ascending
           consequence: reply stages a new message, which is more than a
           private mark and less than a destructive removal — and keeping
           delete last leaves it terminal. -->
      <button
        class="turn-action"
        onclick={(e) => {
          onReply?.(message.cid);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label="Reply to message"
        title="Reply"
        type="button"
      >
        <Reply size={15} />
      </button>
    {/if}
    {#if showDelete}
      <button
        class="turn-action danger"
        onclick={(e) => {
          onDelete?.(message.cid);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        aria-label="Delete message"
        title="Delete"
        type="button"
      >
        <Trash2 size={15} />
      </button>
    {/if}
  </div>
{/snippet}

<!-- The citation, above the body it belongs to. Rendered from the durable
     store, so it survives a reload, a room switch and the retention sweep that
     deletes the task. A live parent clicks through; a deleted one says so and
     stays inert — the deletion is a fact about the conversation, and dropping
     the citation would rewrite it. -->
{#snippet replyQuote()}
  {#if cited}
    {#if citedClickable}
      <button
        class="reply-quote"
        class:under-meta={!continuation}
        onclick={(e) => {
          onJumpToMessage?.(cited.msgId);
          if (e.detail > 0) e.currentTarget.blur();
        }}
        title="Go to the message this replies to"
        type="button"
      >
        {#if citedLabel}<span class="reply-quote-author">{citedLabel}</span>{/if}
        <span class="reply-quote-text">{cited.excerpt ?? ''}</span>
      </button>
    {:else}
      <div class="reply-quote" class:under-meta={!continuation} class:deleted={citedDeleted}>
        {#if citedDeleted}
          <span class="reply-quote-text">Original message deleted</span>
        {:else}
          {#if citedLabel}<span class="reply-quote-author">{citedLabel}</span>{/if}
          <span class="reply-quote-text">{cited.excerpt ?? ''}</span>
        {/if}
      </div>
    {/if}
  {/if}
{/snippet}

{#snippet starButton()}
  <button
    class="star-btn"
    class:starred={message.starred}
    onclick={(e) => {
      onToggleStar?.(message.cid);
      // A pointer-driven click leaves the button focused, and a focus ring that
      // also reveals the icon is a second way for a star to sit there lit after
      // the user has moved on (Safari has shipped :focus-visible on tap).
      // `detail > 0` is the pointer's signature — a keyboard activation reports
      // 0, so this can't take focus away from keyboard use.
      if (e.detail > 0) e.currentTarget.blur();
    }}
    aria-label={message.starred ? 'Unstar message' : 'Star message'}
    aria-pressed={message.starred ? 'true' : 'false'}
    title={message.starred ? 'Unstar' : 'Star'}
    type="button"
  >
    <Star size={14} fill={message.starred ? 'currentColor' : 'none'} />
  </button>
{/snippet}

<!-- Per-message metadata + actions: task id / model / duration / tool count,
     then the star. Rendered as the trailing member of the author header on a
     fresh group (so its text baseline-aligns with the timestamp for free), and
     absolutely positioned on a continuation row, which has no header. -->
{#snippet actionsBar()}
  <div class="msg-actions">
    {#if showMeta}
      <span class="meta-footer">{meta.join(' · ')}</span>
    {/if}
    {#if showStar}
      {@render starButton()}
    {/if}
  </div>
{/snippet}

{#if isSystem}
  <!-- Command (!…) output / delivered notifications. Left-aligned block, not a
	     centered notice: it carries lists / code / tables that must read
	     left-to-right. Durable system rows (msgId) are starrable too.

	     It rides the same gutter + content columns as a turn, so its card starts
	     where every message body starts instead of spanning the avatar column as
	     well — a notice that hangs a card into the gutter reads as a different
	     kind of surface rather than as part of the conversation. What the gutter
	     holds is a mark, not an avatar: a notice has no author, so the initial
	     and the author/time header both say something false about it, and the
	     mark is what stands in for them while keeping the column occupied. -->
  <div
    class="cmd-row"
    class:active
    class:touch
    data-cid={message.cid}
    data-task-id={message.taskId ?? undefined}
  >
    <div class="gutter">
      <span class="sys-mark" aria-hidden="true"><Info /></span>
    </div>

    <!-- The image delegation sits on the column rather than on the body inside
         it, so it covers every rendered block in the row at once. A command's
         output goes through the same markdown renderer as an answer, so an
         image can appear here too, and an admitted one announces itself as a
         button whichever row it is in.

         The column takes no role of its own, and the suppression is for that
         rather than around it: the interactive elements here are the images,
         which the renderer already marks `role="button"` and `tabindex="0"`.
         This is where their events are listened for, not what they are. -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="content" onclick={imageClick} onkeydown={imageKeydown}>
      {#if showRoomChip}
        <button class="room-chip" onclick={() => onRoomClick?.(message.roomToken!)} type="button">
          {message.roomName}
        </button>
      {/if}
      {#if message.searchResults}
        <SearchResults data={message.searchResults} {onJump} />
      {:else}
        <div class="cmd-output markdown" class:error={message.error}>
          {@html bodyHtml}
        </div>
      {/if}

      <!-- The same row a turn gets, in the same place: under the body, inside
           the content column, so it lines up with the card above it. It was
           withheld here for a while on the reading that a notice is not a
           turn, but every button does something the server already supports
           on a `role='system'` row — the body is markdown worth copying, the
           delete endpoint takes its id, and a reply cites it through the
           snapshot path that already names a system parent as one of its
           cases. Replying to an alert to ask what caused it is the ordinary
           thing to want, not a category error.

           A search-results row is still carved out: it renders cards rather
           than markdown, so there is no source worth copying. -->
      {#if hasRowActions && !message.searchResults}
        {@render turnActions()}
      {/if}
    </div>
    {#if showStar}
      <div class="msg-actions cmd-actions">
        {@render starButton()}
      </div>
    {/if}
  </div>
{:else}
  <div
    class="msg"
    class:continuation
    class:active
    class:touch
    class:error={message.error}
    class:queued={sendQueued}
    data-cid={message.cid}
    data-task-id={message.taskId ?? undefined}
  >
    <div class="gutter">
      {#if !continuation}
        <Avatar
          kind={isUser ? 'user' : 'bot'}
          userId={authorAvatarId}
          version={isUser ? (ownTurn ? userAvatar : undefined) : botAvatar}
          label={author}
        />
      {:else if revealed}
        <time class="hover-time">{time}</time>
      {/if}
    </div>

    <!-- See the note on the command row's column: one delegation point per
         message, which is also what scopes the gallery to this message, and
         the same reason it carries no role. -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="content" onclick={imageClick} onkeydown={imageKeydown}>
      {#if !continuation}
        <div class="meta">
          <span class="author" class:bot={!isUser}>{author}</span>
          {#if time}<time class="stamp">{time}</time>{/if}
          {#if showRoomChip}
            <button
              class="room-chip"
              onclick={() => onRoomClick?.(message.roomToken!)}
              type="button"
              title="Go to room"
            >
              {message.roomName}
            </button>
          {/if}
          {#if hasActions}
            {@render actionsBar()}
          {/if}
        </div>
      {/if}

      {@render replyQuote()}

      {#if isUser}
        {#if isExternal}
          <!-- Provenance first, body second. The header renders at every
               setting: it is what says a message arrived from outside and who
               sent it, and withholding that is what made a stranger's mail read
               as the reader's own words. Subject and preview are text nodes —
               both are attacker-supplied, and the address in the author header
               above is sanitized at write time. -->
          <div class="external">
            <div class="external-head">
              <span class="external-mark" aria-hidden="true"><Mail size={13} /></span>
              <span class="external-label">{externalLabel}</span>
              {#if message.subject}
                <span class="external-subject">{message.subject}</span>
              {/if}
              {#if canExpandExternal}
                <button
                  class="external-toggle"
                  onclick={(e) => {
                    bodyExpanded = !bodyExpanded;
                    if (e.detail > 0) e.currentTarget.blur();
                  }}
                  aria-expanded={bodyExpanded}
                  type="button"
                >
                  {bodyExpanded ? 'Hide' : 'Show'}
                </button>
              {/if}
            </div>
            {#if externalBodyShown}
              {#if message.text}
                <div class="body user-body">
                  <span class="user-text">{message.text}</span>
                </div>
              {/if}
            {:else if externalDisplay === 'collapsed' && externalPreview}
              <div class="external-preview">{externalPreview}</div>
            {/if}
          </div>
        {:else if message.text}
          <!-- The text carries `pre-wrap`, so it needs its own element: with
               the whitespace rule on the wrapper, the newlines and indentation
               around a sibling button would render as leading and trailing
               blank space in every user message. -->
          <div class="body user-body">
            <span class="user-text">{message.text}</span>
          </div>
        {/if}
        {#if message.attachments?.length}
          <div class="attachments">
            {#each message.attachments as name, i}
              {@const href = message.attachmentPaths?.[i]}
              <!-- A chip is a link only when the file endpoint can serve it to
							     this user (their own workspace). Anything else — a co-member's
							     upload, a deployment with no local workspace — stays the inert
							     label it was, rather than becoming a link that 403s. -->
              {#if href}
                <a class="attachment attachment-link" href={chatFileUrl(href)} download={name}>
                  📎 {name}
                </a>
              {:else}
                <span class="attachment">📎 {name}</span>
              {/if}
            {/each}
          </div>
        {/if}
        <!-- The send's own state, on the message it belongs to. A failure here
             used to be written into the assistant placeholder, which read as
             "the reply failed" rather than "your message never left".

             The queued branch (ISSUE-238) leaves the body above it rendering in
             full — text, chips and the optimistic quote — because the point of
             that row is that the user can see what they committed to sending;
             the line and its buttons say it has not left yet. -->
        {#if sendPending}
          <div class="progress send-pending">
            <span class="dot"></span>
            <span class="status-text">Sending…</span>
          </div>
        {:else if sendFailed}
          <div class="send-failed">
            <span class="send-failed-text">{message.sendError || 'Couldn’t send.'}</span>
            {#if showRetry}
              <Button
                variant="subtle"
                size="sm"
                disabled={retryBusy}
                title={retryBusy ? 'Wait for the current turn to finish' : undefined}
                onclick={() => onRetry?.(message.cid)}
              >
                Retry
              </Button>
            {/if}
          </div>
        {:else if sendQueued}
          <div class="send-queued">
            <span class="send-queued-text">{queueWaitText}</span>
            {#if showQueueSend}
              <Button variant="subtle" size="sm" onclick={() => onQueueSend?.(message.cid)}>
                Send
              </Button>
            {/if}
            <!-- Edit and Remove are icons; Send is not. The two icons are what
                 you do *to* the entry and they read from their glyphs, so the
                 line stops being three competing words after a status. Send is
                 the one that acts on the world — it only appears on a held row,
                 where it is the move being offered — so it keeps its word.
                 `sm` on both, so the icons take the same box as that Button. -->
            {#if onQueueEdit}
              <IconButton
                size="sm"
                label="Edit queued message"
                title="Edit"
                onclick={() => onQueueEdit?.(message.cid)}
              >
                <Pencil size={15} />
              </IconButton>
            {/if}
            {#if onQueueRemove}
              <!-- `danger`, and an X rather than the turn row's Trash2: the two
                   are different acts. Delete removes a turn everyone can see;
                   this discards something that never went out. -->
              <IconButton
                size="sm"
                danger
                label="Remove queued message"
                title="Remove"
                onclick={() => onQueueRemove?.(message.cid)}
              >
                <X size={15} />
              </IconButton>
            {/if}
          </div>
        {/if}
      {:else}
        <!-- The turn renders as ordered groups: substantial prose blocks
				     (prominent markdown) interleaved with activity chips (tool runs
				     fold into one chip each). Short lead-in narration and reasoning
				     are dropped — the pre-tool work phase is the cue below. -->
        {#each groups as g, gi (g.id)}
          {#if g.kind === 'activity'}
            <!-- A chip sandwiched between paragraphs needs room to breathe;
						     the first group sits tight under the meta, like a no-tool
						     text answer. Spacing is neighbour-aware (chips never abut —
						     tool runs coalesce — so a chip's neighbours are prose, a run
						     notice, or the message edge). -->
            <div
              class="chip-slot"
              class:gap-above={groups[gi - 1] && groups[gi - 1].kind !== 'activity'}
              class:gap-below={groups[gi + 1] && groups[gi + 1].kind !== 'activity'}
            >
              <ActivityTrace
                steps={g.steps}
                streaming={message.streaming && gi === lastActivityIdx}
              />
            </div>
          {:else if g.kind === 'notice'}
            <!-- A notice about the run itself (ISSUE-278: the primary brain
					       failed and the answer below it came from elsewhere). The
					       tinted box is the shared `.banner warn` primitive; `.run-notice`
					       adds only what is specific to sitting in a transcript. Rendered
					       through the markdown pipeline because the executor's sentence
					       backticks the brain and model names. Not `.body` — that owns the
					       answer's type size and colour, both of which the notice overrides.
					       `role="status"` marks what the element is; note that a live
					       region inserted already-populated is not reliably announced, so
					       this is semantics rather than a guarantee of an announcement. -->
            <div class="markdown banner warn run-notice" role="status">
              {@html renderMarkdown(g.text)}
            </div>
          {:else}
            <div class="body markdown">
              {@html renderMarkdown(g.text)}
            </div>
          {/if}
        {/each}

        {#if message.streaming && groups.every((g) => g.kind === 'notice')}
          <!-- Work-phase cue: the ack verb + pulsing dot, shown while the
					     model reasons / before the first tool or answer text.
					     A `notice` group does NOT count as content here (ISSUE-278):
					     it is a static sentence, and a brain fallback emits one and
					     then runs for as long as the fallback takes. Counting it
					     would retire the only live cue in the turn at the exact
					     moment the wait gets longest. `every` on an empty array is
					     true, so the no-groups case is unchanged. -->
          <div class="progress">
            <span class="dot"></span>
            <span class="status-text">{message.progress || 'Thinking…'}</span>
          </div>
        {/if}
      {/if}

      {#if message.confirmation && message.taskId}
        <ConfirmationCard
          onConfirm={() => onConfirm(message.cid, message.taskId!)}
          onReject={() => onReject(message.cid, message.taskId!)}
          {botName}
          {botAvatar}
        />
      {/if}

      {#if draftActions}
        {#each drafts as draft (draft.id)}
          <DraftCard
            {draft}
            onApprove={draftActions.approve}
            onDiscard={draftActions.discard}
            onEdit={draftActions.edit}
            onNeedsFullRow={draftActions.refresh}
          />
        {/each}
      {/if}

      {#if hasRowActions}
        {@render turnActions()}
      {/if}
    </div>

    <!-- A continuation row has no author header to hang the bar off, so it
			     floats at the top-right, lined up with the gutter's hover time. -->
    {#if continuation && hasActions}
      {@render actionsBar()}
    {/if}
  </div>
{/if}

<style>
  /* Discord/Slack-style row: avatar gutter on the left, author + time header,
	   then the message body. Consecutive messages from the same author collapse
	   into one visual group (the `.continuation` rows hide the header). */
  .msg {
    display: flex;
    /* Tokenised because on mobile it is load-bearing: the row's inline padding,
		   the gutter and this gap together decide where the message text starts, and
		   that has to match the headings above it. See app.css. */
    gap: var(--chat-avatar-gap);
    /* Extra bottom padding so the hover highlight isn't flush with the last
		   line of text. */
    padding: 0.1rem var(--chat-row-inline) var(--space-2);
    align-items: flex-start;
    /* Anchor for the absolutely-positioned .meta-footer (top-right). */
    position: relative;
  }
  /* A fresh author group separates itself with padding alone. It also carried a
	   `--space-3` top margin, which stacked with this row's own bottom padding and
	   the previous row's — three sources of gap for one boundary, and by far the
	   largest. Padding rather than margin is what the row wants anyway: the hover
	   highlight spans the padding box, so gap expressed as margin is a dead strip
	   between two rows that neither one lights up. */
  .msg:not(.continuation) {
    padding-top: var(--space-2);
  }
  /* Reveal rules. With a real pointer the row's own :hover drives them; under a
	   finger the list marks a single `.active` row instead. Splitting them
	   matters: iOS Safari synthesizes :hover on tap and clears it only when a
	   later tap displaces it, so an unguarded :hover left a star showing on every
	   row the user had ever tapped. `.active` is the touch surrogate and is
	   inherently single. Two guards, because they fail on different devices — the
	   media query knows a phone has no hover at all, `.touch` knows a finger was
	   used on a device that also has a mouse. */
  @media (hover: hover) {
    .msg:not(.touch):hover .hover-time,
    .msg:not(.touch):hover .meta-footer {
      opacity: 1;
    }
  }
  .msg.active .hover-time,
  .msg.active .meta-footer {
    opacity: 1;
  }

  /* Per-message actions bar: hover metadata + the star toggle. One bar so the
		   two hover surfaces can't collide. Where it sits depends on whether the row
		   has an author header, because it must line up with that row's timestamp —
		   and the timestamp lives in two different places. */
  .msg-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }
  /* Fresh group: the bar is the trailing member of the .meta header, so the
		   shared `align-items: baseline` puts its text on the timestamp's baseline by
		   construction. A hand-tuned offset can't do that — it has to hold across font
		   metrics that differ per platform, and it drifted on iOS Safari. */
  .meta .msg-actions {
    margin-left: auto;
    align-self: baseline;
    /* Yield to the author/time rather than pushing them out of the row. */
    min-width: 0;
  }
  /* The star is an icon button with no text baseline of its own; centre it on
		   the bar instead of letting it hang off the synthesized one. */
  .meta .msg-actions .star-btn {
    align-self: center;
  }
  /* Continuation: no header, so float it top-right against the gutter's
		   .hover-time. `top` matches the gutter's own padding, and the two share a
		   font-size + line-height (below), so their line boxes — and therefore their
		   baselines — coincide without a magic offset. */
  .msg.continuation .msg-actions {
    position: absolute;
    right: var(--chat-row-inline);
    top: 0.1rem;
  }

  /* Subtle per-message metadata, revealed on hover (child of the actions bar). */
  .meta-footer {
    font-size: var(--text-xs);
    line-height: 1.6;
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    /* A narrow row trims the tail (tool count, then duration) rather than
			   squeezing the author name — the id and model are the identifying bits. */
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0;
    transition: opacity var(--transition-fast);
  }

  /* Star toggle: hidden at rest, revealed on row hover (or tap-activation on
	   touch) / keyboard focus; a starred message keeps it visible (filled, gold)
	   like the feeds cards.

	   The fade is a pointer-device affordance only — see the `.touch` rule at the
	   end of this block for why it is switched off under a finger. */
  .star-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    padding: 0.1rem;
    color: var(--text-dim);
    cursor: pointer;
    opacity: 0;
    /* Hidden *and* inert. Opacity alone leaves the button hit-testable, so a
		   tap landing on the invisible star starred the message with nothing on
		   screen to explain it. The markup gate above covers the touch path once a
		   finger has been seen; this covers the frame before that and any pointer
		   device where the row renders the button unrevealed. Keyboard focus is
		   unaffected — pointer-events does not gate Tab — and :focus-visible below
		   hands interactivity back. */
    pointer-events: none;
    transition:
      opacity var(--transition-fast),
      color var(--transition-fast);
  }
  @media (hover: hover) {
    .msg:not(.touch):hover .star-btn,
    .cmd-row:not(.touch):hover .star-btn {
      opacity: 1;
      pointer-events: auto;
    }
    .msg:not(.touch) .star-btn:hover,
    .cmd-row:not(.touch) .star-btn:hover {
      color: var(--accent-amber);
    }
  }
  .msg.active .star-btn,
  .cmd-row.active .star-btn,
  .star-btn:focus-visible,
  .star-btn.starred {
    opacity: 1;
    pointer-events: auto;
  }
  .star-btn.starred {
    color: var(--accent-amber);
  }

  /* Under a finger the reveal is a swap, not a fade — for both affordances, so
	   there is one rule rather than a fade here and an on/off there.

	   Not cosmetic. An opacity transition is what asks the compositor to promote
	   an element to its own layer, and a promoted layer whose opacity returns to
	   0 without being repainted keeps showing what it last painted: the star
	   stranded on every row a thumb had tapped, while the plain text span beside
	   it — never promoted — cleared correctly. That asymmetry is what identified
	   the mechanism. The markup gate above does not on its own avoid this: the
	   node is inserted and the row's .active class lands in an order that leaves
	   a style change to animate, so a transition really does run on the touch
	   path (measured: one frame after a tap, opacity 0 with a transition in
	   flight). Keyed on .touch and not a width breakpoint, because the axis is
	   what the user's hand is doing — an iPad is wide and touch, a narrow
	   desktop window is neither. */
  .msg.touch .star-btn,
  .cmd-row.touch .star-btn,
  .msg.touch .meta-footer,
  .msg.touch .hover-time,
  .msg.touch .turn-actions,
  .cmd-row.touch .turn-actions {
    transition: none;
  }

  /* Turn-level action row: copy + delete, left-aligned under the message body.
	   One row per turn rather than a button per block — see the script block for
	   why that moved.

	   Unlike the star, this is *in the flow*: it sits below the content it acts
	   on, so it has to take the space it occupies or revealing it would push the
	   next message down. The row is therefore always in the layout and only its
	   opacity is gated, which is also what lets the buttons be bare icons with
	   no background — they never overlap text.

	   `pointer-events` follows opacity for the same reason the star's does: a
	   control at zero opacity is still hit-testable, and a delete button a thumb
	   can hit without seeing is the worst version of that bug. */
  .turn-actions {
    display: flex;
    align-items: center;
    gap: var(--space-1);
    margin-top: var(--space-1);
    opacity: 0;
    pointer-events: none;
    transition: opacity var(--transition-fast);
  }
  /* Two guards, as everywhere else in this file: the media query knows a phone
	   has no hover at all, `.touch` knows a finger was used on a device that also
	   reports one. `.revealed` is the touch surrogate the row already computes. */
  @media (hover: hover) {
    .msg:not(.touch):hover .turn-actions,
    .cmd-row:not(.touch):hover .turn-actions {
      opacity: 1;
      pointer-events: auto;
    }
  }
  .msg.active .turn-actions.revealed,
  .cmd-row.active .turn-actions.revealed,
  .turn-actions:focus-within {
    opacity: 1;
    pointer-events: auto;
  }
  .turn-action {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-1);
    background: none;
    border: none;
    border-radius: var(--radius-sm);
    color: var(--text-dim);
    font: inherit;
    cursor: pointer;
    transition:
      color var(--transition-fast),
      background var(--transition-fast);
  }
  .turn-action:hover {
    color: var(--text-primary);
    background: var(--surface-raised);
  }
  .turn-action.danger:hover {
    color: var(--status-danger-fg);
  }
  /* The starred colour is state, so it holds without hover — but the row it
	   sits in is itself revealed on hover, so this never shows on a resting row.
	   The hover-bar star is the one that persists at rest; see the script block. */
  .turn-action.star.starred,
  .turn-action.star:hover {
    color: var(--accent-amber);
  }
  /* Touch targets, as an out-of-flow overlay so reaching them costs the row no
	   height (SidebarToggle's device).

	   The full 44px is only taken vertically. Horizontally the overlay is the
	   button plus one gap, so two adjacent overlays meet exactly at the gap's
	   midpoint: a tap in the seam resolves to the side it actually fell on,
	   rather than to whichever won the stacking order. Two 44px-wide overlays
	   would need ~21px between these buttons to stay apart, which is far wider
	   than two adjacent icons should sit — so the width is what gives, and it is
	   derived from the gap rather than restated, or tightening one would silently
	   reintroduce the overlap. */
  @media (max-width: 768px) {
    .turn-actions {
      --turn-action-gap: var(--space-2);
      gap: var(--turn-action-gap);
    }
    .turn-action {
      position: relative;
    }
    .turn-action::before {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: calc(100% + var(--turn-action-gap));
      height: 44px;
      transform: translate(-50%, -50%);
    }
  }

  /* The citation, above the body it belongs to. A quiet card with a leading
	   rule, so it reads as something quoted rather than as part of the message.
	   One rule set for both the clickable <button> and the inert <div>, since
	   the two differ only in whether they respond.

	   Geometry is the activity chip's, because the quote sits in the same slot —
	   a block between the author header and the turn's content. So: the body's
	   width cap, and `gap-below`'s margin, the block beneath being prose on
	   every path but an attachment-only turn, where it is the chips. */
  .reply-quote {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    width: 100%;
    max-width: var(--chat-body-max);
    margin-bottom: var(--space-3);
    padding: var(--space-1) var(--space-2);
    background: var(--surface-card);
    border: none;
    border-left: 2px solid var(--border-default);
    border-radius: var(--radius-sm);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1.4;
    text-align: left;
    cursor: pointer;
  }
  /* Under the author header, the same half gap `.meta + .chip-slot` takes:
	   flush reads cramped against the header, a full paragraph gap reads
	   detached. On a continuation row there is no header and the base rule's
	   flush top is right, exactly as it is for a tool-first chip.

	   Written as a class on the element rather than as `.meta + .reply-quote`,
	   because the condition is `!continuation` — the same variable that decides
	   whether the header renders at all — and not a DOM adjacency that happens
	   to follow from it. It is also the half a jsdom test can see, the sibling
	   selector being reachable only through the cascade. */
  .reply-quote.under-meta {
    margin-top: calc(var(--space-3) / 2);
  }
  .reply-quote:is(div) {
    cursor: default;
  }
  button.reply-quote:hover {
    border-left-color: var(--link);
    color: var(--text-secondary);
  }
  .reply-quote.deleted {
    font-style: italic;
    color: var(--text-dim);
  }
  .reply-quote-author {
    flex: 0 0 auto;
    color: var(--text-dim);
  }
  /* One line: the quote points at a message, it does not reproduce it. */
  .reply-quote-text {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Room label chip (aggregate views): a small clickable room tag in the
	   author header that jumps into the room. */
  .room-chip {
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1.2;
    padding: 0.05rem var(--space-2);
    cursor: pointer;
    max-width: 12rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition:
      color var(--transition-fast),
      border-color var(--transition-fast);
  }
  .room-chip:hover {
    color: var(--text-primary);
    border-color: var(--text-dim);
  }

  .gutter {
    flex: 0 0 var(--chat-gutter);
    display: flex;
    justify-content: center;
    padding-top: 0.1rem;
    /* The gutter is the wrapper an `Avatar` is rendered into, so this is where
       its size is chosen. The box and the chip's fill live in the primitive;
       what belongs to the transcript is which of the chat metrics it takes,
       and this one shrinks at the breakpoint with the column around it. */
    --avatar-size: var(--chat-avatar);
  }

  @media (max-width: 768px) {
    /* The avatar is narrower than its column here (the column's width is fixed
		   by the shared text inset, the avatar's by the sigil it lines up with), so
		   it hugs the leading edge instead of centring in the leftover. */
    .gutter {
      justify-content: flex-start;
    }

    /* The continuation-row stamp shares that column, and a `06:25 PM` does not
		   fit 1.25rem — centred in the gutter it would overhang the row's left edge
		   and spill ~9px into the message text on hover. Drop it here rather than
		   shrink it (no size makes it fit): it is a hover affordance, and this
		   breakpoint is overwhelmingly touch, where it never appears anyway. The
		   time is still on the group header above, and the floating actions bar is
		   positioned independently of it. */
    .hover-time {
      display: none;
    }
  }

  /* Continuation-row timestamp. Font-size and line-height are deliberately the
	   same as .meta-footer's so the two line boxes match and the floating actions
	   bar lands on this baseline exactly. */
  .hover-time {
    font-size: var(--text-xs);
    color: var(--text-dim);
    opacity: 0;
    line-height: 1.6;
    transition: opacity var(--transition-fast);
    font-variant-numeric: tabular-nums;
  }

  .content {
    flex: 1;
    min-width: 0;
  }

  .meta {
    display: flex;
    align-items: baseline;
    gap: var(--space-2);
    margin-bottom: 0.1rem;
  }
  .author {
    font-size: var(--text-base);
    font-weight: 600;
    color: var(--text-primary);
    /* Author and time hold their size; the metadata bar is what gives way when
		   the header row runs out of width. */
    flex-shrink: 0;
  }
  .author.bot {
    color: var(--accent-amber);
  }
  .stamp {
    font-size: var(--text-xs);
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }

  .body {
    font-size: var(--text-base);
    line-height: 1.5;
    color: var(--text-primary);
    word-break: break-word;
    max-width: var(--chat-body-max);
  }
  /* A notice about the run rather than about its content (ISSUE-278): the
	   primary brain failed and the rest of the turn came from the fallback,
	   possibly on a different model. The tint, padding and radius come from the
	   `.banner warn` primitive (web/AGENTS.md: use the shared blocks, don't
	   re-declare them); only the transcript-specific parts are here — the body
	   width it shares with prose, the leading rule, and a smaller type size so
	   the aside doesn't compete with the answer for prominence. */
  .run-notice {
    max-width: var(--chat-body-max);
    margin: var(--space-2) 0;
    border-left: 2px solid var(--status-warn-fg);
    font-size: var(--text-xs);
    line-height: 1.45;
  }
  /* The executor backticks the brain and model names; the default `code` run
	   is sized off body text and would out-weigh the notice around it. */
  .run-notice :global(code) {
    font-size: inherit;
    background: none;
    padding: 0;
  }

  /* On the inner span, not the wrapper: the wrapper also holds the copy
	   button, and under `pre-wrap` the markup whitespace around that button
	   would render as real blank space around the message text. */
  .user-text {
    white-space: pre-wrap;
  }

  /* Activity-chip spacing. Base is flush (a tool-first turn puts the chip
	   directly under the meta, like a text answer). When a chip neighbours a
	   prose block it gets a paragraph-sized gap on that side so it doesn't crowd
	   the surrounding text. (ActivityTrace's own margin is 0 so this is the sole
	   source of vertical spacing.) */
  .chip-slot {
    margin: 0;
  }
  /* A chip's preceding neighbour is always a prose block (tool runs coalesce,
	   so chips never abut), and this margin is the whole of the separation
	   between them. It was briefly cut to a hairline while each block reserved a
	   strip below its text for a per-block copy button; that reserve is gone
	   with the button, so it is back to matching `gap-below`. */
  .chip-slot.gap-above {
    margin-top: var(--space-3);
  }
  .chip-slot.gap-below {
    margin-bottom: var(--space-3);
  }
  /* A tool-first turn opens with a chip directly under the author header. Flush
	   reads cramped against the header, a full paragraph gap reads detached — so
	   it gets half the neighbour gap. Derived rather than written out: it was
	   0.425rem, half of the 0.85rem `gap-below` used to be, and that half went
	   stale the moment the neighbour moved onto the scale. */
  .meta + .chip-slot {
    margin-top: calc(var(--space-3) / 2);
  }

  /* A held draft is another block in the same slot, so it takes the chip
	   slot's neighbour gap above — which also separates two cards when a turn
	   holds more than one. Written here rather than in `DraftCard.svelte`
	   because that component also takes a `banner` placement, whose container is
	   a flex column already spacing its own children; a margin on the
	   component would stack with that gap. `:global()` because Svelte prunes a
	   selector whose subject it cannot see in this file, and the `.content`
	   scope is what keeps the rule from leaking past this row. */
  .content > :global(.draft-card) {
    margin-top: var(--space-3);
    /* The readable-width cap belongs to this placement rather than to the
       component: here the card is one block in a turn's content column and has
       to stop where the prose body above it stops. In `banner` placement it is
       pane chrome spanning its container instead — so the cap travels with the
       slot, not the card. */
    max-width: var(--chat-body-max);
  }

  .msg.error .body,
  .cmd-output.error {
    color: var(--status-danger-fg);
  }

  /* A turn that arrived from outside the room. A filled, ruled block rather
	   than the bare bubble every other user row is — an ordinary turn has no
	   surface of its own, so *having* one is what reads as "not from in here",
	   and it needs no colour to say it.

	   Deliberately neutral rather than a status tint: provenance is not a
	   severity, and borrowing `--status-warn-*` would call every external
	   correspondent suspect. The leading rule is the activity chip's and the
	   citation's geometry, since this sits in the same slot — a block between
	   the author header and the turn's content. */
  .external {
    width: 100%;
    max-width: var(--chat-body-max);
    margin: 0;
    padding: var(--space-2);
    background: var(--surface-badge);
    border-left: 2px solid var(--border-hover);
    border-radius: var(--radius-sm);
  }
  /* Sitting in the chip slot's position, it takes the chip slot's spacing:
	   flush at the base, and the header's half gap when a header precedes it.
	   Below stays flush for the reason `.external-head` gives — what follows
	   (attachments, the send marks) carries its own top margin. */
  .meta + .external {
    margin-top: calc(var(--space-3) / 2);
  }
  .external-head {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }
  /* The icon has no text baseline of its own, so it centres on the row's
	   leading line box rather than hanging off the synthesized one. */
  .external-mark {
    display: inline-flex;
    align-self: center;
    color: var(--text-muted);
  }
  .external-label {
    flex: 0 0 auto;
  }
  /* Attacker-supplied and routinely long: it gives way before the label or the
	   toggle, both of which are what the row is for. */
  .external-subject {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-secondary);
  }
  .external-toggle {
    flex: 0 0 auto;
    margin-left: auto;
    padding: 0;
    background: none;
    border: none;
    color: var(--link);
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1.4;
    cursor: pointer;
  }
  .external-toggle:hover {
    text-decoration: underline;
  }
  /* The one line standing in for a withheld body. Single-line and clipped: it
	   is a preview, not a shortened message, and letting it wrap would make
	   `collapsed` shade into `full` for a mail with one long first paragraph. */
  .external-preview {
    margin-top: var(--space-1);
    font-size: var(--text-sm);
    line-height: 1.4;
    color: var(--text-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* The head sets no bottom margin: what separates it from whatever follows is
	   that block's own top margin, and on a turn showing neither body nor preview
	   there is nothing to separate it from. */
  .external .body {
    margin-top: var(--space-1);
  }

  /* Send lifecycle on the user's own row (ISSUE-200). Both marks sit under the
	   message body, where the turn-action row would be — the send has to settle
	   before that row has anything to act on. */
  .send-pending {
    margin-top: var(--space-1);
  }
  .send-failed {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-1);
    font-size: var(--text-sm);
    color: var(--status-danger-fg);
  }
  .send-failed-text {
    min-width: 0;
  }
  /* Queued (ISSUE-238). Same geometry as the failure mark, muted rather than
	   dangerous: nothing has gone wrong, the message simply has not left. */
  .send-queued {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-2);
    margin-top: var(--space-1);
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .send-queued-text {
    min-width: 0;
  }
  /* Touch targets for the row's two icon buttons, the same out-of-flow overlay
	   device the turn-action row uses so the line keeps its height. Two
	   differences, both because of what this row holds: the overlay grows by one
	   `--space-2` rather than by the whole pitch, and the gap widens to
	   `--space-3`, so no two overlays touch and none of them reaches into the
	   Send button beside them — Send is a real control here, not a gap, and an
	   overlay lapping its edge would take taps meant for it. That leaves 4px of
	   dead space between the icons, which is the safe way to be wrong: a tap
	   there hits nothing rather than the button next door. */
  @media (max-width: 768px) {
    .send-queued {
      gap: var(--space-3);
    }
    .send-queued :global(.icon-btn) {
      position: relative;
    }
    .send-queued :global(.icon-btn)::before {
      content: '';
      position: absolute;
      top: 50%;
      left: 50%;
      width: calc(100% + var(--space-2));
      height: 44px;
      transform: translate(-50%, -50%);
    }
  }
  /* The body is dimmed, not hidden: the user is meant to reread what they wrote
	   before deciding to edit it. Only the message content, so the status line
	   and its buttons stay at full contrast — they are what you act on. */
  .msg.queued .body,
  .msg.queued .attachments,
  .msg.queued .reply-quote {
    opacity: 0.65;
  }

  /* Command (!…) output: a left-aligned block set apart from the conversation
	   by a subtle card, so its lists / code / tables render left-to-right.
	   Position anchor for its own star bar (durable system rows in views).

	   Geometry is `.msg`'s, deliberately down to the padding: the two are the
	   same row with a different left-hand column, and a notice whose card began
	   at the row's own edge sat a whole gutter to the left of every message
	   around it. A system row is always its own group (there is no continuation
	   case for a notice), so it takes the fresh-group top padding unconditionally
	   rather than through a class. */
  .cmd-row {
    display: flex;
    gap: var(--chat-avatar-gap);
    align-items: flex-start;
    padding: var(--space-2) var(--chat-row-inline);
    position: relative;
  }
  .cmd-row .room-chip {
    margin-bottom: var(--space-1);
  }
  /* The gutter mark. Sized at the avatar's box so it occupies the same column
	   the initials do, but drawn as a bare dim glyph rather than a filled chip —
	   an avatar says who wrote this, and nobody wrote a notice. */
  .sys-mark {
    width: var(--chat-avatar);
    height: var(--chat-avatar);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-dim);
  }
  .sys-mark :global(svg) {
    width: 1rem;
    height: 1rem;
  }
  @media (max-width: 768px) {
    /* The mobile gutter is 1.1rem wide, so the glyph comes down with the avatar
			   it shares the column with. */
    .sys-mark :global(svg) {
      width: 0.85rem;
      height: 0.85rem;
    }
  }
  /* A command row has no header to hang its star off, so the bar floats
	   top-right on the row. (The bar is only absolute here and on continuation
	   rows.) */
  .msg-actions.cmd-actions {
    position: absolute;
    right: var(--chat-row-inline);
    top: 0.3rem;
  }
  .cmd-output {
    max-width: var(--chat-body-max);
    font-size: var(--text-sm);
    line-height: 1.5;
    color: var(--text-secondary);
    background: var(--surface-raised);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-sm);
    /* Uniform, and the same figure `.external` and `DraftCard` use. The three
       are the surface's filled blocks inside a turn's content column, so their
       text has to start at one inset — a wider horizontal pad here put a
       notice's first character a step right of an external turn's directly
       above it. */
    padding: var(--space-2);
    text-align: left;
    word-break: break-word;
  }

  .attachments {
    display: flex;
    flex-wrap: wrap;
    min-width: 0;
    gap: var(--space-1);
    margin-top: var(--space-1);
  }
  .attachment {
    max-width: 100%;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: var(--text-xs);
    color: var(--text-muted);
    background: var(--surface-base);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
    padding: 0.1rem var(--space-2);
  }
  .attachment-link {
    text-decoration: none;
    cursor: pointer;
  }
  .attachment-link:hover,
  .attachment-link:focus-visible {
    color: var(--text-primary);
    border-color: var(--border-hover);
  }

  .progress {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
    color: var(--text-muted);
    font-size: var(--text-sm);
  }
  /* Tool descriptions (e.g. a long shell command) shouldn't wrap the row. */
  .status-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .dot {
    flex: 0 0 auto;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--text-muted);
    animation: pulse 1.1s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,
    100% {
      opacity: 0.3;
    }
    50% {
      opacity: 1;
    }
  }
</style>

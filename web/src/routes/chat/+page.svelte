<script lang="ts">
  import { onMount, onDestroy, tick } from 'svelte';
  import { page } from '$app/state';
  import { Plus, MessageSquare, Cloud, ChevronDown, Circle, Star, CheckCheck } from 'lucide-svelte';
  import {
    AppShell,
    ShellHeader,
    Sidebar,
    SidebarToggle,
    KebabMenu,
    Chip,
    ConfirmDialog,
    CountPill,
  } from '$lib/components/ui';
  import Lightbox from '$lib/components/Lightbox.svelte';
  import { roomColorVar } from '$lib/roomColors';
  import Message from '$lib/components/chat/Message.svelte';
  import Composer from '$lib/components/chat/Composer.svelte';
  import RoomSettings from '$lib/components/chat/RoomSettings.svelte';
  import RoomMemory from '$lib/components/chat/RoomMemory.svelte';
  import {
    isTap,
    nextActivation,
    UNCHANGED,
    type Activation,
    type PointerSample,
  } from '$lib/components/chat/tapActivation';
  import { getChatSession } from '$lib/stores/chat';
  import type { RoomPatch } from '$lib/api';
  import { REPLY_EXCERPT_CHARS, isClientOnly, type MessageReply } from '$lib/stores/segments';
  import { notifyError, notifyWarning, dismissNotice } from '$lib/stores/notices';
  import { online } from '$lib/stores/connectivity';
  import { dropDraft } from '$lib/stores/drafts';
  import { dropQueue, MAX_QUEUED_PER_ROOM } from '$lib/stores/sendQueue';
  import { isImeComposing } from '$lib/platform/input';
  import type { ChatAttachment, ChatRoom, ChatView } from '$lib/api';
  import { getCurrentUser } from '$lib/userContext';
  import { getSelectableBrains } from '$lib/components/chat/autocomplete/providers';

  const session = getChatSession();
  const {
    rooms,
    activeRoomId,
    messages,
    status,
    loaded,
    hasMore,
    loadingOlder,
    offlineTranscript,
    queuedCounts,
    view,
    scrollTarget,
    sendSettled,
    sendReturned,
    outboundDrafts,
    externalTurnDisplay,
  } = session;

  // Cross-room views: the transcript pane renders either the active room
  // ('room') or a read-only aggregate stream (all/unread/starred).
  const inViewMode = $derived($view !== 'room');
  const VIEW_LABELS: Record<ChatView, string> = {
    all: 'All messages',
    unread: 'Unread',
    starred: 'Starred',
  };
  // Client-side total for the sidebar Unread badge (sum of per-room counts;
  // the active room is already held at 0 by the store).
  const unreadTotal = $derived($rooms.reduce((n, r) => n + (r.unread_count ?? 0), 0));

  // Where each held draft's card goes. A draft belongs to the turn that
  // composed it, so it renders under that turn when the turn is on screen —
  // that is where the drafted text and "this task also created a calendar
  // event" are legible together.
  //
  // Everything else — a task with no room at all (a cron job mailing an
  // external address), a turn paged out of view, a turn whose task row
  // retention has deleted — is in the notification bell. There used to be a
  // fallback strip above the transcript, and the invariant it carried still
  // holds: no draft can be hidden by not finding a home here. The inbox is
  // where it goes instead, which is also reachable from every other route.
  const draftsByTask = $derived.by(() => {
    const byTask = new Map<number, typeof $outboundDrafts>();
    if (inViewMode) return byTask; // aggregate panes are read-only surfaces
    for (const draft of $outboundDrafts) {
      if (draft.task_id == null) continue;
      const list = byTask.get(draft.task_id);
      if (list) list.push(draft);
      else byTask.set(draft.task_id, [draft]);
    }
    return byTask;
  });
  // The **assistant** row alone. A turn has a user row and an assistant row
  // sharing one task id, so keying on the id without the role renders every
  // card twice — and the card belongs under the answer, not under the question
  // that started the task.
  function draftsForRow(message: { role: string; taskId?: number | null }) {
    if (message.role !== 'assistant' || message.taskId == null) return [];
    return draftsByTask.get(message.taskId) ?? [];
  }
  const draftActions = {
    approve: (id: number) => session.answerDraft(id, 'approve'),
    discard: (id: number) => session.answerDraft(id, 'discard'),
    edit: (id: number, body: string) => session.editDraft(id, body),
    refresh: () => void session.refreshDrafts(),
  };

  // The room whose settings modal is open (null = closed).
  let settingsRoom = $state<ChatRoom | null>(null);
  let memoryRoom = $state<ChatRoom | null>(null);

  let sidebarOpen = $state(false);
  /* The identity the root layout resolved, rather than a `/me` of this page's
     own (ISSUE-355). Derived rather than read once, because the layout swaps a
     cached identity for the live one when the connection returns. The generic
     labels are still the fallback for a record that carries neither. */
  const identity = getCurrentUser();
  const userName = $derived(identity.user.display_name || 'You');
  const botName = $derived(identity.user.bot_name || 'Istota');
  /* The two content hashes `/me` carries, for the two identities every
     transcript renders. `?? null` rather than left undefined: a backend that
     predates the field, or a cached record from one, then reads as "there is no
     picture" and asks for nothing, where undefined would mean "unknown" and put
     a bare request behind every row.

     One consequence of the `live` gate on `userId` below: on an offline cold
     boot the viewer's own turns fall back to the initial chip, because there is
     no id to build a URL with. That is deliberate rather than an oversight —
     the id under a guess is the wrong person's — and it is not a case for
     dropping the gate. */
  const userAvatar = $derived(identity.user.avatars?.user ?? null);
  const botAvatar = $derived(identity.user.avatars?.bot ?? null);
  /* Who is logged in, for the composer's draft key — and **only while the
     server has confirmed it**. `username` is the istota user id, the same value
     the server keys admin checks and workspace paths on, not a display handle.

     The `live` gate is what keeps this from writing into someone else's drawer.
     Offline the layout's record can be the last-user pointer's guess
     (ISSUE-354), and a draft key is a per-user storage key like any other: the
     chat store already refuses to write its cache or drain its queue under a
     guess, and `settleSeededUser` repairs the queue when a guess turns out
     wrong but knows nothing about drafts. Without the gate an offline draft
     would persist under the guessed id with nothing to collect it, and that
     person's next session would restore it into their own composer. Null until
     then, exactly as it was while this page fetched `/me` itself, and it turns
     non-null by itself the moment a retry reaches the server. */
  const userId = $derived(identity.live ? identity.user.username || null : null);
  let creatingRoom = $state(false);
  let newRoomName = $state('');
  let listEl: HTMLDivElement | undefined = $state();
  // The docked composer floats over the transcript, so its height is a layout
  // input: it drives the transcript's bottom padding (keeping the newest message
  // clear of the pill) and the jump-to-latest offset. Measured rather than
  // guessed — the composer grows with attachments, error chips and wrapped text.
  let dockEl: HTMLDivElement | undefined = $state();
  let composerH = $state(0);

  const activeRoom = $derived($rooms.find((r) => r.id === $activeRoomId) ?? null);
  // Where the composer holds unsent text (ISSUE-205). Scoped to the room's
  // token *and* the logged-in user: the room id is a recycled SQLite rowid, so
  // a deleted room's draft would land in whichever room takes its id next, and
  // a shared Talk room has one token across every member, so a bare token
  // would hand one person's half-written message to another on a browser
  // profile they take turns using. Null until both are known — the composer
  // then holds what is typed and carries it in once the key arrives.
  const draftKey = $derived(userId && activeRoom ? `${userId}:room:${activeRoom.token}` : null);
  // The store acks a send by room; the composer settles a draft by key. The
  // page owns the key's shape, so the translation belongs here — and it must
  // use the *acked* room rather than the open one, since a send can land after
  // the user has moved on and would otherwise settle the wrong room's draft.
  const settleSignal = $derived({
    n: $sendSettled.n,
    key: userId && $sendSettled.token ? `${userId}:room:${$sendSettled.token}` : null,
  });
  const busy = $derived($status === 'sending' || $status === 'streaming');
  // How many messages are waiting to send in the open room (ISSUE-238).
  //
  // Counted off the transcript rather than read from the store's queue map,
  // which is a plain Map and reactive to nothing: a queued row and its queue
  // entry are appended and removed in the same breath, so this is the same
  // number by a route Svelte can see. Scoped to the open room's token, since
  // the transcript can still be holding another room's stranded rows.
  const queuedHere = $derived(
    $messages.filter((m) => m.sendState === 'queued' && m.roomToken === activeRoom?.token).length,
  );
  // Past the cap the composer refuses with the reason on screen and keeps the
  // text in the field. The store refuses at the same cap on the way in, which
  // is the backstop under this rather than a substitute for it.
  //
  // Gated on `busy`, because that is the condition the cap itself is under:
  // `send()` only reaches `enqueueSend` while the room is not idle, so in an
  // idle room the same message takes the ordinary path and consults no cap.
  // Ungated, a room holding ten *held* rows — which is what a restored queue
  // looks like on every page load — refused every send in an idle room, with
  // a notice saying messages were waiting on a turn that was not running.
  const queueFull = $derived((busy || !$online) && queuedHere >= MAX_QUEUED_PER_ROOM);

  // The message the next send will cite. Held as the bare id, because that is
  // all the composer ever names and all the draft ever stores; the author
  // label and excerpt are looked up against the transcript below.
  let stagedReplyId = $state<number | null>(null);

  // Resolved live rather than at the moment of staging, and that is what makes
  // a drafted citation work at all: `selectRoom` empties `messages`
  // synchronously and only then awaits the history, so the composer's
  // draft-restore effect always fires against an empty transcript. A one-shot
  // resolve there returned null, cleared the chip, and the next draft flush
  // then erased the stored id — the whole round-trip was dead while its unit
  // tests passed, because they stub the resolver. Deriving instead means the
  // chip fills in as the page loads, and an id no longer in the window still
  // renders as a chip (the composer falls back to a generic label) rather than
  // silently dropping the user's citation.
  const stagedReply = $derived<MessageReply | null>(
    stagedReplyId == null ? null : (citationFor(stagedReplyId) ?? { msgId: stagedReplyId }),
  );

  /** Resolve a canonical id against the open transcript, capped for display. */
  function citationFor(msgId: number | null): MessageReply | null {
    if (msgId == null) return null;
    const m = $messages.find((x) => x.msgId === msgId);
    if (!m) return null;
    return {
      msgId,
      role: m.role,
      // Capped to what the server would send back, so the chip and the
      // reloaded quote are the same text rather than diverging for the life of
      // the session.
      excerpt: m.text.slice(0, REPLY_EXCERPT_CHARS),
    };
  }

  // Lightbox for an inline image in a transcript. One instance for the page,
  // as on the feeds route: a zoom controller per message would put one in every
  // row of a long room. `Message` supplies the list, scoped to the message the
  // click landed in.
  //
  // Wired on every row, the cross-room views included and unlike the handlers
  // beside it there: opening a zoom reads the transcript and changes nothing
  // in it, so there is nothing for a read-only pane to withhold.
  let lightboxImages = $state<string[]>([]);
  let lightboxIndex = $state<number | null>(null);

  // A send whose cited parent turned out to be gone: the store took the row
  // off the transcript, so the text comes back here rather than being lost.
  let seenReturn = $state(0);
  $effect(() => {
    const r = $sendReturned;
    if (r.n === seenReturn) return;
    seenReturn = r.n;
    // Only into the room it was typed in. Leaving a room is not gated on
    // `busy`, so a 404 can land after a switch — and refilling then would put
    // one room's text, and its uploaded attachments, one Enter away from being
    // posted to another. Nothing is lost by declining: the send never acked,
    // so the draft `submit` wrote is still stored under that room's key and
    // comes back on the way in.
    if (r.token !== activeRoom?.token) return;
    // The citation comes back with the text, or is cleared where there is none.
    // `returnSend`'s own path leaves both unset — its premise is that the cited
    // parent is gone — so that case still clears, as it always did. `editQueued`
    // sets them, and dropping them there would quietly turn an edited reply into
    // an ordinary message and send it without its parent.
    stagedReplyId = r.replyToMsgId ?? r.replyTo?.msgId ?? null;
    returnedSend = { n: r.n, text: r.text, attachments: r.attachments };
  });
  let returnedSend = $state<{ n: number; text: string; attachments: ChatAttachment[] } | null>(
    null,
  );

  /** Stage a reply to the message on this transcript row. */
  function stageReply(cid: number) {
    const m = $messages.find((x) => x.cid === cid);
    stagedReplyId = m?.msgId ?? null;
  }

  /** Follow a rendered citation back to the message it names. */
  function jumpToCitedMessage(msgId: number) {
    const token = activeRoom?.token;
    if (token) void session.jumpToMsgId(token, msgId);
  }

  // The room's standing model default as a header badge — the canonical model
  // name (e.g. `claude-opus-4-8`), not the alias, so it's unambiguous. null
  // when the room has no default (or in a cross-room view).
  const modelBadge = $derived.by(() => {
    if (inViewMode || !activeRoom) return null;
    const { model, effort } = activeRoom;
    if (!model && !effort) return null;
    let label = model ?? 'default model';
    if (effort) label += ` · ${effort}`;
    return label;
  });

  // Display names for the brain kinds, so the badge below reads `Native`
  // rather than `claude_code`. Shares the session catalogue the composer
  // autocomplete already loads, so this costs no extra request.
  //
  // It can legitimately come back without the room's own kind in it: the list
  // is the operator's `room_selectable` allowlist and a room can be pinned to
  // a kind since dropped from it, and it is empty outright for a non-admin,
  // who can be in a pinned room without being able to write one. So the badge
  // falls back to the raw kind rather than hiding — the point is to say which
  // brain the room is on, and an unlabelled name still says it. A second copy
  // of the server's label map here would be the thing that drifts.
  //
  // `brainCatalogueLoaded` gates the badge rather than only the label: without
  // it the first paint renders the raw kind and then flips to the label a
  // microtask later, and the empty map is also indistinguishable from "this
  // user may not edit the pin", which the title below depends on telling apart.
  // The `.catch` is not decoration — `loadCatalogue` caches its promise for the
  // life of the session, so one rejection is permanent, and this is a bare
  // `void` where an unhandled rejection has nowhere to go.
  let brainLabels = $state<Record<string, string>>({});
  let brainCatalogueLoaded = $state(false);
  onMount(() => {
    void getSelectableBrains()
      .then((brains) => {
        brainLabels = Object.fromEntries(brains.map((b) => [b.kind, b.label]));
      })
      .catch(() => {})
      .finally(() => (brainCatalogueLoaded = true));
  });

  // Whether this user can actually change the pin. The server collapses "the
  // operator listed no kinds" and "you may not write one" into one empty list,
  // and `RoomSettings` gates its whole brain control on the same emptiness — so
  // an empty catalogue means the modal has no brain field, and a badge saying
  // `click to change` would promise an edit that is not there.
  const canEditBrain = $derived(Object.keys(brainLabels).length > 0);

  // The room's standing brain pin as a header badge, beside the model one. A
  // room with no pin shows nothing: it runs the deployment's own brain, which
  // is not an override and is the same answer in every unpinned room.
  const brainBadge = $derived.by(() => {
    if (inViewMode || !activeRoom || !brainCatalogueLoaded) return null;
    // Off the room row, which is server JSON rather than anything validated
    // here, and it is rendered as text on the fallback path.
    const kind = activeRoom.brain;
    if (!kind || typeof kind !== 'string') return null;
    return brainLabels[kind] ?? kind;
  });

  // Discord/Slack-style grouping: a message continues the previous author's
  // run (collapsing its avatar + header) when it's the same non-system author
  // within a short window.
  const GROUP_WINDOW_MS = 5 * 60 * 1000;
  function isContinuation(i: number): boolean {
    if (i <= 0) return false;
    const prev = $messages[i - 1];
    const cur = $messages[i];
    if (!prev || prev.role !== cur.role || cur.role === 'system') return false;
    // Same role is not the same author: a user row can be an email mirrored
    // into the room, and collapsing its header would hide the one thing that
    // says it wasn't the viewer.
    if (prev.author !== cur.author) return false;
    // The label is a display name and two members can share one; the id is who
    // they are. Collapsing across it would hang one person's run — and now
    // their face — over another's words.
    if (prev.authorId !== cur.authorId) return false;
    // Aggregate views interleave rooms: a room change always starts a fresh
    // group (the header carries the room chip).
    if (prev.roomToken !== cur.roomToken) return false;
    // A message that opens a new day starts a fresh group (full header) under
    // the day divider, even from the same author within the window.
    if (startsNewDay(i)) return false;
    if (prev.createdAt && cur.createdAt) {
      const gap = new Date(cur.createdAt).getTime() - new Date(prev.createdAt).getTime();
      if (Number.isFinite(gap) && gap > GROUP_WINDOW_MS) return false;
    }
    return true;
  }

  // Day-divider support (ISSUE-127). Time-only stamps are ambiguous once
  // backfilled history lands older messages in a room; a divider row between
  // days resolves "is this today or last month" without stamping a full date on
  // every bubble. Day boundaries use the viewer's local timezone, not UTC, so
  // "Today" matches the user's clock.
  function localDayKey(iso: string): string | null {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
  }
  // True when message i is the first (rendered) message of its calendar day —
  // i.e. its day differs from the previous message's (or it's the very first).
  function startsNewDay(i: number): boolean {
    const m = $messages[i];
    const cur = m?.createdAt;
    if (!cur) return false;
    // A client-only row is a pending action, not a day in the history, and it
    // is deliberately pinned below every server row (ISSUE-351). Its stamp is
    // when it was typed — a restored queued entry keeps that for up to a week
    // — so dating it draws "Yesterday" underneath today's conversation and the
    // transcript reads as running backwards.
    if (m && isClientOnly(m)) return false;
    const curKey = localDayKey(cur);
    if (!curKey) return false;
    if (i === 0) return true;
    const prev = $messages[i - 1]?.createdAt;
    const prevKey = prev ? localDayKey(prev) : null;
    return curKey !== prevKey;
  }
  function dayLabel(iso: string): string {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '';
    const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
    const today = startOfDay(new Date());
    const that = startOfDay(d);
    const days = Math.round((today.getTime() - that.getTime()) / 86400000);
    if (days === 0) return 'Today';
    if (days === 1) return 'Yesterday';
    if (days > 1 && days < 7) return d.toLocaleDateString([], { weekday: 'long' });
    const sameYear = d.getFullYear() === today.getFullYear();
    return d.toLocaleDateString(
      [],
      sameYear
        ? { month: 'short', day: 'numeric' }
        : { year: 'numeric', month: 'short', day: 'numeric' },
    );
  }

  onMount(() => {
    session.init().then(() => {
      // Deep link: /chat?room=<token> selects that room for this load,
      // overriding the persisted-room default. An unknown / not-owned token
      // isn't in the per-user list → silent fallback to the default.
      // /chat?view=all|unread|starred opens an aggregate view instead; an
      // unknown value falls back silently, same as an unknown room token.
      const token = page.url.searchParams.get('room');
      const v = page.url.searchParams.get('view');
      const taskParam = page.url.searchParams.get('task');
      if (token) {
        // /chat?room=<token>&task=<id>: after selecting the room, jump to
        // the referenced turn (paging older history if needed). A bare
        // ?room= just selects the room. jumpToTask itself selects the room,
        // so a valid &task supersedes the plain select.
        const taskId = taskParam ? Number(taskParam) : NaN;
        if (Number.isFinite(taskId)) session.jumpToTask(token, taskId);
        else session.selectRoomByToken(token);
      } else if (v === 'all' || v === 'unread' || v === 'starred') session.selectView(v);
    });
  });

  // Being offline, said once, in the notice band.
  //
  // It used to be a row inside the composer dock and then a `NoticeBanner` in
  // the shell's `extras` band. Both were wrong in the same direction: docked it
  // read as a caption on the text box, and as a banner it was a bordered card
  // costing ~55px of a phone's pane for one sentence of chrome. The band
  // overlays rather than reflows, so the transcript is exactly as long offline
  // as on — which is what a statement about the app, rather than about the
  // conversation, should cost.
  //
  // `sticky` is what makes a notice honest here. Connectivity is a condition,
  // not an event, and the three ways this file's own machinery takes an event
  // off screen — the navigation clear, the 30s handover, the queue trim — would
  // each retract it while the composer still could not reach anything. A sticky
  // notice is exempt from all three and still steps aside for events, so a
  // failed send is not stuck behind it.
  //
  // The effect is the whole lifecycle: `$online` going true is what takes it
  // down, so nothing can leave it up over a working connection.
  let offlineNoticeId: number | null = null;
  $effect(() => {
    if (!$online) {
      offlineNoticeId = notifyWarning('Offline — messages will send when you’re back.', {
        duration: 0,
        sticky: true,
        key: 'chat:offline',
      });
      return;
    }
    if (offlineNoticeId !== null) {
      dismissNotice(offlineNoticeId);
      offlineNoticeId = null;
    }
  });

  // Stop the active stream when leaving /chat so the EventSource / poll timer
  // doesn't linger; remounting re-subscribes from persisted events.
  onDestroy(() => {
    if (highlightTimer) clearTimeout(highlightTimer);
    // Sticky survives the navigation clear by design, so leaving /chat has to
    // take it down by hand — the sentence promises the send queue will drain,
    // and no other route has one to promise.
    if (offlineNoticeId !== null) dismissNotice(offlineNoticeId);
    session.teardown();
  });

  // Stick-to-bottom only when the user is already at the bottom (B1). A plain
  // (non-reactive) latch sampled by the scroll handler *before* the store grows
  // the DOM — recomputing it inside the post-update effect would read the
  // already-grown height and always look "not at bottom". Starts true so the
  // first load and new sends pin to the newest message.
  let atBottom = true;
  const BOTTOM_THRESHOLD = 64; // px slack counted as "at the bottom"
  const TOP_THRESHOLD = 160; // px from the top that triggers an older-page load

  // Reactive mirror of `atBottom` for the jump-to-latest affordance. Kept
  // separate from the (non-reactive) `atBottom` latch so reading it never makes
  // the bottom-pin effect re-run on scroll.
  let showJumpToLatest = $state(false);

  function sampleAtBottom() {
    if (!listEl) return;
    atBottom = listEl.scrollHeight - listEl.scrollTop - listEl.clientHeight <= BOTTOM_THRESHOLD;
    showJumpToLatest = !atBottom;
  }

  function jumpToLatest() {
    if (!listEl) return;
    listEl.scrollTo({ top: listEl.scrollHeight, behavior: 'smooth' });
    atBottom = true;
    showJumpToLatest = false;
  }

  /**
   * Pin the transcript to its newest message.
   *
   * `repaint` is for the case where the scroller's whole content was just
   * replaced — a room or view switch. iOS Safari sometimes leaves that frame
   * unpainted: the DOM is there and the offset is right (the newest message
   * shows up exactly where it belongs the instant anything forces a repaint —
   * a swipe, or merely opening the rooms drawer), but the pane reads blank,
   * empty-state placeholder included. It only shows on rooms with enough
   * history to scroll, because only those move the offset at all — replacing
   * the content and jumping in the same frame is what loses the invalidation.
   *
   * So on those switches we make the scroll happen for real, across frames:
   * one pixel off the bottom, then back. Re-assigning the offset it already
   * holds would be a no-op and invalidate nothing, which is why this nudges
   * rather than just repeating the pin. Two frames at 1px is invisible, and it
   * doubles as a late correction if the composer or an image settled after the
   * first pin.
   */
  function pinToBottom(repaint = false) {
    if (!listEl) return;
    listEl.scrollTop = listEl.scrollHeight;
    if (!repaint || typeof requestAnimationFrame === 'undefined') return;
    requestAnimationFrame(() => {
      if (!listEl) return;
      // Relative to the max *scroll offset*, not scrollHeight: scrollHeight - 1
      // clamps straight back to the bottom on any scroller taller than a pixel,
      // so it would leave the offset unchanged and repaint nothing.
      const maxTop = listEl.scrollHeight - listEl.clientHeight;
      if (maxTop <= 1) return; // nothing to scroll, so nothing was jumped
      listEl.scrollTop = maxTop - 1;
      requestAnimationFrame(() => {
        if (listEl) listEl.scrollTop = listEl.scrollHeight;
      });
    });
  }

  async function onScroll() {
    if (!listEl) return;
    clearActivation();
    sampleAtBottom();
    // Near the top with older history available → fetch the previous page and
    // restore the scroll anchor so the viewport stays put (scroll-anchored
    // prepend). The store's loadingOlder guard makes this re-entrancy-safe.
    if (listEl.scrollTop <= TOP_THRESHOLD && $hasMore && !$loadingOlder) {
      const prevHeight = listEl.scrollHeight;
      const prevTop = listEl.scrollTop;
      await session.loadOlder();
      await tick();
      if (listEl) listEl.scrollTop = listEl.scrollHeight - prevHeight + prevTop;
    }
  }

  // Touch surrogate for hover (the per-message metadata + star). A touch device
  // has no hover, and iOS Safari's synthesized one sticks: it clears the pseudo
  // class only on the next tap, so every row tapped in a run kept its star
  // showing. One activated row instead — a mouse pointer is left alone, it has
  // real hover. Rules in tapActivation.ts; this owns the state and the clears.
  let activeCid: Activation = $state(null);
  let tapStart: PointerSample | null = null;

  function onListPointerDown(e: PointerEvent) {
    if (e.pointerType === 'mouse') return;
    tapStart = { x: e.clientX, y: e.clientY, t: e.timeStamp };
  }

  function onListPointerUp(e: PointerEvent) {
    if (e.pointerType === 'mouse') return;
    const start = tapStart;
    tapStart = null;
    // A scroll flick also ends with a pointerup over a row, and a long press is
    // a text selection. Neither activates.
    if (!start || !isTap(start, { x: e.clientX, y: e.clientY, t: e.timeStamp })) return;
    const next = nextActivation(e.target as Element | null, activeCid);
    if (next !== UNCHANGED) activeCid = next;
  }

  // Everything that ends an activation without a tap on the list. Scrolling
  // carries the row off-screen (and starts as a touch on it, so it would
  // otherwise stay lit), and a tap anywhere else on the page — composer,
  // sidebar, header — is the user moving on. Capture phase so a handler that
  // stops propagation can't strand it.
  function clearActivation() {
    if (activeCid !== null) activeCid = null;
  }

  // Last pointer to touch the page. `@media (hover: hover)` is the first line of
  // defence against synthesized hover, but it answers for the device, not the
  // gesture: a touchscreen laptop or an iPad with a trackpad reports hover, and
  // a finger on one of those still leaves a sticky :hover behind. So the rows
  // also defer to what was last used, and hover reveals go quiet after a touch.
  let pointerIsTouch = $state(false);

  $effect(() => {
    const onDocPointerDown = (e: PointerEvent) => {
      pointerIsTouch = e.pointerType !== 'mouse';
      if (!pointerIsTouch) return;
      const t = e.target as Node | null;
      if (t && listEl?.contains(t)) return; // list taps are the list's own business
      clearActivation();
    };
    document.addEventListener('pointerdown', onDocPointerDown, true);
    return () => document.removeEventListener('pointerdown', onDocPointerDown, true);
  });

  // Leaving the room takes the activation with it — the rows it referred to are
  // gone, and a cid from the old transcript could collide with one in the new.
  $effect(() => {
    $activeRoomId;
    $view;
    activeCid = null;
  });

  // A room / view switch replaces the transcript wholesale, so the next
  // non-empty render is a fresh conversation that opens at its newest message —
  // wherever the user happened to be scrolled in the room they left. Without
  // the latch reset, leaving a room mid-history skipped the pin entirely and the
  // new room opened at the top of its first page. Plain (non-reactive) lets, so
  // neither this effect nor the one below re-runs on them.
  let switchPending = false;
  $effect(() => {
    $activeRoomId;
    $view;
    switchPending = true;
    atBottom = true;
  });

  // Auto-scroll to the newest message when the list changes — but only if we
  // were at the bottom before the change (a streamed delta, a new send, a
  // notification append while reading the latest). A scroll-up prepend leaves
  // atBottom false, so the anchor restore in onScroll owns the viewport instead.
  $effect(() => {
    const msgs = $messages;
    if (!atBottom) return;
    // First content after a switch: pin with the repaint pass. The empty render
    // the switch passes through on its way there doesn't count — it has nothing
    // to paint and no offset to lose.
    const afterSwitch = switchPending && msgs.length > 0;
    if (afterSwitch) switchPending = false;
    tick().then(() => pinToBottom(afterSwitch));
  });

  // Track the docked composer's height. The transcript reserves it as bottom
  // padding *inside* the scroller, so scrollHeight already accounts for it and
  // the bottom-pin below stays plain `scrollTop = scrollHeight` — no offset
  // arithmetic. What does need handling is the composer growing (or the dock
  // disappearing in an aggregate view) while pinned: the reserved band changes
  // under a viewport that was at the bottom, so re-pin after each measurement.
  $effect(() => {
    if (!dockEl) {
      composerH = 0;
      return;
    }
    const el = dockEl;
    const measure = () => {
      composerH = el.offsetHeight;
      if (atBottom) tick().then(() => pinToBottom());
    };
    measure();
    // jsdom has no ResizeObserver; the one-shot measure above is enough there.
    if (typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  });

  // Jump-to-response: the store resolves a search result to a transcript cid
  // and bumps `scrollTarget`; here we do the DOM scroll + a transient highlight
  // pulse. The nonce makes a repeated jump to the same row re-fire.
  let highlightTimer: ReturnType<typeof setTimeout> | undefined;
  $effect(() => {
    const t = $scrollTarget;
    if (!t) return;
    tick().then(() => {
      const el = listEl?.querySelector(`[data-cid="${t.cid}"]`) as HTMLElement | null;
      if (!el) return;
      // The row just paged in / room just switched — don't let the
      // stick-to-bottom effect fight the jump. The jump target is centered
      // (off the bottom), so reveal the jump-to-latest affordance; a real
      // scroll event re-samples if it happens to land at the bottom.
      atBottom = false;
      showJumpToLatest = true;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('jump-highlight');
      if (highlightTimer) clearTimeout(highlightTimer);
      highlightTimer = setTimeout(() => el.classList.remove('jump-highlight'), 2000);
    });
  });

  function selectRoom(id: number) {
    session.selectRoom(id);
    sidebarOpen = false;
  }

  function selectView(v: ChatView) {
    session.selectView(v);
    sidebarOpen = false;
  }

  // Mark every room read (header chip). Confirmed like the feeds equivalent —
  // it's a bulk, not-really-undoable cursor advance.
  let confirmMarkAllRead = $state(false);

  async function performMarkAllRead() {
    confirmMarkAllRead = false;
    await session.markAllRead();
  }

  // Per-message delete. The message component only offers the affordance; the
  // confirm lives here so the whole page has one dialog rather than one per
  // transcript row, and so the delete button sitting next to Copy in the same
  // small row can't remove a message on a single mistap.
  let confirmDeleteMessage = $state(false);
  let pendingDeleteCid: number | null = null;

  function askDeleteMessage(cid: number) {
    pendingDeleteCid = cid;
    confirmDeleteMessage = true;
  }

  // Re-send a message whose send never landed. Unconfirmed, unlike delete: the
  // action the user is repeating is the one they already asked for, and the
  // failed row is the only place that message still exists.
  //
  // Offered in the live room only. An aggregate view is a read-only pane with
  // no composer, so re-sending from one would post into a room the user isn't
  // looking at.
  function retryFailedSend(cid: number) {
    atBottom = true;
    showJumpToLatest = false;
    // Caught rather than `void`: the store guards its own body, but a rejection
    // reaching an un-awaited caller is the shape of the bug this whole change
    // is about — silence where a failure should have been reported.
    session.retrySend(cid).catch(() => notifyError('Couldn’t retry that message.'));
    tick().then(() => pinToBottom());
  }

  /**
   * Release a held queued message (ISSUE-238).
   *
   * Only the head of the room's queue can actually go, so releasing one behind
   * a held entry marks it ready and sends nothing until its turn comes round —
   * which is why this reports no failure of its own beyond a rejection.
   */
  function releaseQueuedSend(cid: number) {
    atBottom = true;
    showJumpToLatest = false;
    // Caught for the same reason `retryFailedSend` catches: the store guards
    // its own body, and a rejection reaching an un-awaited caller is silence
    // where a failure should have been reported.
    session.releaseQueued(cid).catch(() => notifyError('Couldn’t send that message.'));
    tick().then(() => pinToBottom());
  }

  // Named only when the room really is Talk-bound: a delete that reaches into
  // Nextcloud Talk is a materially bigger action than one that doesn't, and
  // saying so unconditionally would be a warning about something that isn't
  // going to happen in a web-only room.
  const deleteReachesTalk = $derived(!!activeRoom?.talk_token);

  async function performDeleteMessage() {
    const cid = pendingDeleteCid;
    confirmDeleteMessage = false;
    pendingDeleteCid = null;
    if (cid == null) return;
    await session.deleteMessage(cid);
  }

  async function createRoom() {
    const name = newRoomName.trim();
    if (!name) return;
    newRoomName = '';
    creatingRoom = false;
    await session.newRoom(name);
    sidebarOpen = false;
  }

  async function saveRoomSettings(patch: RoomPatch) {
    if (!settingsRoom) return;
    await session.updateRoomSettings(settingsRoom.id, patch);
    settingsRoom = null;
  }

  // Both the hard delete and the Talk-room hide arrive here.
  async function deleteRoom() {
    if (!settingsRoom) return;
    const id = settingsRoom.id;
    const token = settingsRoom.token;
    settingsRoom = null;
    await session.deleteRoom(id);
    // A room that is gone from the list can never be typed into again, and
    // nothing else collects its draft short of the 30-day TTL. Dropped after
    // the delete rather than before, so a refused delete keeps the text.
    //
    // The composer flushes on its way out of a room, but it is switched away
    // by the store's own reselect *during* the await above — so by now the
    // stored draft is already the final one and this is the last word on it.
    if (userId) {
      dropDraft(`${userId}:room:${token}`);
      // And whatever was waiting to be sent into it. The store drops the room's
      // in-memory queue on its way out, but only while the room is still in
      // `$rooms` to be looked up by id — this holds the token captured before
      // the delete, so it is the half that cannot miss.
      dropQueue(`${userId}:room:${token}`);
    }
  }

  async function promoteRoom() {
    if (!settingsRoom) return;
    const id = settingsRoom.id;
    await session.promoteRoom(id);
    // Reflect the new binding in the open modal (button → "On Talk").
    settingsRoom = $rooms.find((r) => r.id === id) ?? null;
  }
</script>

<!-- insetBottom={false}: the Composer holds the bottom safe-area inset itself, so
     its fill reaches the screen edge while its controls stay above the indicator. -->
<AppShell insetBottom={false}>
  {#snippet header()}
    <ShellHeader
      title={inViewMode ? VIEW_LABELS[$view as ChatView] : activeRoom ? activeRoom.name : 'Chat'}
      onTitleClick={() => (sidebarOpen = !sidebarOpen)}
      titleActionLabel="open rooms"
    >
      {#snippet leading()}
        <SidebarToggle
          open={sidebarOpen}
          label="Rooms"
          count={$rooms.length}
          onclick={() => (sidebarOpen = !sidebarOpen)}
        />
      {/snippet}
      {#snippet nav()}
        {#if brainBadge}
          {#if canEditBrain}
            <button
              class="model-badge brain-badge"
              type="button"
              title="Room brain — click to change"
              onclick={() => activeRoom && (settingsRoom = activeRoom)}
            >
              {brainBadge}
            </button>
          {:else}
            <span class="model-badge brain-badge brain-badge-static" title="Room brain">
              {brainBadge}
            </span>
          {/if}
        {/if}
        {#if modelBadge}
          <button
            class="model-badge"
            type="button"
            title="Room model default — click to change"
            onclick={() => activeRoom && (settingsRoom = activeRoom)}
          >
            {modelBadge}
          </button>
        {/if}
      {/snippet}
      {#snippet tools()}
        <Chip icon onclick={() => (confirmMarkAllRead = true)} title="Mark all rooms as read">
          <CheckCheck size={14} />
        </Chip>
      {/snippet}
    </ShellHeader>
  {/snippet}

  {#snippet sidebar()}
    <Sidebar
      title="Rooms"
      count={$rooms.length}
      open={sidebarOpen}
      onClose={() => (sidebarOpen = false)}
    >
      <!-- Cross-room views, above the rooms list (mirrors the feeds sidebar's
			     All / Unread / Starred entries). Selecting one deselects the room. -->
      <div class="views">
        <button
          class="view-btn"
          class:active={$view === 'all'}
          onclick={() => selectView('all')}
          type="button"
        >
          <span class="view-name">All</span>
        </button>
        <button
          class="view-btn"
          class:active={$view === 'unread'}
          onclick={() => selectView('unread')}
          type="button"
        >
          <Circle size={12} />
          <span class="view-name">Unread</span>
          <CountPill count={unreadTotal} title={`${unreadTotal} unread`} />
        </button>
        <button
          class="view-btn"
          class:active={$view === 'starred'}
          onclick={() => selectView('starred')}
          type="button"
        >
          <Star size={12} />
          <span class="view-name">Starred</span>
        </button>
      </div>

      <!-- Sits with the rooms list it adds to, below the cross-room views. -->
      <div class="room-new">
        {#if creatingRoom}
          <!-- svelte-ignore a11y_autofocus -->
          <input
            class="room-input"
            bind:value={newRoomName}
            placeholder="Room name…"
            autofocus
            onkeydown={(e) => {
              // Not the Enter that commits an input-method candidate — this is
              // a free-form name field, so that is exactly where someone types
              // CJK and would get a room named after a half-finished word.
              if (e.key === 'Enter' && !isImeComposing(e)) createRoom();
              if (e.key === 'Escape') {
                creatingRoom = false;
                newRoomName = '';
              }
            }}
            onblur={() => {
              if (!newRoomName.trim()) creatingRoom = false;
            }}
          />
        {:else}
          <button class="room-add" onclick={() => (creatingRoom = true)} type="button">
            <Plus size={14} /> New room
          </button>
        {/if}
      </div>

      {#each $rooms as room (room.id)}
        {@const isTalk = room.origin === 'talk' || !!room.talk_token}
        {@const unreadCount = room.unread_count ?? 0}
        {@const unread = unreadCount > 0 && room.id !== $activeRoomId}
        {@const waiting = room.id === $activeRoomId ? 0 : ($queuedCounts[room.token] ?? 0)}
        {@const tint = roomColorVar(room.color)}
        <!-- The tint goes on the row box, not on `.room-btn`, so it covers the
			     kebab too. It layers over `.sidebar .list-row`'s shared hover/active
			     background rather than editing it — that rule is shared with the
			     briefings archive row (ISSUE-433). -->
        <div
          class="list-row room-row"
          class:active={room.id === $activeRoomId}
          class:tinted={!!tint}
          style:--room-tint={tint}
        >
          <button class="room-btn" onclick={() => selectRoom(room.id)} type="button">
            {#if isTalk}
              <!-- Leading origin glyph: a tinted cloud marks a room mirrored
							     to Nextcloud Talk. Sits in its own flex slot before the
							     title so it never eats name width or gets clipped by the
							     title's ellipsis (ISSUE-129). -->
              <span class="room-origin talk" title="Also on Nextcloud Talk">
                <Cloud size={13} />
              </span>
            {:else}
              <span class="room-origin" title="Web room">
                <MessageSquare size={13} />
              </span>
            {/if}
            <span class="room-text">
              <span class="room-line">
                {#if tint}
                  <!-- The dot carries the colour where the wash cannot: a
									     forced-colours or high-contrast mode drops the tinted
									     background entirely, and this survives as a shape. -->
                  <span class="room-dot" aria-hidden="true"></span>
                {/if}
                <span class="room-name" class:unread>{room.name}</span>
                {#if unread}
                  <CountPill count={unreadCount} title={`${unreadCount} unread`} />
                {/if}
                <!-- What has not gone out of this room yet (ISSUE-202). The
								     drain runs for the room on screen only, so for every other
								     room this badge is the whole of the affordance: it says
								     which one to open for what is in it to go. Not drawn for
								     the open room, like the unread pill above it, where the
								     rows themselves are the count. Held entries are in it —
								     they also need this room opened — which is why the title
								     says "not sent yet" rather than promising they will send
								     on their own. Muted, because nothing has arrived and
								     nothing has gone wrong: it is a state the user put there. -->
                {#if waiting > 0}
                  <CountPill count={waiting} tone="muted" title={`${waiting} not sent yet`} />
                {/if}
              </span>
            </span>
          </button>
          <KebabMenu
            ariaLabel="Room actions"
            items={[
              { label: 'Settings', onSelect: () => (settingsRoom = room) },
              // A sibling of Settings rather than a button inside it: the pane
              // is a full-width markdown editor, and opening one modal from
              // another is a shape nothing else in this frontend uses.
              { label: 'Memory', onSelect: () => (memoryRoom = room) },
            ]}
          />
        </div>
      {/each}
    </Sidebar>
  {/snippet}

  <div class="chat-pane" style:--composer-h="{composerH}px">
    <div class="messages-wrap">
      <div
        class="messages"
        bind:this={listEl}
        role="log"
        aria-live="polite"
        onscroll={onScroll}
        onpointerdown={onListPointerDown}
        onpointerup={onListPointerUp}
        onpointercancel={() => (tapStart = null)}
      >
        {#if !$loaded}
          <div class="chat-empty">Loading…</div>
        {:else if $messages.length === 0}
          <div class="chat-empty">
            {#if $view === 'unread'}
              <CheckCheck size={28} />
              <p>All caught up</p>
            {:else if $view === 'starred'}
              <Star size={28} />
              <p>Nothing starred yet.</p>
              <span class="hint">Hover a message and hit the star.</span>
            {:else if $view === 'all'}
              <MessageSquare size={28} />
              <p>No messages yet</p>
            {:else if $offlineTranscript}
              <!-- An empty room offline is two different facts, and the prompt
                   to ask something is only right for one of them. Nothing is
                   saved for this room — it was never opened with a connection,
                   or its tail has expired — and saying so is the difference
                   between a room that is empty and a room that cannot be read
                   from here. The composer below still queues, which is why the
                   second line says so rather than leaving it to be found. -->
              <MessageSquare size={28} />
              <p>Nothing from this room is saved on this device.</p>
              <span class="hint">
                Its messages are here again when you’re back online. You can still write one — it
                waits until then.
              </span>
            {:else}
              <MessageSquare size={28} />
              <p>
                {activeRoom
                  ? `Ask anything in #${activeRoom.name.replace(/^#+/, '')}.`
                  : 'Ask Istota anything.'}
              </p>
              <span class="hint">Configuration help, quick tasks, or one-off questions.</span>
            {/if}
          </div>
        {:else}
          <!-- Older-history affordance (B3): a spinner while a page loads, a
				     quiet marker once the start of the conversation is reached. -->
          {#if $loadingOlder}
            <div class="older-status" role="status">Loading older messages…</div>
          {:else if !$hasMore && !$offlineTranscript}
            <div class="older-status begin">Beginning of conversation</div>
          {/if}
          {#each $messages as message, i (message.cid)}
            {#if message.createdAt && startsNewDay(i)}
              <div class="day-divider" role="separator">
                <span class="day-label">{dayLabel(message.createdAt)}</span>
              </div>
            {/if}
            <Message
              {message}
              continuation={isContinuation(i)}
              {userName}
              userId={userId ?? undefined}
              {userAvatar}
              {botName}
              {botAvatar}
              onConfirm={session.confirm}
              onReject={session.reject}
              onToggleStar={session.toggleStar}
              onDelete={askDeleteMessage}
              onRetry={inViewMode ? undefined : retryFailedSend}
              retryBusy={busy}
              onQueueSend={inViewMode ? undefined : releaseQueuedSend}
              onQueueEdit={inViewMode ? undefined : session.editQueued}
              onQueueRemove={inViewMode ? undefined : session.removeQueued}
              onReply={inViewMode ? undefined : stageReply}
              onJumpToMessage={inViewMode ? undefined : jumpToCitedMessage}
              onRoomClick={inViewMode ? (token) => session.selectRoomByToken(token) : undefined}
              onJump={(token, taskId) => session.jumpToTask(token, taskId)}
              onImageOpen={(imgs, idx) => {
                lightboxImages = imgs;
                lightboxIndex = idx;
              }}
              drafts={draftsForRow(message)}
              draftActions={inViewMode ? undefined : draftActions}
              externalDisplay={$externalTurnDisplay}
              aggregate={inViewMode}
              active={message.cid === activeCid}
              touch={pointerIsTouch}
            />
          {/each}
        {/if}
        <!-- Bottom reserve: keeps the newest message clear of the docked
             composer. A spacer rather than padding on the scroller, because the
             fade below is a sticky child and sticky is constrained to its
             containing block's *content* box — as padding, the reserve would
             park the fade that far above the scrollport's bottom edge. Either
             way it is inside the scroller, so scrollHeight accounts for it and
             the stick-to-bottom pin stays a plain `scrollTop = scrollHeight`.

             Only with messages present: with none, there is nothing to keep
             clear of the pill, and the reserve made the scroller taller than
             its own viewport, so the bottom-pin scrolled the empty-state notice
             up by the reserve's height — it read as sitting above centre for
             exactly the composer's worth of space. Without it the empty
             scroller has no scroll range and `height: 100%` centres in the
             scrollport. -->
        {#if $loaded && $messages.length > 0}
          <div class="composer-reserve" aria-hidden="true"></div>
        {/if}
        {#if !inViewMode && $loaded && $messages.length > 0}
          <!-- Fade layer, sized to the composer band it sits behind: content
               scrolling into that band dissolves into the pane fill instead of
               running under the pill at full strength. It is a child of the
               scroller (sticky, pinned to the bottom of the scrollport) rather
               than an overlay over it, because a scroller paints its scrollbar
               above its own content — an overlay sibling painted over the
               bottom of the scrollbar too, so the thumb dissolved along with
               the text. -->
          <div class="composer-fade" aria-hidden="true"></div>
        {/if}
      </div>
      <!-- Jump-to-latest: shown only when scrolled up off the bottom. -->
      {#if showJumpToLatest}
        <button
          class="jump-latest"
          onclick={jumpToLatest}
          aria-label="Scroll to latest message"
          title="Scroll to latest"
        >
          <ChevronDown size={20} />
        </button>
      {/if}
    </div>
    {#if !inViewMode}
      <!-- Sending is room-scoped; aggregate views are read-only panes.
           Docked over the transcript rather than sharing the column with it, so
           the message list runs the full height of the pane and content passes
           under the composer instead of stopping short of it.

           It carries the composer and nothing else. The offline banner used to
           ride here too, which is what made the dock render in an aggregate
           view that has no composer at all; it is the shell's `extras` band
           now, so this condition is back to the one thing the dock is for. -->
      <div class="composer-dock" bind:this={dockEl}>
        <Composer
          onSend={(t, atts, reply) => {
            // Sending is the end of reading back: whatever the user had scrolled
            // up to look at, the message they just wrote — and the reply to it —
            // is what they want to see. So the send re-arms the stick-to-bottom
            // latch rather than respecting it, which is the one case where the
            // "only if you were already at the bottom" rule is wrong.
            //
            // Pinned immediately as well as latched: `send` is async, so the
            // message may be a network round trip away, and the transcript
            // should be waiting at the bottom for it rather than jumping when it
            // lands. The $messages effect covers the landing itself.
            atBottom = true;
            showJumpToLatest = false;
            // See retryFailedSend: the store settles its own failures onto the
            // message row, so this only covers a rejection that escaped it.
            session
              .send(t, atts, reply ?? undefined)
              .catch(() => notifyError('Couldn’t send that message.'));
            tick().then(() => pinToBottom());
          }}
          onCancel={() => session.cancel()}
          {busy}
          queueing={busy || !$online}
          {queueFull}
          placeholder="Your message…"
          {draftKey}
          sendSettled={settleSignal}
          replyTo={stagedReply}
          onReplyChange={(msgId) => (stagedReplyId = msgId)}
          restoreSend={returnedSend}
        />
      </div>
    {/if}
  </div>

  {#if settingsRoom}
    <RoomSettings
      room={settingsRoom}
      onSave={saveRoomSettings}
      onDelete={deleteRoom}
      onPromote={promoteRoom}
      onClose={() => (settingsRoom = null)}
    />
  {/if}

  {#if memoryRoom}
    <RoomMemory
      open
      roomId={memoryRoom.id}
      roomName={memoryRoom.name}
      onClose={() => (memoryRoom = null)}
    />
  {/if}

  <ConfirmDialog
    bind:open={confirmMarkAllRead}
    title="Mark all rooms as read"
    message="Are you sure you want to mark all rooms as read? This can't be undone."
    confirmLabel="Mark all read"
    confirmVariant="primary"
    onConfirm={performMarkAllRead}
  />

  <ConfirmDialog
    bind:open={confirmDeleteMessage}
    title="Delete message"
    message={deleteReachesTalk
      ? "Are you sure you want to delete this message? It will be removed from this conversation and from the Nextcloud Talk room it's synced with. This can't be undone."
      : "Are you sure you want to delete this message? This can't be undone."}
    confirmLabel="Delete"
    onCancel={() => (pendingDeleteCid = null)}
    onConfirm={performDeleteMessage}
  />

  <!-- Rendered unconditionally: the component's own `{#if}` is inside it, and
       its gesture teardown assumes it is never unmounted between two opens. -->
  <Lightbox images={lightboxImages} index={lightboxIndex} onClose={() => (lightboxIndex = null)} />
</AppShell>

<style>
  /* Room model-default badge beside the header title. Clickable → opens the
	   room-settings modal. Only shown when the room has a standing default. */
  .model-badge {
    display: inline-flex;
    align-items: center;
    font: inherit;
    font-size: var(--text-xs);
    line-height: 1;
    color: var(--text-muted);
    background: var(--surface-base);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-pill);
    padding: 0.2rem var(--space-2);
    cursor: pointer;
    white-space: nowrap;
    transition:
      color var(--transition-fast),
      border-color var(--transition-fast);
  }
  .model-badge:hover {
    color: var(--text-primary);
    border-color: var(--border-hover);
  }

  /* The brain pin, in the interactive blue rather than the model badge's
	   muted gray — two overrides sitting side by side have to be tellable
	   apart at a glance, and they are set in the same modal, so shape is not
	   available to separate them. Blue rather than a status color on purpose:
	   a pinned brain is a choice someone made, not a severity. */
  .brain-badge {
    color: var(--accent-blue);
    border-color: var(--accent-blue);
  }
  .brain-badge:hover {
    color: var(--accent-blue);
    border-color: var(--accent-blue);
    background: var(--surface-raised);
  }
  /* Read-only where the user cannot change the pin: same colour, because it
	   still says which brain the room is on, minus the affordance. */
  .brain-badge-static {
    cursor: default;
  }
  .brain-badge-static:hover {
    background: var(--surface-base);
  }

  .chat-pane {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
    /* Anchors the docked composer. */
    position: relative;
    /* Published as a variable as well as applied: the docked composer paints
       this same fill behind itself and fades the transcript out into it, and it
       can't read a sibling component's `background`. */
    --chat-bg: var(--surface-reading);
    background: var(--chat-bg);
    /* Soften body text a step (scoped to chat) to ease sustained reading. The
		   token flips to a soft dark in light, so this needs no override. */
    --text-primary: var(--text-reading);
  }

  /* The composer floats over the transcript instead of taking a row of its own,
	   so the message list keeps the full pane height and content scrolls under it.
	   The composer itself is transparent — the fade layer below is the backdrop —
	   and the transcript reserves the dock's measured height as bottom padding. */
  .composer-dock {
    position: absolute;
    left: 0;
    right: 0;
    bottom: 0;
    z-index: 6;
  }

  /* Covers the composer's band plus a short run-up above it, so the dissolve is
	   already under way before content reaches the pill. The gradient's solid stop
	   is an absolute length rather than a percentage: the band's height moves with
	   the composer (attachments, wrapped text), and a percentage would stretch the
	   soft part with it — the fade would start over the transcript proper on a
	   tall composer. z-index keeps it under the jump-to-latest FAB (5) and the
	   dock (6); pointer-events: none so it never swallows a click. */
  .composer-fade {
    position: sticky;
    bottom: 0;
    height: calc(var(--composer-h, 0px) + 1.5rem);
    /* Cancels its own height so it overlaps the reserve above rather than
       extending the scroll range. */
    margin-top: calc(-1 * (var(--composer-h, 0px) + 1.5rem));
    background: linear-gradient(to bottom, transparent, var(--chat-bg) 2.5rem);
    pointer-events: none;
  }

  .composer-reserve {
    height: calc(var(--composer-h, 0px) + 1rem);
  }

  /* Wrapper anchors the floating jump-to-latest button to the bottom of the
	   scroll area; the button offsets itself above the docked composer. */
  .messages-wrap {
    position: relative;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .messages {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    /* The transcript owns the vertical gesture. Without this, a pan that runs
		   past the top or bottom of the list chains to the page, which on mobile
		   reads as the page bouncing while the messages sit still. */
    overscroll-behavior: contain;
    /* Row padding lives in Message (so the hover highlight spans the full
		   channel width, Discord-style). Just a little breathing room here. */
    /* The bottom reserve is the `.composer-reserve` spacer at the end of the
			 list rather than padding here — see its comment in the markup. */
    padding: var(--space-2) 0 0;
    width: 100%;
  }

  /* Jump-to-latest FAB — appears bottom-right when the user scrolls up off the
	   newest message; click smooth-scrolls back to the bottom. */
  .jump-latest {
    position: absolute;
    /* Centered over the scroll area. It used to hang off the right edge to line
		   up with the old square send button; against the composer's round send
		   circle that reads as two mismatched arrows stacked in the same corner,
		   so it sits in the middle instead — which also keeps it clear of the
		   text as the pill grows. The centering translate is folded into the
		   hover/active transforms below, or they would cancel it. */
    left: 50%;
    transform: translateX(-50%);
    /* Rides above the docked composer, which now overlaps this wrapper's bottom
			 edge; without the offset it would sit behind the pill. */
    bottom: calc(var(--composer-h, 0px) + 0.75rem);
    z-index: 5;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 36px;
    height: 36px;
    border-radius: var(--radius-pill);
    border: 1px solid var(--border-default);
    background: var(--surface-overlay);
    color: var(--text-primary);
    box-shadow: var(--shadow-overlay);
    cursor: pointer;
    /* Fully opaque: centered it floats over message text rather than over the
		   right margin, and the old 0.9 let that text read straight through. */
    opacity: 1;
    transition: transform 0.12s ease;
  }
  .jump-latest:hover {
    transform: translate(-50%, -1px);
  }
  .jump-latest:active {
    transform: translate(-50%, 0);
  }

  .chat-empty {
    height: 100%;
    /* The composer is docked *over* the scrollport, so centring in the full
		   height puts the notice half a composer below the middle of the space the
		   user can actually see. Reserving the pill's height discounts it from the
		   centring instead. Inside the 100% (border-box globally), so unlike the
		   `.composer-reserve` spacer this adds no scroll range for the
		   stick-to-bottom pin to act on — the reason the notice used to read high
		   by the same measure. 0 in an aggregate view, which has no dock. */
    padding-bottom: var(--composer-h, 0px);
    /* Message rows get their inset from Message; an empty state has no row, so
		   without this the notice runs edge to edge — most visibly the offline
		   one, whose two-line hint then touches both sides of a phone. */
    padding-inline: var(--space-6);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    color: var(--text-dim);
    /* Matches the whole-pane loading message every other section uses
	     (`.center-msg`, app.css). The empty states set their own larger type on
	     the `<p>` below, so this only lands on the bare "Loading…" line. */
    font-size: var(--text-sm);
    text-align: center;
  }
  .chat-empty p {
    margin: 0.2rem 0 0;
    color: var(--text-muted);
    font-size: var(--text-base);
  }
  .chat-empty .hint {
    font-size: var(--text-sm);
  }

  /* Older-history affordance (ISSUE-131): a centered, low-key status row at the
	   top of the transcript while a previous page loads or once the start is
	   reached. */
  .older-status {
    text-align: center;
    color: var(--text-dim);
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-3) var(--space-3);
  }
  .older-status.begin {
    color: var(--text-dim);
    opacity: 0.6;
  }

  /* Day divider (ISSUE-127): a centered date pill on a hairline rule, marking
	   the boundary between calendar days in the transcript. */
  .day-divider {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: var(--space-4) 0 var(--space-1);
    padding: 0 var(--space-3);
  }
  .day-divider::before,
  .day-divider::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border-subtle);
  }
  .day-label {
    flex-shrink: 0;
    font-size: var(--text-xs);
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--text-dim);
    text-transform: uppercase;
  }

  /* Cross-room view entries above the rooms list — styled like the feeds
	   sidebar's All / Unread / Starred buttons (.feed-btn.special). */
  /* .views / .view-btn / .view-name (the All / Unread / Starred block) come
	   from web/src/lib/styles/sidebar.css, shared with the feeds sidebar. */

  /* No horizontal padding: this sits inside .sidebar-list, which already
	   insets its children, so the button lines up with the rows around it. */
  .room-new {
    padding: 0 0 var(--space-2);
  }
  .room-add {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    width: 100%;
    background: none;
    border: 1px dashed var(--border-default);
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
    cursor: pointer;
  }
  .room-add:hover {
    color: var(--text-primary);
    border-color: var(--text-dim);
  }
  .room-input {
    width: 100%;
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font: inherit;
    font-size: var(--text-sm);
    padding: var(--space-2) var(--space-2);
    border-radius: var(--radius-sm);
  }

  .room-btn {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex: 1;
    min-width: 0;
    background: none;
    border: none;
    color: var(--text-muted);
    font: inherit;
    font-size: var(--text-base);
    cursor: pointer;
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-sm);
    text-align: left;
    transition: color var(--transition-fast);
  }
  .room-row:hover .room-btn {
    color: var(--text-secondary);
  }
  .room-row.active .room-btn {
    color: var(--text-primary);
  }
  /* Title + badge on one line. The origin glyph stays a sibling of this column
	   so it keeps its own fixed slot. */
  .room-text {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-width: 0;
  }
  .room-line {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    min-width: 0;
  }
  .room-name {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  /* A room with unseen bot/system messages reads bolder; the active room never
	   bolds (looking at it is reading it). */
  .room-name.unread {
    font-weight: 700;
    color: var(--text-primary);
  }
  /* Leading origin glyph. Fixed slot before the title so a long room name
	   still gets the full row width and the icon never enters the title's
	   truncation box. */
  .room-origin {
    flex-shrink: 0;
    display: inline-flex;
    align-items: center;
    color: var(--text-dim);
  }
  .room-origin.talk {
    color: var(--accent-amber);
  }

  /* Room colour (ISSUE-433). The wash is deliberately weak — it has to be
	   scannable at a glance without competing with the unread bold and its count
	   pill, which are the two things in this row that mean something has changed.
	   `color-mix` over transparent is the idiom this file already uses for a
	   subtle wash (see @keyframes jump-pulse below). */
  .room-row.tinted {
    background: color-mix(in srgb, var(--room-tint) 14%, transparent);
  }
  /* Layered rather than a replacement: `.sidebar .list-row:hover` (sidebar.css)
	   sets an opaque --surface-raised, so the tint is mixed *into* that colour
	   instead of being painted over by it.
	   `.sidebar` is deliberately NOT in this selector. It lives in the Sidebar
	   component, so Svelte cannot see the subject in this file and prunes the
	   whole rule — the silent-stop-applying trap web/AGENTS.md names, which here
	   would leave a tinted row losing its tint on hover with nothing failing.
	   Scoped, this is (0,4,0) against the shared rule's (0,3,0), so it wins
	   without needing the ancestor. */
  .room-row.tinted:hover,
  .room-row.tinted.active {
    background: color-mix(in srgb, var(--room-tint) 26%, var(--surface-raised));
  }
  /* Sits in the title's flex line before the name, so a long name still
	   truncates against the row rather than against the dot. */
  .room-dot {
    flex-shrink: 0;
    width: 0.5rem;
    height: 0.5rem;
    border-radius: var(--radius-pill);
    background: var(--room-tint);
    /* Without this the dot is no fallback for the wash — it fails *with* it.
		   Forced-colours substitutes a background-color exactly as it does the
		   row tint, so the dot would flatten to the forced background and the
		   colour would be gone from the row entirely. Opting out is what the
		   escape exists for: here the colour is the content, not decoration. */
    forced-color-adjust: none;
  }

  /* Jump-to-response: a brief pulse on the row a search result jumps to. The
	   class is toggled on the Message component's root (data-cid anchor), so the
	   rule is :global; it fades a soft accent wash under the row for ~2s. */
  :global(.jump-highlight) {
    animation: jump-pulse 2s ease-out;
    border-radius: var(--radius-card);
  }
  @keyframes jump-pulse {
    0% {
      background: color-mix(in srgb, var(--accent-amber) 26%, transparent);
    }
    100% {
      background: transparent;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    :global(.jump-highlight) {
      animation-duration: 0.01ms;
    }
  }
</style>

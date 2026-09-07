/**
 * Web chat session engine.
 *
 * Owns rooms, the active room's message list, the in-flight task, and the
 * send / cancel / confirm / room actions. Streaming prefers SSE (EventSource)
 * and falls back to polling the snapshot endpoint when SSE is unavailable
 * (e.g. the mock dev backend, or a proxy that buffers event-streams).
 *
 * A single module-level instance is shared across the /chat surfaces.
 */
import { get, writable, type Readable, type Writable } from 'svelte/store';
import { notifyError, notifySuccess, notifyWarning } from './notices';
import {
  AuthError,
  cancelChatTask,
  chatStreamUrl,
  confirmChatTask,
  createChatRoom,
  deleteChatMessage,
  deleteChatRoom,
  ChatMessageBusyError,
  ChatRoomBusyError,
  getChatConfig,
  getChatMessagesView,
  getRoomMessages,
  getChatRooms,
  getRoomEvents,
  getTaskEvents,
  chatRoomStreamUrl,
  type ChatRoomEvent,
  listOutboundDrafts,
  approveOutboundDraft,
  discardOutboundDraft,
  editOutboundDraft,
  type OutboundDraft,
  markAllRoomsRead,
  markRoomRead,
  sendChatMessage,
  setChatMessageStarred,
  updateChatRoom,
  promoteChatRoom,
  type ChatAttachment,
  type ChatConfig,
  type ChatRoom,
  type RoomPatch,
  type ChatHistory,
  type ChatView,
  type ExternalTurnDisplay,
  type SendResult,
  type SendFailure,
  uploadChatAttachment,
  UploadUnreachableError,
} from '$lib/api';
import { loadSetting, saveSetting } from '$lib/stores/persisted';
import {
  dropQueue as dropStoredQueue,
  readAllQueues,
  writeQueue,
  MAX_QUEUED_PER_ROOM,
  OFFLINE_AUTO_SEND_MAX_AGE_MS,
  type PendingAttachment,
  type QueueReason,
  type StoredQueuedSend,
} from '$lib/stores/sendQueue';
import { online, noteTransport } from '$lib/stores/connectivity';
import {
  appendTranscriptRows,
  deleteBlob,
  deleteTranscript,
  getBlob,
  pruneOffline,
  readConfig,
  readRooms,
  readTranscript,
  removeCachedMessages,
  writeConfig,
  writeRooms,
  writeTranscript,
} from '$lib/offline/db';
import { rememberLastUserId, seedUserId } from '$lib/offline/lastUser';
import { normalizeExternalTurnDisplay } from '$lib/stores/externalTurns';
import { sortRoomsByActivity, touchRoomActivity } from '$lib/stores/roomOrder';
import { applyNotificationCounts } from '$lib/stores/notifications';
import {
  isKnownCommand,
  dropRoomCatalogue,
  loadCommandNames,
  resetCommandCatalogue,
} from '$lib/components/chat/autocomplete/providers';
import {
  applyEvent as applySegmentEvent,
  isStranded,
  isQueued,
  isClientOnly,
  type ChatMessage,
  type Segment,
  type ToolEntry,
  type SearchResultsData,
  type SearchResultItem,
  type ConfirmationAnsweredData,
  type SteerRecordedData,
  type MessageReply,
  type SendPayload,
} from '$lib/stores/segments';

// The message / segment model lives in the pure reducer module so it can be
// unit-tested without a DOM; re-export here so existing `$lib/stores/chat`
// importers keep working.
export type {
  ChatMessage,
  Segment,
  ToolEntry,
  SearchResultsData,
  SearchResultItem,
  ConfirmationAnsweredData,
  SteerRecordedData,
  MessageReply,
};

/** Build an assistant message's `segments` from a finished task's history
 * payload. Tool entries render as neutral "done" chips (history carries no
 * per-tool success / progress / timing); the last text segment is the answer
 * (unsettled, prominent), all earlier text segments are settled narration. */
function historySegments(raw: { kind: string; text: string }[]): Segment[] {
  const segs: Segment[] = raw.map((s, i) => {
    if (s.kind === 'tool') {
      return {
        kind: 'tool',
        id: `h${i}`,
        tool: { id: `h${i}`, name: '', description: s.text, running: false },
      };
    }
    if (s.kind === 'thinking') {
      return { kind: 'thinking', id: `k${i}`, text: s.text, settled: true };
    }
    return { kind: 'text', id: `s${i}`, text: s.text, settled: true };
  });
  // Only the last *text* segment is the answer; thinking stays settled.
  for (let i = segs.length - 1; i >= 0; i--) {
    const s = segs[i];
    if (s.kind === 'text') {
      s.settled = false;
      break;
    }
  }
  return segs;
}

export type ChatStatus = 'idle' | 'sending' | 'streaming';

// Client-side ack verbs. The backend stamps its own verb in `task_started`,
// but that event can't arrive until the scheduler claims the task off its
// poll queue (a second or two cold). Seeding one of these the instant we
// create the placeholder removes the perceived "Thinking…" gap; the backend
// `task_started` verb is then skipped (see applyEvent) so the line doesn't
// flicker from one random verb to another. Real status (progress_text,
// tool_start) still takes over normally.
//
// This MUST mirror the master list in src/istota/events.py (PROGRESS_MESSAGES)
// so the client-side seed never shows a verb the backend wouldn't. Same verbs,
// only the trailing "..." rendered as a single "…". Keep the two lists in sync.
const ACK_VERBS = [
  'On it…',
  'Hmm…',
  'Heard, chef…',
  'Investigating…',
  'One sec…',
  'Copy that…',
  'Roger…',
  'Considering…',
  'Thinkifying…',
  'Braining…',
  'Improvising…',
  'Jamming…',
  'Riffing…',
  'Grooving…',
  'Beboppin’…',
  'Noodling…',
  'Syncopating…',
  'Comping…',
  'Soloing…',
  // Cephalopod
  'Inking…',
  'Tentacling…',
  'Suckering…',
  'Jetting…',
  'Unfurling…',
  'Chromatophoring…',
  'Squidding…',
  'Grasping…',
  'Probing…',
  'Siphoning…',
  // Cheeky
  'Instigating…',
  'Scheming…',
  'Concocting…',
  'Percolating…',
  'Marinating…',
  'Hatching…',
  'Sleuthing…',
  'Finagling…',
  'Wrangling…',
  'Tinkering…',
  'Rummaging…',
  'Conjuring…',
  'Fermenting…',
  'Machinating…',
  'Gallivanting…',
];

function randomAckVerb(): string {
  return ACK_VERBS[Math.floor(Math.random() * ACK_VERBS.length)];
}

// How long a send may stay open before its pending mark earns the screen.
//
// A send that resolves normally does so in well under 100ms, so an indicator
// shown unconditionally would flash for a frame on every message — noise that
// teaches you to ignore the one place a real problem would be reported. Past
// this, the send is slow enough that saying so is useful, and slow is also the
// state that precedes a failure.
const SEND_PENDING_GRACE_MS = 400;

// The 4xx that mean "later", not "no". Everything else in the range is a
// verdict on the request itself, so a retry of the same payload is futile.
// (429 arrives classified as `rate_limit` and never reaches this set.)
const TRANSIENT_4XX = new Set([408, 425, 429]);

// The cross-room aggregate views, in sidebar order. Also the validator for the
// persisted selection — anything else falls back to room mode.
const AGGREGATE_VIEWS: ChatView[] = ['all', 'unread', 'starred'];

const STREAM_KINDS = [
  'task_started',
  'tool_start',
  'tool_end',
  'tool_progress',
  'progress_text',
  'thinking',
  'text_delta',
  'context_management',
  'brain_fallback',
  'confirmation',
  'result',
  'error',
  'cancelled',
  'done',
];

/**
 * How a task's stream ended.
 *
 * Only `done` is a normal finish. The distinction is the send queue's (a turn
 * that errored or was stopped holds the messages typed behind it); the stream
 * queue advances on all three alike, since either way the task is over.
 */
type StreamTerminal = 'done' | 'error' | 'cancelled';

/**
 * A send handed back to the composer, with the room it was typed in.
 *
 * Only `reply_target_gone` produces one — see `returnSend`. The attachments
 * travel because they are already uploaded: re-picking them would orphan the
 * first copies server-side and cost the user the work twice.
 */
export interface SendReturn {
  n: number;
  token: string | null;
  text: string;
  attachments: ChatAttachment[];
  /**
   * The citation the message carried, for a return that has one.
   *
   * Unset on the `reply_target_gone` path, whose whole premise is that the
   * cited parent is gone. Set by `editQueued`, where the parent is fine and
   * dropping it would quietly turn an edited reply into an ordinary message.
   */
  replyTo?: MessageReply;
  replyToMsgId?: number;
}

export interface ChatSession {
  rooms: Writable<ChatRoom[]>;
  activeRoomId: Writable<number | null>;
  messages: Writable<ChatMessage[]>;
  status: Writable<ChatStatus>;
  activeTaskId: Writable<number | null>;
  loaded: Writable<boolean>;
  // Cross-room aggregate views: 'room' renders the active room's live
  // transcript; the other three render a read-only stream across all member
  // rooms (no composer, no live streaming — reload on entry).
  view: Writable<'room' | ChatView>;
  selectView: (v: ChatView) => Promise<void>;
  // Star / unstar the durable message behind a transcript row (optimistic,
  // reverted on failure). No-op for rows without a msgId.
  toggleStar: (cid: number) => Promise<void>;
  // Hard-delete the durable message behind a transcript row. No-op for rows
  // without a msgId (a live placeholder isn't stored yet). The caller is
  // expected to have confirmed first — this does not prompt.
  deleteMessage: (cid: number) => Promise<void>;
  // Advance every room's web read cursor at once (header mark-all chip).
  markAllRead: () => Promise<void>;
  // Older-history paging (ISSUE-131): whether an older page exists, an
  // in-flight guard, and the fetch-and-prepend action the scroll handler calls.
  hasMore: Writable<boolean>;
  loadingOlder: Writable<boolean>;
  loadOlder: () => Promise<void>;
  // True when what the transcript is showing came out of the offline cache
  // rather than off the wire (ISSUE-202). `hasMore` is forced false alongside
  // it — an older page is a fetch, and offline there is none to be had — so
  // this is what tells the page apart from the case `hasMore: false` normally
  // means, which is that the top of the pane really is the start of the
  // conversation.
  offlineTranscript: Writable<boolean>;
  // How many messages are waiting to send in each room, keyed by room token
  // (ISSUE-202). The room list's badge, and the only place a *background*
  // room's queue is visible: the drain runs for the active room only, so this
  // is what says which room to open to send what is waiting in it.
  queuedCounts: Readable<Record<string, number>>;
  init: () => Promise<void>;
  selectRoom: (id: number) => Promise<void>;
  selectRoomByToken: (token: string) => Promise<boolean>;
  // Jump-to-response: resolve a search result's turn (select room + page to it)
  // and signal the transcript to scroll. `scrollTarget` is the signal the route
  // watches to perform the DOM scroll + transient highlight.
  jumpToTask: (roomToken: string, taskId: number) => Promise<boolean>;
  // The same jump keyed on a canonical `messages.id` — what a rendered
  // citation clicks through to. A sibling of `jumpToTask` rather than a
  // parameter on it: only the resolution step differs.
  jumpToMsgId: (roomToken: string, msgId: number) => Promise<boolean>;
  scrollToCid: (cid: number) => void;
  scrollTarget: Writable<{ cid: number; nonce: number } | null>;
  newRoom: (name: string) => Promise<void>;
  renameRoom: (id: number, name: string) => Promise<void>;
  updateRoomSettings: (id: number, patch: RoomPatch) => Promise<void>;
  promoteRoom: (id: number) => Promise<void>;
  archiveRoom: (id: number) => Promise<void>;
  deleteRoom: (id: number) => Promise<void>;
  // The last send the backend acked: a monotonic counter plus the room it
  // belongs to. The composer holds the submitted text as a draft until this
  // fires — a failed row does not survive a reload and the stored draft does —
  // so it needs the ack as a signal, and it owns the key the draft is stored
  // under, which is why the room travels rather than the key.
  //
  // The room is what makes it safe. Two sends can be open at once (a room
  // switch resets `status` to 'idle', which un-gates the composer), and a bare
  // counter would let whichever acked first settle the other's draft — the
  // cross-turn leak Stage 3 removed from `pendingSend`, reintroduced here.
  sendSettled: Writable<{ n: number; token: string | null }>;
  // A send whose cited parent turned out to be gone: the text and attachments
  // go back to the composer, since Retry cannot resolve that failure. Same
  // counter-plus-room shape as `sendSettled`, and for the same reason.
  sendReturned: Writable<SendReturn>;
  send: (text: string, attachments?: ChatAttachment[], replyTo?: MessageReply) => Promise<void>;
  // Re-POST a failed send from its own row (ISSUE-200). Reuses the row rather
  // than appending a new one, so the canonical echo folds into it. No-op for a
  // row that didn't fail, or one whose failure a retry can't resolve.
  retrySend: (cid: number) => Promise<void>;
  // The three verbs on a queued message (ISSUE-238), all no-ops on a cid that
  // is not one. Nothing here has been POSTed, which is what makes them
  // possible at all.
  //
  // `removeQueued` drops it; `editQueued` drops it and hands the text and
  // attachments back to the composer through `sendReturned`; `releaseQueued`
  // clears the hold Stop (or a failure) put on it and tries to send.
  removeQueued: (cid: number) => void;
  editQueued: (cid: number) => void;
  releaseQueued: (cid: number) => Promise<void>;
  cancel: () => Promise<void>;
  confirm: (cid: number, taskId: number) => Promise<void>;
  reject: (cid: number, taskId: number) => Promise<void>;
  // Outbound mail the approval gate is holding. User-scoped rather than
  // room-scoped — rooms are shared and a co-member must not see the body — so
  // the client places each card by the `room_token` on the draft.
  outboundDrafts: Writable<OutboundDraft[]>;
  refreshDrafts: () => Promise<void>;
  // The stream and the polling fallback both land here. Exposed because the
  // `unavailable` guard and the answered-suppression window are the two rules
  // that decide whether a card stays on screen, and neither is reachable
  // through the public verbs.
  applyDraftsSnapshot: (drafts: OutboundDraft[] | undefined, unavailable?: boolean) => void;
  answerDraft: (draftId: number, action: 'approve' | 'discard') => Promise<boolean>;
  editDraft: (draftId: number, body: string) => Promise<boolean>;
  // How much of an external-origin turn the transcript shows. Read from
  // `/chat/config` at init and edited on /settings, so it is server state
  // rather than a client-local preference — the reader may be on a phone one
  // day and a laptop the next, and how much of a stranger's mail they want
  // inline is a decision about the account, not about the browser. Seeded at
  // the default so the first paint is never `full` by accident.
  externalTurnDisplay: Writable<ExternalTurnDisplay>;
  teardown: () => void;
}

function createSession(): ChatSession {
  const rooms = writable<ChatRoom[]>([]);
  const activeRoomId = writable<number | null>(null);
  const messages = writable<ChatMessage[]>([]);
  const status = writable<ChatStatus>('idle');
  const activeTaskId = writable<number | null>(null);
  // Set when Stop is tapped before the send POST has returned a task id; applied
  // by `sendTurn` the moment it has one. See `cancel`.
  let cancelRequested = false;
  const loaded = writable(false);
  // Which pane the transcript renders: the active room, or a cross-room
  // aggregate view (All / Unread / Starred). Aggregate views are read-only
  // reading surfaces — no composer, no SSE; re-entering refreshes. The
  // selection is one thing at a time: entering a view deselects the room and
  // vice versa, and `init` restores whichever was last chosen.
  const view = writable<'room' | ChatView>('room');
  // Older-history paging (ISSUE-131). `oldestCursor` is the keyset to fetch the
  // next older page (raw stored created_at + id), `hasMore` whether one exists,
  // `loadingOlder` a re-entrancy guard the scroll handler reads. Reset per room.
  const hasMore = writable(false);
  const loadingOlder = writable(false);
  const offlineTranscript = writable(false);
  let oldestCursor: { ts: string; id: number } | null = null;
  function resetPaging() {
    oldestCursor = null;
    hasMore.set(false);
    loadingOlder.set(false);
    // Cleared with the rest of the transcript's state, so the flag is only
    // ever true for a transcript `loadHistory` actually left on the cache.
    offlineTranscript.set(false);
  }

  let cidCounter = 0;
  const nextCid = () => ++cidCounter;
  let pollIntervalMs = 1500;
  // The single in-flight stream for the active room, plus a FIFO of tasks
  // waiting their turn. A room runs one task at a time (the backend's
  // per-channel claim gate serializes them), so the UI streams them in order:
  // start one, queue the rest, advance when the active one settles. Different
  // rooms run concurrently on the backend; switching rooms tears this down and
  // resumes from the new room's history.
  let activeStream: { stop: () => void } | null = null;
  let streamQueue: { taskId: number; cid: number }[] = [];
  // Bot-delivered messages (alerts / logs / notifications routed to the `web`
  // surface) arrive on the room stream as `role: 'system'` rows carrying a
  // notif_id. `seenNotifIds` dedups a streamed row against one the history
  // load already rendered; it's reset per room in loadHistory.
  const seenNotifIds = new Set<number>();
  // Slow metadata reconciler, NOT the live path — the room stream carries
  // content and unread deltas now. It is kept (rather than deleted) because
  // GET /chat/rooms is what drives the Talk→web read-state pull, which is
  // itself server-throttled at [web.chat] talk_read_sync_interval (60s); 30s
  // satisfies it comfortably. Do not remove this timer without moving that
  // pull somewhere else, or Talk read sync silently stops.
  let roomsTimer: ReturnType<typeof setInterval> | null = null;
  const ROOMS_REFRESH_MS = 30000;
  let onVisibility: (() => void) | null = null;

  // ---- Live room-event stream (live-web-chat-room-stream spec) ----
  //
  // One user-scoped SSE connection carries every message in every room the user
  // is a member of, whatever surface produced it. Room switching is a
  // client-side filter; background rooms get real content, not just a count.
  // Cursor is `messages.id` — one monotonic integer over user turns, assistant
  // turns and system rows.
  let roomStream: { stop: () => void } | null = null;
  // True only while an EventSource is actually open. Positive evidence that no
  // `messages` row can have been missed, which is what lets a recovery skip the
  // history reload (see `recoverStream`'s `metadataOnly`).
  let roomStreamLive = false;
  // Bumped by every `init()` and by `teardown()`, so a load interrupted
  // mid-flight abandons its remaining side effects instead of installing them
  // on a page the user has left.
  let initGeneration = 0;
  let roomCursor = 0;
  // The deletion tail's own cursor. Separate from `roomCursor` because a
  // delete is hard — there is no `messages` row left for the id-ordered event
  // tail to carry — so the server keeps a ledger and this tracks it. Passed
  // back on reconnect, or a message deleted while the tab was disconnected
  // would come back to life on the next resume.
  let roomDeletionCursor = 0;
  // Whether `roomCursor` is a real position in the tail or merely its initial
  // zero. It matters because zero is not a neutral value to stream from: the
  // server answers `since_id: 0` with *every* message the user can see, as
  // individual `message` frames, so an unseeded connection replays the whole
  // history — inflating every background room's unread badge and appending
  // rows older than the rendered page below the newest ones, which the day
  // dividers then follow. Reachable whenever the seed request alone fails, and
  // routine once `init()` runs to completion with no connection at all.
  let roomCursorSeeded = false;
  let lastRoomEventAt = Date.now();
  let hiddenSince: number | null = null;
  // Frames that land while a recovery reload is in flight. The reload's
  // `messages.set` would otherwise drop a row written after its DB read, and
  // the server's per-connection cursor has already moved past it so it will
  // never be re-sent. Buffer, then re-apply (dedup makes that idempotent).
  let recoveryBuffer: ChatRoomEvent[] | null = null;
  let recovering = false;
  // A recovery reload must not be able to wedge the live path. `applyRoomEvent`
  // buffers every frame while a reload is in flight and only `recoverStream`'s
  // `finally` releases it, so a request that never settles would swallow frames
  // forever and the `recovering` guard would refuse every future attempt.
  // `fetch` has no timeout of its own, so bound these three explicitly.
  const RECOVERY_FETCH_TIMEOUT_MS = 15000;
  // Frames for the room we are mid-send into. The canonical `messages` user row
  // is written by the POST before it returns — and, with user-scoped OAuth on,
  // before a bounded ~5s Talk mirror — so our own echo can arrive while the
  // bubble on screen still has no `task_id` to dedup against. Appending it then
  // produces a second user bubble AND (no assistant carries the id yet) a
  // second placeholder + task stream. Hold that room's frames for the duration
  // of the send and replay them once the id is stamped; the (role, task_id) key
  // then matches and the echo is dropped. Bounded by the POST, and scoped to
  // the one room, so nothing else is delayed.
  let pendingSend: { token: string; rows: ChatRoomEvent[] } | null = null;
  // Past roughly a minute of silence a reconnect has probably missed state the
  // stream does not carry — a star toggled on another device, a read cursor
  // advanced by the Talk→web sync, a membership change — so a reload is *more
  // correct*, not merely cheaper. Under a minute, a transparent patch beats a
  // flicker, and EventSource reconnects on ordinary blips often enough that
  // forcing a reload each time would be constant churn on a flaky network.
  const ROOM_STREAM_STALE_MS = 60000;
  const sendSettled = writable<{ n: number; token: string | null }>({ n: 0, token: null });
  const sendReturned = writable<SendReturn>({
    n: 0,
    token: null,
    text: '',
    attachments: [],
  });
  // Seeded at the default rather than left undefined: `init` may not have
  // answered before the first transcript paints, and the safe direction there
  // is to show less of a stranger's mail rather than more.
  const externalTurnDisplay = writable<ExternalTurnDisplay>('collapsed');

  // ---- Client-only rows -----------------------------------------------------
  //
  // A send the server never took exists nowhere but here. `messages` is
  // otherwise a projection of server history — `loadHistory` and `loadViewPage`
  // both rebuild it wholesale from the response — so a room switch, a
  // stream-recovery reload or a step into an aggregate view dropped the one
  // copy of the user's text along with the Retry that was its only way back.
  // A network outage triggers the reload and the failure at once, so the user
  // watched their message be reported as unsent and then vanish.
  //
  // Rows sit in this map only while they are off screen; a rebuild takes them
  // back out. Keyed by room, so a row is re-appended to the transcript it
  // belongs to and nowhere else.
  const strandedSends = new Map<string, ChatMessage[]>();

  // `isStranded` / `isQueued` / `isClientOnly` are imported from `segments.ts`:
  // the transcript renders these rows too, and the page needs the same
  // predicate to keep them out of its day dividers (ISSUE-351).

  /**
   * `arr` with `row` inserted above whatever client-only rows sit at the tail.
   *
   * The rule the whole transcript order turns on (ISSUE-351): a client-only
   * row has not been POSTed, so it is a pending action rather than an event in
   * the history — it carries Send / Edit / Remove or a Retry, it belongs
   * against the composer, and everything the server produced sorts above it.
   * Every append into `messages` that is *not* itself a client-only row goes
   * through here; `enqueueSend` and `appendQueuedRows` keep their plain tail
   * pushes, because they are the block this walks back past.
   *
   * A drained row needs no special case. `beginSend` stamps `'sending'` on it
   * before the POST, so it stops being client-only where it already sits, and
   * its own placeholder then lands directly under it, above whatever is still
   * queued behind it.
   *
   * **The rule is enforced at append time and is not maintained afterwards.**
   * Two paths settle a row in place — `beginSend` above, and the failed-send
   * adoption in `appendStreamedRow` — and where a *stranded* failed row sits
   * above a queued one, settling the queued one splits the block: the failed
   * row is left above a live turn until the next rebuild, where
   * `carryClientOnlyRows` puts it back at the tail. Left alone deliberately.
   * It is the behaviour that shipped before this walk existed, the row above
   * is still the Retry the user wants, and the alternative — repositioning a
   * row on settle — moves a bubble out from under the pointer heading for it.
   *
   * The walk covers a stranded failed row as well as a queued one, and that
   * half is a judgement rather than a fact the client holds: nothing records
   * whether the failed send was attempted before or after the row being
   * appended. Treating both the same keeps one rule, and the tie-break it
   * picks — the server's row above, the rows with nothing behind them below —
   * is the one that puts every actionable row together at the bottom.
   */
  function appendAboveClientOnly(arr: ChatMessage[], row: ChatMessage): ChatMessage[] {
    let at = arr.length;
    while (at > 0 && isClientOnly(arr[at - 1])) at--;
    if (at === arr.length) return [...arr, row];
    const next = arr.slice();
    next.splice(at, 0, row);
    return next;
  }

  /** Move whatever client-only rows are on screen into the holding map. */
  function stashStrandedSends() {
    for (const m of get(messages)) {
      if (!isClientOnly(m) || !m.roomToken) continue;
      const held = strandedSends.get(m.roomToken) ?? [];
      if (!held.some((x) => x.cid === m.cid)) held.push(m);
      strandedSends.set(m.roomToken, held);
    }
  }

  /**
   * `next`, with a room's client-only rows re-appended at the tail.
   *
   * The tail is where they were: a failed send is always the newest thing in
   * the room from this client's point of view, and its `createdAt` is later
   * than anything the server can return for that room.
   *
   * `token` names the room being rebuilt, or null for the All view, which
   * spans every room. Held rows are taken *out* of the map — they are back on
   * screen, and `stashStrandedSends` puts them away again on the way out.
   *
   * A *queued* row is carried for a named room only. An aggregate view is a
   * read-only reading surface with no composer, and a row whose Send / Edit /
   * Remove act on a room you are not in does not belong in it — so those rows
   * stay in the holding map on the way past rather than being rendered or
   * dropped. A failed row keeps its existing behaviour in that branch: its
   * Retry is the only way back to a message the server never took.
   */
  function carryClientOnlyRows(
    prev: ChatMessage[],
    next: ChatMessage[],
    token: string | null,
  ): ChatMessage[] {
    const carried: ChatMessage[] = [];
    const seen = new Set(next.map((m) => m.cid));
    // The bodies the rebuild is already carrying for this room. A send parked
    // after it reached the wire (see `parkedAfterPost`) may be one of them —
    // the server took it and the reload is where that first becomes visible —
    // and carrying the queued mirror on top would show the same message twice
    // and then send it again when the entry drained. Same body match, and the
    // same fallback, as the stream's own adoption in `appendStreamedRow`.
    const rebuilt = new Set(next.filter((m) => m.role === 'user').map((m) => m.text));
    const take = (m: ChatMessage) => {
      if (!isClientOnly(m) || seen.has(m.cid)) return;
      if (token === null && isQueued(m)) return;
      if (token !== null && m.roomToken !== token) return;
      if (parkedAfterPost.has(m.cid) && rebuilt.has(m.text)) {
        // The entry goes with the row, or the drain would POST a message the
        // server has already answered.
        dropQueuedEntry(m.cid);
        return;
      }
      seen.add(m.cid);
      carried.push(m);
    };
    for (const m of prev) take(m);
    if (token === null) {
      for (const [key, held] of strandedSends) {
        held.forEach(take);
        // Whatever `take` refused above is still the only copy of itself.
        const kept = held.filter((m) => !seen.has(m.cid));
        if (kept.length) strandedSends.set(key, kept);
        else strandedSends.delete(key);
      }
    } else {
      (strandedSends.get(token) ?? []).forEach(take);
      strandedSends.delete(token);
    }
    return carried.length ? [...next, ...carried] : next;
  }

  // ---- The send queue (ISSUE-238) -------------------------------------------
  //
  // Messages typed into a room whose turn is still running. Not to be confused
  // with `streamQueue` above, which holds assistant placeholders waiting for
  // the *stream* of a task the server already has. Nothing in here has been
  // POSTed, which is what makes Edit and Remove possible and what makes Stop a
  // decision about the queue rather than about a set of server-side tasks.
  //
  // Keyed by room *token*, not room id, for the reason `drafts.ts` gives:
  // `web_chat_rooms.id` is an `INTEGER PRIMARY KEY` without `AUTOINCREMENT`,
  // so SQLite hands a freed rowid straight back out and a deleted room's queue
  // would be inherited by whichever room takes its id next.
  //
  // The entry is the source of truth for what will be sent. The transcript row
  // it names (`cid`) is a mirror for display and can be absent — a room switch
  // takes it off screen — so nothing on the drain path may read the payload
  // back off the row.
  interface QueuedSend {
    cid: number;
    text: string;
    attachments: ChatAttachment[];
    // The unresolved half of `attachments`, in upload order: one record per
    // chip whose `path` is null, naming the blob holding its bytes. The drain
    // resolves them a pair at a time, so a half-resolved entry is an ordinary
    // state — see `resolvePendingAttachments`.
    pendingAttachments?: PendingAttachment[];
    // For the optimistic quote on the bubble.
    replyTo?: MessageReply;
    // What the POST carries.
    replyToMsgId?: number;
    // Minted at enqueue rather than at drain, so it is stable across a
    // persistence round trip: two drains of the same restored entry are then
    // answered with one task.
    idempotencyKey?: string;
    // True = will not drain on its own; the user has to release it.
    held: boolean;
    queuedAt: number;
    // Why it is here: a busy room, or no connection (ISSUE-202). Read on the
    // way back from storage, where it decides whether the entry is held —
    // nothing in a live session branches on it except the row's own sentence.
    reason: QueueReason;
  }
  const sendQueue = new Map<string, QueuedSend[]>();
  /**
   * Queued rows whose message did reach a POST, so the server may hold it.
   *
   * `parkSend` puts a message back in the queue when the send never got an
   * answer, and one of the two failures it does that for — a `timeout` — is
   * ambiguous: the task may exist and its echo may arrive over the room stream
   * before the drain ever re-POSTs. The echo has to fold into the row rather
   * than appear beside it, and the entry has to go with it or the message
   * would be sent a second time.
   *
   * A set rather than a flag on the entry, because it is a fact about *this
   * attempt* rather than about the message: an entry queued before any POST
   * (offline, or behind a running turn) has no echo coming, so a body match
   * claiming its row would swallow a message the user still expects to send.
   * Nothing persists it for the same reason — after a reload the server's copy
   * is in the history paint and the re-POST resolves to its task.
   */
  const parkedAfterPost = new Set<number>();

  /**
   * How many messages are waiting to send, per room token.
   *
   * `sendQueue` is a plain `Map` and reactive to nothing, and the transcript —
   * which the open room's own count is derived from — holds rows for one room
   * at a time. So a badge on the room *list* needs this: it is the only place
   * a background room's queue is visible at all, and with the drain running
   * for the active room only (see `canDrain`) it is also the affordance that
   * says which room to open to send it.
   *
   * Kept in step by `persistRoomQueue`, which every mutation of the map
   * already calls, plus the two that deliberately do not write back.
   */
  const queuedCounts = writable<Record<string, number>>({});

  function syncQueuedCounts() {
    const next: Record<string, number> = {};
    for (const [token, entries] of sendQueue) {
      if (entries.length) next[token] = entries.length;
    }
    queuedCounts.set(next);
  }

  const roomTokenOf = (roomId: number) => get(rooms).find((r) => r.id === roomId)?.token;

  // ---- Persistence -----------------------------------------------------
  //
  // A queued message is text the user has committed to sending, so losing it
  // to a reload is worse than losing a draft — and a draft survives. The
  // storage key needs the caller's own id (a shared Talk room has one token
  // across every member of a browser profile), which arrives on
  // `GET /chat/config`: `init()` already awaits that before anything else, so
  // the id is known before there is anything to restore. Until it is, the
  // queue is in memory only and nothing is written.
  //
  // The offline cache below is keyed the same way and for the same reason, so
  // this is the id both stores hang off rather than the queue's alone: one
  // person's cached transcript must not paint into another's session on a
  // profile two people take turns using, exactly as one person's queued
  // message must not be restored into the other's room.
  let storageUserId: string | null = null;
  /**
   * Whether `storageUserId` is a guess rather than something the server said.
   *
   * True only on the path Stage 5 exists for: a cold launch with no connection,
   * where the service worker serves the app but `GET /chat/config` never
   * answers, so the id every cache key is namespaced by comes from the
   * `chat.lastUserId` pointer instead (`offline/lastUser.ts`, and read only
   * inside the shell). Everything painted while this is true came out of a
   * namespace nothing has confirmed, which is what `settleSeededUser` below is
   * for.
   */
  let userIdFromPointer = false;
  /**
   * The id to **write** the cache under, or null while it is only a guess.
   *
   * Reading by the guess is the point of the pointer; writing by it is the
   * hazard, and the two are separated here rather than at each call site. A
   * session that seeded from the pointer and then reaches the server — the
   * config read failed but the room list succeeded, which is the ordinary
   * "launched in a lift, signal returned" sequence — would otherwise write the
   * real session's rooms and transcripts into the guessed user's namespace,
   * where the next session under that id would read them back as its own.
   * That is exactly the cross-user read the namespace exists to prevent, so
   * nothing is written until the server has confirmed who this is. Every
   * helper in `offline/db.ts` treats a null id as "no cache" and no-ops.
   */
  const cacheUserId = () => (userIdFromPointer ? null : storageUserId);
  /**
   * Cids the restore brought in, as against the ones this session minted.
   *
   * Only read on the wrong-guess path, where the two have to be told apart:
   * entries the restore read out of the guessed user's storage are theirs and
   * are left alone, while entries this session wrote are the real user's words
   * sitting in the wrong drawer and have to be taken out of it.
   */
  const restoredCids = new Set<number>();
  // The connectivity subscription's teardown, or null while nothing is
  // watching. Session-lived like the room stream, and dropped by `teardown`
  // for the same reason: a reconcile-and-drain fired at a page the user has
  // left would rebuild a transcript nothing is rendering.
  let unwatchOnline: (() => void) | null = null;
  const QUEUE_KEY_INFIX = ':room:';
  const queueKeyFor = (token: string) =>
    storageUserId ? `${storageUserId}${QUEUE_KEY_INFIX}${token}` : null;

  /**
   * Write one room's queue out, or drop its stored copy when it is empty.
   *
   * Storage mirrors memory rather than standing beside it: every mutation of
   * `sendQueue` calls this for the room it touched, so there is one direction
   * of truth and no reconciliation to get wrong.
   *
   * `writeQueue`'s return — what was actually stored, after its clamp and its
   * bounds — is deliberately *not* adopted back into memory. The in-memory
   * queue is the live one, and a storage bound trimming it would delete a
   * message the user can see on screen and expects to go out. The cost of a
   * refused or trimmed write is a queue that does not survive a reload, which
   * is the same cost `persisted.ts` already swallows for drafts.
   */
  function persistRoomQueue(token: string | null | undefined) {
    // First, and ahead of both guards below: this is the one call every
    // mutation of `sendQueue` makes, and the badge has to follow the map even
    // where the write does not happen — a session with no user id yet queues
    // in memory only.
    syncQueuedCounts();
    if (!token) return;
    const key = queueKeyFor(token);
    if (!key) return;
    writeQueue(key, sendQueue.get(token) ?? []);
  }

  /**
   * Whether a restored entry has to wait for the user to say so.
   *
   * **A `busy` entry always does.** The turn it was written against is over
   * and unobserved, the user is not watching the room they wrote it in, and
   * firing it on page load is the one surprise a send queue must never
   * produce. It comes back as a queued bubble reading "Held — not sent", one
   * tap from going out.
   *
   * An `offline` entry is the opposite case (ISSUE-202): it was written to a
   * server that could not be reached, and going out by itself when the
   * connection returns is the whole of what it is for — a relaunch is exactly
   * the moment that has to work, since a force-quit or an OS kill is what the
   * durability is for. So it comes back ready, up to
   * `OFFLINE_AUTO_SEND_MAX_AGE_MS`; past that the hold is the better answer
   * again, because a message written days ago fires into a conversation that
   * has moved on while the user is looking at something else.
   */
  function holdOnRestore(entry: StoredQueuedSend, now: number): boolean {
    // A hold the last session applied is kept, whatever the reason says. Stop,
    // an error and a parked confirmation mark *every* entry in the room
    // through `holdRoomQueue`, offline ones included — so without this, three
    // messages queued in a lift and then held by a Stop would come back unheld
    // and fire into the turn the user had just abandoned. The age rule can
    // only ever keep a hold; it cannot clear one.
    if (entry.held) return true;
    if (entry.reason !== 'offline') return true;
    return now - entry.queuedAt >= OFFLINE_AUTO_SEND_MAX_AGE_MS;
  }

  /**
   * Bring every stored queue back into memory.
   *
   * Called from `init()` after the room list lands, because the token is what
   * the queue is keyed by and a key naming a room this user no longer has is
   * left alone rather than restored — there is nothing to render it in. Left
   * *alone* rather than dropped: a room that is merely archived, or a room
   * list that came back short, must not cost the text. The TTL collects it.
   *
   * A room that already has entries in memory keeps them. `init()` runs again
   * on every remount of the page while the session outlives it, and storage is
   * this map's mirror — so re-reading it over a live queue would duplicate
   * every entry.
   *
   * This is one of the two mutations of `sendQueue` that do *not* write back
   * (`forgetRoom` is the other), and the exception is deliberate: it would
   * rewrite the whole map on every page load, for rooms nobody has touched,
   * which is the pointless write `drafts.ts` goes out of its way to avoid.
   * Nothing depends on it, because the two fields the restore decides — the
   * cid, re-minted, and `held`, derived by `holdOnRestore` — are derived the
   * same way every time from what is stored, so the two copies can disagree
   * about them and still produce the same result on the next load.
   */
  function restoreQueues() {
    if (!storageUserId) return;
    const now = Date.now();
    // Only this user's own keys. A browser profile two people take turns using
    // holds both their queues under the one storage key, and one person's
    // committed message must never be restored into the other's transcript.
    const prefix = `${storageUserId}${QUEUE_KEY_INFIX}`;
    const known = new Set(get(rooms).map((r) => r.token));
    for (const [storedKey, entries] of Object.entries(readAllQueues())) {
      if (!storedKey.startsWith(prefix)) continue;
      const token = storedKey.slice(prefix.length);
      if (!token || !known.has(token) || sendQueue.has(token)) continue;
      sendQueue.set(
        token,
        entries.map((entry) => {
          // The stored cid belongs to the session that wrote it. `cidCounter`
          // starts fresh on every load, so carrying it over would collide with
          // a row this session is about to mint. The cid is a client-local
          // display key, not durable identity.
          const cid = nextCid();
          // Noted as restored, which only the wrong-guess path in
          // `settleSeededUser` reads: it is what tells an entry belonging to
          // whoever the storage key names from one this session typed.
          restoredCids.add(cid);
          return { ...entry, cid, held: holdOnRestore(entry, now) };
        }),
      );
    }
    // `restoreQueues` is one of the two mutations that deliberately do not
    // write back, so the sync `persistRoomQueue` would have done is here.
    syncQueuedCounts();
  }

  /**
   * Settle a session that booted from the last-user pointer against the id the
   * server actually reports (ISSUE-202).
   *
   * A cold launch with no connection has to read the cache by *some* id, and
   * the only one available is the pointer `init()` wrote on the last
   * successful config read. That is a guess, and this is the net underneath
   * it: the first config that answers either confirms the guess — the ordinary
   * case, one device, one person — or says it was wrong, in which case
   * everything painted from the other namespace goes.
   *
   * "Everything" is four things, and each of them would otherwise outlive the
   * correction. The transcript, the room list and the selected room are the
   * visible half. The **send queue** is the half that matters, and it has two
   * sides. Entries the restore read out of the guessed id's storage belong to
   * whoever that is: they go out of memory, and are deliberately left in
   * storage, where that person's own next session restores and sends them as
   * themselves. Entries *this* session typed are the real user's words written
   * into the wrong drawer by `persistRoomQueue`, which keyed them by the guess
   * — those are taken back out of the guessed id's storage, or they would be
   * restored and auto-sent under that identity later.
   *
   * They are dropped rather than re-keyed onto the right id. They were written
   * into rooms read out of another namespace, against a transcript this is
   * about to throw away, so carrying them forward would move a message into a
   * conversation it was not written in. What matters is that they cannot go
   * out as someone else, and nothing was sent while the guess stood: `canDrain`
   * refuses for the whole of it.
   *
   * The caller repaints — `init()` by carrying on into its own room-list and
   * history reads, `onBackOnline` through `recoverStream`. Returns whether the
   * guess was wrong, because the caller's repaint differs in the two cases.
   */
  function settleSeededUser(live: ChatConfig): boolean {
    if (!userIdFromPointer) return false;
    userIdFromPointer = false;
    const real = live.user_id ?? null;
    const guess = storageUserId;
    storageUserId = real;
    rememberLastUserId(real);
    if (real === guess) return false;
    if (guess) {
      for (const [token, entries] of sendQueue) {
        const theirs = entries.filter((entry) => restoredCids.has(entry.cid));
        if (theirs.length === entries.length) continue;
        writeQueue(`${guess}${QUEUE_KEY_INFIX}${token}`, theirs);
      }
    }
    sendQueue.clear();
    restoredCids.clear();
    syncQueuedCounts();
    messages.set([]);
    rooms.set([]);
    // The selection is an id out of the other namespace, and room ids are
    // per-user rowids — left standing, the reload below would fetch whichever
    // room happens to share the number. The caller picks one from the room
    // list it refetches.
    activeRoomId.set(null);
    externalTurnDisplay.set(normalizeExternalTurnDisplay(live.external_turn_display));
    return true;
  }

  /**
   * Every blob some queue entry still names, anywhere on this profile.
   *
   * Read from **storage** as well as from memory, and both halves are needed.
   * Memory holds entries queued this session, including a session with no user
   * id where nothing is written at all. Storage holds what `restoreQueues`
   * deliberately did not restore — another user's queue on a shared profile,
   * and a key naming a room this user no longer has, both of which it leaves
   * alone rather than dropping. Collecting from memory alone would delete the
   * bytes out from under either.
   *
   * A file staged in the composer and not yet sent is named by nothing here,
   * which is what `BLOB_GC_MIN_AGE_MS` is for.
   */
  function referencedBlobIds(): Set<string> {
    const ids = new Set<string>();
    for (const entries of sendQueue.values()) {
      for (const entry of entries) {
        for (const p of entry.pendingAttachments ?? []) ids.add(p.blobId);
      }
    }
    for (const entries of Object.values(readAllQueues())) {
      for (const entry of entries) {
        for (const p of entry.pendingAttachments ?? []) ids.add(p.blobId);
      }
    }
    return ids;
  }

  // ---- The offline read cache (ISSUE-202) ------------------------------
  //
  // What the server last said, per room, so the transcript paints with no
  // connection at all. `offline/db.ts` holds the storage and its bounds; what
  // is here is when to read it, when to write it, and the one thing storage
  // cannot decide — which rows count as the same turn on the way in.
  //
  // Two writers. `loadHistory` replaces a room's tail wholesale from a fetch,
  // and the room stream folds each frame into whatever is already stored, for
  // *any* room rather than the one on screen: that is what leaves a background
  // room current when the user switches to it with no connection.
  //
  // Nothing here is awaited on a render path and nothing here can throw — every
  // helper resolves, an unusable database reading as an empty cache — so a
  // storage failure costs the offline paint and nothing else.

  /**
   * How long a room's streamed frames are collected before they are written.
   *
   * A trailing debounce rather than a write per frame: a turn arrives as a
   * burst (the user row, the assistant row running, the same row finished), and
   * one transaction per burst is the difference between a write and a write
   * storm on a room the user is not even looking at. Trailing rather than
   * leading, because the last frame of a burst is the one worth having.
   */
  const CACHE_WRITE_DEBOUNCE_MS = 2000;
  const pendingCacheRows = new Map<
    string,
    { rows: ChatRoomEvent[]; timer: ReturnType<typeof setTimeout> }
  >();

  /** Write one room's collected frames out, cancelling whatever is scheduled. */
  function flushCachedRoom(token: string) {
    const pending = pendingCacheRows.get(token);
    if (!pending) return;
    pendingCacheRows.delete(token);
    clearTimeout(pending.timer);
    void appendTranscriptRows(cacheUserId(), token, pending.rows);
  }

  function flushCachedRooms() {
    for (const token of [...pendingCacheRows.keys()]) flushCachedRoom(token);
  }

  /**
   * A wire row as it should be *stored*: a cached turn is never a live one.
   *
   * An assistant row can be cached mid-turn — a history load carries the
   * running turn so the resume loop can bind to it, and the stream carries it
   * again as it progresses. Stored with that status intact, the offline paint
   * rebuilds it as `streaming: true` with no segments and nothing to settle
   * it: a blank bubble pulsing forever, for a turn that finished hours ago.
   * Dropping the status leaves the row rendering whatever the server had said
   * by then, which is what is actually known about it.
   */
  function cacheableRow(row: ChatRoomEvent): ChatRoomEvent {
    if (row.role !== 'assistant' || !inFlight(row.status)) return row;
    const { status: _live, ...settled } = row;
    return settled;
  }

  /** Collect a streamed row for its room's cached tail. */
  function cacheStreamedRow(row: ChatRoomEvent) {
    const token = row.room_token;
    if (!storageUserId || !token) return;
    const pending = pendingCacheRows.get(token);
    if (pending) {
      pending.rows.push(cacheableRow(row));
      return;
    }
    pendingCacheRows.set(token, {
      rows: [cacheableRow(row)],
      timer: setTimeout(() => flushCachedRoom(token), CACHE_WRITE_DEBOUNCE_MS),
    });
  }

  /**
   * Take deleted messages out of the cache as well as off the screen.
   *
   * Including out of whatever is waiting to be written: a frame collected a
   * moment ago can name a row the deletion has just removed, and letting the
   * flush land after the removal would put it back until the room is next
   * loaded in full.
   */
  function forgetCachedMessages(msgIds: number[]) {
    if (!storageUserId || !msgIds.length) return;
    const gone = new Set(msgIds);
    for (const pending of pendingCacheRows.values()) {
      pending.rows = pending.rows.filter(
        (r) => typeof r.msg_id !== 'number' || !gone.has(r.msg_id),
      );
    }
    void removeCachedMessages(cacheUserId(), msgIds);
  }

  /**
   * The transcript row that mirrors a queued entry.
   *
   * Rebuilt rather than stored: the entry is the source of truth for what will
   * be sent, and the row is a mirror for display. `createdAt` comes from
   * `queuedAt` so a restored bubble keeps the time it was actually written.
   */
  function queuedRow(entry: QueuedSend, roomToken: string): ChatMessage {
    return {
      cid: entry.cid,
      role: 'user',
      text: entry.text,
      segments: [],
      streaming: false,
      roomToken,
      attachments: entry.attachments.map((x) => x.name),
      attachmentPaths: entry.attachments.map((x) => x.workspace_path ?? null),
      createdAt: new Date(entry.queuedAt).toISOString(),
      replyTo: entry.replyTo,
      sendState: 'queued',
      queueReason: entry.reason,
      ...(entry.held ? { queueHeld: true } : {}),
    };
  }

  /**
   * Give a room's queued entries their rows, for any entry that has none.
   *
   * A row that is merely off screen comes back through `carryClientOnlyRows`
   * with whatever the user last saw on it, so this only ever fires for an
   * entry restored from storage — where the queue outlived every row it had.
   */
  function appendQueuedRows(roomToken: string) {
    const entries = sendQueue.get(roomToken);
    if (!entries?.length) return;
    messages.update((arr) => {
      const seen = new Set(arr.map((m) => m.cid));
      const missing = entries.filter((e) => !seen.has(e.cid));
      return missing.length ? [...arr, ...missing.map((e) => queuedRow(e, roomToken))] : arr;
    });
  }

  /**
   * Stop every entry in a room's queue from draining on its own.
   *
   * The rule the whole queue turns on: it drains when the turn it was written
   * against finished normally, and holds otherwise. Holding rather than
   * discarding — the follow-ups were written against work that has just been
   * abandoned or has just failed, so they must not fire, but destroying the
   * text to say so is a worse trade than one tap to release it.
   */
  function holdRoomQueue(token: string | null | undefined) {
    if (!token) return;
    const entries = sendQueue.get(token);
    if (!entries?.length) return;
    for (const entry of entries) {
      entry.held = true;
      updateMsg(entry.cid, (m) => {
        m.queueHeld = true;
      });
    }
    // The rows are only in `messages` while the room is on screen; a send that
    // fails after a room switch has to reach the stashed copies too, or they
    // come back reading "Waiting to send" for something that never will.
    for (const m of strandedSends.get(token) ?? []) {
      if (isQueued(m)) m.queueHeld = true;
    }
    // Storage mirrors memory after every mutation, this one included. A
    // restore re-holds everything regardless, so nothing depends on the flag
    // reaching disk — but leaving one mutation out is how the two copies start
    // disagreeing about something that later does.
    persistRoomQueue(token);
  }

  /** Where a queued entry lives, or null if `cid` names no queued message. */
  function findQueued(cid: number): { token: string; entries: QueuedSend[]; idx: number } | null {
    for (const [token, entries] of sendQueue) {
      const idx = entries.findIndex((e) => e.cid === cid);
      if (idx !== -1) return { token, entries, idx };
    }
    return null;
  }

  /**
   * Take a queued entry out of the queue, leaving its row where it is.
   *
   * The drain's half of `takeQueued`. A drained entry's row is not going
   * anywhere — it is the turn now, settled or failed — so only the entry is
   * removed, and storage is mirrored as after any other mutation.
   *
   * A no-op for a cid that names no entry, which is every ordinary send and
   * every retry: the send path calls this on each terminal outcome rather than
   * asking first whether this message came out of the queue.
   */
  function dropQueuedEntry(cid: number): void {
    parkedAfterPost.delete(cid);
    const found = findQueued(cid);
    if (!found) return;
    found.entries.splice(found.idx, 1);
    if (!found.entries.length) sendQueue.delete(found.token);
    forgetStrandedRow(found.token, cid);
    persistRoomQueue(found.token);
  }

  /**
   * Drop one off-screen row from the holding map.
   *
   * Shared by both ways an entry leaves the queue: a row left behind there
   * comes back on the next room switch with no entry behind it, rendering as
   * something that is going to send and never will.
   */
  function forgetStrandedRow(token: string, cid: number): void {
    const stashed = (strandedSends.get(token) ?? []).filter((m) => m.cid !== cid);
    if (stashed.length) strandedSends.set(token, stashed);
    else strandedSends.delete(token);
  }

  /** Take a queued entry out of the queue and its row off the transcript. */
  function takeQueued(cid: number): { token: string; entry: QueuedSend } | null {
    parkedAfterPost.delete(cid);
    const found = findQueued(cid);
    if (!found) return null;
    const [entry] = found.entries.splice(found.idx, 1);
    if (!found.entries.length) sendQueue.delete(found.token);
    messages.update((arr) => arr.filter((m) => m.cid !== cid));
    // And out of the holding map, or a later room switch would re-append a row
    // with no entry behind it.
    forgetStrandedRow(found.token, cid);
    persistRoomQueue(found.token);
    return { token: found.token, entry };
  }

  // Clone a segment (and its tool) so a keyed {#each} sees a fresh reference.
  // text/thinking are flat; only a tool segment has a nested object to clone.
  const cloneSeg = (s: Segment): Segment =>
    s.kind === 'tool' ? { ...s, tool: { ...s.tool } } : { ...s };

  const updateMsg = (cid: number, fn: (m: ChatMessage) => void) => {
    messages.update((arr) => {
      const idx = arr.findIndex((x) => x.cid === cid);
      if (idx === -1) return arr;
      const m = arr[idx];
      fn(m); // the reducer + helpers mutate the message in place
      // Rebuild references at every level — new array, new message object,
      // new segment + tool objects — so BOTH keyed `{#each}`s (the page's over
      // $messages, and Message's over segments) re-render. Svelte 5 treats a
      // same-reference keyed item as unchanged and skips its child, so an
      // in-place deep mutation (a streamed text append, the `result`
      // overwrite) never reaches the DOM — which is exactly why a full page
      // reload (rebuilds the array via messages.set) rendered correctly while
      // the live in-place stream froze after the first paint.
      const next = arr.slice();
      next[idx] = { ...m, segments: m.segments.map(cloneSeg) };
      return next;
    });
  };

  function applyEvent(cid: number, kind: string, payload: Record<string, any>) {
    updateMsg(cid, (m) => {
      if (kind === 'task_started') {
        // Generic "working on it" verb stamped by the executor (shared with
        // Talk). We already seeded a client-side verb when the placeholder
        // was created, so skip the overwrite to avoid a flicker from one
        // random verb to another — real status (progress_text / tool_start /
        // the first text delta) takes over via the reducer below.
        if (payload.text && !m.progress) m.progress = String(payload.text);
        // Falls through: the reducer also settles the open block, because a
        // retry re-runs under this same message (ISSUE-361). The verb stays
        // here — it is message state, not a segment.
      }
      // Every event kind builds the ordered segment list. The reducer is pure
      // and unit-tested in segments.test.ts.
      applySegmentEvent(m, kind, payload);
    });
  }

  function streamTask(taskId: number, cid: number): { stop: () => void } {
    let lastSeq = 0;
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let finished = false;
    // A task parked awaiting confirmation owns its room until the user acts —
    // hold the queue rather than advancing past it.
    let paused = false;
    // Consecutive SSE errors while the browser is still retrying on its own.
    // Same rule as the room stream: a blip must not cost the connection, but
    // a persistently failing endpoint concedes to polling eventually.
    let sseFailures = 0;
    const SSE_FAILURE_LIMIT = 3;

    // Stop the stream without touching the queue. Used both as the terminal
    // path (settle, below) and as the external "stop now" hook for room
    // switches / unmount.
    const halt = () => {
      if (finished) return;
      finished = true;
      if (es) {
        es.close();
        es = null;
      }
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    // Natural terminal: halt, then let the session advance to the next queued
    // task (or go idle) — unless we paused for a confirmation.
    //
    // The terminal kind travels because the *send* queue distinguishes them:
    // only a turn that finished normally releases the messages typed behind it.
    const settle = (terminal: StreamTerminal) => {
      if (finished) return;
      halt();
      onStreamSettled(paused, terminal);
    };

    const handle = (kind: string, dataStr: string, seq: number) => {
      // Idempotent on seq. An SSE reconnect/replay (Last-Event-ID) or a brief
      // SSE↔poll overlap can redeliver an already-applied event; seq is
      // writer-assigned and monotonic per task, so anything at-or-below the
      // high-water mark is a duplicate. (Poll already fetches seq > lastSeq;
      // this guards the SSE branch too.) seq-less events (0) bypass the guard.
      if (seq) {
        if (seq <= lastSeq) return;
        lastSeq = seq;
      }
      let payload: Record<string, any> = {};
      try {
        payload = JSON.parse(dataStr);
      } catch {
        /* keep {} */
      }
      // A reducer/render throw must never wedge the stream — keep advancing
      // so later events (notably `result` / `done`) still apply.
      try {
        applyEvent(cid, kind, payload);
      } catch {
        /* swallow */
      }
      if (kind === 'confirmation') paused = true;
      // `done` is the normal terminal; settle on `error`/`cancelled` too so a
      // failure that arrives without a trailing `done` (older paths, dropped
      // connection) can't leave the room stuck on "Working…".
      if (kind === 'done' || kind === 'cancelled' || kind === 'error') settle(kind);
    };

    const poll = async (): Promise<boolean> => {
      if (finished) return false;
      try {
        const { events } = await getTaskEvents(taskId, lastSeq);
        for (const ev of events) handle(ev.kind, JSON.stringify(ev.payload), ev.seq);
      } catch {
        /* transient; try again next tick */
        return false;
      }
      // A snapshot landed. If SSE is still live beside a running poll timer,
      // the timer was only the hydration retry below — the stream carries the
      // tail from here. (When SSE conceded to polling, `es` is null and the
      // timer is the live path, so it stays.)
      if (es && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
      return true;
    };
    const startPolling = () => {
      if (pollTimer || finished) return;
      poll();
      pollTimer = setInterval(poll, pollIntervalMs);
    };

    try {
      es = new EventSource(chatStreamUrl(taskId), { withCredentials: true });
      let opened = false;
      es.onopen = () => {
        sseFailures = 0;
        // EventSource reuses this object when it reconnects. The server will
        // replay from Last-Event-ID, but a proxy may buffer that replay until a
        // later frame. Read the durable snapshot as soon as the connection is
        // back so already-written progress appears immediately.
        if (opened) void poll();
        opened = true;
      };
      for (const k of STREAM_KINDS) {
        es.addEventListener(k, (e: MessageEvent) => {
          // The browser fires a native 'error' event (no data) on the
          // EventSource for connection failures, which collides with our
          // server-sent `event: error` task error. Ignore the data-less
          // native one — es.onerror handles the fallback to polling.
          if (e.data == null) return;
          handle(k, e.data, Number(e.lastEventId) || 0);
        });
      }
      es.onerror = () => {
        if (finished) return;
        sseFailures += 1;
        // readyState CONNECTING (0, per the spec constant) means the browser
        // has already scheduled its own retry; closing here would throw that
        // away and pre-empt exactly the free reconnect SSE was chosen for. Let
        // it try, up to the limit. Anything else — CLOSED, or an implementation
        // with no readyState at all — is fatal, so fall back at once.
        if (es?.readyState === 0 && sseFailures < SSE_FAILURE_LIMIT) return;
        // SSE failed (or the mock backend isn't an event-stream): close it
        // and fall back to polling the snapshot endpoint.
        if (es) {
          es.close();
          es = null;
        }
        startPolling();
      };
    } catch {
      startPolling();
    }

    // SSE is the low-latency tail, not the initial-state loader. Hydrate from
    // the durable log now; the seq guard deduplicates its overlap with SSE. A
    // failed snapshot must not wait for the next SSE frame to retry — the poll
    // timer reruns it until one lands (and stops again once it does, above).
    void poll().then((ok) => {
      if (!ok) startPolling();
    });

    return { stop: halt };
  }

  // Start streaming `taskId` immediately. Caller guarantees no stream is active.
  function startStream(taskId: number, cid: number) {
    status.set('streaming');
    activeTaskId.set(taskId);
    activeStream = streamTask(taskId, cid);
  }

  // Stream now, or queue behind the active stream. Queued placeholders show a
  // "Queued…" line until their turn (task_started then stamps the real verb).
  function enqueueStream(taskId: number, cid: number) {
    if (get(activeTaskId) === taskId) return;
    if (streamQueue.some((q) => q.taskId === taskId)) return;
    if (activeStream) {
      // Insert in taskId order: ids are monotonic with backend execution
      // order, and concurrent send() POSTs can resolve out of order, so a
      // plain push could stream them in the wrong sequence.
      const at = streamQueue.findIndex((q) => q.taskId > taskId);
      if (at === -1) streamQueue.push({ taskId, cid });
      else streamQueue.splice(at, 0, { taskId, cid });
      updateMsg(cid, (m) => {
        if (!m.progress) m.progress = 'Queued…';
      });
      // A stream is still running — keep the room in the streaming state
      // (send() flipped it to 'sending' optimistically before the POST).
      status.set('streaming');
    } else {
      startStream(taskId, cid);
    }
  }

  // The active stream reached a terminal state. If it paused for a
  // confirmation, hold the queue (the user must confirm/reject first).
  // Otherwise advance to the next queued task, or go idle.
  function onStreamSettled(paused: boolean, terminal: StreamTerminal) {
    activeStream = null;
    const rid = get(activeRoomId);
    // The send queue's one rule: a turn that finished normally releases the
    // messages typed behind it, and anything else holds them. A paused turn
    // holds for the sharper reason that the room is idle only because it is
    // waiting on the user — firing past an unanswered question is the surprise
    // the queue exists to avoid.
    //
    // Decided here, *above* the stream-queue advance, because a second task
    // waiting its turn returns early below. A room can hold two live tasks (a
    // Talk turn adopted by `pickUpStreamedTask`, or two resumed from history),
    // and if this turn's Stop did not mark the entries, the *next* turn's
    // `done` would release messages written behind a turn the user abandoned.
    if (rid != null && (paused || terminal !== 'done')) holdRoomQueue(roomTokenOf(rid));
    if (!paused) {
      const next = streamQueue.shift();
      if (next) {
        startStream(next.taskId, next.cid);
        return;
      }
    }
    status.set('idle');
    activeTaskId.set(null);
    // A turn finished in the open room — its reply is now on screen, so mark
    // the room read (visibility-gated) before the user switches away.
    if (rid == null) return;
    markActiveRead(rid);
    if (!paused && terminal === 'done') void drainSendQueue(rid);
  }

  // Halt the active stream and drop the queue without advancing — for room
  // switches and unmount. Remounting/reselecting resumes from history.
  function stopActive() {
    if (activeStream) {
      activeStream.stop();
      activeStream = null;
    }
    streamQueue = [];
    resetPaging();
    status.set('idle');
    activeTaskId.set(null);
    cancelRequested = false;
    // The single "this transcript is about to be replaced" hook — every caller
    // (selectRoom, selectView, teardown) clears `messages` right after this, so
    // rows that exist only on the client have to be put away here or they are
    // gone before the rebuild that would carry them ever runs.
    stashStrandedSends();
    // `sendQueue` is deliberately NOT cleared here. It is keyed by room and
    // survives a switch, an unmount and a Stop: nothing in it has been sent,
    // so there is nothing to abandon — the rows come back with the room.
    // The echo buffer belongs to the turn that opened it, and this call has
    // just released the gates (`status` back to 'idle') that were supposed to
    // keep another turn from draining it. Abandoned rather than drained: those
    // frames are for a room the user has left, and `loadHistory` rebuilds that
    // room's transcript from the server on the way back in.
    pendingSend = null;
  }

  // Set a single room's unread badge locally (optimistic clears + merge).
  function setRoomUnread(id: number, n: number) {
    rooms.update((r) => r.map((x) => (x.id === id ? { ...x, unread_count: n } : x)));
  }

  // Persist "I've read this room up to now" — but only while the tab is
  // actually showing it (a background tab shouldn't eat the badge). The open
  // room's *display* is held at 0 by refreshRooms regardless; this call makes
  // that durable so the badge stays clear after switching away.
  function markActiveRead(roomId: number) {
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    setRoomUnread(roomId, 0);
    markRoomRead(roomId).catch(() => {
      /* transient; next open/poll retries */
    });
  }

  // Re-fetch the room list and merge fresh unread counts (and any name/origin
  // backfill) into the existing entries by id — no drop of local state. The
  // active room is forced to 0 so looking at it always reads as clear, even if
  // a count lands before the mark-read round-trips.
  //
  // The merged list IS re-sorted by activity, which is the one thing this pass
  // used to refuse to do: the sidebar's order is a function of `last_activity`
  // now, so freezing the order here would strand a room whose stream frames the
  // client missed (a sleeping tab, a dropped connection) wherever it happened
  // to be. Reconciling that is the whole reason this poll survives.
  async function refreshRooms(timeoutMs = 0) {
    let list: ChatRoom[];
    try {
      ({ rooms: list } = await getChatRooms(timeoutMs));
    } catch {
      return;
    }
    const byId = new Map(list.map((r) => [r.id, r]));
    const active = get(activeRoomId);
    const unreadFor = (r: ChatRoom) => (r.id === active ? 0 : (r.unread_count ?? 0));
    rooms.update((cur) => {
      const seen = new Set<number>();
      const merged = cur.map((r) => {
        const fresh = byId.get(r.id);
        seen.add(r.id);
        if (!fresh) return r; // transiently absent — keep as-is
        return {
          ...r,
          name: fresh.name,
          origin: fresh.origin,
          talk_token: fresh.talk_token,
          // model/effort/brain ride along so the header's model badge and the
          // settings modal can't go stale until reload when a default is
          // changed on another device.
          model: fresh.model,
          effort: fresh.effort,
          brain: fresh.brain,
          // Same reason, for the sidebar tint: the stream's first pass only
          // establishes the baseline, so a colour changed on another device
          // while this tab was disconnected produces no `room` frame on
          // reconnect and this reconciler is the only thing that would catch
          // it (ISSUE-433). `?? null` because the key is optional on the wire.
          color: fresh.color ?? null,
          unread_count: unreadFor(fresh),
          // Whichever stamp is newer. This response was built before it was
          // awaited, so a frame that landed in between is ahead of it — taking
          // the server's value unconditionally would drop a room the user is
          // watching back down the list until the next poll.
          last_activity:
            (fresh.last_activity ?? '') > (r.last_activity ?? '')
              ? fresh.last_activity
              : r.last_activity,
        };
      });
      // Append rooms that newly surfaced (e.g. a Talk room first mirrored in).
      for (const fresh of list) {
        if (!seen.has(fresh.id)) merged.push({ ...fresh, unread_count: unreadFor(fresh) });
      }
      return sortRoomsByActivity(merged);
    });
    // Write the merged list through, not just `init()`'s first read. A room
    // created or renamed since then would otherwise be missing from the cached
    // sidebar — and a room missing from it has no cached token, which is what
    // its transcript cache is keyed by, so it could not be painted at all.
    void writeRooms(cacheUserId(), get(rooms));
  }

  // ---- Held outbound drafts ----

  const outboundDrafts = writable<OutboundDraft[]>([]);

  // Ids answered in the last few seconds, and when. A snapshot is computed on a
  // worker thread on the room-check tick, so one read a moment before `release`
  // committed its claim arrives *after* the answer and puts the card back —
  // with a live Send button on mail already going out. Pressing it again is
  // harmless (`release` short-circuits on a `sent` row and returns the same
  // Message-ID), but the card reads as the send not having taken, which on an
  // irreversible action is the one thing this surface must not say. Bounded, so
  // a misclassified answer cannot hide held mail for longer than the window:
  // after it, the server's own view wins.
  const answeredAt = new Map<number, number>();
  const ANSWERED_SUPPRESS_MS = 20_000;

  function suppressAnswered(list: OutboundDraft[]): OutboundDraft[] {
    if (answeredAt.size === 0) return list;
    const now = Date.now();
    for (const [id, at] of answeredAt) {
      if (now - at > ANSWERED_SUPPRESS_MS) answeredAt.delete(id);
    }
    return answeredAt.size === 0 ? list : list.filter((d) => !answeredAt.has(d.id));
  }

  function dropDraftCard(draftId: number) {
    outboundDrafts.update((list) => list.filter((d) => d.id !== draftId));
  }

  /**
   * Drop a card and hold it down against an in-flight snapshot.
   *
   * Only for an answer the server **accepted**. On a refusal the row may still
   * be held, and suppressing there would hide answerable mail for the length of
   * the window — so those paths drop the card and let the re-read be the
   * authority, which is what puts it back if it is still there.
   */
  function forgetAnswered(draftId: number) {
    answeredAt.set(draftId, Date.now());
    dropDraftCard(draftId);
  }

  // Coalesces concurrent callers onto one request. A frame that stubs K drafts
  // makes K cards each ask for the full row, and the endpoint they ask is
  // deliberately un-budgeted — so without this the byte budget is "saved" by
  // fetching every body K times over.
  let draftsInFlight: Promise<void> | null = null;

  function refreshDrafts(): Promise<void> {
    if (draftsInFlight) return draftsInFlight;
    draftsInFlight = (async () => {
      try {
        const res = await listOutboundDrafts();
        outboundDrafts.set(suppressAnswered(res.drafts ?? []));
      } catch {
        // Same rule as the confirmations poll: a failed read is nothing the
        // user did, and the cards on screen stay until a read succeeds.
        // Clearing them on a transient failure would read as the mail having
        // gone out.
      } finally {
        draftsInFlight = null;
      }
    })();
    return draftsInFlight;
  }

  /**
   * Apply a drafts snapshot from the stream or the polling fallback.
   *
   * A whole-set replace, because that is what the server sends — and the set
   * shrinking is how a draft reports being sent or discarded elsewhere. Guarded
   * on `unavailable`, since a failed server-side read must leave the cards
   * alone rather than empty them.
   */
  function applyDraftsSnapshot(drafts: OutboundDraft[] | undefined, unavailable = false) {
    if (unavailable || !drafts) return;
    outboundDrafts.set(suppressAnswered(drafts));
  }

  /**
   * Approve or discard a held draft, returning whether it left the list.
   *
   * Removal is **optimistic on success only**, and the row is kept on every
   * failure: this card is the only place the held mail is visible in the web
   * UI, so dropping it on a refused approve would leave the user believing a
   * message went out that did not.
   *
   * A 409 is read against the action, not on its own. "Someone discarded this
   * elsewhere" settles a *discard* and is a refusal of a *send* — dropping the
   * card silently in the second case gives the user who pressed Send the same
   * feedback a successful send gives them, for mail that never left.
   */
  async function answerDraft(draftId: number, action: 'approve' | 'discard'): Promise<boolean> {
    let res;
    try {
      res =
        action === 'approve'
          ? await approveOutboundDraft(draftId)
          : await discardOutboundDraft(draftId);
    } catch {
      // An expired session throws `AuthError` out of the fetch wrapper. Without
      // this the button simply un-busies and nothing is said, on the one
      // surface where "nothing happened" is indistinguishable from "it worked".
      notifyError('Could not reach the server. Your message has not been sent.', {
        key: `chat:draft:${draftId}`,
      });
      return false;
    }
    if (res.ok) {
      forgetAnswered(draftId);
      return true;
    }
    const settledElsewhere =
      res.failure === 'gone' ||
      (res.failure === 'conflict' && (res.state === 'discarded' || res.state === 'sent'));
    if (settledElsewhere && action === 'discard') {
      // The row is already gone or already binned, which is what Discard was
      // for. Only this view was stale, so the card goes without a complaint.
      dropDraftCard(draftId);
      void refreshDrafts();
      return true;
    }
    if (settledElsewhere) {
      // The user pressed Send and nothing went out. Saying so is the whole
      // point: dropping the card silently here gives them exactly the feedback
      // a successful send gives.
      notifyError(
        res.state === 'sent'
          ? 'That message had already been sent.'
          : res.state === 'discarded'
            ? 'That message was discarded elsewhere, so it was not sent.'
            : 'That draft is no longer there, so nothing was sent.',
        { key: `chat:draft:${draftId}` },
      );
      dropDraftCard(draftId);
      void refreshDrafts();
      return false;
    }
    if (res.failure === 'sent_unrecorded') {
      // The mail left. Never a retry — see `DraftFailure`.
      notifyError(
        'That message was sent, but recording it failed. Check your Sent folder before resending.',
        { key: `chat:draft:${draftId}` },
      );
    } else if (res.failure === 'conflict') {
      // Either `sending`, or a 409 that named no state — both mean the row is
      // in motion and the card must stay.
      notifyError('That message is being sent right now.', {
        key: `chat:draft:${draftId}`,
      });
    } else {
      notifyError(res.error || 'Could not answer that draft.', {
        key: `chat:draft:${draftId}`,
      });
    }
    // The row's own state may have moved under us; re-read rather than guess.
    void refreshDrafts();
    return false;
  }

  /** Replace a held draft's body. The server returns the re-read row. */
  async function editDraft(draftId: number, body: string): Promise<boolean> {
    let res;
    try {
      res = await editOutboundDraft(draftId, body);
    } catch {
      notifyError('Could not save that edit.', { key: `chat:draft:${draftId}` });
      return false;
    }
    if (res.ok) {
      // The edit committed. A 2xx whose body did not parse is still a committed
      // edit, so it closes the editor and leaves the re-read to settle the
      // displayed text — reporting a failure there would tell the user their
      // correction was lost while the server holds it.
      if (res.draft) {
        const updated = res.draft;
        outboundDrafts.update((list) => list.map((d) => (d.id === draftId ? updated : d)));
      } else {
        void refreshDrafts();
      }
      return true;
    }
    notifyError(res.error || 'Could not save that edit.', {
      key: `chat:draft:${draftId}`,
    });
    void refreshDrafts();
    return false;
  }

  function startRoomsRefresh() {
    if (roomsTimer) return;
    roomsTimer = setInterval(() => {
      void refreshRooms();
    }, ROOMS_REFRESH_MS);
  }

  function stopRoomsRefresh() {
    if (roomsTimer) {
      clearInterval(roomsTimer);
      roomsTimer = null;
    }
  }

  const inFlight = (s?: string) => s === 'pending' || s === 'locked' || s === 'running';
  // A task that has not produced its final answer yet — in-flight, or parked
  // awaiting a confirmation the user must act on.
  const unsettled = (s?: string) => inFlight(s) || s === 'pending_confirmation';

  // ---- Room-stream frame handling ----

  // A burst of streamed rows would otherwise fire one mark-read POST each.
  // The cursor call is idempotent and the display is already held at 0, so
  // coalescing on a short window costs nothing and saves the round-trips.
  let lastStreamReadAt = 0;
  const STREAM_READ_THROTTLE_MS = 1000;
  function markActiveReadThrottled(roomId: number) {
    const now = Date.now();
    if (now - lastStreamReadAt < STREAM_READ_THROTTLE_MS) return;
    lastStreamReadAt = now;
    markActiveRead(roomId);
  }

  // Open a task stream for a turn that started on another surface (most often a
  // Talk turn under unified room sync) so its progress animates here too. The
  // task-events endpoint is ownership-gated, not source-gated, so the substrate
  // the web client already tails works unchanged. A `pending_confirmation` task
  // is picked up too — its persisted `confirmation` event replays and the card
  // renders, which the old poller skipped outright.
  function pickUpStreamedTask(taskId: number, status?: string) {
    if (get(messages).some((m) => m.role === 'assistant' && m.taskId === taskId)) return;
    const ph: ChatMessage = {
      cid: nextCid(),
      role: 'assistant',
      text: '',
      taskId,
      status,
      segments: [],
      streaming: true,
      createdAt: new Date().toISOString(),
    };
    messages.update((arr) => appendAboveClientOnly(arr, ph));
    enqueueStream(taskId, ph.cid);
  }

  // Append a streamed row to the open room's transcript, deduped three ways —
  // the durable id (a reload may already hold it), the (role, task_id) key our
  // own optimistic placeholders carry, and notif_id for system rows.
  function appendStreamedRow(row: ChatRoomEvent) {
    const cur = get(messages);
    if (typeof row.msg_id === 'number' && cur.some((m) => m.msgId === row.msg_id)) return;
    if (typeof row.task_id === 'number') {
      const mine = cur.find((m) => m.taskId === row.task_id && m.role === row.role);
      if (mine) {
        // Already on screen (our own send, or a placeholder being streamed
        // into). Stamp the durable star key so the row is starrable without a
        // reload, then drop the frame.
        const msgId = typeof row.msg_id === 'number' ? row.msg_id : null;
        const starred = !!row.starred;
        // For a user turn, adopt the canonical body too. The server does not
        // always store what was typed — an attachment-only send becomes a
        // descriptor, a `!model …` prefix is stripped — and without this the
        // web transcript would keep showing the raw text while Talk, a reload
        // and the LLM's own context all show the stored one. Never for an
        // assistant row: that text is the task stream's to build.
        const body = row.role === 'user' && typeof row.text === 'string' ? row.text : null;
        if ((msgId != null && mine.msgId !== msgId) || (body != null && body !== mine.text)) {
          updateMsg(mine.cid, (m) => {
            if (msgId != null) m.msgId = msgId;
            m.starred = starred;
            if (body != null) m.text = body;
          });
        }
        return;
      }
    }
    // A send we gave up on that the server had in fact accepted. A timeout, or
    // a socket dropped after the request was processed, leaves the row marked
    // failed with no task id — and its echo then arrives as a *second* bubble,
    // so the user sees the same message twice: once reported as unsent, once
    // being answered. Adopt the echo into the row it belongs to instead.
    //
    // Matched on the body, which is what makes it safe to claim a row: the
    // server rewrites a few sends (an attachment-only descriptor, a stripped
    // `!model` prefix), and those simply fall through to appending — the same
    // duplicate as before, rather than a wrong row being silently claimed.
    //
    // A row the server attributed to somebody else is refused outright. The
    // body was the only test while a user row named no writer, and a room is
    // shared: a co-member typing the same words while this client holds an
    // unsent row had their turn folded into it, and since the adoption writes
    // no author the row went on reading as the viewer's own. That cost the
    // wrong name; with `author_id` on the wire it costs the reader's own face
    // over another member's words. Both fields are tested, since an external
    // sender carries a label and no id.
    if (
      row.role === 'user' &&
      typeof row.task_id === 'number' &&
      typeof row.text === 'string' &&
      !row.author &&
      !row.author_id
    ) {
      const stranded = cur.find(
        (m) =>
          m.role === 'user' &&
          // Failed, or parked by a send that did go out (`parkedAfterPost`).
          // Since ISSUE-202 a timeout parks rather than fails, so without the
          // second case this adoption would stop covering the very outcome it
          // was written for.
          (m.sendState === 'failed' || (isQueued(m) && parkedAfterPost.has(m.cid))) &&
          m.taskId === undefined &&
          m.text === row.text,
      );
      if (stranded) {
        // The server has the message, so the entry that was waiting to send it
        // must go — left in the queue it would drain into a second POST. The
        // row stays where it is and settles.
        dropQueuedEntry(stranded.cid);
        updateMsg(stranded.cid, (m) => {
          m.taskId = row.task_id!;
          if (typeof row.msg_id === 'number') m.msgId = row.msg_id;
          m.starred = !!row.starred;
          m.sendState = undefined;
          m.queueReason = undefined;
          m.queueHeld = undefined;
          m.sendError = undefined;
          m.retryable = undefined;
          m.sendPayload = undefined;
          m.showSending = undefined;
        });
        // The turn is live after all, so pick up its stream the way a freshly
        // streamed user row would.
        if (unsettled(row.status)) pickUpStreamedTask(row.task_id, row.status);
        return;
      }
    }
    if (typeof row.notif_id === 'number') {
      if (seenNotifIds.has(row.notif_id)) return;
      seenNotifIds.add(row.notif_id);
    }
    messages.update((arr) => appendAboveClientOnly(arr, buildHistoryMessage(row)));
    if (row.role === 'user' && typeof row.task_id === 'number' && unsettled(row.status)) {
      pickUpStreamedTask(row.task_id, row.status);
    }
    // Content just landed in the room the user is looking at — persist the read
    // cursor past it (visibility-gated) so it doesn't resurface as unread.
    const rid = get(activeRoomId);
    if (rid != null) markActiveReadThrottled(rid);
  }

  // Background room: bump the unread badge. Rows stream for every member room,
  // so this is real content, not a count refetch.
  function bumpBackgroundRoom(roomId: number, row: ChatRoomEvent, countUnread = true) {
    if (!countUnread || row.role === 'user') return;
    // count_unread_messages excludes the user's own turns, so a turn mirrored
    // in from Talk must not ring its own room. `countUnread` is false for a row
    // a just-completed refreshRooms already counted.
    rooms.update((rs) =>
      rs.map((r) => (r.id === roomId ? { ...r, unread_count: (r.unread_count ?? 0) + 1 } : r)),
    );
  }

  // Keep the aggregate panes live instead of frozen snapshots. Starred is
  // skipped: a freshly arrived row is unstarred by definition. Unread applies
  // the same "not your own turn" rule as the badge math.
  function feedAggregateView(row: ChatRoomEvent) {
    const v = get(view);
    if (v === 'room' || v === 'starred') return;
    if (v === 'unread' && row.role === 'user') return;
    if (typeof row.msg_id === 'number' && get(messages).some((m) => m.msgId === row.msg_id)) return;
    // The All view carries no queued rows (`carryClientOnlyRows` drops them in
    // the `token === null` branch) but it does carry stranded failed ones, and
    // the tail rule is the same for those.
    messages.update((arr) => appendAboveClientOnly(arr, buildHistoryMessage(row)));
  }

  function applyRoomEvent(row: ChatRoomEvent, opts: { countUnread?: boolean } = {}) {
    if (recoveryBuffer) {
      recoveryBuffer.push(row);
      return;
    }
    const token = row.room_token;
    if (!token) return;
    // Every message row is activity in its room, whichever room that is and
    // whoever sent it — this is the single funnel every frame passes through,
    // so the sidebar's order stays live without a room refetch. Ahead of the
    // `pendingSend` buffer deliberately: a send's own echo is held back to
    // dedup the bubble, but the room it went to should rise immediately.
    rooms.update((rs) => touchRoomActivity(rs, token, row.created_at));
    if (pendingSend && token === pendingSend.token) {
      pendingSend.rows.push(row);
      return;
    }
    // Every applied frame goes to the offline cache, for whichever room it
    // names rather than the one on screen — that is the whole point of doing it
    // here, at the funnel, instead of in `appendStreamedRow`. A *drained* held
    // frame is cached when the buffer comes back through this function, so
    // both early returns above merely defer it.
    //
    // A held frame that is never drained is not cached, and that is the one
    // gap: `stopActive` and `stopRoomStream` drop the send buffer rather than
    // releasing it, so the echo of a send in a room the user is leaving does
    // not reach storage. Online the room's next `loadHistory` rewrites the
    // whole tail over it; offline that reload is what the cache is standing in
    // for, so the tail is one turn short until the connection returns. Left
    // alone rather than flushed on the way out: those frames are dropped
    // precisely because they belong to a turn nothing is going to reconcile,
    // and the cache should not be the one place they survive.
    cacheStreamedRow(row);
    const room = get(rooms).find((r) => r.token === token);
    if (room && room.id === get(activeRoomId) && get(view) === 'room') {
      appendStreamedRow(row);
      return;
    }
    feedAggregateView(row);
    if (room) bumpBackgroundRoom(room.id, row, opts.countUnread ?? true);
  }

  // `message_deleted` frame: rows another client (or another tab) removed.
  // Applied to whatever is on screen — the room transcript and the aggregate
  // panes are one `messages` store, so one filter covers both. Unread badges
  // are deliberately left alone: they are the server's count and the 30s
  // reconciler settles them, and decrementing here would double-count against
  // a badge the deleting client's own read cursor may already have cleared.
  function applyDeletions(deletions: { msg_id: number }[]) {
    if (!deletions.length) return;
    const ids = deletions.map((d) => d.msg_id);
    const gone = new Set(ids);
    messages.update((arr) => arr.filter((m) => m.msgId == null || !gone.has(m.msgId)));
    // A row deleted while online must not come back the next time the app
    // opens without a connection.
    forgetCachedMessages(ids);
  }

  // Replay the frames held for the duration of a send, now that the turn's
  // task id is on screen and the ordinary dedup can recognise our own echo.
  //
  // `expected` is the buffer the caller opened. A turn drains only its own:
  // the slot is one module-level reference, and a room switch between two
  // sends leaves whatever was open in it, so an unqualified drain releases
  // another turn's frames before that turn's task id has been stamped — which
  // is precisely the duplicate the buffer exists to prevent.
  type PendingSend = { token: string; rows: ChatRoomEvent[] };

  function drainPendingSend(expected?: PendingSend) {
    if (expected && pendingSend !== expected) return;
    const held = pendingSend;
    pendingSend = null;
    if (!held) return;
    for (const row of held.rows) {
      try {
        applyRoomEvent(row);
      } catch {
        /* one bad row must not strand the rest */
      }
    }
  }

  // `room` metadata frame: a rename / model / effort change, or a room
  // appearing or disappearing on another device or surface. Closes the
  // "renamed or deleted elsewhere never propagates" gap without a room refetch.
  function applyRoomFrame(frame: {
    action?: string;
    id?: number;
    room?: Partial<ChatRoom> & { id: number };
  }) {
    if (frame.action === 'remove') {
      const id = frame.id;
      if (typeof id !== 'number') return;
      forgetRoom(id);
      rooms.update((rs) => rs.filter((r) => r.id !== id));
      if (get(activeRoomId) === id) {
        const remaining = get(rooms);
        if (remaining[0]) void selectRoom(remaining[0].id);
        else {
          activeRoomId.set(null);
          messages.set([]);
        }
      }
      return;
    }
    const fresh = frame.room;
    if (!fresh || typeof fresh.id !== 'number') return;
    rooms.update((rs) => {
      const idx = rs.findIndex((r) => r.id === fresh.id);
      // The snapshot deliberately omits unread counts (they ride the `message`
      // frames), so merge rather than replace. It omits `last_activity` for the
      // same reason — it changes on every message, so diffing it would turn
      // every turn into a `room` frame — which leaves a room appearing here
      // with no stamp. Its appearance is itself the activity, so it takes one
      // now; the arriving message that caused it, and the 30s poll, both settle
      // it to the server's value.
      if (idx === -1) {
        const added = {
          ...(fresh as ChatRoom),
          unread_count: 0,
          last_activity: fresh.last_activity ?? new Date().toISOString(),
        };
        return sortRoomsByActivity([...rs, added]);
      }
      const next = rs.slice();
      next[idx] = {
        ...next[idx],
        name: fresh.name ?? next[idx].name,
        origin: fresh.origin ?? next[idx].origin,
        // `??` rather than a bare adopt: a promote sets this and an unbound
        // room sends null, so taking the frame's value unconditionally would
        // erase it the way the room-list poll used to (ISSUE-342).
        talk_token: fresh.talk_token ?? next[idx].talk_token,
        model: fresh.model ?? null,
        effort: fresh.effort ?? null,
        brain: fresh.brain ?? null,
        // The sidebar tint, adopted from the frame the way the three above are:
        // `_room_snapshot` sends it on every room, so the frame is authoritative
        // and a clear made on another tab has to arrive as one. A field missing
        // from this list is not merely stale — it is erased on the next frame,
        // which a rename in a busy room produces (ISSUE-433).
        color: fresh.color ?? null,
      };
      // Same invalidation the local save does, for a brain changed on another
      // surface: `!brain` on Talk, or this user's other device. The frame is
      // the only notice this client gets.
      if ((fresh.brain ?? null) !== (rs[idx].brain ?? null)) dropRoomCatalogue(fresh.id);
      return next;
    });
  }

  // Recovery routine shared by the server's `gap` frame and the client-side age
  // rule. Reloading is cheap AND authoritative — refreshRooms returns
  // server-computed unread counts for every room and the active room is one
  // 50-row page — so it is the right answer whenever replay is doubtful.
  // `cursor` is the server's max *scanned* id (null → ask for a fresh one).
  //
  // `metadataOnly` skips the transcript reload and reconciles the room list
  // alone. It is only ever passed when the SSE connection demonstrably stayed
  // open across the quiet period, which is positive evidence that no `messages`
  // row was missed — the stream delivered them — so the reload would buy
  // nothing and cost a visible flicker plus a restarted task stream.
  async function recoverStream(cursor: number | null, opts: { metadataOnly?: boolean } = {}) {
    if (recovering) return;
    recovering = true;
    recoveryBuffer = [];
    // Rows buffered before `refreshRooms` is issued are already in the DB the
    // server counts, so re-bumping them would inflate the badge. Rows arriving
    // after are counted locally — erring toward a duplicate rather than a lost
    // increment, and the 30s reconciler settles either way.
    let countedUpTo = 0;
    try {
      let target = cursor;
      if (target == null) {
        try {
          // limit=1 → the server does the cheap MAX(id) gate and hands back a
          // cursor without serializing a backlog we're about to discard.
          const seed = await getRoomEvents(roomCursor, 1, RECOVERY_FETCH_TIMEOUT_MS);
          target = seed.cursor;
          // The reload below re-reads from the server, which has already
          // dropped the deleted rows — so skip past them rather than replaying
          // deletions for messages that are no longer on screen.
          const d = Number(seed.deletion_cursor) || 0;
          if (d > roomDeletionCursor) roomDeletionCursor = d;
        } catch {
          target = null;
        }
      }
      const v = get(view);
      const rid = get(activeRoomId);
      // Every reload here is bounded: an unbounded one would hold the frame
      // buffer open indefinitely (see RECOVERY_FETCH_TIMEOUT_MS). The abort
      // also means a late response can never land on top of whatever replaced
      // it — the request is cancelled, not merely ignored.
      // Whether the reload actually read the room off the wire. A bounded
      // reload that times out reports `timeout`, which the connectivity store
      // reads as a gap — so `loadHistory` paints the cache and returns rather
      // than throwing, and without this the cursor advance below would step
      // over every row between the two cursors on a merely *slow* connection.
      let reloaded = true;
      if (!opts.metadataOnly) {
        if (v === 'room' && rid != null) {
          stopActive();
          reloaded = await loadHistory(rid, RECOVERY_FETCH_TIMEOUT_MS);
        } else if (v !== 'room') {
          await loadViewPage(v);
        }
      }
      countedUpTo = recoveryBuffer?.length ?? 0;
      await refreshRooms(RECOVERY_FETCH_TIMEOUT_MS);
      // The drafts frame is a diffed snapshot, so a change that happened during
      // the gap produced a frame the reconnecting client did not receive and no
      // later frame will repeat. Metadata-only recoveries need this too: a
      // backgrounded tab is exactly where an approval given on the phone would
      // otherwise leave a card on screen for a message already sent.
      void refreshDrafts();
      if (reloaded && target != null && target > roomCursor) roomCursor = target;
    } catch {
      /* transient — the next frame or poll retries */
    } finally {
      const buffered = recoveryBuffer ?? [];
      recoveryBuffer = null;
      recovering = false;
      buffered.forEach((row, i) => {
        try {
          applyRoomEvent(row, { countUnread: i >= countedUpTo });
        } catch {
          /* one bad row must not strand the rest */
        }
      });
    }
  }

  function startRoomStream() {
    if (roomStream) return;
    let es: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;
    let stopped = false;
    let opened = false;
    // Consecutive SSE errors while the browser is still retrying on its own.
    // Reconnect-for-free is one of the reasons this is SSE and not a WebSocket,
    // so an ordinary blip must not cost the connection — but a persistently
    // failing endpoint (a buffering proxy that accepts and then drops) has to
    // concede to polling eventually.
    let sseFailures = 0;
    const SSE_FAILURE_LIMIT = 3;
    // Once polling, re-probe SSE on this cadence. Unlike streamTask — where the
    // stream is short-lived and a permanent downgrade is harmless — this
    // connection is session-lived, so a single transient failure must not leave
    // the tab polling for the rest of the day.
    const SSE_RETRY_MS = 60000;
    let lastSseAttemptAt = 0;

    const closeEs = () => {
      roomStreamLive = false;
      if (es) {
        es.close();
        es = null;
      }
    };
    const stopPolling = () => {
      if (pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    };
    const halt = () => {
      stopped = true;
      closeEs();
      stopPolling();
    };

    // Polling fallback over the snapshot endpoint — the same shape streamTask
    // already uses when SSE is unavailable (mock dev backend, buffering proxy).
    const poll = async () => {
      if (stopped) return;
      // A tick spent buying a cursor rather than a backlog. `init()` could not
      // reach the server, so this is the first contact that can — and asking
      // for the tail from zero here is what would replay the whole history.
      // limit=1 is the same MAX(id) gate `init()` uses.
      if (!roomCursorSeeded) {
        try {
          const seed = await getRoomEvents(0, 1);
          roomCursor = seed.cursor;
          roomDeletionCursor = Number(seed.deletion_cursor) || 0;
          roomCursorSeeded = true;
          lastRoomEventAt = Date.now();
          // The transcript and the room list were painted from the cache while
          // this was unreachable, so they are as stale as the cursor was.
          void recoverStream(null);
        } catch {
          /* still nothing there; the next tick tries again */
        } finally {
          maybeReconnect();
        }
        return;
      }
      try {
        const page = await getRoomEvents(roomCursor, 0, 0, roomDeletionCursor);
        lastRoomEventAt = Date.now();
        // Deletions first, and before the gap bail-out: a gap reloads the open
        // room from the server, which already omits the deleted rows, but the
        // cursor still has to advance or every poll re-sends the same batch.
        applyDeletions(page.deletions ?? []);
        applyDraftsSnapshot(page.drafts, page.drafts_unavailable === true);
        const delCursor = Number(page.deletion_cursor) || 0;
        if (delCursor > roomDeletionCursor) roomDeletionCursor = delCursor;
        if (page.gap) {
          if (page.cursor > roomCursor) roomCursor = page.cursor;
          void recoverStream(page.cursor);
          return;
        }
        for (const row of page.events) {
          try {
            applyRoomEvent(row);
          } catch {
            /* swallow */
          }
        }
        if (page.cursor > roomCursor) roomCursor = page.cursor;
      } catch {
        /* transient; try again next tick */
      } finally {
        maybeReconnect();
      }
    };
    const startPolling = () => {
      if (pollTimer || stopped) return;
      void poll();
      pollTimer = setInterval(() => void poll(), Math.max(pollIntervalMs, 1000));
    };

    // Try SSE again from the polling loop. Overlap is harmless: both paths are
    // idempotent on `roomCursor`, and polling stops as soon as a stream opens.
    const maybeReconnect = () => {
      if (stopped || es || !roomCursorSeeded) return;
      if (Date.now() - lastSseAttemptAt < SSE_RETRY_MS) return;
      sseFailures = 0;
      connect();
    };

    function connect() {
      if (stopped || es) return;
      // Nothing to resume from. The poll seeds the cursor first and
      // `maybeReconnect` brings the stream up once it has, so the SSE
      // connection is never opened at `since_id: 0`.
      if (!roomCursorSeeded) return;
      lastSseAttemptAt = Date.now();
      try {
        es = new EventSource(chatRoomStreamUrl(roomCursor, roomDeletionCursor), {
          withCredentials: true,
        });
      } catch {
        es = null;
        startPolling();
        return;
      }
      es.addEventListener('message', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        // Idempotent on the durable id: a Last-Event-ID resume or a brief
        // SSE↔poll overlap can redeliver a row we already applied.
        const id = Number(e.lastEventId) || 0;
        if (id) {
          if (id <= roomCursor) return;
          roomCursor = id;
        }
        let row: ChatRoomEvent;
        try {
          row = JSON.parse(e.data);
        } catch {
          return;
        }
        try {
          applyRoomEvent(row);
        } catch {
          /* a render throw must never wedge the stream */
        }
      });
      es.addEventListener('gap', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        let cursor = 0;
        try {
          cursor = Number(JSON.parse(e.data).cursor) || 0;
        } catch {
          return;
        }
        if (cursor > roomCursor) roomCursor = cursor;
        void recoverStream(cursor);
      });
      es.addEventListener('room', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        try {
          applyRoomFrame(JSON.parse(e.data));
        } catch {
          /* swallow */
        }
      });
      // Auxiliary frame, and a whole-set snapshot rather than a tail — it
      // carries no SSE `id:` for the same reason `room` and `message_deleted`
      // do not: that cursor belongs to the message tail.
      es.addEventListener('drafts', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        try {
          applyDraftsSnapshot(JSON.parse(e.data).drafts);
        } catch {
          /* swallow */
        }
      });
      // The bell's fast path, and the one frame here that publishes outside the
      // chat session: this route already holds a stream open, so a question
      // parked while the user is reading a room lights the bell in about a
      // second rather than waiting on the root layout's thirty-second poll.
      // That poll is still the contract — this frame rides the room-check tick,
      // which `room_stream_room_check_seconds = 0` disables outright.
      es.addEventListener('notifications', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        try {
          applyNotificationCounts(JSON.parse(e.data));
        } catch {
          /* swallow */
        }
      });
      // Auxiliary frame — it carries no SSE `id:` (that cursor belongs to the
      // message tail), so the deletion cursor travels inside the payload.
      es.addEventListener('message_deleted', (e: MessageEvent) => {
        if (e.data == null) return;
        lastRoomEventAt = Date.now();
        try {
          const payload = JSON.parse(e.data);
          applyDeletions(payload.deletions ?? []);
          const c = Number(payload.cursor) || 0;
          if (c > roomDeletionCursor) roomDeletionCursor = c;
        } catch {
          /* swallow */
        }
      });
      es.onopen = () => {
        sseFailures = 0;
        roomStreamLive = true;
        // A stream that opened is a server that answered, which is the second
        // input the connectivity store's own doc names and leaves to whoever
        // touches this file (ISSUE-202). Only the open: the error path fires
        // for a proxy, a server restart and a client with no room as readily
        // as for a gap, so it is no evidence of the opposite.
        noteTransport(true);
        stopPolling(); // a re-probe succeeded — the stream is the live path again
        const idle = Date.now() - lastRoomEventAt;
        lastRoomEventAt = Date.now();
        // First open follows a fresh history load — nothing to recover.
        if (opened && idle > ROOM_STREAM_STALE_MS) void recoverStream(null);
        opened = true;
      };
      es.onerror = () => {
        if (stopped) return;
        sseFailures += 1;
        roomStreamLive = false;
        // readyState CONNECTING (0, per the spec constant) means the browser
        // has already scheduled its own retry; closing here would throw that
        // away and pre-empt exactly the free reconnect SSE was chosen for. Let
        // it try, up to the limit. Anything else — CLOSED, or an implementation
        // with no readyState at all — is fatal, so fall back at once.
        if (es?.readyState === 0 && sseFailures < SSE_FAILURE_LIMIT) return;
        closeEs();
        startPolling();
      };
    }

    connect();
    if (!es) startPolling();
    roomStream = { stop: halt };
  }

  /**
   * What happens the moment the app can reach the server again.
   *
   * Two steps, in this order (ISSUE-202). `recoverStream(null)` first, because
   * the transcript on screen was painted from the cache and the room list with
   * it, and sending into a room whose last few turns are unknown is how a
   * queued message lands as an answer to something that was already answered.
   * Then the drain, which sends one entry and hands the rest to the ordinary
   * settle-and-drain loop.
   *
   * The active room only, because that is all `canDrain` permits — an entry
   * queued in a room the user is not looking at goes when they open it, and
   * the room-list badge is what says so.
   */
  async function onBackOnline() {
    // Not while the first load is still running. `init()` is rebuilding the
    // same transcript this would rebuild, and two `messages.set` passes racing
    // each other is a scrambled room; the drain at the foot of `init` is what
    // covers a connection that returned during it.
    if (!get(loaded)) return;
    // Everything below runs after an await, and `teardown` cannot recall a
    // call already suspended in one — so the same generation counter `init`
    // checks is captured here. Without it a page left mid-reconnect still gets
    // a settle (which rewrites `messages`, `rooms` and the queue) and a stream
    // recovery, on a session nothing is rendering.
    const gen = initGeneration;
    const superseded = () => gen !== initGeneration;
    // Ahead of the reconcile, and only for a session that booted from the
    // pointer (ISSUE-202): this is the first moment the server can say who
    // this device belongs to, and if the guess was wrong the transcript being
    // reconciled and the queue about to drain are the wrong user's. Skipped
    // entirely otherwise — `settleSeededUser` returns at once and no request
    // is made. Consulted for the id alone: `pollIntervalMs` and the
    // external-turn setting were taken from this user's own cached config at
    // boot and are the same values, and re-storing the config here would push
    // its expiry out on a read nobody asked for.
    let repainted = false;
    if (userIdFromPointer) {
      const live = await getChatConfig().catch(() => null);
      if (superseded()) return;
      if (live) repainted = settleSeededUser(live);
    }
    await recoverStream(null);
    if (superseded()) return;
    if (repainted) {
      // The room list the recovery just fetched is what the queue is keyed
      // against, so the restore has to come after it — and the rows for what
      // it restored have to be put back, since the history load that would
      // normally append them ran while the queue was empty.
      restoreQueues();
      // `settleSeededUser` dropped the selection with the rest of the wrong
      // namespace, so a room is picked out of the list that just arrived, the
      // way `init` picks one.
      if (get(activeRoomId) == null) {
        const first = get(rooms)[0];
        if (first) await selectRoom(first.id);
        if (superseded()) return;
      }
      const rid = get(activeRoomId);
      const token = rid == null ? undefined : roomTokenOf(rid);
      if (token) appendQueuedRows(token);
    }
    // `recoverStream` returns at once when one is already running, so the
    // await above is not always the reconcile it reads as. Draining into a
    // rebuild that is still in flight is worse than not draining at all: that
    // rebuild carries client-only rows, and `beginSend` stamps 'sending' on
    // the row it drains, which is exactly what stops it being one — the POST
    // would be left with no bubble on screen. The recovery's own load drains
    // at its foot anyway.
    if (recovering) return;
    const rid = get(activeRoomId);
    if (rid != null) await drainSendQueue(rid);
  }

  /**
   * Watch the connectivity store for the transition *into* online.
   *
   * A subscription rather than a call from `probe`'s own success path: the
   * store has three inputs and any of them can be the one that clears it, and
   * the reconcile-then-drain belongs to the fact rather than to whichever
   * observation produced it.
   *
   * Idempotent, and the current value is read before subscribing so the
   * initial emission every Svelte store makes is not mistaken for a
   * transition.
   */
  function watchConnectivity() {
    stopConnectivityWatch();
    let was = get(online);
    unwatchOnline = online.subscribe((now) => {
      const transitioned = now && !was;
      was = now;
      if (transitioned) void onBackOnline();
    });
  }

  function stopConnectivityWatch() {
    if (unwatchOnline) unwatchOnline();
    unwatchOnline = null;
  }

  function stopRoomStream() {
    // Release any recovery / send hold so a teardown mid-reload can't leave the
    // session permanently swallowing frames (both guards are module-singleton
    // state, so a route remount would otherwise inherit the wedge).
    recovering = false;
    recoveryBuffer = null;
    pendingSend = null;
    roomStreamLive = false;
    if (roomStream) {
      roomStream.stop();
      roomStream = null;
    }
  }

  function removeVisibilityListener() {
    if (onVisibility && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility);
    }
    onVisibility = null;
  }

  // Build a render-ready ChatMessage from a server history row. Shared by the
  // first load and the scroll-up older-page prepend so both reconstruct the
  // segment list identically (ISSUE-122 / ISSUE-131).
  function buildHistoryMessage(m: ChatHistory['messages'][number]): ChatMessage {
    // Rebuild the ordered segment list from the persisted trace so a finished
    // turn renders the same interleaved layout across reloads. Prefer the
    // server's ordered `segments`; fall back to the flat `tools` descriptions +
    // answer for an in-flight turn or an old payload. History has no per-tool
    // success/timing, so chips render a neutral "done" state. An in-flight
    // assistant turn starts empty — its resumed SSE rebuilds the segments live.
    let segments: Segment[] = [];
    if (m.role === 'assistant') {
      if (m.segments && m.segments.length) {
        segments = historySegments(m.segments);
      } else if (!inFlight(m.status)) {
        segments = historySegments([
          ...(m.tools ?? []).map((d) => ({ kind: 'tool', text: d })),
          ...(m.text ? [{ kind: 'text', text: m.text }] : []),
        ]);
      }
    }
    return {
      cid: nextCid(),
      role: m.role,
      text: m.text,
      taskId: m.task_id,
      status: m.status,
      confirmation: !!m.confirmation,
      segments,
      streaming: m.role === 'assistant' && inFlight(m.status),
      createdAt: m.created_at,
      durationSeconds: typeof m.duration_seconds === 'number' ? m.duration_seconds : undefined,
      model: typeof m.model === 'string' && m.model ? m.model : undefined,
      // Durable-store identity → the star affordance; room labels ride along
      // on aggregate-view rows.
      msgId: typeof m.msg_id === 'number' ? m.msg_id : undefined,
      starred: typeof m.msg_id === 'number' ? !!m.starred : undefined,
      roomToken: m.room_token,
      roomName: m.room_name,
      // Server-resolved author for a user row the viewer did not write, and —
      // when that writer holds an account here — the id their picture is
      // served under. Folded to `undefined` the same way, since an empty id
      // would build a URL matching no route.
      author: typeof m.author === 'string' && m.author ? m.author : undefined,
      authorId: typeof m.author_id === 'string' && m.author_id ? m.author_id : undefined,
      // Provenance for a user row that entered from outside the room. Both are
      // set here and nowhere else, so history, the aggregate panes and the live
      // stream mark the same turns as external.
      origin: typeof m.origin === 'string' && m.origin ? m.origin : undefined,
      subject: typeof m.subject === 'string' && m.subject ? m.subject : undefined,
      // Persisted server-side, so the chip survives leaving the room and
      // coming back (the composer's names are long gone by then).
      attachments: m.attachments?.length ? m.attachments : undefined,
      attachmentPaths: m.attachment_paths?.length ? m.attachment_paths : undefined,
      // The single place the server's citation becomes a row field, which is
      // what makes history, the aggregate panes and the live stream agree —
      // all three build their rows through here.
      replyTo: m.reply_to
        ? m.reply_to.deleted
          ? { msgId: m.reply_to.msg_id, deleted: true }
          : {
              msgId: m.reply_to.msg_id,
              role: m.reply_to.role,
              excerpt: m.reply_to.excerpt,
              deleted: false,
            }
        : undefined,
    };
  }

  /**
   * Rebuild the transcript from a room's wire rows and return its task index.
   *
   * Extracted because the offline cache paints the same room from the same
   * shape a moment before the fetch does, and the two must produce identical
   * rows — which they do by construction here, sharing one pass and one
   * `buildHistoryMessage`. A cached row that could render differently from a
   * fetched one is the whole failure mode caching the *wire* row avoids.
   *
   * `prev` is what was on screen before, which is where the client-only rows
   * come back from. `null` as the token means every room to
   * `carryClientOnlyRows`, which is right for the All view and wrong here — a
   * room missing from `$rooms` would inherit every other room's held rows, so
   * carry nothing rather than everything.
   */
  function paintRoomRows(
    rows: ChatHistory['messages'],
    prev: ChatMessage[],
    roomToken: string | null,
  ): Map<number, number> {
    // taskId → cid for assistant placeholders, so an in-flight task's stream
    // binds to the message the server already laid out in order.
    const cidByTask = new Map<number, number>();
    // Reset the per-room dedup set, then record every notification already in
    // the transcript so the idle poller only appends ones that arrive later.
    seenNotifIds.clear();
    const msgs: ChatMessage[] = rows.map((m) => {
      const cm = buildHistoryMessage(m);
      if (m.role === 'assistant' && typeof m.task_id === 'number') {
        cidByTask.set(m.task_id, cm.cid);
      }
      if (m.role === 'system' && typeof m.notif_id === 'number') {
        seenNotifIds.add(m.notif_id);
      }
      return cm;
    });
    messages.set(roomToken ? carryClientOnlyRows(prev, msgs, roomToken) : msgs);
    // A queued entry restored from storage has no row anywhere — the queue
    // outlived every transcript that ever rendered it — so the carry above
    // finds nothing to bring back and the bubble has to be rebuilt from the
    // entry. Here rather than in `init()` because a row is only ever appended
    // for the room being rendered, and this is the one place that knows which
    // that is.
    if (roomToken) appendQueuedRows(roomToken);
    return cidByTask;
  }

  /**
   * Rebuild a room's transcript. Resolves true when the *wire* answered.
   *
   * The return matters to `recoverStream` and to nothing else: a recovery
   * advances `roomCursor` past everything its reload covered, so a reload that
   * quietly painted the cache instead would move the cursor past rows that
   * were never loaded and are now never coming — the frames between the old
   * cursor and the new one are gone until the next gap. It used to be
   * protected by this function throwing; the offline path returns instead, so
   * the fact has to travel some other way.
   */
  async function loadHistory(roomId: number, timeoutMs = 0): Promise<boolean> {
    // Issued before the cache is read, so the cached paint happens *while* the
    // request is in flight rather than after it. Online that makes the cache a
    // first frame the fetch replaces; offline it is the only frame there is.
    const fetching = getRoomMessages(roomId, { timeoutMs });
    // The `await` that handles this rejection is several statements below, and
    // a promise that rejects with nothing attached in the meantime is an
    // unhandled rejection — which offline is the ordinary case, not the rare
    // one.
    fetching.catch(() => {});
    // A reload of the room already on screen (a stream recovery) still has its
    // client-only rows in `messages`; one reached via a room switch has them in
    // the holding map. Stashing first puts both cases in one place, so the
    // carry below is the only thing that has to know where they came from.
    const before = get(messages);
    let prev = before;
    stashStrandedSends();
    const roomToken = get(rooms).find((r) => r.id === roomId)?.token ?? null;
    // Whether there is a transcript to protect. A recovery reload runs against
    // a full one, and painting the cache into that would swap a live transcript
    // for an older tail and then swap it back — so the cache only ever fills a
    // room that has nothing in it, which is every path the user can see it on:
    // a room switch and `init()` both clear first.
    const empty = !prev.some((m) => !isClientOnly(m));
    let painted = false;

    if (roomToken && empty) {
      const cached = await readTranscript(storageUserId, roomToken);
      // The read is an await, so the room may have changed under it. Same
      // guard the drain at the foot of this function uses.
      if (cached && roomId === get(activeRoomId) && get(view) === 'room') {
        paintRoomRows(cached.messages, prev, roomToken);
        // Re-read rather than reused: the paint has just taken the held rows
        // out of `strandedSends`, so a second carry against the original
        // `prev` would find neither copy and drop them.
        prev = get(messages);
        painted = true;
        oldestCursor = cached.oldestCursor;
        hasMore.set(false);
        loadingOlder.set(false);
        // Set here rather than only on the failure below, because it is true
        // here: what is on screen is the cache. The window between this and
        // the fetch resolving is a frame on a good connection and the whole
        // request on a stalled one, and for its duration `hasMore: false`
        // would otherwise let the page state that a cached tail is the start
        // of the conversation.
        offlineTranscript.set(true);
      }
    }

    let hist: ChatHistory;
    try {
      hist = await fetching;
    } catch (e) {
      // Only a gap is tolerated here, and the connectivity store is what says
      // so — `apiFetch` has already reported this very request to it, so by
      // the time the throw arrives the answer is current. Anything else (a
      // 500, a body that would not parse) is a real failure and still throws:
      // reporting a server error as "you are offline" would put the banner up
      // on a working connection and leave the user waiting for it to clear.
      if (get(online)) {
        // A server that answered with a failure is not an outage, so this is
        // the pre-cache behaviour: the caller's error path, over the
        // transcript that was there before. Undoing the cached paint is the
        // whole of the difference — left up, it would be a 50-row tail with
        // paging disabled and no sign that anything had failed, which reads
        // as a complete conversation rather than as a broken load.
        if (painted) {
          messages.set(before);
          offlineTranscript.set(false);
        }
        throw e;
      }
      offlineTranscript.set(true);
      // `loadOlder` is a fetch, so there is no older page to be had; a
      // spinner that can never resolve is worse than an absent affordance.
      hasMore.set(false);
      loadingOlder.set(false);
      // Nothing cached for this room, and nothing on screen either — but the
      // queued and failed rows are still this room's, and they are the ones
      // carrying the actions the user has offline. An empty room with its
      // outbox visible beats a blank pane.
      if (!painted && roomToken && empty) {
        messages.set(carryClientOnlyRows(prev, [], roomToken));
        appendQueuedRows(roomToken);
      }
      return false;
    }
    offlineTranscript.set(false);
    const cidByTask = paintRoomRows(hist.messages, prev, roomToken);
    if (roomToken) {
      // A room the server says is empty gives up its cached tail rather than
      // storing an empty one: the entry would read as no cache on the way back
      // out anyway, while still holding one of `MAX_CACHED_ROOMS` slots.
      if (hist.messages.length) {
        void writeTranscript(cacheUserId(), {
          roomId,
          roomToken,
          messages: hist.messages.map(cacheableRow),
          oldestCursor: hist.oldest_cursor ?? null,
        });
      } else {
        void deleteTranscript(cacheUserId(), roomToken);
      }
    }
    // Seed paging state from the first-load response.
    oldestCursor = hist.oldest_cursor ?? null;
    hasMore.set(!!hist.has_more);
    loadingOlder.set(false);

    // Resume the room's in-flight tasks in order: the first streams, the rest
    // queue behind it. A leading pending_confirmation is left parked (its card
    // is shown) — the user must act before the queue moves.
    const actives = hist.active_tasks ?? (hist.active_task ? [hist.active_task] : []);
    for (const at of actives) {
      if (at.status === 'pending_confirmation') continue;
      let cid = cidByTask.get(at.id);
      if (cid == null) {
        const ph: ChatMessage = {
          cid: nextCid(),
          role: 'assistant',
          text: '',
          taskId: at.id,
          status: at.status,
          segments: [],
          streaming: true,
          createdAt: new Date().toISOString(),
        };
        // In front of whatever client-only rows are sitting at the tail, not
        // after them: a queued message was typed *behind* the turn this
        // placeholder stands for, and the carry and the queued-row rebuild
        // both ran above, so a plain push would put the running turn under the
        // message waiting on it. This was the one call site that got the rule
        // right; `appendAboveClientOnly` is that walk, extracted (ISSUE-351).
        messages.update((arr) => appendAboveClientOnly(arr, ph));
        cid = ph.cid;
      }
      enqueueStream(at.id, cid);
    }
    // A room whose turn finished while the user was elsewhere sends its queue
    // now. Here rather than in `selectRoom`, which is only one of three ways
    // this transcript gets rebuilt: `init()` restores the last room by calling
    // `loadHistory` directly, and `recoverStream` rebuilds it after a stale
    // reconnect — and that one halts the active stream first, so its task's
    // `settle` early-returns on `finished` and `onStreamSettled` never fires
    // for it. A queued entry would have no trigger left at all in either case
    // until the user switched rooms and came back.
    //
    // After the resume loop above, so `canDrain` sees whatever is still
    // running rather than an empty room. Guarded on this being the room on
    // screen, because a background load must send nothing. Not awaited — a
    // transcript load must not wait on a POST.
    if (roomId === get(activeRoomId)) void drainSendQueue(roomId);
    return true;
  }

  // Load (or reload) the first page of an aggregate view into the transcript.
  // Shared by selectView and the mark-all-read reload of an open Unread view.
  async function loadViewPage(v: ChatView) {
    try {
      const hist = await getChatMessagesView(v);
      // Switched away mid-fetch — drop the page.
      if (get(view) !== v) return;
      const prev = get(messages);
      stashStrandedSends();
      const next = hist.messages.map(buildHistoryMessage);
      // Only All spans every room, so only All can honestly show a failed send
      // from one. Unread and Starred are filtered panes a client-only row is
      // not a member of, so their rebuilds leave the held rows where they are —
      // the room's own transcript still gets them back.
      messages.set(v === 'all' ? carryClientOnlyRows(prev, next, null) : next);
      oldestCursor = hist.oldest_cursor ?? null;
      hasMore.set(!!hist.has_more);
    } catch {
      // The aggregate panes are deliberately not cached — they are a cross-room
      // query the client cannot reproduce from per-room tails — so offline they
      // are empty, and that is the answer rather than a failure. The banner
      // above the composer already says why, and a notice on top of it would
      // report a state twice and then take one of the reports away.
      if (!get(online)) {
        offlineTranscript.set(true);
        return;
      }
      // A load failure would belong in the page's own banner, but chat has
      // none — the pane just renders empty, which is indistinguishable from a
      // room with nothing in it. A notice beats silence; giving chat a real
      // load-failure banner is the better fix and is ISSUE-200's territory.
      notifyError('Failed to load messages');
    }
  }

  // Enter an aggregate view: tear down the room's live machinery (stream,
  // queue, notif poll, paging state), deselect the room, and load the first
  // page. The rooms-refresh timer keeps running so sidebar badges stay live.
  async function selectView(v: ChatView) {
    stopActive();
    view.set(v);
    saveSetting('chat.view', v);
    activeRoomId.set(null);
    messages.set([]);
    await loadViewPage(v);
  }

  // Star/unstar a transcript row optimistically; revert on failure. In the
  // Starred view a successful unstar also removes the row (kept during flight
  // so a failure can revert in place) — mirrors the feeds starred view.
  async function toggleStar(cid: number) {
    const m = get(messages).find((x) => x.cid === cid);
    if (!m || typeof m.msgId !== 'number') return;
    const next = !m.starred;
    updateMsg(cid, (mm) => {
      mm.starred = next;
    });
    try {
      await setChatMessageStarred(m.msgId, next);
      if (!next && get(view) === 'starred') {
        messages.update((arr) => arr.filter((x) => x.cid !== cid));
      }
    } catch {
      updateMsg(cid, (mm) => {
        mm.starred = !next;
      });
      notifyError("Couldn't update star.");
    }
  }

  // Hard-delete a transcript row. Pessimistic, unlike `toggleStar`: a star
  // reverts cleanly, but a row removed optimistically and then restored would
  // reappear in the middle of the transcript after the user watched it go —
  // and the request is one round trip.
  async function deleteMessage(cid: number) {
    const m = get(messages).find((x) => x.cid === cid);
    if (!m || typeof m.msgId !== 'number') return;
    try {
      await deleteChatMessage(m.msgId);
    } catch (e) {
      notifyError(
        e instanceof ChatMessageBusyError
          ? 'That turn is still running — delete it once it finishes.'
          : "Couldn't delete the message.",
      );
      return;
    }
    messages.update((arr) => arr.filter((x) => x.cid !== cid));
    notifySuccess('Message deleted.', { key: 'chat:message-delete' });
  }

  // Mark every room read in one shot (the header chip). Badges zero locally on
  // success; an open Unread view reloads to its (likely empty) fresh state.
  async function markAllRead() {
    try {
      await markAllRoomsRead();
    } catch {
      notifyError("Couldn't mark all rooms read.");
      return;
    }
    rooms.update((r) => r.map((x) => ({ ...x, unread_count: 0 })));
    if (get(view) === 'unread') await loadViewPage('unread');
  }

  // Fetch the next older page and prepend it (scroll-up paging). The scroll
  // handler captures the scroll anchor before calling and restores it after the
  // store updates, so the viewport stays put. Never touches active_tasks /
  // enqueueStream — an older page carries no in-flight slot, and resuming one
  // here would double-stream a task.
  async function loadOlder() {
    const v = get(view);
    if (v !== 'room') {
      // Aggregate views page the cross-room endpoint. No aux/notif dedup
      // bands here — the durable store is the only source — but dedup by
      // msg_id anyway so a boundary anomaly can't double a row.
      if (!get(hasMore) || get(loadingOlder) || !oldestCursor) return;
      loadingOlder.set(true);
      try {
        const hist = await getChatMessagesView(v, { before: oldestCursor });
        if (get(view) !== v) return;
        const have = new Set<number>();
        for (const m of get(messages)) {
          if (typeof m.msgId === 'number') have.add(m.msgId);
        }
        const page = hist.messages
          .filter((m) => typeof m.msg_id !== 'number' || !have.has(m.msg_id))
          .map(buildHistoryMessage);
        if (page.length) messages.update((cur) => [...page, ...cur]);
        oldestCursor = hist.oldest_cursor ?? null;
        hasMore.set(!!hist.has_more);
      } catch {
        // Transient — leave the cursor untouched so the next scroll retries.
      } finally {
        loadingOlder.set(false);
      }
      return;
    }
    const roomId = get(activeRoomId);
    if (roomId == null || !get(hasMore) || get(loadingOlder) || !oldestCursor) return;
    loadingOlder.set(true);
    try {
      const hist = await getRoomMessages(roomId, { before: oldestCursor });
      // Switched rooms mid-fetch — drop the page rather than prepend it into
      // the wrong transcript.
      if (get(activeRoomId) !== roomId) return;
      // Dedup against what's already on screen by the same identity the server
      // dedups on: (role, taskId) for task-backed turns, notif_id for system
      // rows. The band tiling already prevents overlap; this guards a
      // created_at tie straddling the page boundary.
      const haveTask = new Set<string>();
      for (const m of get(messages)) {
        if (typeof m.taskId === 'number') haveTask.add(`${m.role}:${m.taskId}`);
      }
      const fresh = hist.messages.filter((m) => {
        if (typeof m.notif_id === 'number') {
          if (seenNotifIds.has(m.notif_id)) return false;
          seenNotifIds.add(m.notif_id);
          return true;
        }
        if (typeof m.task_id === 'number') return !haveTask.has(`${m.role}:${m.task_id}`);
        return true;
      });
      const page = fresh.map(buildHistoryMessage);
      if (page.length) messages.update((cur) => [...page, ...cur]);
      oldestCursor = hist.oldest_cursor ?? null;
      hasMore.set(!!hist.has_more);
    } catch {
      // Transient — leave the cursor untouched so the next scroll retries.
    } finally {
      loadingOlder.set(false);
    }
  }

  async function init() {
    // Warm the command catalogue, because `send()` reads its snapshot
    // synchronously to decide whether a body typed against a running turn is
    // answered inline or queued (ISSUE-300, ISSUE-238). `isKnownCommand` with
    // no names argument reads a module-level set that is empty until some
    // caller has awaited a fetch, and the only other callers are the
    // autocomplete providers, which fetch lazily — on the keystroke that opens
    // the popover, resolving some time after it. So without this a `!stop`
    // typed and sent quickly enough is not recognised as a command and is
    // queued *behind* the turn it was meant to cancel, which is the one
    // outcome the inline-command exemption exists to prevent.
    //
    // Here rather than in the composer, which is where it used to live as a
    // side effect of a derivation it no longer has: the consumer of the
    // snapshot owns loading it, and this is the function `teardown`'s
    // `resetCommandCatalogue()` pairs with. Fire-and-forget and idempotent —
    // the catalogue caches its own promise — so it needs no generation guard.
    //
    // Caught rather than left to `void`: `loadCatalogue` already degrades a
    // failed *fetch* to an empty catalogue, so anything reaching here is a
    // throw from the call itself, and an unhandled rejection out of the
    // store's boot path is noise wherever it lands. An empty catalogue is the
    // conservative answer this had before the warm existed — a command is not
    // recognised and the message queues — so failing quietly loses nothing
    // that was not already lost.
    void loadCommandNames().catch(() => {});
    // Before the first await, and before anything that can throw. The watch is
    // a subscription and needs nothing from the config, while an `init` that
    // gives up — offline with no cached room list — would otherwise leave the
    // session with no way to hear the connection come back at all.
    watchConnectivity();
    // `onMount` does not await this and `onDestroy` calls teardown regardless,
    // so a navigation away mid-load would otherwise let the rest of init run
    // *after* teardown — starting a stream, a timer and a visibility listener
    // on a page the user has left, and leaking one more of each per remount
    // (only the newest listener is ever removed). Every await below is followed
    // by a generation check; teardown bumps the counter.
    const gen = ++initGeneration;
    const superseded = () => gen !== initGeneration;
    try {
      // Who to read the cache by before anything has said (ISSUE-202). A cold
      // launch with no connection gets no config at all, so without this seed
      // it has no key and boots to an empty cache — the one outcome the
      // service worker exists to prevent. Null off the shell and null once the
      // session already knows: `seedUserId` reads the pointer only inside the
      // app, and a remount has the real id already.
      if (!storageUserId) {
        const seeded = seedUserId();
        if (seeded) {
          storageUserId = seeded;
          userIdFromPointer = true;
        }
      }
      const live = await getChatConfig().catch(() => null);
      if (superseded()) return;
      // Before the cached reads below, so a wrong guess is corrected while
      // nothing has been painted from it *this* time round — what it drops is
      // what an earlier offline load left in memory.
      if (live) settleSeededUser(live);
      // The cached config stands in for a fetch that did not answer, which is
      // only reachable on a remount: the id it is keyed by comes from a config
      // read, so a session that has never had one has nothing to look this up
      // by. Worth the read anyway — without it a remount offline reverts the
      // external-turn display to its default, silently expanding every
      // stranger's mail in the transcript.
      const cfg = live ?? (await readConfig(storageUserId));
      if (superseded()) return;
      if (cfg?.client_poll_interval_ms) pollIntervalMs = cfg.client_poll_interval_ms;
      // Who the send queue's storage key belongs to. It rides on the config
      // rather than being pushed down from the page: the page's id used to come
      // from a `getMe()` that resolved after this, which put an ordering hazard
      // exactly where the restore below happens. The page reads the root
      // layout's identity now (ISSUE-355), so that hazard is gone, but the
      // config stays the source here — this store is a module singleton that
      // outlives every mount of the page, and taking the id from whichever page
      // happens to be up would make its lifetime the shorter of the two. An
      // older backend sends no `user_id`, and the queue is then in memory only.
      //
      // Guarded on the config having resolved at all, not merged with the
      // line above: the session outlives the page, so a transient failure on a
      // remount would otherwise clear an id this session already knew — and
      // persistence would go quietly off, leaving a drained message stored and
      // restored later as a bubble for something already sent.
      //
      // Split by where the config came from, since Stage 5's pointer gave
      // `storageUserId` a third possible source. A *live* config is the
      // authority and its absent `user_id` (an older backend) really does mean
      // "no id, queue in memory only". A *cached* one carrying no id says
      // nothing about who this is — adopting its null would throw away the
      // seed that is the only reason there was a cache to read at all, and
      // leave the boot the pointer exists for with no key.
      if (live) storageUserId = live.user_id ?? null;
      else if (cfg?.user_id) storageUserId = cfg.user_id;
      // The pointer the *next* cold launch reads the cache by, written on
      // every successful config read rather than once — a device that changes
      // hands re-points before anything is read by the old id. Only from a
      // live read: writing back what the cache just answered with would make
      // the pointer self-confirming.
      if (live) rememberLastUserId(storageUserId);
      // After the id is known, or it would be stored under nothing. Only a
      // live read is worth writing back — re-storing what was just read would
      // push its own expiry out forever.
      if (live && storageUserId) void writeConfig(cacheUserId(), live);
      // Normalized rather than adopted: the column takes any string a hand
      // edit puts in it, and an unrecognized value must read as the default
      // instead of leaving the transcript with no branch to take.
      externalTurnDisplay.set(normalizeExternalTurnDisplay(cfg?.external_turn_display));
      // Same shape as the transcript below: ask, paint whatever is stored
      // while the answer is on its way, then reconcile. The room list is what
      // the sidebar renders *and* what the transcript cache is read through —
      // a cached tail is keyed by token, and the token comes from here — so
      // with no room list there is no offline anything.
      const fetchingRooms = getChatRooms();
      fetchingRooms.catch(() => {});
      const cachedRooms = await readRooms(storageUserId);
      if (superseded()) return;
      if (cachedRooms?.length) rooms.set(sortRoomsByActivity(cachedRooms));
      let list: ChatRoom[];
      try {
        list = (await fetchingRooms).rooms;
        void writeRooms(cacheUserId(), list);
      } catch (e) {
        // Offline with a cached list, the rest of `init` is worth running: the
        // queue restores, the last room paints from its own cache, and the
        // stream machinery starts and reconnects on its own when the
        // connection comes back. Anything else is a real failure and still
        // reaches the notice below.
        if (get(online) || !cachedRooms?.length) throw e;
        list = get(rooms);
      }
      if (superseded()) return;
      rooms.set(sortRoomsByActivity(list));
      // After the room list, because the queue is keyed by token and a key
      // naming a room this user no longer has is left where it is. Before the
      // history read, so `loadHistory` finds the restored entries and gives
      // them their rows.
      restoreQueues();
      // Seed the stream cursor BEFORE the history read, not after. A row
      // committed in between is then re-delivered by the stream and dropped by
      // the `msg_id` dedup; seeding afterwards would place it below the cursor
      // *and* outside the rendered page — and `markRoomRead` below would have
      // already consumed it, so it would not even show as unread. Same
      // capture-before-reload discipline `recoverStream` uses.
      // (limit=1 → the server answers from its MAX(id) gate, not a serialized
      // page.)
      try {
        const seed = await getRoomEvents(0, 1);
        roomCursor = seed.cursor;
        // Seed the deletion cursor too, from the same call: the history load
        // below already reflects every deletion so far, so replaying them as
        // frames would be pure noise.
        roomDeletionCursor = Number(seed.deletion_cursor) || 0;
        roomCursorSeeded = true;
      } catch {
        // Left unseeded rather than set to a position we do not have. The
        // stream below refuses to run from here and seeds itself on its first
        // successful tick instead.
        roomCursor = 0;
        roomDeletionCursor = 0;
        roomCursorSeeded = false;
      }
      if (superseded()) return;
      // Restore the last selection. An aggregate view is a selection in its own
      // right, not a mode layered over a room — restoring the room here while
      // `view` still said 'all' (the session is a module singleton, so `view`
      // outlives the route) left both highlighted in the sidebar and rendered
      // the room's history inside the aggregate pane.
      const savedView = loadSetting<string | null>('chat.view', null);
      const aggregate = AGGREGATE_VIEWS.includes(savedView as ChatView)
        ? (savedView as ChatView)
        : null;
      if (aggregate) {
        view.set(aggregate);
        activeRoomId.set(null);
        await loadViewPage(aggregate);
        if (superseded()) return;
      } else {
        view.set('room');
        const persisted = loadSetting<number | null>('chat.activeRoomId', null);
        const target = list.find((r) => r.id === persisted) ?? list[0];
        if (target) {
          activeRoomId.set(target.id);
          setRoomUnread(target.id, 0);
          await loadHistory(target.id);
          if (superseded()) return;
          markRoomRead(target.id).catch(() => {});
        }
      }
      loaded.set(true);
      // Collect what has aged out, after the first paint rather than before
      // it. An expired entry costs storage and not correctness — every read
      // refuses one anyway — so this is work nobody is waiting on, and it
      // holds `readwrite` on the store the cached paint is about to read.
      //
      // After `restoreQueues`, because the blob half of the collection is
      // "unreferenced by any live queue entry" and the queue is what says which
      // those are.
      void pruneOffline(Date.now(), referencedBlobIds());
      startRoomStream();
      // A load that finished offline returned before the drain trigger at its
      // own foot, and a connection that came back while `init` was running
      // produced no transition for the watch to act on — it was installed
      // before either could happen, but `onBackOnline` stands aside until the
      // first load is done. One attempt here covers both, and `canDrain` is
      // the gate here as everywhere else.
      const rid = get(activeRoomId);
      if (rid != null) void drainSendQueue(rid);
      // Slow metadata reconciler (see ROOMS_REFRESH_MS) — the stream is the
      // live path.
      startRoomsRefresh();
      // Seeded on entry because the drafts frame is *diffed* against a baseline
      // seeded empty, so an instance where the set has not changed since the
      // connection opened pushes no frame at all. Without this seed a draft
      // held before the tab opened would wait for the next change to something
      // else.
      void refreshDrafts();
      if (typeof document !== 'undefined') {
        removeVisibilityListener(); // never stack two
        onVisibility = () => {
          if (document.visibilityState !== 'visible') {
            hiddenSince = Date.now();
            // The last callback an iOS WebView is guaranteed before it may be
            // discarded, which is what `drafts.ts` uses it for. Two seconds of
            // collected frames is small, but the app being killed while
            // backgrounded is exactly the case the cache exists to survive.
            flushCachedRooms();
            return;
          }
          const away = hiddenSince == null ? 0 : Date.now() - hiddenSince;
          hiddenSince = null;
          const rid = get(activeRoomId);
          if (rid != null) markActiveRead(rid);
          // Client-side half of the gap threshold: only the client knows how
          // long it was away, only the server knows what the delta costs, so
          // each decides with what it has. Same recovery routine either way.
          // A connection that stayed open across the hidden period cannot have
          // missed a `messages` row, so that case reconciles metadata only —
          // otherwise every alt-tab during a long turn would tear down a
          // perfectly healthy task stream and re-render its answer from seq 0.
          if (away > ROOM_STREAM_STALE_MS) {
            void recoverStream(null, { metadataOnly: roomStreamLive });
          }
        };
        document.addEventListener('visibilitychange', onVisibility);
      }
    } catch {
      // Same exemption as loadViewPage above: no banner exists to put this in.
      notifyError('Failed to load chat');
    }
  }

  async function selectRoom(id: number) {
    if (get(activeRoomId) === id && get(view) === 'room') return;
    stopActive();
    view.set('room');
    saveSetting('chat.view', 'room');
    activeRoomId.set(id);
    saveSetting('chat.activeRoomId', id);
    setRoomUnread(id, 0); // optimistic — chip vanishes immediately on click
    messages.set([]);
    await loadHistory(id);
    markRoomRead(id).catch(() => {
      /* non-fatal; refresh/poll will retry */
    });
  }

  async function newRoom(name: string) {
    const room = await createChatRoom(name);
    rooms.update((r) => sortRoomsByActivity([...r, room]));
    await selectRoom(room.id);
  }

  // Both of these merge rather than replace: the PATCH response is the room's
  // own record and carries no `last_activity`, so adopting it wholesale would
  // strip the sidebar's sort key and drop a renamed room to the bottom of the
  // list. A rename is not activity, so the stamp is kept, not bumped.
  async function renameRoom(id: number, name: string) {
    const updated = await updateChatRoom(id, { name });
    rooms.update((r) => r.map((x) => (x.id === id ? { ...x, ...updated } : x)));
  }

  async function updateRoomSettings(id: number, patch: RoomPatch) {
    // `cleared` is a report about this request, not room state, so it is taken
    // off before the merge — spread onto the record it would stay there for the
    // life of the session and read as a standing property of the room.
    const { cleared, ...updated } = await updateChatRoom(id, patch);
    if (patch.brain !== undefined) {
      // The room's model aliases were resolved through the brain it had when
      // they were fetched, and the picker's cache is per session. Without this
      // the modal's own "pick a new one after saving" caption walks the user
      // back into a list the next save refuses with a 400.
      dropRoomCatalogue(id);
    }
    rooms.update((r) => r.map((x) => (x.id === id ? { ...x, ...updated } : x)));
    if (cleared?.length) {
      // Said after the fact as well as before it. The modal disables the model
      // select when it can see the change coming, but it is not the only client
      // and it cannot see a brain someone changed on another surface between
      // the modal opening and the save.
      notifyWarning(
        cleared.length > 1
          ? "This room's model and effort defaults were cleared — that model belongs to the previous brain."
          : "This room's model default was cleared — that model belongs to the previous brain.",
        { key: 'chat:room-model-cleared' },
      );
    }
  }

  async function promoteRoom(id: number) {
    // Doubles as the repair path for a room whose Talk conversation was deleted
    // (ISSUE-401), so the outcome has to be reported rather than assumed: three
    // of the five statuses change nothing, and one of those is the server
    // telling the user their existing link is fine.
    try {
      const { status, room } = await promoteChatRoom(id);
      if (room) rooms.update((r) => r.map((x) => (x.id === id ? { ...x, ...room } : x)));
      if (status === 'ok') {
        notifySuccess('This room is now open in Nextcloud Talk.', { key: 'chat:promote' });
      } else if (status === 'reconnected') {
        notifySuccess('Reconnected — this room has a fresh Talk conversation.', {
          key: 'chat:promote',
        });
      } else if (status === 'live') {
        notifyWarning('This room is already connected to a Talk conversation.', {
          key: 'chat:promote',
        });
      } else if (status === 'bot_removed') {
        notifyWarning(
          'That Talk conversation still exists, but Istota was removed from it. Add it back in Nextcloud.',
          { key: 'chat:promote' },
        );
      } else if (status === 'unreachable') {
        notifyError("Couldn't reach Nextcloud to check the existing Talk link. Try again.", {
          key: 'chat:promote',
        });
      } else {
        // `raced` carries the winner's room when it can, so the merge above has
        // already corrected the token this client was showing.
        notifyWarning('Another request connected this room to Talk first.', {
          key: 'chat:promote',
        });
      }
    } catch {
      notifyError("Couldn't open this room in Talk.", { key: 'chat:promote' });
    }
  }

  // A room the user has just deleted or hidden takes its unsent messages with
  // it: they were only ever going to be re-sent into that room, and holding
  // them would leak an entry nothing can reach — or, for a hidden Talk room
  // that the user's next message un-hides, resurrect them under a token that
  // has come back.
  //
  // That covers a queued message as well as a failed one, and it is the only
  // place either is dropped, so the three callers (delete, archive, and a
  // `remove` frame from another device) cannot diverge. A queue entry outlives
  // its room in `$rooms` otherwise, which leaves it neither drainable nor
  // droppable — and, if the token ever comes back, sends it.
  //
  // Clearing the map is not enough on its own. The departed room's transcript
  // is still in `messages` at this point (`deleteRoom` reselects a neighbour,
  // and `selectRoom` clears `messages` only *after* `stopActive`), so the
  // reselect's own stash would put every one of these rows straight back.
  function forgetRoom(id: number) {
    const token = get(rooms).find((r) => r.id === id)?.token;
    if (!token) return;
    strandedSends.delete(token);
    for (const entry of sendQueue.get(token) ?? []) parkedAfterPost.delete(entry.cid);
    sendQueue.delete(token);
    // The other mutation that does not write back — the stored copy stays on
    // purpose, see below — so the badge is squared here too.
    syncQueuedCounts();
    // The cached transcript goes, and it goes here rather than only on a real
    // delete. A queue holds text the user committed to sending, so an archive
    // or another device's `remove` frame must not destroy it; a cache holds
    // what the server already has, so keeping it buys a stale copy of a room
    // that is off the list, and the room coming back refetches it in full.
    // Whatever is still collected for it goes too, or the next flush would
    // write the entry straight back — timer included, or it would outlive the
    // session the teardown flush is there to clear.
    const pending = pendingCacheRows.get(token);
    if (pending) clearTimeout(pending.timer);
    pendingCacheRows.delete(token);
    void deleteTranscript(cacheUserId(), token);
    // The *stored* copy deliberately stays. Two of this function's three
    // callers are recoverable — an archive is undone by unarchiving, and a
    // `remove` frame can be another device's edit — and dropping the key here
    // would make either of them destroy text the user committed to sending,
    // silently and for good. Nothing can fire it in the meantime: a room
    // missing from `$rooms` is not restored at all, and a restore always
    // re-holds. `deleteRoom` drops the key on its own, because that one is not
    // recoverable; the TTL collects whatever else is left.
    messages.update((arr) => arr.filter((m) => !(isClientOnly(m) && m.roomToken === token)));
  }

  /**
   * Drop a room's *stored* queue, for a room that is not coming back.
   *
   * Separate from `persistRoomQueue` because the intent differs: this is the
   * room being destroyed, not its queue changing. Called from `deleteRoom`,
   * and again by the page after the same delete resolves — which is not
   * redundant, since a room already gone from `$rooms` leaves the store
   * nothing to look up while the page captured the token beforehand.
   */
  function dropRoomQueue(token: string | null | undefined) {
    if (!token) return;
    const key = queueKeyFor(token);
    if (key) dropStoredQueue(key);
  }

  async function archiveRoom(id: number) {
    await updateChatRoom(id, { archived: true });
    forgetRoom(id);
    rooms.update((r) => r.filter((x) => x.id !== id));
    if (get(activeRoomId) === id) {
      const remaining = get(rooms);
      if (remaining[0]) await selectRoom(remaining[0].id);
      else {
        activeRoomId.set(null);
        messages.set([]);
      }
    }
  }

  async function deleteRoom(id: number) {
    try {
      await deleteChatRoom(id);
    } catch (e) {
      if (e instanceof ChatRoomBusyError) {
        notifyWarning('This room has a task in progress — wait for it to finish or cancel it.');
      } else {
        notifyError("Couldn't delete room.");
      }
      return;
    }
    // On success (or a 404 already-gone) drop it from the list, mirroring
    // archiveRoom's fall-through when the active room disappears.
    // Read before `forgetRoom`, which is the last point at which the room is
    // still in `$rooms` to be looked up by id.
    const goneToken = roomTokenOf(id);
    forgetRoom(id);
    // A deleted room cannot come back, so its queue is the one case where the
    // stored copy goes too. Archive and a `remove` frame deliberately keep it.
    dropRoomQueue(goneToken);
    rooms.update((r) => r.filter((x) => x.id !== id));
    if (get(activeRoomId) === id) {
      const remaining = get(rooms);
      if (remaining[0]) await selectRoom(remaining[0].id);
      else {
        activeRoomId.set(null);
        messages.set([]);
      }
    }
  }

  async function selectRoomByToken(token: string): Promise<boolean> {
    const room = get(rooms).find((r) => r.token === token);
    if (!room) return false;
    await selectRoom(room.id);
    return true;
  }

  // Jump-to-response (memory-search overhaul): a search result card asks the
  // transcript to scroll to a specific turn. The store owns resolution (select
  // the room, page history to find the turn); the DOM scroll + highlight is the
  // route's job, driven by the `scrollTarget` signal. The nonce lets a repeated
  // jump to the same cid re-fire the effect.
  const scrollTarget = writable<{ cid: number; nonce: number } | null>(null);
  let scrollNonce = 0;
  function scrollToCid(cid: number) {
    scrollTarget.set({ cid, nonce: ++scrollNonce });
  }

  // The cid of the (assistant, else any) transcript row for a task, or null.
  function findCidByTask(taskId: number): number | null {
    const msgs = get(messages);
    const assistant = msgs.find((m) => m.taskId === taskId && m.role === 'assistant');
    if (assistant) return assistant.cid;
    const any = msgs.find((m) => m.taskId === taskId);
    return any ? any.cid : null;
  }

  const JUMP_MAX_PAGES = 5;

  function findCidByMsgId(msgId: number): number | null {
    return get(messages).find((m) => m.msgId === msgId)?.cid ?? null;
  }

  // Select the target room (if needed), locate the row `resolve` names — paging
  // older history up to a bound when it's outside the loaded window — then
  // scroll to it. Returns false (and sets a transient error) on any miss rather
  // than throwing, so a stale/foreign link degrades gracefully.
  async function jumpToRow(roomToken: string, resolve: () => number | null): Promise<boolean> {
    try {
      const room = get(rooms).find((r) => r.token === roomToken);
      if (!room) {
        notifyError("Couldn't open that conversation.");
        return false;
      }
      if (get(activeRoomId) !== room.id || get(view) !== 'room') {
        const ok = await selectRoomByToken(roomToken);
        if (!ok) {
          notifyError("Couldn't open that conversation.");
          return false;
        }
      }
      let cid = resolve();
      let pages = 0;
      while (cid == null && get(hasMore) && !get(loadingOlder) && pages < JUMP_MAX_PAGES) {
        await loadOlder();
        pages += 1;
        cid = resolve();
      }
      if (cid == null) {
        notifyError("Couldn't locate that message.");
        return false;
      }
      scrollToCid(cid);
      return true;
    } catch {
      notifyError("Couldn't jump to that message.");
      return false;
    }
  }

  function jumpToTask(roomToken: string, taskId: number): Promise<boolean> {
    return jumpToRow(roomToken, () => findCidByTask(taskId));
  }

  /** The jump a rendered citation performs: same routine, keyed on the
   * canonical `messages.id` rather than on a task. */
  function jumpToMsgId(roomToken: string, msgId: number): Promise<boolean> {
    return jumpToRow(roomToken, () => findCidByMsgId(msgId));
  }

  async function send(text: string, attachments: ChatAttachment[] = [], replyTo?: MessageReply) {
    const roomId = get(activeRoomId);
    const trimmed = text.trim();
    if (!roomId || (!trimmed && attachments.length === 0)) return;
    // Any `!word`, not only a catalogued one, and matching `sendTurn`'s own
    // test rather than `isKnownCommand`: what the two rules below turn on is
    // that the *endpoint* may answer this inside the request, and the client's
    // catalogue is not the authority on that. An attachment disqualifies it
    // either way, since a file belongs to a task.
    const isCommandBody = attachments.length === 0 && trimmed.startsWith('!');

    // Known offline, so the POST cannot land and there is nothing to learn by
    // making it: the message is queued and the drain sends it when the
    // connection is back (ISSUE-202). Ahead of the busy branch below on
    // purpose. A room can be both busy and offline, and of the two facts it is
    // the connection that says what happens next — an entry queued because a
    // turn was running is held on the next page load, where one queued because
    // the phone was in a lift is exactly what should go out on its own.
    //
    // A `!command` takes this path too, and has to: the endpoint answers it
    // inside the request, so with nothing to reach it cannot be answered at
    // all, and the alternative is a 30s timeout ending in a failed row.
    if (!get(online)) {
      // Queued as a *busy* entry when it is a command, so a page load does not
      // fire it. Same reasoning `sendTurn` withholds Retry and the park on:
      // the endpoint answers a command inside the request, `!steer` appends a
      // note per call and `!retry` creates a task per call, and by the time a
      // restored one went out the turn it named would be long over. It still
      // goes on its own if the connection returns in this session, which is as
      // close to "now" as there is with nothing to send it to.
      enqueueSend(roomId, trimmed, attachments, replyTo, isCommandBody ? 'busy' : 'offline');
      return;
    }

    // Online, but carrying bytes that are still in this browser — a voice note
    // recorded in a lift, with the signal back by the time Send was tapped. The
    // two-step resolution that turns those into host paths lives in the drain
    // and nowhere else, so the message goes through the queue and is drained
    // immediately rather than taking the direct path and POSTing a file
    // reference the server cannot resolve.
    if (attachments.some((a) => !!a.pendingBlobId)) {
      // 'offline' even though the connection is back: the bytes were held
      // because there was none, and if this drain does not land the entry
      // should still go out by itself later. A busy room is the exception —
      // there the wait is the turn, which is what `busy` means.
      const idle = get(status) === 'idle';
      enqueueSend(roomId, trimmed, attachments, replyTo, idle ? 'offline' : 'busy');
      // `canDrain` is the gate here as everywhere else, so a busy room queues
      // and waits for the running turn to settle.
      await drainSendQueue(roomId);
      return;
    }

    // A turn is already running. The one message that may still go out is a
    // `!command`: the endpoint answers it inside the request and returns no
    // task id, so it does not need the turn machinery below — and must not take
    // it, since `status`, the `pendingSend` echo slot and the pending-cancel
    // flag all belong to the turn that is running.
    //
    // Anything else here would be a second `runTurn` in a room that already
    // has one, so it is queued (ISSUE-238) and drains into the same single
    // entry point when the running turn settles. `runTurn`'s invariant is
    // unchanged; the queue feeds the `status === 'idle'` guards rather than
    // becoming an exception to them. An attachment belongs to a task either
    // way, which is why an attachment never takes the inline path.
    //
    // "No task id" is a statement about this response, not about the server:
    // `!retry`, `!resume` and `!confirm` all leave a task queued and still
    // answer inline. Those turns arrive over the room stream and are picked up
    // by `pickUpStreamedTask` like any turn started elsewhere, which is why
    // they are none of this path's business.
    if (get(status) !== 'idle') {
      if (attachments.length === 0 && isKnownCommand(trimmed)) {
        await sendInlineCommand(roomId, trimmed);
        return;
      }
      enqueueSend(roomId, trimmed, attachments, replyTo);
      return;
    }

    const userCid = nextCid();
    // Which room this row belongs to, stamped now rather than waiting for the
    // echo: a send that fails has no echo, and the row has to be re-appendable
    // to its own transcript (and only its own) after a rebuild.
    const roomToken = get(rooms).find((r) => r.id === roomId)?.token;
    const idempotencyKey = newIdempotencyKey();
    // Above the client-only block: the room can be idle with a *held* queue on
    // screen (Stop, an error, a parked confirmation), and this message is going
    // out now while those are still waiting.
    messages.update((a) =>
      appendAboveClientOnly(a, {
        cid: userCid,
        role: 'user',
        text: trimmed,
        segments: [],
        streaming: false,
        roomToken,
        attachments: attachments.map((x) => x.name),
        // The upload already told us where each file is reachable, so a chip
        // is a working link the moment it appears rather than only after the
        // turn comes back from history.
        attachmentPaths: attachments.map((x) => x.workspace_path ?? null),
        createdAt: new Date().toISOString(),
        // Rendered optimistically from what the composer staged, so the quote
        // is on the bubble the moment it appears; the echo replaces it with
        // the server's own resolution.
        replyTo,
        sendState: 'sending',
        // Stashed now rather than reconstructed on failure: the row keeps
        // display names and workspace paths, and a retry needs the host paths.
        sendPayload: {
          text: trimmed,
          attachments,
          idempotencyKey,
          replyToMsgId: replyTo?.msgId,
        },
      }),
    );
    await runTurn(roomId, userCid, trimmed, attachments, idempotencyKey, replyTo?.msgId);
  }

  /**
   * A per-message identity the server dedups on, or undefined when the browser
   * cannot mint one.
   *
   * Undefined is a working send, not a failure: the endpoint treats a missing
   * key exactly as it did before the field existed. `crypto.randomUUID` is in
   * every target browser but is a secure-context API, and a send is not the
   * place to find out this page is not one.
   */
  function newIdempotencyKey(): string | undefined {
    try {
      return crypto.randomUUID();
    } catch {
      return undefined;
    }
  }

  /**
   * Take a message the room cannot send right now, to go out when it can.
   *
   * Appends the same user row `send()` would have appended — same cid, room
   * token, attachments, timestamp, optimistic reply quote — but marked
   * 'queued' and carrying no `sendPayload`, because the queue entry holds it.
   * Nothing here touches `status`, `cancelRequested` or `pendingSend`: those
   * belong to the turn that is running.
   *
   * `reason` is why it could not go: a turn was running (ISSUE-238) or there
   * was no connection (ISSUE-202). Both wait for the same drain; they part
   * company on a restore, where only the second may send itself.
   */
  /**
   * The blob references behind a staged list's unresolved chips, in order.
   *
   * The two live side by side on a queue entry and name each other: a chip with
   * no `path` carries the `pendingBlobId` of exactly one of these, and the
   * drain resolves them a pair at a time. Derived here rather than passed in,
   * so the composer hands `send()` one list and the split happens in the one
   * place that knows what a queue entry looks like.
   */
  function pendingOf(attachments: ChatAttachment[]): PendingAttachment[] {
    const out: PendingAttachment[] = [];
    for (const a of attachments) {
      if (!a.pendingBlobId) continue;
      out.push({
        blobId: a.pendingBlobId,
        name: a.name,
        mimeType: a.mimeType ?? '',
        size: a.size,
      });
    }
    return out;
  }

  function enqueueSend(
    roomId: number,
    trimmed: string,
    attachments: ChatAttachment[],
    replyTo?: MessageReply,
    reason: QueueReason = 'busy',
  ) {
    const roomToken = roomTokenOf(roomId);
    // The queue is keyed by token, so a room that has left `$rooms` (deleted
    // on another device, mid-frame) has nowhere to file this. The idle path
    // would have POSTed it regardless — `runTurn` takes the id, not the token
    // — so refusing here is a real loss and has to be reported rather than
    // swallowed. A row on screen with no entry behind it would be worse.
    if (!roomToken) {
      notifyError('Couldn’t queue that message — the room is no longer available.');
      return;
    }
    // The per-room cap is enforced here, not only where the queue is stored.
    // `writeQueue` trims a room to its first `MAX_QUEUED_PER_ROOM` entries —
    // the FIFO head, since that is what drains next — so an eleventh message
    // accepted into memory would sit on screen looking queued while the copy
    // that survives a reload was the one silently dropped. Refusing keeps the
    // two in step. The composer gains its own refusal on top, which is the
    // better one because it keeps the text in the field rather than in a
    // notice; this is the backstop under it.
    if ((sendQueue.get(roomToken)?.length ?? 0) >= MAX_QUEUED_PER_ROOM) {
      notifyError('Too many messages waiting to send in this room.');
      return;
    }
    const cid = nextCid();
    messages.update((a) => [
      ...a,
      {
        cid,
        role: 'user',
        text: trimmed,
        segments: [],
        streaming: false,
        roomToken,
        attachments: attachments.map((x) => x.name),
        attachmentPaths: attachments.map((x) => x.workspace_path ?? null),
        createdAt: new Date().toISOString(),
        replyTo,
        sendState: 'queued',
        queueReason: reason,
      },
    ]);
    const entries = sendQueue.get(roomToken) ?? [];
    entries.push({
      cid,
      text: trimmed,
      attachments,
      ...(pendingOf(attachments).length ? { pendingAttachments: pendingOf(attachments) } : {}),
      replyTo,
      replyToMsgId: replyTo?.msgId,
      idempotencyKey: newIdempotencyKey(),
      held: false,
      queuedAt: Date.now(),
      reason,
    });
    sendQueue.set(roomToken, entries);
    // Written now rather than on unload, so a tab closed on a queued message
    // depends on catching no departure event.
    persistRoomQueue(roomToken);
  }

  async function retrySend(cid: number) {
    // Retry is the first entry into `runTurn` that isn't gated by the
    // composer's `busy`, and `runTurn` is still not re-entrant: it drains the
    // `pendingSend` slot on the way in, so retrying during another send would
    // release that send's echo before its task id was stamped. It also resets
    // `cancelRequested`, discarding a Stop tapped moments earlier.
    if (get(status) !== 'idle') return;
    const m = get(messages).find((x) => x.cid === cid);
    // Only a failed row that a retry could actually resolve. `retryable` is
    // false for an expired session, where re-POSTing would fail identically.
    if (!m || m.sendState !== 'failed' || m.retryable === false || !m.sendPayload) return;
    const roomId = get(activeRoomId);
    if (!roomId) return;
    await beginSend(roomId, cid, m.sendPayload);
  }

  /**
   * Put an existing user row back into flight: a retry, or a queued message
   * whose turn has come.
   *
   * Both re-enter `runTurn` with the *same* cid deliberately — see `runTurn`.
   * The rendered row's `attachmentPaths` still carry the workspace paths, so
   * the chips stay live across it; only the POST needs the host ones.
   *
   * `sendPayload` is stamped rather than assumed: for a retry it is already
   * there, and for a drain the row was carrying none (the queue entry held it)
   * and needs one now, so a failure has its Retry.
   *
   * The payload's *original* idempotency key rides along rather than a fresh
   * one, which is the whole point of it. A send the server accepted and then
   * failed to report (a client timeout, a dropped socket, a second tab
   * draining the same restored entry) is recognised and answered with the
   * first task, so no second task and no second bubble exist to reconcile.
   */
  async function beginSend(roomId: number, cid: number, payload: SendPayload) {
    // A second send of the same message, while the first is still uploading
    // its files, is refused here rather than left to the row state. See
    // `resolvingSends`.
    if (resolvingSends.has(cid)) return;
    updateMsg(cid, (m) => {
      m.sendState = 'sending';
      m.sendError = undefined;
      m.retryable = undefined;
      m.queueHeld = undefined;
      m.sendPayload = payload;
    });
    let resolved: SendPayload | null;
    resolvingSends.add(cid);
    try {
      resolved = await resolvePendingAttachments(roomId, cid, payload);
    } finally {
      resolvingSends.delete(cid);
    }
    // The resolution reported the outcome itself — parked, failed, or the
    // entry left under it — so there is nothing left for this send to do.
    if (!resolved) return;
    if (resolved !== payload) {
      // A retry after this point has to POST the paths, not the chips that no
      // longer name anything.
      updateMsg(cid, (m) => {
        m.sendPayload = resolved;
      });
    }
    await runTurn(
      roomId,
      cid,
      resolved.text,
      resolved.attachments,
      resolved.idempotencyKey,
      resolved.replyToMsgId,
    );
  }

  /**
   * The cid a placeholder would have had, for a send that never opened a turn.
   *
   * `parkSend` and `failSend` both take the assistant placeholder's cid so they
   * can remove it; a resolution that fails before `runTurn` has minted one has
   * none to remove, and no row can hold this value.
   */
  const NO_PLACEHOLDER = -1;

  /**
   * Sends whose files are being uploaded right now, by cid.
   *
   * What stops a second drain of the same entry, and it has to be this rather
   * than the row's own `sendState`. A drain marks its row `'sending'`, and
   * `canDrain` refuses while any row is — but a `'sending'` row is not
   * client-only, so a room switch drops it (`stashStrandedSends` keeps only
   * failed and queued rows) and the switch back rebuilds it from the *entry*,
   * which is still in the queue, as `'queued'`. `loadHistory` then drains at
   * its foot and a second resolution starts on an entry the first is still
   * working through: whichever calls `getBlob` after the other's `deleteBlob`
   * finds nothing and fails a message whose upload had in fact succeeded.
   *
   * That window exists for a plain text send too — it is the length of the
   * POST — and the idempotency key is what makes it harmless there. Nothing
   * makes an *upload* idempotent, and this window is as long as the file takes,
   * so the claim is explicit. Session-lived and never persisted: it describes
   * work in flight in this tab, which by definition does not survive a reload.
   */
  const resolvingSends = new Set<number>();

  /**
   * Turn an entry's held bytes into host paths, one file at a time.
   *
   * The first step of a two-step send (ISSUE-202). An attachment written with
   * no connection has no path, because the path is what the upload returns —
   * so the queue entry carries the bytes and this is where they become a file
   * the message can reference.
   *
   * **The entry is persisted after each upload lands**, before the next one
   * starts and before the blob is deleted. A drain interrupted between two
   * files — a force-quit, a signal lost again — resumes at the file that has
   * not been uploaded rather than re-uploading one the server already has.
   * That is the whole reason the resolved chip and its pending record are two
   * fields kept in step rather than one list rewritten at the end.
   *
   * Returns the payload to POST, or null when it has already reported the
   * outcome. The failures split exactly as a send's do: a gap leaves the entry
   * and its bytes where they are, and a refusal — a 413, an extension the
   * server does not take, a `max_attachment_mb` lowered since — fails the whole
   * row and drops its bytes, because a retry cannot fix a file the server will
   * not accept and holding it forever is the wrong answer.
   *
   * The failed row carries no Retry, for that reason, and the text on it is all
   * that is left of the message. The spec's §4 says the text is "recoverable
   * through Edit"; no failed row in this app has ever offered Edit, and adding
   * one is a decision about every failed send rather than about this path.
   */
  async function resolvePendingAttachments(
    roomId: number,
    cid: number,
    payload: SendPayload,
  ): Promise<SendPayload | null> {
    const start = findQueued(cid);
    if (!start?.entries[start.idx].pendingAttachments?.length) {
      // A payload carrying chips with no entry to resolve them from cannot be
      // sent: `sendTurn` would drop them and the message would go without the
      // file it was written about. Only reachable for a row whose entry was
      // taken while its retry was in flight.
      if (payload.attachments.some((a) => !a.path)) {
        failSend(cid, NO_PLACEHOLDER, MISSING_BYTES_REASON, false, roomId);
        return null;
      }
      return payload;
    }
    // Claim the room for the length of the uploads, as `runTurn` does for the
    // POST. Without it the composer reads the room as idle for however long a
    // voice note takes to go up, and a message typed in that window would start
    // a second turn in a room that already has one — the invariant `runTurn`
    // rests on. Stop stays live: `cancel` latches the intent while the status
    // is 'sending', and the loop honours it below.
    const owned = get(activeRoomId) === roomId;
    if (owned) status.set('sending');
    const releaseRoom = () => {
      if (owned && get(activeRoomId) === roomId) status.set('idle');
    };
    /**
     * Stop, tapped while a file was going up.
     *
     * The intent is *consumed* here, not merely read: `runTurn` is the only
     * other place that clears the flag and this path never reaches it, so
     * leaving it set would make the row's own Send button re-enter, read the
     * stale latch, and hold itself again — for the life of the session.
     *
     * The message has not been sent, so it goes back to waiting, held, like
     * every other queue the user has abandoned work in front of.
     */
    const cancelled = (token: string): null => {
      cancelRequested = false;
      requeueRow(cid);
      holdRoomQueue(token);
      releaseRoom();
      return null;
    };
    for (;;) {
      const found = findQueued(cid);
      if (!found) {
        // Removed or edited while a file was uploading. The row went with the
        // entry, so there is nothing to send — but it is normalized off
        // 'sending' first, because a row left in that state would wedge the
        // room's queue behind `canDrain`'s no-row-is-sending clause.
        requeueRow(cid);
        releaseRoom();
        return null;
      }
      // Ahead of the empty check below, not after it. With one attachment —
      // the voice note this feature is written for — the last upload finishes,
      // the list empties, and a Stop tapped during that upload would otherwise
      // fall straight out of the loop and be cleared unread by `runTurn`.
      if (cancelRequested) return cancelled(found.token);
      const entry = found.entries[found.idx];
      const item = entry.pendingAttachments?.[0];
      if (!item) break;
      const record = await getBlob(item.blobId);
      if (!record) {
        // The bytes are gone — evicted with the origin's storage, or collected
        // after the entry that named them was somehow lost. Sending the message
        // without them would be a different message.
        dropHeldBytes(entry);
        failSend(cid, NO_PLACEHOLDER, MISSING_BYTES_REASON, false, roomId);
        return null;
      }
      try {
        const att = await uploadChatAttachment(
          new File([record.bytes], item.name, { type: record.mimeType }),
        );
        // A 200 that carries no path is not a stored file. Unchecked it would
        // be written into the queue entry as a chip the *next* read refuses,
        // which deletes the whole message — after its bytes have been dropped
        // as successfully uploaded. Treated as the refusal it is instead.
        if (typeof att.path !== 'string' || !att.path) {
          throw new Error('the server did not say where it put the file');
        }
        // Re-found rather than reused: the awaits above are exactly where the
        // entry can have been taken out from under this.
        const after = findQueued(cid);
        if (!after) {
          void deleteBlob(item.blobId);
          requeueRow(cid);
          releaseRoom();
          return null;
        }
        const live = after.entries[after.idx];
        live.attachments = live.attachments.map((a) => (a.pendingBlobId === item.blobId ? att : a));
        const rest = (live.pendingAttachments ?? []).filter((p) => p.blobId !== item.blobId);
        if (rest.length) live.pendingAttachments = rest;
        else delete live.pendingAttachments;
        // Before the blob is deleted, so the two can never both be gone.
        persistRoomQueue(after.token);
        await deleteBlob(item.blobId);
        // The chip is a working link the moment its file exists, as it is for
        // a file uploaded from the composer.
        updateMsg(cid, (m) => {
          m.attachmentPaths = live.attachments.map((x) => x.workspace_path ?? null);
        });
      } catch (e) {
        if (e instanceof UploadUnreachableError) {
          // Nothing was decided about this message. It goes back to waiting
          // with its bytes intact, and the next drain resumes where this one
          // stopped.
          if (!parkSend(cid, NO_PLACEHOLDER, roomId, true, { reachedWire: false })) {
            failSend(
              cid,
              NO_PLACEHOLDER,
              'Couldn’t send — the server is unreachable.',
              true,
              roomId,
            );
          }
          return null;
        }
        dropHeldBytes(findQueued(cid)?.entries[findQueued(cid)!.idx] ?? entry);
        failSend(cid, NO_PLACEHOLDER, uploadRefusalReason(e), false, roomId);
        return null;
      }
    }
    const done = findQueued(cid);
    return {
      ...payload,
      attachments: done ? done.entries[done.idx].attachments : payload.attachments,
    };
  }

  /** The sentence for a message whose file cannot be found to send. */
  const MISSING_BYTES_REASON = 'Couldn’t send — the attached file is no longer available.';

  /** The sentence for a file the server itself turned away. */
  function uploadRefusalReason(e: unknown): string {
    if (e instanceof AuthError) {
      return 'Your session expired. Reload to sign in again.';
    }
    const message = e instanceof Error ? e.message : '';
    return message ? `Couldn’t send — ${message}.` : 'Couldn’t send — the file was refused.';
  }

  /** Drop every blob an entry still holds. Its message is not going out. */
  function dropHeldBytes(entry: QueuedSend | undefined): void {
    for (const p of entry?.pendingAttachments ?? []) void deleteBlob(p.blobId);
  }

  /** Put a row that was mid-send back to the queued state its entry is in. */
  function requeueRow(cid: number): void {
    updateMsg(cid, (m) => {
      m.sendState = 'queued';
      m.sendError = undefined;
      m.retryable = undefined;
      m.showSending = undefined;
      m.sendPayload = undefined;
    });
  }

  /**
   * Whether the head of `roomId`'s send queue may go out right now.
   *
   * Every trigger re-tests this rather than trusting the state it was called
   * from; a drain that is not allowed is a silent no-op, not an error.
   */
  function canDrain(roomId: number): boolean {
    // An aggregate view has no composer and is not a room. A background room's
    // queue waits for the user to come back: the client has no stream for a
    // task it is not watching, and firing a message into a room nobody is
    // looking at is worse than waiting.
    if (get(view) !== 'room' || get(activeRoomId) !== roomId) return false;
    // Draining with no connection just manufactures failures: every entry
    // would be POSTed, time out, and be parked again — one 30s stall per
    // message, for an answer the store already has (ISSUE-202).
    if (!get(online)) return false;
    // Nothing goes out under a guessed identity (ISSUE-202). A session that
    // booted from the `chat.lastUserId` pointer restored its queue out of that
    // id's storage, and if the guess is wrong those are someone else's
    // messages — sending one under this session's cookie would post it as this
    // user. The guess is settled by the first config the server answers, which
    // on a connection good enough to drain is moments away, so this costs a
    // beat on the one path where it applies and closes the outcome outright.
    if (userIdFromPointer) return false;
    // The three ways the room can still be busy. `status` alone is not enough:
    // it is set idle while a stream is being handed on, and a send that is
    // mid-POST has not claimed it yet.
    if (get(status) !== 'idle' || activeStream !== null || streamQueue.length > 0) return false;
    if (get(messages).some((m) => m.sendState === 'sending')) return false;
    const token = roomTokenOf(roomId);
    if (!token) return false;
    const head = sendQueue.get(token)?.[0];
    return !!head && !head.held;
  }

  /**
   * Send the head of a room's queue, if it may go.
   *
   * One entry per drain: the next one goes when *this* turn settles.
   *
   * **The entry stays in the queue until the POST resolves** (ISSUE-202), and
   * the resolution is what removes it: the ack through `settleSend`, a refusal
   * through `failSend`, a dead citation through `returnSend`. This reverses
   * the order ISSUE-238 shipped, where the entry was shifted off first so that
   * a reload could not restore a message that had already gone. That trade was
   * right when the POST was not idempotent and is not now: every entry carries
   * the `idempotencyKey` it was minted with, so a second POST of the same
   * message is answered with the first task rather than creating a second one
   * (`beginSend`, and `transport/ingest.py`'s replay resolution). What the
   * early shift cost instead was real — a force-quit between the shift and the
   * ack lost the message client-side while the server may well have taken it,
   * which is the one outcome an outbox exists to prevent.
   *
   * Draining the same head twice is impossible either way: `canDrain` refuses
   * while any row is `sendState: 'sending'`, which `beginSend` sets
   * synchronously before it awaits.
   */
  async function drainSendQueue(roomId: number) {
    if (!canDrain(roomId)) return;
    const token = roomTokenOf(roomId);
    const entry = token ? sendQueue.get(token)?.[0] : undefined;
    if (!token || !entry) return;
    await beginSend(roomId, entry.cid, {
      text: entry.text,
      attachments: entry.attachments,
      idempotencyKey: entry.idempotencyKey,
      replyToMsgId: entry.replyToMsgId,
    });
  }

  /**
   * Take a queued message back out of the queue.
   *
   * Its uploaded attachments are left orphaned server-side — the same
   * already-tolerated outcome as closing the tab mid-compose. Bytes it was
   * still *holding* are a different case and are deleted: they are ours, they
   * are on this device, and nothing else names them once the entry is gone.
   * `editQueued` deliberately does not do this — there the chip goes back to
   * the composer and its bytes are the only copy left.
   */
  function removeQueued(cid: number) {
    const taken = takeQueued(cid);
    dropHeldBytes(taken?.entry);
  }

  /**
   * Put a queued message back in the composer to be edited.
   *
   * Edit is remove-plus-restore rather than an in-place editor on the bubble:
   * `sendReturned` already carries text and uploaded attachments back to the
   * composer, and the page already guards that restore on the room token,
   * which is exactly the guard this needs.
   */
  function editQueued(cid: number) {
    // The destructive half runs first, and the page's restore returns early
    // when the token is not the active room's — so without this the only copy
    // of a background room's message would be deleted and nothing would put it
    // back. `findQueued` scans every room's queue by cid, so the check has to
    // be here rather than assumed from the caller.
    const found = findQueued(cid);
    if (!found || found.token !== roomTokenOf(get(activeRoomId) ?? -1)) return;
    const taken = takeQueued(cid);
    if (!taken) return;
    sendReturned.update((s) => ({
      n: s.n + 1,
      token: taken.token,
      text: taken.entry.text,
      attachments: taken.entry.attachments,
      // Carried so an edited reply does not come back as an ordinary message
      // and get re-sent without its parent. `returnSend` leaves both unset:
      // its whole reason for existing is that the cited parent is gone.
      replyTo: taken.entry.replyTo,
      replyToMsgId: taken.entry.replyToMsgId,
    }));
  }

  /**
   * Clear the hold on one entry and try to send.
   *
   * Only the head can actually go, so releasing an entry behind a held one
   * marks it ready and sends nothing until its turn comes round.
   */
  async function releaseQueued(cid: number) {
    const found = findQueued(cid);
    if (!found) return;
    found.entries[found.idx].held = false;
    updateMsg(cid, (m) => {
      m.queueHeld = undefined;
    });
    // The mirror of `holdRoomQueue`'s second loop: a row that is off screen
    // lives in the holding map, and leaving it marked would bring it back
    // rendering as held while its entry says otherwise.
    for (const m of strandedSends.get(found.token) ?? []) {
      if (m.cid === cid) m.queueHeld = undefined;
    }
    persistRoomQueue(found.token);
    const roomId = get(activeRoomId);
    if (roomId != null) await drainSendQueue(roomId);
  }

  /**
   * The shared body of a first send and a retry: open the echo buffer, POST,
   * settle, then hand the turn to its assistant placeholder.
   *
   * Retry re-enters here with the *same* `userCid` deliberately — the echo
   * dedup in `appendStreamedRow` keys on `(role, task_id)`, so stamping the new
   * task id onto the existing row is what folds the canonical `messages` row
   * into it. Appending a fresh row would leave the failed one behind and show
   * two user bubbles for one message.
   */
  async function runTurn(
    roomId: number,
    userCid: number,
    trimmed: string,
    attachments: ChatAttachment[],
    idempotencyKey?: string,
    replyToMsgId?: number,
  ) {
    const phCid = nextCid();
    let graceTimer: ReturnType<typeof setTimeout> | undefined;
    // This turn's echo buffer, held locally so the drain below can name it.
    let mine: PendingSend | null = null;
    // The whole body is guarded, not just the POST: a throw anywhere in here
    // escaped to an un-awaited caller and left the row stuck on 'sending'
    // forever — and on a *retry* that is unrecoverable, since `retrySend`'s own
    // guard only accepts a row whose state is 'failed'.
    try {
      // The assistant placeholder is NOT appended here — `sendTurn` adds it on
      // the ack. Appended up front it spun its ack verb ("Sleuthing…") before
      // the message had reached the server, so once the grace below opened
      // `Sending…` the turn carried two progress indicators at once, and the
      // assistant one was claiming work that had not started. A `!command`
      // makes that certain rather than occasional: it runs inside the request,
      // so the POST stays open for the command's whole duration.
      status.set('sending');
      cancelRequested = false;

      // The mark's job is "this is taking longer than it should", not "a send
      // happened". The common send resolves in well under 100ms, and a mark that
      // appears and vanishes inside one frame is noise that trains you to ignore
      // it — so the row carries the truthful `sendState` immediately and the
      // render gate opens only if the POST is still open after the grace.
      //
      // With the placeholder deferred this is also the turn's *only* indicator
      // until the ack, which is what keeps the count at one: pre-ack the user
      // row owns it, post-ack the assistant row does.
      graceTimer = setTimeout(() => {
        updateMsg(userCid, (m) => {
          if (m.sendState === 'sending') m.showSending = true;
        });
      }, SEND_PENDING_GRACE_MS);

      // Hold this room's stream frames until the turn's task id is stamped
      // below — see `pendingSend`.
      //
      // The slot is empty by the time any turn reaches here, and the drain is
      // a safety net rather than a step: a turn's own `finally` clears it, a
      // room switch abandons it in `stopActive`, and the three ways to start a
      // turn are all gated against overlapping in the *same* room (the send
      // button is in Stop mode, `submit` refuses the keyboard chord while
      // busy, `retrySend` requires an idle status). What it must never do is
      // release a *live* turn's buffer, whose task id is not stamped yet —
      // that is the duplicate the buffer exists to prevent.
      drainPendingSend();
      const sendToken = get(rooms).find((r) => r.id === roomId)?.token;
      if (sendToken) {
        mine = { token: sendToken, rows: [] };
        pendingSend = mine;
      }

      await sendTurn(roomId, trimmed, attachments, userCid, phCid, idempotencyKey, replyToMsgId);
    } catch {
      // `sendChatMessage` classifies rather than throwing, so this is the
      // unforeseen case. It still must not escape: an un-reset 'sending' left
      // the composer locked in Stop mode until reload (ISSUE-200).
      //
      // Only when the send itself hadn't settled. `settleSend` runs the moment
      // the backend acks, ahead of everything downstream that could throw — so
      // a later failure is a problem with the *turn*, and reporting it as a
      // failed send would delete a placeholder whose task is genuinely running.
      // Past the ack the turn owns its own status transitions, so this leaves
      // them alone — forcing 'idle' here would strand a live stream by telling
      // the composer the room is free.
      if (get(messages).find((m) => m.cid === userCid)?.sendState === 'sending') {
        failSend(userCid, phCid, 'Couldn’t send — something went wrong.', true, roomId);
      }
    } finally {
      if (graceTimer) clearTimeout(graceTimer);
      updateMsg(userCid, (m) => {
        m.showSending = undefined;
      });
      // Runs after the task id is on both halves of the turn, so the replayed
      // echo dedups instead of duplicating. Only this turn's buffer: a room
      // switch (or a later send) may have abandoned or replaced the slot, and
      // draining that one would release frames whose turn has no id yet.
      if (mine) drainPendingSend(mine);
    }
  }

  /**
   * Hold a send that never reached the server, instead of failing it.
   *
   * `unreachable` and `timeout` are the two outcomes that say nothing about
   * the message: the server was not reached, so nobody has refused anything
   * and a failed row would report a verdict that was never given (ISSUE-202).
   * The row goes back to `queued` and the entry waits for the connection —
   * which is the whole of the offline outbox on this side.
   *
   * Two ways in, and they differ only in whether the entry exists already.
   * A drained entry never left the queue (see `drainSendQueue`), so it is
   * marked `offline` where it sits and ordering against anything behind it is
   * preserved by doing nothing. A send that started from an idle room while
   * the store still believed it was online has no entry, so it takes the head
   * — it was written before everything already waiting there.
   *
   * Holding a `timeout` needs the more careful justification, because a
   * timeout is ambiguous: the task may exist. It is safe because the entry
   * carries the `idempotencyKey` this message was minted with, so the re-POST
   * resolves to the first task rather than creating a second one.
   *
   * Returns false when there is nowhere to hold it — a room that has left
   * `$rooms`, or a row that left the screen with its payload — so the caller
   * reports the ordinary failure it would have reported anyway.
   */
  function parkSend(
    userCid: number,
    phCid: number,
    roomId: number,
    settleStatus: boolean,
    {
      // False when the *message* POST was never made — an attachment upload
      // that found no server, ahead of it. No echo can be coming for a message
      // the server was never asked to take, so the row must not join the set
      // that a body match may adopt a server row into.
      reachedWire = true,
    }: { reachedWire?: boolean } = {},
  ): boolean {
    const token = roomTokenOf(roomId);
    if (!token) return false;
    const found = findQueued(userCid);
    const row = get(messages).find((m) => m.cid === userCid);
    const payload = row?.sendPayload;
    if (!found && !payload) return false;
    // The same cap `enqueueSend` enforces, and for the same reason: `prune`
    // keeps a room's first `MAX_QUEUED_PER_ROOM` entries, so an eleventh
    // accepted into memory would sit on screen as a queued row whose stored
    // copy was the one silently dropped. A room already at the cap is a room
    // with nowhere to hold this, so it takes the ordinary failed row and its
    // Retry — which is the recovery, since nothing here is lost.
    if (!found && (sendQueue.get(token)?.length ?? 0) >= MAX_QUEUED_PER_ROOM) return false;
    if (found) {
      found.entries[found.idx].reason = 'offline';
      found.entries[found.idx].held = false;
    } else {
      const entries = sendQueue.get(token) ?? [];
      const pending = pendingOf(payload!.attachments);
      entries.unshift({
        cid: userCid,
        text: payload!.text,
        attachments: payload!.attachments,
        // Rebuilt from the chips, not carried from an entry there is none of.
        // A chip with no path and no record behind it is exactly the shape
        // `readEntry` refuses, so leaving this out would store a message that
        // the next page load deletes without a word.
        //
        // Hardening rather than a live path: this branch needs a row whose
        // entry is gone, and every route that takes an entry takes its row
        // with it (`takeQueued`) or takes the room, which the token lookup
        // above already refuses. It is written because the *shape* is now
        // constructible, not because a caller reaches it — the cost of being
        // wrong about that is a message deleted with no report.
        ...(pending.length ? { pendingAttachments: pending } : {}),
        // Off the row, which is the only place the optimistic quote lives —
        // `sendPayload` carries the id the POST takes and not the excerpt the
        // bubble draws.
        replyTo: row?.replyTo,
        replyToMsgId: payload!.replyToMsgId,
        idempotencyKey: payload!.idempotencyKey,
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
      });
      sendQueue.set(token, entries);
    }
    // The turn produced no assistant message, exactly as in `failSend`.
    messages.update((arr) => arr.filter((m) => m.cid !== phCid));
    updateMsg(userCid, (m) => {
      m.sendState = 'queued';
      m.queueReason = 'offline';
      m.queueHeld = undefined;
      m.sendError = undefined;
      m.retryable = undefined;
      m.showSending = undefined;
      // The entry holds what will be sent from here, as it does for every
      // other queued row.
      m.sendPayload = undefined;
    });
    persistRoomQueue(token);
    // This attempt reached the wire, so an echo for it may be on its way.
    if (reachedWire) parkedAfterPost.add(userCid);
    if (settleStatus && get(activeRoomId) === roomId) status.set('idle');
    // The rest of the queue is deliberately *not* held. A hold says the turn
    // these messages were written against ended badly; nothing ended here, and
    // the entries behind this one are waiting on the same connection it is.
    //
    // The composer has been holding this text as a draft against an ack that
    // is not coming, and the queue is the durable copy now — the same claim
    // `settleSend` makes, which is why it is the same signal. A drained entry
    // never had a draft outstanding, and `settleDraft` no-ops there.
    sendSettled.update((s) => ({ n: s.n + 1, token }));
    return true;
  }

  /**
   * Attribute a send failure to the message that failed.
   *
   * The assistant placeholder is *removed* rather than repurposed as the error
   * surface. Writing "Failed to send" into it is what made a send failure read
   * as "the reply failed" — the misattribution ISSUE-200 is about. The turn
   * produced no assistant message, so it has no assistant row.
   */
  function failSend(
    userCid: number,
    phCid: number,
    reason: string,
    retryable: boolean,
    roomId: number,
    {
      settleStatus = true,
      failure,
    }: {
      // False for a `!command` that failed alongside a running turn: the
      // status is that turn's, and reporting the room idle would hide its Stop
      // and unlock the composer while it is still streaming.
      settleStatus?: boolean;
      // How the send failed, where the caller could classify it. A gap is not
      // a failure the user has to act on, so it parks below instead
      // (ISSUE-202); absent, everything here behaves as it did.
      failure?: SendFailure;
    } = {},
  ) {
    // A gap discovered by trying: the server was never reached, so nothing has
    // been decided about this message and reporting it as failed would be a
    // verdict nobody gave. It goes back to the queue instead — where, if it
    // drained from there, it still is.
    if (
      (failure === 'unreachable' || failure === 'timeout') &&
      parkSend(userCid, phCid, roomId, settleStatus)
    ) {
      return;
    }
    messages.update((arr) => arr.filter((m) => m.cid !== phCid));
    updateMsg(userCid, (m) => {
      m.sendState = 'failed';
      m.sendError = reason;
      m.retryable = retryable;
      m.showSending = undefined;
    });
    // A drained entry is resolved by its POST, and this is one of the three
    // resolutions (see `drainSendQueue`). The server refused this message, so
    // re-POSTing it unchanged is not the recovery — the failed row's own Retry
    // is. Ahead of the hold below, or the entry that has just failed would be
    // marked held and kept alongside the ones that are genuinely waiting.
    dropQueuedEntry(userCid);
    // Only when this turn's room is still the one on screen. Switching rooms
    // isn't gated on `busy`, so a send failing after the switch would report
    // 'idle' about a room that may have a task streaming in it — unlocking the
    // composer, hiding Stop, and putting the next send into the backend's
    // per-channel gate. The row updates above no-op on their own (the failed
    // row left with its room).
    if (settleStatus && get(activeRoomId) === roomId) status.set('idle');
    // Whatever was typed behind this send was written on the assumption that
    // it would go out; it did not, so the rest of the queue holds. Not guarded
    // by the *active* room — the queue belongs to `roomId` whether or not that
    // room is the one on screen.
    //
    // `settleStatus` is what separates the two callers, and it is exactly the
    // right line: it is false only for a `!command` failing alongside a
    // running turn, whose failure says nothing about the turn the queued
    // messages were written against. Holding there would mark every queued
    // message on a failed `!status` and then strand them, since the turn's own
    // `done` no longer drains a held queue.
    if (settleStatus) holdRoomQueue(roomTokenOf(roomId));
  }

  /**
   * Take the message back off the transcript and hand it to the composer.
   *
   * Only for `reply_target_gone`. Everything else leaves its failed row on
   * screen, because the row is the recovery path there; here it cannot be, so
   * leaving it would strand an un-retryable bubble in the transcript.
   */
  function returnSend(
    userCid: number,
    phCid: number,
    roomId: number,
    text: string,
    attachments: ChatAttachment[],
  ) {
    messages.update((arr) => arr.filter((m) => m.cid !== userCid && m.cid !== phCid));
    // The third resolution of a drained entry (see `drainSendQueue`). The row
    // is gone and the text is going back to the composer, so an entry left
    // behind would be a queued message with nothing on screen — and would come
    // back after a reload as a second copy of what the user is now editing.
    dropQueuedEntry(userCid);
    const token = get(rooms).find((r) => r.id === roomId)?.token ?? null;
    // The room travels with the counter for the same reason it does on
    // `sendSettled`: two sends can be open at once, and the composer must not
    // repopulate a room the text was not typed in.
    sendReturned.update((s) => ({ n: s.n + 1, token, text, attachments }));
    notifyWarning('That message is no longer available to reply to.');
    if (get(activeRoomId) === roomId) status.set('idle');
    // `failSend`'s sibling path, and it holds for the same reason: this send
    // did not go out, and the composer now has its text back.
    holdRoomQueue(token);
  }

  /**
   * The backend has the message: drop the send lifecycle off the row entirely.
   *
   * Absence is the settled state — the same state every row rebuilt from
   * history is in — so a delivered row is indistinguishable from a reloaded
   * one, and nothing downstream has to learn a third value.
   */
  /** Clear the send marks off a user row. The row half of `settleSend`. */
  function settleSendRow(userCid: number) {
    updateMsg(userCid, (m) => {
      m.sendState = undefined;
      m.showSending = undefined;
      m.sendError = undefined;
      m.retryable = undefined;
      m.sendPayload = undefined;
    });
  }

  function settleSend(userCid: number, roomId: number) {
    settleSendRow(userCid);
    // The ack is what takes a drained entry out of the queue (see
    // `drainSendQueue`). Here rather than back in the drain, and *before* the
    // inline-command branch below re-enters `drainSendQueue`: a `!command`
    // answers inside the request, so that branch runs while this call is still
    // on the stack, and an entry still at the head then would be drained a
    // second time.
    dropQueuedEntry(userCid);
    // The composer has been holding this message as a draft since it was
    // submitted — the stored draft is the only copy that survives a reload, so
    // it is dropped on the ack rather than on submit. This is the ack, and it
    // names the room so a second send open at the same time keeps its own.
    //
    // Only a *drafted* send may signal here. The room is the whole identity
    // the composer has to match an ack against, and since ISSUE-300 two sends
    // can be open in one room — a `!command` sent while an ordinary message is
    // still pre-ack. The command is not a draft and never displaced one, so
    // signalling on its ack would drop the other send's stored copy, which for
    // a send that then fails is the only copy there is. `sendInlineCommand`
    // therefore settles its row through `settleSendRow` and stops there.
    const token = get(rooms).find((r) => r.id === roomId)?.token ?? null;
    sendSettled.update((s) => ({ n: s.n + 1, token }));
  }

  /** The user-facing sentence for each way a send can fail. */
  function sendFailureReason(res: SendResult): { reason: string; retryable: boolean } {
    switch (res.failure) {
      case 'rate_limit':
        return {
          reason: `Rate limit reached — wait ${res.retry_after ?? 60}s and try again.`,
          retryable: true,
        };
      case 'unreachable':
        return { reason: 'Couldn’t send — the server is unreachable.', retryable: true };
      case 'timeout':
        return { reason: 'Couldn’t send — the server didn’t respond.', retryable: true };
      case 'auth':
        // No retry: re-POSTing with a dead session fails identically.
        return { reason: 'Your session expired. Reload to sign in again.', retryable: false };
      default:
        return {
          reason: res.error
            ? `Couldn’t send — ${res.error}.`
            : `Couldn’t send — the server returned ${res.status}.`,
          // A 4xx is a verdict on this request, so re-POSTing it unchanged
          // fails the same way — an archived room (409), a message over the
          // length cap (400), a body nginx refused (413). Offering Retry there
          // is the same lie the `auth` case is carved out to avoid. The two
          // 4xx that mean "later" keep it, matching the server's own split
          // (`PERMANENT_STATUS_CODES` in brain/claude_code.py).
          retryable: !(res.status >= 400 && res.status < 500) || TRANSIENT_4XX.has(res.status),
        };
    }
  }

  async function sendTurn(
    roomId: number,
    trimmed: string,
    attachments: ChatAttachment[],
    userCid: number,
    phCid: number,
    idempotencyKey?: string,
    replyToMsgId?: number,
  ) {
    // Only the chips the server can resolve, and the two lists stay positional
    // against each other. A chip with no path is one whose bytes are still in
    // this browser — `beginSend` uploads those and replaces them before the
    // POST, so reaching here with one means that step did not run, and sending
    // a null path would ask the server to read a file that does not exist.
    const paths: string[] = [];
    const names: string[] = [];
    for (const a of attachments) {
      if (typeof a.path !== 'string') continue;
      paths.push(a.path);
      names.push(a.name);
    }
    const res = await sendChatMessage(roomId, trimmed, paths, names, undefined, idempotencyKey, {
      replyToMsgId,
    });
    if (!res.ok) {
      // The one failure whose recovery is not Retry: the server rejected the
      // *citation*, so re-POSTing the same dead parent id fails identically.
      // The row is removed and the text handed back to the composer instead,
      // which contradicts the ISSUE-200 rule that a failed send never
      // repopulates the box — narrowly, and it earns it: this is a synchronous
      // pre-flight refusal, no time has passed in which anything else could
      // have been typed, and the alternative is a permanently un-retryable row.
      if (res.failure === 'reply_target_gone') {
        returnSend(userCid, phCid, roomId, trimmed, attachments);
        return;
      }
      const { reason, retryable } = sendFailureReason(res);
      // A `!command` runs inside the request rather than becoming a task, so
      // it returns before the endpoint ever consults the idempotency key — and
      // a timeout cannot distinguish "never arrived" from "ran, answer lost".
      // `!steer` appends a note per call and `!retry` creates a task per call,
      // so Retry is withheld for every command rather than guessing which are
      // safe to repeat. Same rule as the permanent 4xx above: an affordance
      // that would do the wrong thing is worse than none.
      const isCommand = trimmed.startsWith('!');
      // The classification travels so a gap can park the message rather than
      // fail it (ISSUE-202). Withheld for a command on exactly the reasoning
      // above, and it is the stronger case here: parking re-sends it later
      // without asking, which for `!steer` is a second note and for `!retry`
      // a second task — and by then the turn it named is over anyway.
      failSend(userCid, phCid, reason, retryable && !isCommand, roomId, {
        failure: isCommand ? undefined : res.failure,
      });
      return;
    }
    // The backend acked, so the send itself is settled either way below. The
    // pending mark clearing is the ack's visible form; there is no receipt to
    // leave behind.
    settleSend(userCid, roomId);
    // Hand the turn over to its assistant row. Deferred to here rather than
    // appended before the POST so the transcript never carries two progress
    // indicators for one message — see `runTurn`.
    //
    // Guarded on the room, because this now runs after an await: `messages` is
    // rebuilt per room, so an unguarded append would drop this turn's spinner
    // into whichever transcript is on screen. The updates below then no-op on
    // their own (`updateMsg` is a no-op on an absent cid, and `enqueueStream`
    // streams into nothing) — the same already-tolerated state a room switch
    // produced before, when the switch wiped the placeholder out from under a
    // send in flight.
    let assistantCid = phCid;
    let streamAlreadyBound = false;
    if (get(activeRoomId) === roomId) {
      const recovered =
        res.task_id == null
          ? undefined
          : get(messages).find((m) => m.role === 'assistant' && m.taskId === res.task_id);
      if (recovered) {
        assistantCid = recovered.cid;
        streamAlreadyBound = true;
        updateMsg(recovered.cid, (m) => {
          if (!m.progress) m.progress = randomAckVerb();
        });
      } else {
        // Above the client-only block. This runs on the ack, so a message
        // typed while the POST was still open has already been queued and its
        // row is at the tail — a plain push would put this turn's answer under
        // the message that is waiting on this very turn (ISSUE-351).
        messages.update((a) =>
          appendAboveClientOnly(a, {
            cid: phCid,
            role: 'assistant',
            text: '',
            segments: [],
            streaming: true,
            progress: randomAckVerb(),
            createdAt: new Date().toISOString(),
          }),
        );
      }
    }
    if (res.task_id == null) {
      applyInlineResult(userCid, phCid, res);
      // Room-guarded for the reason `failSend` is: switching rooms isn't gated
      // on `busy`, so a command settling after a switch would report 'idle'
      // about a room that may have a task streaming in it — unlocking the
      // composer and hiding its Stop. The append above already guards; this
      // line did not, one line away from it.
      if (get(activeRoomId) === roomId) {
        status.set('idle');
        // This turn produced no task, so no stream will settle and
        // `onStreamSettled` will never run for it. Without this the entry that
        // just drained is the last one that ever does: the endpoint answers
        // every `!word` inline, and `send()` queues any it cannot find in the
        // catalogue (a typo, an unlisted alias, anything typed before the
        // catalogue lands), so a queued body can land here.
        void drainSendQueue(roomId);
      }
      return;
    }
    // Stamp the task id on BOTH halves of the turn. The assistant placeholder
    // needs it to bind its stream; the user bubble needs it so the room stream
    // recognises its own echo — the canonical `messages` user row arrives with
    // this task_id, and (role, task_id) is what dedups it away.
    updateMsg(userCid, (m) => {
      m.taskId = res.task_id!;
    });
    updateMsg(assistantCid, (m) => {
      m.taskId = res.task_id!;
      if (!streamAlreadyBound) m.status = 'pending';
    });
    // A Stop tapped while this POST was in flight (see `cancel`) has an id to
    // act on now. Cancel first, then stream anyway: the stream is what renders
    // the cancellation as the turn's terminal state.
    if (cancelRequested) {
      cancelRequested = false;
      try {
        await cancelChatTask(res.task_id);
      } catch {
        /* ignore */
      }
    }
    // Stream now if the room is free, otherwise queue behind the in-flight
    // task. The backend gate keeps this task pending until its turn either way.
    if (!streamAlreadyBound) enqueueStream(res.task_id, assistantCid);
  }

  /**
   * Turn the assistant placeholder into the answer a `!command` came back with.
   *
   * No task, no stream: the command ran inside the request, so this row is the
   * whole of the reply. Shared by the ordinary send path and the mid-turn one,
   * which differ only in who owns `status` afterwards.
   */
  function applyInlineResult(userCid: number, phCid: number, res: SendResult) {
    const cd = res.command_data as
      SearchResultsData | ConfirmationAnsweredData | SteerRecordedData | null | undefined;
    updateMsg(phCid, (m) => {
      m.role = 'system';
      m.text = res.inline_result || '';
      // A structured search_results payload renders as result cards; any
      // other kind (or absent data) falls back to the markdown text.
      if (cd && cd.kind === 'search_results') m.searchResults = cd;
      m.progress = undefined;
      m.streaming = false;
    });
    if (cd && cd.kind === 'confirmation_answered') {
      // Unlike every other inline result, this one is *durable*: the server
      // wrote the answer and the ack into `messages`, so both echo back over
      // the room stream. Stamp their ids onto the two rows already on screen
      // — `appendStreamedRow` drops a frame whose `msg_id` is present — or
      // the exchange renders twice. This is also what makes the rows
      // starrable and deletable without a reload.
      const answered = cd as ConfirmationAnsweredData;
      if (typeof answered.user_msg_id === 'number') {
        updateMsg(userCid, (m) => {
          m.msgId = answered.user_msg_id!;
        });
      }
      if (typeof answered.system_msg_id === 'number') {
        updateMsg(phCid, (m) => {
          m.msgId = answered.system_msg_id!;
        });
      }
      // The bell holds the same question, and `confirmations.apply_answer`
      // has just closed its row. Nothing is refreshed from here: the count
      // is the notification store's, which the root layout polls on every
      // route, and on this one the room stream's `notifications` frame
      // carries it on the next room-check tick. Reaching into that store
      // from the chat session would make the chat route the one place the
      // badge is maintained by hand.
    }
    if (cd && cd.kind === 'steer_recorded') {
      // Durable in the same way and stamped for the same reason: `cmd_steer`
      // records the note as a `task_id IS NULL` user row, which echoes back
      // over the room stream with `msg_id` as the only dedup key available —
      // unstamped, the steer appears twice. The body is adopted along with the
      // id because the two rows differ: this one was drawn from the whole
      // `!steer <note>` line, while what is stored, and what a reload shows,
      // is the note alone.
      const steered = cd as SteerRecordedData;
      if (typeof steered.user_msg_id === 'number') {
        updateMsg(userCid, (m) => {
          m.msgId = steered.user_msg_id!;
          m.text = steered.body;
        });
      }
    }
  }

  /**
   * Send a `!command` while a turn is already running (ISSUE-300).
   *
   * Deliberately not `runTurn`. That entry point announces a turn — it sets
   * `status`, clears the pending-cancel flag and claims the single
   * `pendingSend` echo slot — and all three of those belong to the turn that
   * is streaming. A command's answer arrives in its own response rather than
   * over a stream, so it needs none of them, and this path owns nothing beyond
   * its own two rows and the draft slot it is careful not to touch.
   *
   * The caller has already established that the room is busy, that `trimmed`
   * names a registered command and that there are no attachments.
   */
  async function sendInlineCommand(roomId: number, trimmed: string) {
    const userCid = nextCid();
    const phCid = nextCid();
    const roomToken = get(rooms).find((r) => r.id === roomId)?.token;
    // Above the client-only block if there is one — `send()` routes here on a
    // busy room, which says nothing about the queue, so the ordinary case is an
    // empty block and a plain tail push. Where something *is* queued it was
    // typed earlier and the command still sorts above it, deliberately: a
    // queued row is a pending action, not an event in the history (ISSUE-351).
    messages.update((a) =>
      appendAboveClientOnly(a, {
        cid: userCid,
        role: 'user',
        text: trimmed,
        segments: [],
        streaming: false,
        roomToken,
        attachments: [],
        attachmentPaths: [],
        createdAt: new Date().toISOString(),
        sendState: 'sending',
        // No `sendPayload`, which is what makes the row un-retryable below
        // even if something else were to offer it one.
      }),
    );
    // The same grace-gated pending mark `runTurn` opens, and needed more here:
    // a command runs *inside* the request, so the POST stays open for its whole
    // duration (`!search` over a memory corpus is seconds of it), and the one
    // spinner on screen belongs to the turn underneath. Without this the command
    // is silent for as long as it takes and reads as having been swallowed.
    const graceTimer = setTimeout(() => {
      updateMsg(userCid, (m) => {
        if (m.sendState === 'sending') m.showSending = true;
      });
    }, SEND_PENDING_GRACE_MS);
    try {
      const res = await sendChatMessage(
        roomId,
        trimmed,
        [],
        [],
        undefined,
        newIdempotencyKey(),
        {},
      );
      if (!res.ok) {
        const { reason } = sendFailureReason(res);
        // Never retryable, for the reason `sendTurn` gives: a command runs
        // before the endpoint consults the idempotency key, so a repeat is a
        // second execution rather than a resend.
        failSend(userCid, phCid, reason, false, roomId, { settleStatus: false });
        return;
      }
      // The row half only. A command was never held as a draft, so signalling
      // the composer here would settle the *other* send's — see `settleSend`.
      settleSendRow(userCid);
      // Guarded on the room for the same reason `sendTurn` guards its own
      // append: this runs after an await, and `messages` is rebuilt per room.
      if (get(activeRoomId) !== roomId) return;
      if (res.task_id != null) {
        // Not expected to be reachable: `dispatch` answers every `!word` inline,
        // registered or not, so a `!`-prefixed body cannot come back with a task
        // id — the one that could, the `!model` prefix, is refused by the
        // catalogue gate because no command is registered under that name. So
        // this deliberately does nothing rather than guessing: the turn the
        // server made will arrive over the room stream, where an unsettled user
        // row is handed to `pickUpStreamedTask` and queued behind the running
        // one. Claiming it here instead would take the active stream slot off
        // that turn, since `enqueueStream` only queues while a stream is live
        // and the running turn has none of its own until its own ack lands.
        return;
      }
      messages.update((a) =>
        appendAboveClientOnly(a, {
          cid: phCid,
          role: 'assistant',
          text: '',
          segments: [],
          streaming: true,
          progress: randomAckVerb(),
          createdAt: new Date().toISOString(),
        }),
      );
      applyInlineResult(userCid, phCid, res);
      // No `status` write: the running turn still owns it.
    } catch {
      // `sendChatMessage` classifies rather than throwing, so this is the
      // unforeseen case — and it must not leave the row on 'sending' forever.
      if (get(messages).find((m) => m.cid === userCid)?.sendState === 'sending') {
        failSend(userCid, phCid, 'Couldn’t send — something went wrong.', false, roomId, {
          settleStatus: false,
        });
      }
    } finally {
      clearTimeout(graceTimer);
      updateMsg(userCid, (m) => {
        m.showSending = undefined;
      });
      // A command's row is 'sending' for the life of its request, which
      // `canDrain` reads as a busy room — correctly, since a drain would put a
      // second send in flight beside it. But that means a turn settling `done`
      // *during* the command loses its drain: the trigger has fired and the
      // conditions were false. Re-test them here rather than adding a policy;
      // without it the queue waits for the next room switch.
      void drainSendQueue(roomId);
    }
  }

  async function cancel() {
    const taskId = get(activeTaskId);
    if (taskId == null) {
      // The turn is between the POST and its response, so there is no id to
      // cancel yet — but the composer has been showing Stop since `send` set
      // 'sending', and on a slow connection that window is long enough to tap
      // in. Latch the intent for `sendTurn` to apply against the real id rather
      // than dropping it silently. Gated on an in-flight send so a stray cancel
      // can never arm a later turn.
      if (get(status) === 'sending') cancelRequested = true;
      return;
    }
    try {
      await cancelChatTask(taskId);
    } catch {
      /* ignore */
    }
  }

  async function confirm(cid: number, taskId: number) {
    await confirmChatTask(taskId);
    updateMsg(cid, (m) => {
      m.confirmation = false;
      m.status = 'pending';
      // Drop the confirmation prompt's segments so the resumed stream
      // rebuilds the answer fresh (the prompt was a question, not the answer).
      m.segments = [];
      m.text = '';
      m.streaming = true;
      m.error = false;
    });
    // The confirmed task resumes ahead of anything queued behind it. The
    // stream paused (so no stream is active); enqueueStream starts it now.
    enqueueStream(taskId, cid);
  }

  async function reject(cid: number, taskId: number) {
    try {
      await cancelChatTask(taskId);
    } catch {
      /* ignore */
    }
    updateMsg(cid, (m) => {
      m.confirmation = false;
      m.status = 'cancelled';
      m.streaming = false;
      // Strike the declined prompt (the trailing text segment), or leave a
      // bare notice when there was none.
      const last = m.segments[m.segments.length - 1];
      if (last && last.kind === 'text' && last.text) last.text = `~~${last.text}~~`;
      else m.segments.push({ kind: 'text', id: 'declined', text: '_(declined)_', settled: false });
      m.text =
        m.segments[m.segments.length - 1].kind === 'text'
          ? (m.segments[m.segments.length - 1] as Extract<Segment, { kind: 'text' }>).text
          : '';
    });
    // The parked confirmation was holding the stream queue; release it so the
    // next queued *task* (if any) starts. `cancelled` rather than `done`
    // because that is what this is: the send queue stays held, so a message
    // typed behind a turn the user has just declined does not go out on its
    // own.
    onStreamSettled(false, 'cancelled');
  }

  // Stop the active SSE / poll loop without cancelling the task. The route
  // calls this on unmount so navigating away from /chat doesn't leave an
  // EventSource (or poll timer) running; remounting re-subscribes from the
  // persisted task_events via loadHistory, so no progress is lost.
  function teardown() {
    // Invalidate any `init()` still in flight, so a navigation away mid-load
    // can't install a stream / timer / listener behind us.
    initGeneration += 1;
    stopConnectivityWatch();
    // Nothing in the next session can be the same send, and a membership left
    // here would let a body-matched echo claim a row that session re-minted.
    parkedAfterPost.clear();
    // Write out what the debounce is still holding, rather than dropping it:
    // a navigation away is exactly the moment the last two seconds of frames
    // are worth keeping, and leaving the timers armed would fire them into a
    // torn-down session.
    flushCachedRooms();
    stopActive();
    stopRoomStream();
    stopRoomsRefresh();
    // Drop the cached command/alias catalogue so a fresh session refetches it.
    resetCommandCatalogue();
    removeVisibilityListener();
  }

  return {
    rooms,
    activeRoomId,
    messages,
    status,
    activeTaskId,
    loaded,
    view,
    selectView,
    toggleStar,
    deleteMessage,
    markAllRead,
    hasMore,
    loadingOlder,
    loadOlder,
    offlineTranscript,
    queuedCounts: { subscribe: queuedCounts.subscribe },
    init,
    selectRoom,
    selectRoomByToken,
    newRoom,
    renameRoom,
    updateRoomSettings,
    jumpToTask,
    jumpToMsgId,
    scrollToCid,
    scrollTarget,
    promoteRoom,
    archiveRoom,
    deleteRoom,
    sendSettled,
    sendReturned,
    send,
    retrySend,
    removeQueued,
    editQueued,
    releaseQueued,
    cancel,
    confirm,
    reject,
    outboundDrafts,
    refreshDrafts,
    applyDraftsSnapshot,
    answerDraft,
    editDraft,
    externalTurnDisplay,
    teardown,
  };
}

let _session: ChatSession | null = null;

export function getChatSession(): ChatSession {
  if (!_session) _session = createSession();
  return _session;
}

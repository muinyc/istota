/**
 * Live room-event stream — store behaviour (live-web-chat-room-stream spec,
 * stages 3–5).
 *
 * The session opens one user-scoped SSE connection carrying every room the
 * user is a member of. These tests drive it through the polling fallback (no
 * EventSource in jsdom, which is the same degradation path a buffering proxy
 * produces in production) and assert routing, dedup, the fast-turn fix, the
 * gap/recovery threshold, background badges, and `room` frames.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatRoom, ChatHistory } from '$lib/api';

const api = vi.hoisted(() => ({
  getChatConfig: vi.fn(),
  getChatRooms: vi.fn(),
  getRoomMessages: vi.fn(),
  getChatMessagesView: vi.fn(),
  getRoomEvents: vi.fn(),
  chatRoomStreamUrl: vi.fn(() => '/stream'),
  setChatMessageStarred: vi.fn(),
  markAllRoomsRead: vi.fn(),
  markRoomRead: vi.fn(),
  getTaskEvents: vi.fn(),
  sendChatMessage: vi.fn(),
  createChatRoom: vi.fn(),
  updateChatRoom: vi.fn(),
  deleteChatRoom: vi.fn(),
  promoteChatRoom: vi.fn(),
  cancelChatTask: vi.fn(),
  confirmChatTask: vi.fn(),
  chatStreamUrl: vi.fn(() => '/task-stream'),
  getNotificationCounts: vi.fn(),
  ChatRoomBusyError: class extends Error {},
}));

vi.mock('$lib/api', () => api);
vi.mock('$lib/stores/persisted', () => ({
  loadSetting: vi.fn(() => null),
  saveSetting: vi.fn(),
}));

// Partial rather than wholesale: the store reads `loadCommandNames` and
// `resetCommandCatalogue` from this module on its own boot and teardown paths.
const dropRoomCatalogue = vi.hoisted(() => vi.fn());
vi.mock('$lib/components/chat/autocomplete/providers', async (importOriginal) => ({
  ...((await importOriginal()) as object),
  dropRoomCatalogue,
}));

function room(id: number, unread = 0, name = `Room ${id}`, last_activity?: string): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: unread,
    last_activity,
  };
}

type Row = ChatHistory['messages'][number] & { room_token: string };

function row(msgId: number, token: string, over: Partial<Row> = {}): Row {
  return {
    role: 'assistant',
    text: `msg ${msgId}`,
    created_at: '2026-07-26T10:00:00Z',
    msg_id: msgId,
    starred: false,
    room_token: token,
    room_name: 'Room',
    ...over,
  } as Row;
}

/** Queue one poll response; everything after it reports "nothing new". */
function queueEvents(events: Row[], cursor: number) {
  api.getRoomEvents.mockResolvedValueOnce({ events, cursor, gap: false });
  api.getRoomEvents.mockResolvedValue({ events: [], cursor, gap: false });
}

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

/** A minimal EventSource stand-in so the SSE branch (named `message` / `gap` /
 * `room` listeners) can be exercised — jsdom has none, and without one every
 * test would only ever cover the polling fallback. */
function installFakeEventSource(): {
  current: FakeEventSource | null;
  instances: FakeEventSource[];
  opened: number;
} {
  const ref: {
    current: FakeEventSource | null;
    instances: FakeEventSource[];
    opened: number;
  } = {
    current: null,
    instances: [],
    opened: 0,
  };
  class FakeEventSource {
    url: string;
    listeners = new Map<string, ((e: any) => void)[]>();
    onerror: (() => void) | null = null;
    onopen: (() => void) | null = null;
    closed = false;
    // 0 = CONNECTING (the browser is retrying on its own), 1 = OPEN,
    // 2 = CLOSED. Left undefined by default so the pre-existing tests keep
    // exercising the "fatal error → poll" branch.
    readyState: number | undefined = undefined;
    constructor(url: string) {
      this.url = url;
      ref.current = this as unknown as FakeEventSource;
      ref.instances.push(this as unknown as FakeEventSource);
      ref.opened += 1;
    }
    addEventListener(kind: string, fn: (e: any) => void) {
      const cur = this.listeners.get(kind) ?? [];
      cur.push(fn);
      this.listeners.set(kind, cur);
    }
    close() {
      this.closed = true;
    }
    emit(kind: string, payload: unknown, lastEventId = '') {
      for (const fn of this.listeners.get(kind) ?? []) {
        fn({ data: JSON.stringify(payload), lastEventId });
      }
    }
    fail() {
      this.onerror?.();
    }
  }
  (globalThis as any).EventSource = FakeEventSource;
  return ref;
}
type FakeEventSource = {
  url: string;
  emit: (kind: string, payload: unknown, lastEventId?: string) => void;
  fail: () => void;
  onerror: (() => void) | null;
  onopen: (() => void) | null;
  closed: boolean;
  readyState: number | undefined;
};

const emptyHistory = { messages: [], active_task: null, active_tasks: [] };

/** Drive the visibilitychange listener through a hidden period of `ms`. */
async function hideFor(ms: number) {
  const set = (v: string) =>
    Object.defineProperty(document, 'visibilityState', { value: v, configurable: true });
  set('hidden');
  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(ms);
  set('visible');
  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(0);
}

describe('chat store — live room stream', () => {
  beforeEach(() => {
    Object.values(api).forEach((v) => {
      if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
    });
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500 });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.chatRoomStreamUrl.mockReturnValue('/stream');
    api.chatStreamUrl.mockReturnValue('/task-stream');
    api.getTaskEvents.mockResolvedValue({ events: [] });
    // No EventSource in jsdom → startRoomStream falls through to polling,
    // which is the branch these tests drive.
    (globalThis as any).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('seeds the cursor from the server before connecting', async () => {
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 41, gap: false });
    const s = await freshSession();
    await s.init();
    // limit=1 → a cursor, not the backlog the session just rendered.
    expect(api.getRoomEvents).toHaveBeenCalledWith(0, 1);
    s.teardown();
  });

  it('seeds the cursor before reading history, not after', async () => {
    // Capture-before-reload: a row committed between the two reads must be
    // re-delivered by the stream and dropped by the msg_id dedup. Seeding
    // afterwards puts it below the cursor AND outside the rendered page — and
    // the markRoomRead that follows consumes it, so it isn't even unread.
    const order: string[] = [];
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockImplementation(async () => {
      order.push('cursor');
      return { events: [], cursor: 41, gap: false };
    });
    api.getRoomMessages.mockImplementation(async () => {
      order.push('history');
      return emptyHistory;
    });
    const s = await freshSession();
    await s.init();
    expect(order.slice(0, 2)).toEqual(['cursor', 'history']);
    s.teardown();
  });

  it('abandons an init that was torn down mid-load', async () => {
    // onMount does not await init() and onDestroy tears down regardless, so
    // without a generation guard the rest of init runs on a page the user has
    // left — installing a stream, a 30s timer and a visibility listener, one
    // more of each per remount (only the newest listener is ever removed).
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    let release: (v: any) => void = () => {};
    api.getRoomMessages.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const s = await freshSession();
    const loading = s.init();
    await vi.advanceTimersByTimeAsync(0);
    s.teardown(); // navigate away mid-load
    release(emptyHistory);
    await loading;
    await vi.advanceTimersByTimeAsync(0);
    expect(es.opened).toBe(0);
    const roomsCalls = api.getChatRooms.mock.calls.length;
    await vi.advanceTimersByTimeAsync(35000); // the 30s reconciler never started
    expect(api.getChatRooms.mock.calls.length).toBe(roomsCalls);
    // ...and no listener survived to fire a mark-read from the abandoned page.
    api.markRoomRead.mockClear();
    await hideFor(1000);
    expect(api.markRoomRead).not.toHaveBeenCalled();
  });

  it('appends a message for the active room', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents([row(10, 't1', { text: 'from talk' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).map((m) => m.text)).toContain('from talk');
    s.teardown();
  });

  it('bumps a background room badge instead of appending', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // room 1 active
    queueEvents([row(10, 't2', { text: 'background news' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages)).toHaveLength(0);
    const r2 = get(s.rooms).find((r) => r.id === 2)!;
    expect(r2.unread_count).toBe(2);
    s.teardown();
  });

  it('does not ring a room for the user’s own mirrored turn', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 0)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents([row(10, 't2', { role: 'user', text: 'typed in Talk' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    // Matches count_unread_messages, which excludes role='user'.
    expect(get(s.rooms).find((r) => r.id === 2)!.unread_count).toBe(0);
    s.teardown();
  });

  it('opens a task stream for an in-flight turn from another surface', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'talk prompt', task_id: 77, status: 'running' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const msgs = get(s.messages);
    expect(msgs.map((m) => m.text)).toContain('talk prompt');
    // A placeholder bound to the task, and its stream started.
    expect(msgs.some((m) => m.role === 'assistant' && m.taskId === 77)).toBe(true);
    expect(get(s.activeTaskId)).toBe(77);
    s.teardown();
  });

  it('hydrates an active task from its durable events before another SSE frame arrives', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.getRoomMessages.mockResolvedValue({
      messages: [
        row(10, 't1', { role: 'user', text: 'long task', task_id: 77, status: 'running' }),
        row(11, 't1', { role: 'assistant', text: '', task_id: 77, status: 'running' }),
      ],
      active_task: { id: 77, status: 'running' },
      active_tasks: [{ id: 77, status: 'running' }],
    });
    api.getTaskEvents.mockResolvedValue({
      events: [{ seq: 4, kind: 'progress_text', payload: { text: 'Reading the archive…' } }],
    });

    const s = await freshSession();
    await s.init();
    await Promise.resolve();

    const active = get(s.messages).find((m) => m.role === 'assistant' && m.taskId === 77);
    expect(active?.progress).toBe('Reading the archive…');
    expect(api.getTaskEvents).toHaveBeenCalledWith(77, 0);
    expect(es.opened).toBeGreaterThan(0);
    s.teardown();
  });

  it('rehydrates missed task progress when EventSource reconnects', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.getRoomMessages.mockResolvedValue({
      messages: [
        row(10, 't1', { role: 'user', text: 'long task', task_id: 77, status: 'running' }),
        row(11, 't1', { role: 'assistant', text: '', task_id: 77, status: 'running' }),
      ],
      active_task: { id: 77, status: 'running' },
      active_tasks: [{ id: 77, status: 'running' }],
    });
    api.getTaskEvents.mockResolvedValueOnce({ events: [] }).mockResolvedValueOnce({
      events: [{ seq: 5, kind: 'progress_text', payload: { text: 'Resumed progress' } }],
    });

    const s = await freshSession();
    await s.init();
    await Promise.resolve();
    const taskStream = es.instances.find((instance) => instance.url === '/task-stream');
    expect(taskStream?.onopen).toBeTypeOf('function');
    taskStream!.readyState = 1;
    taskStream?.onopen?.();
    taskStream!.readyState = 0;
    taskStream?.fail();
    taskStream?.onopen?.();
    await Promise.resolve();

    const active = get(s.messages).find((m) => m.role === 'assistant' && m.taskId === 77);
    expect(active?.progress).toBe('Resumed progress');
    expect(taskStream?.closed).toBe(false);
    expect(api.getTaskEvents).toHaveBeenLastCalledWith(77, 0);
    s.teardown();
  });

  it('retries a failed active-task hydration without waiting for an SSE frame', async () => {
    vi.useFakeTimers();
    installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.getRoomMessages.mockResolvedValue({
      messages: [
        row(10, 't1', { role: 'user', text: 'long task', task_id: 77, status: 'running' }),
        row(11, 't1', { role: 'assistant', text: '', task_id: 77, status: 'running' }),
      ],
      active_task: { id: 77, status: 'running' },
      active_tasks: [{ id: 77, status: 'running' }],
    });
    api.getTaskEvents.mockRejectedValueOnce(new Error('temporary')).mockResolvedValueOnce({
      events: [{ seq: 3, kind: 'progress_text', payload: { text: 'Recovered snapshot' } }],
    });

    const s = await freshSession();
    await s.init();
    await vi.advanceTimersByTimeAsync(1500);

    const active = get(s.messages).find((m) => m.role === 'assistant' && m.taskId === 77);
    expect(active?.progress).toBe('Recovered snapshot');
    expect(api.getTaskEvents).toHaveBeenCalledTimes(2);
    s.teardown();
  });

  it('picks up a pending_confirmation turn (the old poller skipped it)', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'do it', task_id: 88, status: 'pending_confirmation' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.activeTaskId)).toBe(88);
    s.teardown();
  });

  it('does not open a task stream for a settled turn', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'old', task_id: 5, status: 'completed' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.activeTaskId)).toBeNull();
    s.teardown();
  });

  it('dedups a row already on screen and stamps its star key', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({
      ...emptyHistory,
      messages: [
        {
          role: 'assistant',
          text: 'already here',
          task_id: 3,
          status: 'completed',
          created_at: '2026-07-26T09:00:00Z',
        },
      ],
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { text: 'already here', task_id: 3, status: 'completed', starred: true })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const msgs = get(s.messages);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].msgId).toBe(10);
    expect(msgs[0].starred).toBe(true);
    // An assistant body belongs to the task stream — never overwritten here.
    expect(msgs[0].text).toBe('already here');
    s.teardown();
  });

  it('dedups the echo of a confirmation answered from the composer', async () => {
    // ISSUE-243's exchange is the one inline result that is also durable: the
    // server writes the answer and the ack into `messages`, so both come back
    // over this stream. Neither carries a task id — they are display-only rows
    // like a `!steer` — so the `msg_id` the send response hands back is the
    // only thing standing between one exchange and two copies of it.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.sendChatMessage.mockResolvedValue({
      ok: true,
      status: 200,
      task_id: null,
      inline_result: 'Confirmed.',
      command_data: { kind: 'confirmation_answered', user_msg_id: 71, system_msg_id: 72 },
    });
    const s = await freshSession();
    await s.init();
    await s.send('yes');
    expect(get(s.messages)).toHaveLength(2);

    queueEvents(
      [
        // No task id on either — they are display-only rows, which is exactly
        // why the msg_id guard is the only one that can fire.
        row(71, 't1', { role: 'user', text: 'yes' }),
        row(72, 't1', { role: 'system', text: 'Confirmed.' }),
      ],
      72,
    );
    await vi.advanceTimersByTimeAsync(2000);

    expect(get(s.messages)).toHaveLength(2);
    s.teardown();
  });

  it('adopts the canonical body when deduping our own user turn', async () => {
    // The server does not always store what was typed: an attachment-only send
    // becomes a descriptor and a `!model …` prefix is stripped. Keeping the raw
    // text would leave web showing something Talk, a reload and the LLM's own
    // context all disagree with.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomMessages.mockResolvedValue({
      ...emptyHistory,
      messages: [
        {
          role: 'user',
          text: '!model opus summarise this',
          task_id: 4,
          created_at: '2026-07-26T09:00:00Z',
        },
      ],
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'summarise this', task_id: 4, status: 'completed' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    const msgs = get(s.messages);
    expect(msgs).toHaveLength(1);
    expect(msgs[0].text).toBe('summarise this');
    expect(msgs[0].msgId).toBe(10);
    s.teardown();
  });

  it('reloads on a gap instead of replaying the backlog', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const historyCalls = api.getRoomMessages.mock.calls.length;
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 900, gap: true });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 900, gap: false });
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(0);
    // Reloaded the open room + the room list rather than patching.
    expect(api.getRoomMessages.mock.calls.length).toBeGreaterThan(historyCalls);
    s.teardown();
  });

  it('does not duplicate a turn when our own echo beats the send response', async () => {
    // The server writes the canonical user row inside the POST — and, with
    // user-scoped OAuth on, before a bounded ~5s Talk mirror — so the frame can
    // arrive while the bubble on screen still has no task_id to dedup against.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    let release: (v: any) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const sending = s.send('hello');
    // The echo lands mid-POST: user row first, then nothing else.
    queueEvents(
      [row(10, 't1', { role: 'user', text: 'hello', task_id: 7, status: 'running' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);
    release({ ok: true, task_id: 7 });
    await sending;
    await vi.advanceTimersByTimeAsync(0);

    const msgs = get(s.messages);
    expect(msgs.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(msgs.filter((m) => m.role === 'assistant' && m.taskId === 7)).toHaveLength(1);
    // The durable id still reached the bubble, so it is starrable without a reload.
    expect(msgs.find((m) => m.role === 'user')!.msgId).toBe(10);
    s.teardown();
  });

  it('does not duplicate a turn when recovery beats the send response', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    let release: (v: any) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const sending = s.send('hello');

    api.getRoomMessages.mockResolvedValue({
      messages: [row(10, 't1', { role: 'user', text: 'hello', task_id: 7, status: 'running' })],
      active_task: { id: 7, status: 'running' },
      active_tasks: [{ id: 7, status: 'running' }],
    });
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 900, gap: true });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 900, gap: false });
    await vi.advanceTimersByTimeAsync(2000);
    await vi.advanceTimersByTimeAsync(0);

    release({ ok: true, task_id: 7 });
    await sending;
    expect(get(s.messages).filter((m) => m.role === 'assistant' && m.taskId === 7)).toHaveLength(1);

    api.getTaskEvents.mockResolvedValue({
      events: [
        { seq: 1, kind: 'result', payload: { text: 'the answer' } },
        { seq: 2, kind: 'done', payload: {} },
      ],
    });
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).filter((m) => m.text === 'the answer')).toHaveLength(1);
    s.teardown();
  });

  it('adopts the echo of a send the client gave up on but the server accepted', async () => {
    // ISSUE-200: the POST is not idempotent and carries no client id, so a
    // timeout (or a socket dropped after the request was processed) leaves the
    // row marked failed while the task really is running. Its echo used to
    // append as a second bubble — the same message shown twice, once reported
    // as unsent and once being answered.
    //
    // Since ISSUE-202 the row is *parked* rather than failed — the outbox will
    // send it again — which is what makes the adoption matter more, not less:
    // without it the queue would also POST a message the server already has.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });
    await s.send('hello');
    expect(get(s.messages)[0].sendState).toBe('queued');

    queueEvents(
      [row(10, 't1', { role: 'user', text: 'hello', task_id: 7, status: 'running' })],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);

    const msgs = get(s.messages);
    expect(msgs.filter((m) => m.role === 'user')).toHaveLength(1);
    const mine = msgs.find((m) => m.role === 'user')!;
    expect(mine.sendState).toBeUndefined();
    expect(mine.sendError).toBeUndefined();
    expect(mine.taskId).toBe(7);
    expect(mine.msgId).toBe(10);
    // And the entry went with it, or the drain would POST it a second time.
    expect(mine.sendState).toBeUndefined();
    s.teardown();
  });

  it('refuses to adopt an echo the server attributed to somebody else', async () => {
    // The adoption claims a row on the body alone, which was safe while a user
    // row carried no writer. It does now: a co-member typing the same words
    // while this client holds an unsent row would have their turn folded into
    // it — and the adoption writes no author, so the row keeps reading as the
    // viewer's own. That used to cost the wrong name; since `author_id` it
    // costs the reader's own face over another member's words.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    api.sendChatMessage.mockResolvedValue({ ok: false, status: 0, failure: 'timeout' });
    await s.send('hello');
    expect(get(s.messages)[0].sendState).toBe('queued');

    queueEvents(
      [
        row(10, 't1', {
          role: 'user',
          text: 'hello',
          task_id: 7,
          status: 'running',
          author: 'Bob',
          author_id: 'bob',
        }),
      ],
      10,
    );
    await vi.advanceTimersByTimeAsync(2000);

    const users = get(s.messages).filter((m) => m.role === 'user');
    // Two bubbles: the viewer's message, still unsent, and Bob's. A duplicate
    // is the outcome the adoption exists to avoid, and it is the right one
    // here — these are two different people's messages.
    expect(users).toHaveLength(2);
    const mine = users.find((m) => m.authorId === undefined)!;
    expect(mine.sendState).toBe('queued');
    expect(mine.taskId).toBeUndefined();
    const theirs = users.find((m) => m.authorId === 'bob')!;
    expect(theirs.taskId).toBe(7);
    s.teardown();
  });

  it('dedups the echo of a retried send into the row it was retried from', async () => {
    // Reusing the failed row's cid is justified only by this: the dedup keys on
    // (role, task_id), so stamping the retry's new task id onto the existing
    // row is what folds the canonical echo into it instead of appending.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    api.sendChatMessage.mockResolvedValue({
      ok: false,
      status: 500,
      failure: 'rejected',
      error: 'boom',
    });
    await s.send('hello');
    const cid = get(s.messages)[0].cid;

    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 55 });
    await s.retrySend(cid);

    queueEvents(
      [row(11, 't1', { role: 'user', text: 'hello', task_id: 55, status: 'running' })],
      11,
    );
    await vi.advanceTimersByTimeAsync(2000);

    const msgs = get(s.messages);
    expect(msgs.filter((m) => m.role === 'user')).toHaveLength(1);
    expect(msgs.find((m) => m.role === 'user')!.cid).toBe(cid);
    expect(msgs.find((m) => m.role === 'user')!.msgId).toBe(11);
    s.teardown();
  });

  it('does not release one turn’s echo buffer from another turn', async () => {
    // The buffer used to be a single module-level slot drained unconditionally
    // on the way into `runTurn`, on the stated grounds that no other turn's
    // could be open — an invariant `selectRoom` defeats, since `stopActive`
    // resets `status` to 'idle' without touching the slot. Room 2's send then
    // released room 1's held echo before room 1's task id existed, and room 2
    // showed two bubbles for one message, both bound to its task.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    let releaseOne: (v: any) => void = () => {};
    let releaseTwo: (v: any) => void = () => {};
    api.sendChatMessage.mockReturnValueOnce(
      new Promise((res) => {
        releaseOne = res;
      }),
    );
    const first = s.send('from room one');

    await s.selectRoom(2);
    api.sendChatMessage.mockReturnValueOnce(
      new Promise((res) => {
        releaseTwo = res;
      }),
    );
    // Deliberately not awaited: room 2's POST has to still be open when room
    // 1's resolves, which is the whole window the buffer covers.
    const second = s.send('from room two');

    queueEvents(
      [row(20, 't2', { role: 'user', text: 'from room two', task_id: 99, status: 'running' })],
      20,
    );
    await vi.advanceTimersByTimeAsync(2000);

    releaseOne({ ok: true, status: 200, task_id: 7 });
    await first;
    await vi.advanceTimersByTimeAsync(0);

    releaseTwo({ ok: true, status: 200, task_id: 99 });
    await second;
    await vi.advanceTimersByTimeAsync(0);

    const users = get(s.messages).filter((m) => m.role === 'user');
    expect(users).toHaveLength(1);
    expect(users[0].taskId).toBe(99);
    s.teardown();
  });

  it('abandons the echo buffer when the room is switched away from', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();

    let release: (v: any) => void = () => {};
    api.sendChatMessage.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const sending = s.send('hello');
    await s.selectRoom(2);

    // Room 1's frame arrives with nothing holding it, so it takes the ordinary
    // background path — the badge — rather than being buffered for a room the
    // user has left and a transcript that has since been rebuilt.
    queueEvents([row(30, 't1')], 30);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.rooms).find((r) => r.id === 1)?.unread_count).toBe(1);

    release({ ok: true, status: 200, task_id: 7 });
    await sending;
    s.teardown();
  });

  it('does not re-count a buffered row the recovery refresh already counted', async () => {
    // recoverStream buffers frames while it reloads, then drains them. Its own
    // refreshRooms returns server-computed counts that already include a row
    // written before that call, so bumping it again would inflate the badge.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 0)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // room 1 active

    // The gap reload hands back room 2 with the server's count of 1 — the very
    // row that arrives as a frame while the reload is in flight.
    let releaseHistory: (v: any) => void = () => {};
    api.getRoomMessages.mockReturnValue(
      new Promise((res) => {
        releaseHistory = res;
      }),
    );
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2, 1)] });
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 900, gap: true });
    api.getRoomEvents.mockResolvedValueOnce({
      events: [row(901, 't2', { text: 'counted once' })],
      cursor: 901,
      gap: false,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 901, gap: false });
    await vi.advanceTimersByTimeAsync(2000); // gap → recovery starts, reload pends
    await vi.advanceTimersByTimeAsync(2000); // the frame lands and is buffered
    releaseHistory(emptyHistory);
    await vi.advanceTimersByTimeAsync(0);

    expect(get(s.rooms).find((r) => r.id === 2)!.unread_count).toBe(1);
    s.teardown();
  });

  it('does not wedge the live path when a recovery reload never settles', async () => {
    // recoverStream buffers every frame while it reloads and releases only in
    // its finally, so an unbounded fetch would swallow frames forever and the
    // `recovering` guard would refuse every future attempt.
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    // A reload that hangs until its abort fires.
    api.getRoomMessages.mockImplementation(
      (_id: number, opts: { timeoutMs?: number } = {}) =>
        new Promise((_res, rej) => {
          setTimeout(() => rej(new Error('aborted')), opts.timeoutMs ?? 1e9);
        }),
    );
    api.getRoomEvents.mockResolvedValueOnce({ events: [], cursor: 900, gap: true });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 900, gap: false });
    await vi.advanceTimersByTimeAsync(2000);
    // Past the recovery bound the state is released...
    await vi.advanceTimersByTimeAsync(20000);
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    queueEvents([row(950, 't1', { text: 'after the hang' })], 950);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).map((m) => m.text)).toContain('after the hang');
    s.teardown();
  });

  it('applies a room rename frame to the sidebar', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1, 0, 'old name')] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.emit('room', {
      action: 'upsert',
      room: { id: 1, token: 't1', name: 'new name', origin: 'web', model: null, effort: null },
    });
    expect(get(s.rooms)[0].name).toBe('new name');
    s.teardown();
  });

  // The brain rides the metadata frame beside model and effort, so a `!brain`
  // typed on Talk — or the settings modal on another device — reaches this
  // client without a room refetch. Both directions in one test: a bare
  // `expect(...).toBe('native')` would pass against a store that adopted the
  // frame wholesale and never cleared anything.
  it('applies a room brain frame, and clears it again', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...room(1), brain: null }] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const frame = (brain: string | null) => ({
      action: 'upsert',
      room: {
        id: 1,
        token: 't1',
        name: 'Room 1',
        origin: 'web',
        model: null,
        effort: null,
        brain,
      },
    });
    es.current!.emit('room', frame('native'));
    expect(get(s.rooms)[0].brain).toBe('native');
    es.current!.emit('room', frame(null));
    expect(get(s.rooms)[0].brain).toBeNull();
    s.teardown();
  });

  // ISSUE-433. `applyRoomFrame` merges field by field rather than spreading, so
  // a field it does not name is erased on the next frame — and a busy room
  // produces one on every rename. Both directions in one test for the reason
  // the brain test above gives: a bare set-and-read passes against a store that
  // adopted the frame wholesale and could never clear anything.
  it('applies a room colour frame, and clears it again', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...room(1), color: null }] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const frame = (color: string | null) => ({
      action: 'upsert',
      room: { id: 1, token: 't1', name: 'Room 1', origin: 'web', color },
    });
    es.current!.emit('room', frame('teal'));
    expect(get(s.rooms)[0].color).toBe('teal');
    es.current!.emit('room', frame(null));
    expect(get(s.rooms)[0].color).toBeNull();
    s.teardown();
  });

  it('does not lose the colour to an unrelated frame', async () => {
    // The failure the field-by-field merge actually produces: the colour is
    // set, then a rename frame arrives naming every field the snapshot sends.
    // It looks like it works locally and wipes itself seconds later.
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...room(1), color: 'rose' }] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    expect(get(s.rooms)[0].color).toBe('rose');
    es.current!.emit('room', {
      action: 'upsert',
      room: { id: 1, token: 't1', name: 'Renamed', origin: 'web', color: 'rose' },
    });
    expect(get(s.rooms)[0].color).toBe('rose');
    expect(get(s.rooms)[0].name).toBe('Renamed');
    s.teardown();
  });

  it('drops the room model catalogue when a frame moves the brain', async () => {
    // A `!brain` typed on Talk, or this user's other device. The room's model
    // aliases were resolved through the brain it had when they were fetched
    // and the picker caches them per session, so the frame is the only notice
    // this client gets that the list is now wrong.
    dropRoomCatalogue.mockClear();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...room(1), brain: null }] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const frame = (extra: Record<string, unknown>) => ({
      action: 'upsert',
      room: { id: 1, token: 't1', name: 'Room 1', origin: 'web', ...extra },
    });
    // The control first: a rename arrives as the same frame, and every turn in
    // a busy room can produce one.
    es.current!.emit('room', frame({ name: 'Renamed', brain: null }));
    expect(dropRoomCatalogue).not.toHaveBeenCalled();
    es.current!.emit('room', frame({ brain: 'native' }));
    expect(dropRoomCatalogue).toHaveBeenCalledWith(1);
    s.teardown();
  });

  // The 30s rooms poll is the reconciler behind the stream, so it carries the
  // brain for the same reason it carries the model: a change made elsewhere
  // must not sit stale until reload if a frame was missed.
  it('carries the brain through the 30s reconciler', async () => {
    vi.useFakeTimers();
    installFakeEventSource();
    api.getChatRooms.mockResolvedValueOnce({ rooms: [{ ...room(1), brain: null }] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    expect(get(s.rooms)[0].brain).toBeNull();
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...room(1), brain: 'native' }] });
    await vi.advanceTimersByTimeAsync(31000);
    expect(get(s.rooms)[0].brain).toBe('native');
    s.teardown();
  });

  it('carries the colour through the 30s reconciler', async () => {
    // The stream's first pass only establishes the baseline, so a colour
    // changed on another device while this tab was disconnected produces no
    // `room` frame on reconnect — this reconciler is the only thing that
    // catches it. Both directions, since the merge spreads the old record and
    // a set-only test would pass against one that never cleared (ISSUE-433).
    vi.useFakeTimers();
    installFakeEventSource();
    api.getChatRooms.mockResolvedValueOnce({ rooms: [{ ...room(1), color: null }] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    expect(get(s.rooms)[0].color).toBeNull();
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...room(1), color: 'teal' }] });
    await vi.advanceTimersByTimeAsync(31000);
    expect(get(s.rooms)[0].color).toBe('teal');
    api.getChatRooms.mockResolvedValue({ rooms: [{ ...room(1), color: null }] });
    await vi.advanceTimersByTimeAsync(31000);
    expect(get(s.rooms)[0].color).toBeNull();
    s.teardown();
  });

  /** A fresh notification store bound to the session just created.
   *
   * `freshSession` resets the module registry, so this has to be imported
   * *after* it or the frame would publish into a different instance of the
   * store than the one under assertion — which is also exactly the bug a
   * caller wiring this up in an app would hit.
   */
  async function startNotifications() {
    api.getNotificationCounts.mockResolvedValue({ open: 0, actionable: 0 });
    const mod = await import('./notifications');
    mod.startNotificationPoll();
    return mod;
  }

  it('publishes a notifications frame into the bell', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    const notifications = await startNotifications();
    await s.init();

    es.current!.emit('notifications', { open: 3, actionable: 2 });

    expect(get(notifications.notificationCounts)).toEqual({ open: 3, actionable: 2 });
    notifications.stopNotificationPoll();
    s.teardown();
  });

  it('ignores a notifications frame once the poll has stopped', async () => {
    // An EventSource outliving a logout by a tick must not put a badge back
    // over a page that has logged out — the same guard `generation` gives a
    // request already on the wire.
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    const notifications = await startNotifications();
    await s.init();
    notifications.stopNotificationPoll();

    es.current!.emit('notifications', { open: 9, actionable: 9 });

    expect(get(notifications.notificationCounts)).toEqual({ open: 0, actionable: 0 });
    s.teardown();
  });

  it('a malformed notifications frame does not kill the stream', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    const notifications = await startNotifications();
    await s.init();

    for (const fn of (es.current as any).listeners.get('notifications') ?? []) {
      fn({ data: 'not json' });
    }
    es.current!.emit('message', row(10, 't1', { text: 'still here' }), '10');

    expect(get(s.messages).some((m) => m.text === 'still here')).toBe(true);
    notifications.stopNotificationPoll();
    s.teardown();
  });

  it('applies a room removal frame and moves off the deleted room', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // room 1 active
    es.current!.emit('room', { action: 'remove', token: 't1', id: 1 });
    expect(get(s.rooms).map((r) => r.id)).toEqual([2]);
    s.teardown();
  });

  it('ignores a redelivered row via the durable-id guard', async () => {
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.emit('message', row(10, 't1', { text: 'once' }), '10');
    es.current!.emit('message', row(10, 't1', { text: 'once' }), '10');
    expect(get(s.messages).filter((m) => m.text === 'once')).toHaveLength(1);
    s.teardown();
  });

  it('keeps SSE through a transient error the browser is already retrying', async () => {
    // Free reconnect is one of the reasons this is SSE and not a WebSocket;
    // closing on the first blip would throw it away and downgrade a
    // session-lived connection to polling for the rest of the day.
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const before = api.getRoomEvents.mock.calls.length;
    es.current!.readyState = 0; // CONNECTING — retry already scheduled
    es.current!.fail();
    await vi.advanceTimersByTimeAsync(5000);
    expect(es.current!.closed).toBe(false);
    expect(api.getRoomEvents.mock.calls.length).toBe(before);
    s.teardown();
  });

  it('concedes to polling after repeated failures, then re-probes SSE', async () => {
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const opened = es.opened;
    es.current!.readyState = 0;
    es.current!.fail();
    es.current!.fail();
    es.current!.fail(); // third consecutive → give up on the connection
    expect(es.current!.closed).toBe(true);
    const polled = api.getRoomEvents.mock.calls.length;
    await vi.advanceTimersByTimeAsync(3000);
    expect(api.getRoomEvents.mock.calls.length).toBeGreaterThan(polled);
    // ...and the poll loop re-probes SSE rather than polling forever.
    await vi.advanceTimersByTimeAsync(61000);
    expect(es.opened).toBeGreaterThan(opened);
    s.teardown();
  });

  it('recovers on reconnect after a long silence, but not on the first open', async () => {
    // The client-side half of the gap threshold: past ROOM_STREAM_STALE_MS a
    // reconnect has probably missed state the stream does not carry, so a
    // reload is more correct than trusting the delta. The first open follows a
    // fresh history load, so it must not recover.
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.onopen!();
    await vi.advanceTimersByTimeAsync(0);
    const afterFirstOpen = api.getRoomMessages.mock.calls.length;

    await vi.advanceTimersByTimeAsync(61000); // silence past the stale window
    es.current!.onopen!(); // reconnected
    await vi.advanceTimersByTimeAsync(0);
    expect(api.getRoomMessages.mock.calls.length).toBeGreaterThan(afterFirstOpen);
    s.teardown();
  });

  it('reloads after a long hidden period when the connection did not hold', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init(); // no EventSource in jsdom → polling, so nothing "held"
    const before = api.getRoomMessages.mock.calls.length;
    await hideFor(61000);
    expect(api.getRoomMessages.mock.calls.length).toBeGreaterThan(before);
    s.teardown();
  });

  it('reconciles metadata only when the connection held across the hidden period', async () => {
    // A stream that stayed open cannot have missed a `messages` row, so tearing
    // down the transcript (and with it a healthy in-flight task stream, which
    // would re-render its answer from seq 0) buys nothing.
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    es.current!.onopen!(); // connection is live
    await vi.advanceTimersByTimeAsync(0);
    const history = api.getRoomMessages.mock.calls.length;
    const roomsCalls = api.getChatRooms.mock.calls.length;
    await hideFor(61000);
    expect(api.getRoomMessages.mock.calls.length).toBe(history);
    expect(api.getChatRooms.mock.calls.length).toBeGreaterThan(roomsCalls);
    s.teardown();
  });

  it('falls back to polling when the stream errors', async () => {
    vi.useFakeTimers();
    const es = installFakeEventSource();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    const before = api.getRoomEvents.mock.calls.length;
    es.current!.fail();
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.getRoomEvents.mock.calls.length).toBeGreaterThan(before);
    s.teardown();
  });

  it('feeds the All view live instead of leaving it a frozen snapshot', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getChatMessagesView.mockResolvedValue({
      messages: [],
      has_more: false,
      oldest_cursor: null,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    await s.selectView('all');
    queueEvents([row(10, 't2', { text: 'aggregate live' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages).map((m) => m.text)).toContain('aggregate live');
    s.teardown();
  });

  it('keeps the user’s own turns out of the Unread view', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getChatMessagesView.mockResolvedValue({
      messages: [],
      has_more: false,
      oldest_cursor: null,
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    await s.selectView('unread');
    queueEvents([row(10, 't2', { role: 'user', text: 'mine' })], 10);
    await vi.advanceTimersByTimeAsync(2000);
    expect(get(s.messages)).toHaveLength(0);
    s.teardown();
  });

  it('stops polling on teardown', async () => {
    vi.useFakeTimers();
    api.getChatRooms.mockResolvedValue({ rooms: [room(1)] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    const s = await freshSession();
    await s.init();
    s.teardown();
    const calls = api.getRoomEvents.mock.calls.length;
    await vi.advanceTimersByTimeAsync(10000);
    expect(api.getRoomEvents.mock.calls.length).toBe(calls);
  });

  // The sidebar renders `$rooms` in store order, so the order the store holds
  // IS the order on screen. The server sends it activity-first; these pin the
  // paths that have to keep it that way once the page is live.
  describe('activity ordering', () => {
    const older = '2026-07-01T00:00:00Z';
    const newer = '2026-07-20T00:00:00Z';

    it('adopts the order the server sent', async () => {
      api.getChatRooms.mockResolvedValue({
        rooms: [room(1, 0, 'Stale', older), room(2, 0, 'Fresh', newer)],
      });
      api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
      const s = await freshSession();
      await s.init();
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Fresh', 'Stale']);
      s.teardown();
    });

    it('lifts a background room to the top when a message lands in it', async () => {
      vi.useFakeTimers();
      api.getChatRooms.mockResolvedValue({
        rooms: [room(1, 0, 'Active', newer), room(2, 0, 'Quiet', older)],
      });
      api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
      const s = await freshSession();
      await s.init();
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Active', 'Quiet']);
      queueEvents([row(10, 't2', { created_at: '2026-08-01T00:00:00Z' })], 10);
      await vi.advanceTimersByTimeAsync(2000);
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Quiet', 'Active']);
      s.teardown();
    });

    it('lifts the room the user is looking at, not only background rooms', async () => {
      // The unread badge is deliberately blind to the active room and to the
      // user's own turns; activity is neither — a turn you just took is the
      // strongest reason for a room to be at the top.
      vi.useFakeTimers();
      api.getChatRooms.mockResolvedValue({
        rooms: [room(1, 0, 'Other', newer), room(2, 0, 'Open', older)],
      });
      api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
      const s = await freshSession();
      await s.init();
      await s.selectRoom(2);
      queueEvents([row(10, 't2', { role: 'user', created_at: '2026-08-01T00:00:00Z' })], 10);
      await vi.advanceTimersByTimeAsync(2000);
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Open', 'Other']);
      s.teardown();
    });

    it('re-sorts on the 30s reconciler for frames the client missed', async () => {
      // A sleeping tab or a dropped stream means the order can drift; the poll
      // used to be pinned "no reorder", which would have stranded it.
      vi.useFakeTimers();
      api.getChatRooms.mockResolvedValueOnce({
        rooms: [room(1, 0, 'Active', newer), room(2, 0, 'Quiet', older)],
      });
      api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
      const s = await freshSession();
      await s.init();
      api.getChatRooms.mockResolvedValue({
        rooms: [room(1, 0, 'Active', newer), room(2, 3, 'Quiet', '2026-08-01T00:00:00Z')],
      });
      await vi.advanceTimersByTimeAsync(31000);
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Quiet', 'Active']);
      s.teardown();
    });

    it('does not let a stale poll response demote a just-active room', async () => {
      // The response is built before it is awaited, so a frame landing in
      // between is ahead of it. Taking the server's stamp unconditionally
      // would drop the room back down until the next poll.
      vi.useFakeTimers();
      api.getChatRooms.mockResolvedValue({
        rooms: [room(1, 0, 'Active', newer), room(2, 0, 'Quiet', older)],
      });
      api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
      const s = await freshSession();
      await s.init();
      queueEvents([row(10, 't2', { created_at: '2026-08-01T00:00:00Z' })], 10);
      await vi.advanceTimersByTimeAsync(2000);
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Quiet', 'Active']);
      await vi.advanceTimersByTimeAsync(31000); // the poll still reports `older`
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Quiet', 'Active']);
      s.teardown();
    });

    it('keeps a renamed room where it was', async () => {
      // The PATCH response carries no `last_activity`; adopting it wholesale
      // would strip the sort key and sink the room.
      api.getChatRooms.mockResolvedValue({
        rooms: [room(1, 0, 'Quiet', older), room(2, 0, 'Active', newer)],
      });
      api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
      const s = await freshSession();
      await s.init();
      const { last_activity: _drop, ...patched } = room(2, 0, 'Renamed', newer);
      api.updateChatRoom.mockResolvedValue(patched);
      await s.renameRoom(2, 'Renamed');
      expect(get(s.rooms).map((r) => r.name)).toEqual(['Renamed', 'Quiet']);
      s.teardown();
    });
  });
});

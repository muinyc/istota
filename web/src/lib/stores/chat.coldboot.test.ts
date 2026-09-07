/**
 * Booting with no connection at all (ISSUE-202, Stage 5).
 *
 * The service worker answers the navigation, so the app runs — but
 * `GET /chat/config` does not, and that is where the id every cache key is
 * namespaced by comes from. Without something standing in for it a cold launch
 * offline boots to an empty cache, which is the one outcome the whole stage
 * exists to prevent. The stand-in is the `chat.lastUserId` pointer, read only
 * inside the shell, with a repaint underneath it for the case where the guess
 * turns out to be wrong.
 *
 * The mocking follows `chat.offline.test.ts`: `api.ts`, `offline/db.ts`,
 * `connectivity` and `persisted` are all stubbed, so what is asserted here is
 * the store's decisions rather than a database's behaviour. `platform/native`
 * is mocked as well, because the pointer's read is gated on the shell and the
 * gate is a user-agent string.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatHistory, ChatRoom } from '$lib/api';
import { LAST_USER_KEY } from '$lib/offline/lastUser';
import { SEND_QUEUE_STORAGE_KEY } from './sendQueue';

const api = vi.hoisted(() => ({
  getChatConfig: vi.fn(),
  getChatRooms: vi.fn(),
  getRoomMessages: vi.fn(),
  getChatMessagesView: vi.fn(),
  getRoomEvents: vi.fn(),
  chatRoomStreamUrl: vi.fn(() => '/stream'),
  chatStreamUrl: vi.fn(() => '/task-stream'),
  markRoomRead: vi.fn(),
  markAllRoomsRead: vi.fn(),
  setChatMessageStarred: vi.fn(),
  deleteChatMessage: vi.fn(),
  getTaskEvents: vi.fn(),
  sendChatMessage: vi.fn(),
  createChatRoom: vi.fn(),
  updateChatRoom: vi.fn(),
  deleteChatRoom: vi.fn(),
  promoteChatRoom: vi.fn(),
  cancelChatTask: vi.fn(),
  confirmChatTask: vi.fn(),
  getNotificationCounts: vi.fn(),
  listOutboundDrafts: vi.fn(),
  uploadChatAttachment: vi.fn(),
  // The double has to carry every class the product does `instanceof`
  // against, or the property read throws inside the branch instead of
  // answering it.
  AuthError: class AuthError extends Error {},
  UploadUnreachableError: class extends Error {},
  ChatRoomBusyError: class extends Error {},
  ChatMessageBusyError: class extends Error {},
}));

const db = vi.hoisted(() => ({
  readTranscript: vi.fn(),
  writeTranscript: vi.fn(),
  appendTranscriptRows: vi.fn(),
  deleteTranscript: vi.fn(),
  removeCachedMessages: vi.fn(),
  readRooms: vi.fn(),
  writeRooms: vi.fn(),
  readConfig: vi.fn(),
  writeConfig: vi.fn(),
  pruneOffline: vi.fn(),
  getBlob: vi.fn(),
  deleteBlob: vi.fn(),
}));

const conn = vi.hoisted(() => {
  let value = true;
  const subscribers = new Set<(v: boolean) => void>();
  return {
    online: {
      subscribe(fn: (v: boolean) => void) {
        subscribers.add(fn);
        fn(value);
        return () => void subscribers.delete(fn);
      },
    },
    setOnline(next: boolean) {
      value = next;
      for (const fn of subscribers) fn(value);
    },
    noteTransport: vi.fn(),
    probe: vi.fn(),
    startConnectivity: vi.fn(() => () => {}),
  };
});

const native = vi.hoisted(() => ({
  isNativeShell: vi.fn(() => true),
  shellVersion: vi.fn(() => '0.10.0'),
  shellAtLeast: vi.fn(() => true),
  onKeyboardGeometry: vi.fn(() => () => {}),
}));

const persisted = vi.hoisted(() => {
  const store = new Map<string, string>();
  return {
    store,
    loadSetting: vi.fn((key: string, fallback: unknown) =>
      store.has(key) ? JSON.parse(store.get(key) as string) : fallback,
    ),
    saveSetting: vi.fn((key: string, value: unknown) => {
      store.set(key, JSON.stringify(value));
    }),
  };
});

const notices = vi.hoisted(() => ({
  notify: vi.fn(),
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));

vi.mock('$lib/api', () => api);
vi.mock('$lib/offline/db', () => db);
vi.mock('$lib/stores/connectivity', () => conn);
vi.mock('$lib/platform/native', () => native);
vi.mock('$lib/stores/persisted', () => persisted);
vi.mock('$lib/stores/notices', () => notices);

function room(id: number, name = `Room ${id}`): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 0,
  } as ChatRoom;
}

type Row = ChatHistory['messages'][number];

function row(msgId: number, text: string): Row {
  return {
    role: 'assistant',
    text,
    created_at: '2026-08-30T10:00:00Z',
    msg_id: msgId,
    starred: false,
    room_token: 't1',
  } as Row;
}

const NO_CONNECTION = () => Promise.reject(new Error('Failed to fetch'));

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

/** A cached transcript for whichever user asks — the wrong-namespace hazard. */
function cacheFor(user: string, text: string) {
  db.readTranscript.mockImplementation(async (userId: string | null) =>
    userId === user
      ? {
          roomId: 1,
          roomToken: 't1',
          messages: [row(1, text)],
          oldestCursor: null,
          savedAt: Date.now(),
        }
      : null,
  );
  db.readRooms.mockImplementation(async (userId: string | null) =>
    userId === user ? [room(1), room(2)] : null,
  );
}

function seedQueue(user: string, token: string, entries: Record<string, unknown>[]) {
  persisted.store.set(
    SEND_QUEUE_STORAGE_KEY,
    JSON.stringify({ [`${user}:room:${token}`]: entries }),
  );
}

const storedEntry = (text: string, over: Record<string, unknown> = {}) => ({
  cid: 1,
  text,
  attachments: [],
  held: false,
  queuedAt: Date.now(),
  reason: 'busy',
  ...over,
});

describe('chat store — a cold launch with no connection', () => {
  beforeEach(() => {
    for (const bag of [api, db]) {
      Object.values(bag).forEach((v) => {
        if (typeof v === 'function' && 'mockReset' in v) (v as { mockReset(): void }).mockReset();
      });
    }
    conn.setOnline(false);
    persisted.store.clear();
    native.isNativeShell.mockReturnValue(true);
    native.shellAtLeast.mockReturnValue(true);
    Object.values(notices).forEach((v) => v.mockReset());
    api.getChatConfig.mockImplementation(NO_CONNECTION);
    api.getChatRooms.mockImplementation(NO_CONNECTION);
    api.getRoomMessages.mockImplementation(NO_CONNECTION);
    api.getRoomEvents.mockImplementation(NO_CONNECTION);
    api.markRoomRead.mockImplementation(NO_CONNECTION);
    api.listOutboundDrafts.mockImplementation(NO_CONNECTION);
    api.getTaskEvents.mockResolvedValue({ events: [] });
    db.readTranscript.mockResolvedValue(null);
    db.readRooms.mockResolvedValue(null);
    db.readConfig.mockResolvedValue(null);
    (globalThis as { EventSource?: unknown }).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('reads the cache by the last user the server named', async () => {
    persisted.store.set(LAST_USER_KEY, JSON.stringify('alice'));
    cacheFor('alice', 'what alice was reading');

    const s = await freshSession();
    await s.init();

    expect(get(s.rooms).map((r) => r.token)).toEqual(['t1', 't2']);
    expect(get(s.messages).map((m) => m.text)).toEqual(['what alice was reading']);
    expect(get(s.offlineTranscript)).toBe(true);
    s.teardown();
  });

  it('boots to nothing without the pointer — which is what it buys', async () => {
    // The control on the test above. Every cache read is namespaced, so with
    // no id there is no key, and this is what a cold launch offline did before
    // the pointer existed.
    cacheFor('alice', 'what alice was reading');

    const s = await freshSession();
    await s.init();

    expect(get(s.rooms)).toEqual([]);
    expect(get(s.messages)).toEqual([]);
    s.teardown();
  });

  it('does not read the pointer in a browser or in an older app', async () => {
    // The gate that makes the pointer safe: off the shell, the per-user
    // namespace is guarding a profile two people take turns using, and a
    // pointer answering there would paint one of them the other's transcript.
    // It is the shell *version* that decides, the same gate the service worker
    // registers behind — without a worker there is no boot with no connection
    // for the guess to be needed on.
    native.shellAtLeast.mockReturnValue(false);
    persisted.store.set(LAST_USER_KEY, JSON.stringify('alice'));
    cacheFor('alice', 'what alice was reading');

    const s = await freshSession();
    await s.init();

    expect(get(s.messages)).toEqual([]);
    expect(db.readTranscript).not.toHaveBeenCalledWith('alice', expect.anything());
    s.teardown();
  });

  it('restores the outbox written before the relaunch', async () => {
    // The other half of what the pointer is for: an offline entry is keyed by
    // the same id, so without the seed a message queued in a lift comes back
    // to a session that cannot find it.
    persisted.store.set(LAST_USER_KEY, JSON.stringify('alice'));
    cacheFor('alice', 'earlier');
    seedQueue('alice', 't1', [storedEntry('written in the lift', { reason: 'offline' })]);

    const s = await freshSession();
    await s.init();

    const queued = get(s.messages).find((m) => m.text === 'written in the lift');
    expect(queued?.sendState).toBe('queued');
    expect(get(s.queuedCounts)).toEqual({ t1: 1 });
    s.teardown();
  });
});

describe('chat store — settling the guess against the server', () => {
  beforeEach(() => {
    for (const bag of [api, db]) {
      Object.values(bag).forEach((v) => {
        if (typeof v === 'function' && 'mockReset' in v) (v as { mockReset(): void }).mockReset();
      });
    }
    conn.setOnline(false);
    persisted.store.clear();
    native.isNativeShell.mockReturnValue(true);
    native.shellAtLeast.mockReturnValue(true);
    Object.values(notices).forEach((v) => v.mockReset());
    api.getChatConfig.mockImplementation(NO_CONNECTION);
    api.getChatRooms.mockImplementation(NO_CONNECTION);
    api.getRoomMessages.mockImplementation(NO_CONNECTION);
    api.getRoomEvents.mockImplementation(NO_CONNECTION);
    api.markRoomRead.mockImplementation(NO_CONNECTION);
    api.listOutboundDrafts.mockImplementation(NO_CONNECTION);
    api.getTaskEvents.mockResolvedValue({ events: [] });
    db.readTranscript.mockResolvedValue(null);
    db.readRooms.mockResolvedValue(null);
    db.readConfig.mockResolvedValue(null);
    (globalThis as { EventSource?: unknown }).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** The connection comes back, and the server answers as `user`. */
  function serverIs(user: string) {
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500, user_id: user });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue({
      messages: [row(5, `${user}'s real transcript`)],
      active_task: null,
      active_tasks: [],
    });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.listOutboundDrafts.mockResolvedValue({ drafts: [] });
  }

  it('drops everything painted from a namespace the server disagrees with', async () => {
    persisted.store.set(LAST_USER_KEY, JSON.stringify('alice'));
    cacheFor('alice', 'alice’s cached transcript');
    seedQueue('alice', 't1', [storedEntry('alice’s unsent message', { reason: 'offline' })]);

    const s = await freshSession();
    await s.init();
    expect(get(s.messages).some((m) => m.text === 'alice’s unsent message')).toBe(true);

    serverIs('bob');
    conn.setOnline(true);

    await vi.waitFor(() =>
      expect(get(s.messages).map((m) => m.text)).toEqual(["bob's real transcript"]),
    );
    // The visible half is the transcript; the half that matters is the queue,
    // whose entries would otherwise be persisted back under bob's key and sent
    // as his.
    expect(get(s.queuedCounts)).toEqual({});
    expect(api.sendChatMessage).not.toHaveBeenCalled();
    expect(persisted.store.get(LAST_USER_KEY)).toBe('"bob"');
    s.teardown();
  });

  it('keeps what it painted when the server confirms the guess', async () => {
    persisted.store.set(LAST_USER_KEY, JSON.stringify('alice'));
    cacheFor('alice', 'alice’s cached transcript');
    seedQueue('alice', 't1', [storedEntry('waiting on a turn', { reason: 'busy' })]);

    const s = await freshSession();
    await s.init();

    serverIs('alice');
    conn.setOnline(true);

    await vi.waitFor(() =>
      expect(get(s.messages).some((m) => m.text === "alice's real transcript")).toBe(true),
    );
    expect(get(s.queuedCounts)).toEqual({ t1: 1 });
    expect(get(s.messages).some((m) => m.text === 'waiting on a turn')).toBe(true);
    s.teardown();
  });

  it('asks the server nothing extra on a session that never guessed', async () => {
    // The ordinary online path: the id came from a live config at `init`, so
    // there is nothing to settle and the reconnect costs no second request.
    serverIs('alice');
    conn.setOnline(true);
    const s = await freshSession();
    await s.init();
    const configReads = api.getChatConfig.mock.calls.length;

    conn.setOnline(false);
    conn.setOnline(true);
    await vi.waitFor(() => expect(api.getRoomMessages.mock.calls.length).toBeGreaterThan(1));

    expect(api.getChatConfig.mock.calls.length).toBe(configReads);
    s.teardown();
  });

  it('sends nothing at all while the guess stands', async () => {
    // The sequence the repaint alone cannot cover, and the one that makes an
    // unsettled guess dangerous: the config read fails so nothing settles, but
    // the connection is back by the time the room list is asked for. Without a
    // gate the drain at the foot of `init` POSTs the guessed user's restored
    // messages under this session's cookie.
    persisted.store.set(LAST_USER_KEY, JSON.stringify('alice'));
    cacheFor('alice', 'alice’s cached transcript');
    seedQueue('alice', 't1', [storedEntry('alice’s unsent message', { reason: 'offline' })]);
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue({ messages: [], active_task: null, active_tasks: [] });
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    conn.setOnline(true);

    const s = await freshSession();
    await s.init();

    expect(api.sendChatMessage).not.toHaveBeenCalled();
    // The other half: what the server just answered with is this session's,
    // and stored under the guess it would be read back as the guessed user's
    // own on their next launch — the cross-user read the namespace exists to
    // prevent, arrived at from the writing side.
    for (const write of [db.writeRooms, db.writeTranscript, db.writeConfig]) {
      for (const call of write.mock.calls) expect(call[0]).not.toBe('alice');
    }
    s.teardown();
  });

  it('takes what this session typed out of the guessed storage', async () => {
    // A message typed while the guess stood was keyed by the guess. Dropping it
    // from memory is not enough: left in storage it is restored by the guessed
    // user's own next session and, being an offline entry inside the auto-send
    // window, goes out as them.
    persisted.store.set(LAST_USER_KEY, JSON.stringify('alice'));
    cacheFor('alice', 'alice’s cached transcript');
    seedQueue('alice', 't1', [storedEntry('alice’s own unsent message', { reason: 'offline' })]);

    const s = await freshSession();
    await s.init();
    await s.send('typed by whoever is actually here');

    const stored = () => {
      const raw = persisted.store.get(SEND_QUEUE_STORAGE_KEY);
      return raw ? ((JSON.parse(raw)['alice:room:t1'] ?? []) as { text: string }[]) : [];
    };
    expect(stored().map((e) => e.text)).toContain('typed by whoever is actually here');

    serverIs('bob');
    conn.setOnline(true);
    await vi.waitFor(() =>
      expect(get(s.messages).some((m) => m.text === "bob's real transcript")).toBe(true),
    );

    const left = stored().map((e) => e.text);
    expect(left).not.toContain('typed by whoever is actually here');
    // The guessed user's own entry stays exactly where it was: it is theirs,
    // and their next session is where it belongs.
    expect(left).toEqual(['alice’s own unsent message']);
    s.teardown();
  });

  it('writes the pointer on every successful config read', async () => {
    serverIs('alice');
    conn.setOnline(true);
    const s = await freshSession();
    await s.init();

    expect(persisted.store.get(LAST_USER_KEY)).toBe('"alice"');
    s.teardown();
  });
});

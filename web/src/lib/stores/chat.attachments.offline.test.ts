/**
 * The outbox's second half: a file attached with no connection (ISSUE-202).
 *
 * An attachment is a two-step send — the bytes are POSTed first and the message
 * references the host path that comes back — so offline there is no path to
 * reference. The queue entry carries the bytes instead, in IndexedDB, and the
 * drain does both steps. What is pinned here is the *order* of those steps and
 * what each failure between them does, because that is where a voice note gets
 * lost, doubled, or uploaded twice.
 *
 * The storage layer is mocked, as it is in `chat.offline.test.ts` and for the
 * same reason: these are assertions about the store's decisions. `db.test.ts`
 * drives the real thing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get } from 'svelte/store';
import type { ChatHistory, ChatRoom } from '$lib/api';
import type { ChatSession } from './chat';
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
  uploadChatAttachment: vi.fn(),
  createChatRoom: vi.fn(),
  updateChatRoom: vi.fn(),
  deleteChatRoom: vi.fn(),
  promoteChatRoom: vi.fn(),
  cancelChatTask: vi.fn(),
  confirmChatTask: vi.fn(),
  getNotificationCounts: vi.fn(),
  // The double has to carry every class the product does `instanceof`
  // against, or the property read throws inside the branch instead of
  // answering it.
  AuthError: class AuthError extends Error {},
  UploadUnreachableError: class UploadUnreachableError extends Error {},
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
  putBlob: vi.fn(),
  deleteBlob: vi.fn(),
  listBlobIds: vi.fn(),
  hasHeadroom: vi.fn(),
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

vi.mock('$lib/api', () => api);
vi.mock('$lib/offline/db', () => db);
vi.mock('$lib/stores/connectivity', () => conn);

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
vi.mock('$lib/stores/persisted', () => persisted);

const notices = vi.hoisted(() => ({
  notify: vi.fn(),
  notifyError: vi.fn(),
  notifySuccess: vi.fn(),
  notifyWarning: vi.fn(),
}));
vi.mock('$lib/stores/notices', () => notices);

function room(id: number): ChatRoom {
  return {
    id,
    token: `t${id}`,
    name: `Room ${id}`,
    archived: false,
    created_at: '',
    updated_at: '',
    origin: 'web',
    unread_count: 0,
  };
}

const emptyHistory: ChatHistory = { messages: [], active_task: null, active_tasks: [] };

const history = (messages: ChatHistory['messages']): ChatHistory => ({ ...emptyHistory, messages });

async function freshSession() {
  vi.resetModules();
  const mod = await import('./chat');
  return mod.getChatSession();
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  // Nothing awaits this before it settles, and an unhandled rejection out of a
  // promise held across a test is noise wherever it lands.
  promise.catch(() => {});
  return { promise, resolve, reject };
}

/** A staged chip whose bytes are held here rather than on the server. */
function pendingChip(blobId: string, name: string, size = 1024) {
  return { path: null, pendingBlobId: blobId, name, size, mimeType: 'audio/mp4' };
}

/** What `getBlob` hands back for one held file. */
function heldBlob(name: string, size = 1024) {
  return {
    bytes: new TextEncoder().encode('xxxxxxxx').buffer as ArrayBuffer,
    name,
    mimeType: 'audio/mp4',
    size,
    createdAt: Date.now(),
  };
}

/** The uploaded attachment the server answers with. */
function uploaded(name: string) {
  return { path: `/host/inbox/${name}`, name, size: 1024, workspace_path: `ws/${name}` };
}

describe('chat store — attachments in the outbox', () => {
  beforeEach(() => {
    for (const bag of [api, db]) {
      Object.values(bag).forEach((v) => {
        if (typeof v === 'function' && 'mockReset' in v) (v as any).mockReset();
      });
    }
    conn.setOnline(true);
    persisted.store.clear();
    Object.values(notices).forEach((v) => v.mockReset());
    api.getChatConfig.mockResolvedValue({ client_poll_interval_ms: 1500, user_id: 'alice' });
    api.getChatRooms.mockResolvedValue({ rooms: [room(1), room(2)] });
    api.getRoomMessages.mockResolvedValue(emptyHistory);
    api.getRoomEvents.mockResolvedValue({ events: [], cursor: 0, gap: false });
    api.markRoomRead.mockResolvedValue({ ok: true, last_read_message_id: 0 });
    api.getTaskEvents.mockResolvedValue({ events: [] });
    api.sendChatMessage.mockResolvedValue({ ok: true, status: 200, task_id: 43 });
    db.readTranscript.mockResolvedValue(null);
    db.readRooms.mockResolvedValue(null);
    db.readConfig.mockResolvedValue(null);
    db.deleteBlob.mockResolvedValue(undefined);
    (globalThis as any).EventSource = undefined;
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /** What is stored for a room right now. */
  function storedQueue(token: string): Record<string, any>[] {
    const raw = persisted.store.get(SEND_QUEUE_STORAGE_KEY);
    return raw ? (JSON.parse(raw)[`alice:room:${token}`] ?? []) : [];
  }

  /** Seed the stored map, as a previous session would have left it. */
  function seedQueue(token: string, entries: Record<string, unknown>[]) {
    persisted.store.set(
      SEND_QUEUE_STORAGE_KEY,
      JSON.stringify({ [`alice:room:${token}`]: entries }),
    );
  }

  const rowFor = (s: ChatSession, text: string) => get(s.messages).find((m) => m.text === text);

  it('queues a voice note recorded offline, bytes and all, without uploading', async () => {
    const s = await freshSession();
    await s.init();
    conn.setOnline(false);

    await s.send('', [pendingChip('b1', 'memo.m4a') as any]);

    expect(api.uploadChatAttachment).not.toHaveBeenCalled();
    expect(api.sendChatMessage).not.toHaveBeenCalled();
    const stored = storedQueue('t1');
    expect(stored).toHaveLength(1);
    expect(stored[0].text).toBe('');
    expect(stored[0].reason).toBe('offline');
    expect(stored[0].pendingAttachments).toEqual([
      { blobId: 'b1', name: 'memo.m4a', mimeType: 'audio/mp4', size: 1024 },
    ]);
    // The chip is on the row too, so the queued bubble shows what is waiting.
    expect(get(s.messages).at(-1)?.attachments).toEqual(['memo.m4a']);
    s.teardown();
  });

  it('uploads each held file, persists after each, and POSTs only once they are paths', async () => {
    seedQueue('t1', [
      {
        cid: 1,
        text: 'two files',
        attachments: [pendingChip('b1', 'one.m4a'), pendingChip('b2', 'two.m4a')],
        pendingAttachments: [
          { blobId: 'b1', name: 'one.m4a', mimeType: 'audio/mp4', size: 1024 },
          { blobId: 'b2', name: 'two.m4a', mimeType: 'audio/mp4', size: 1024 },
        ],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
        idempotencyKey: 'key-1',
      },
    ]);
    db.getBlob.mockImplementation(async (id: string) =>
      heldBlob(id === 'b1' ? 'one.m4a' : 'two.m4a'),
    );
    const seen: string[][] = [];
    api.uploadChatAttachment.mockImplementation(async (file: File) => {
      // What is stored at the moment each upload starts, so the persist-after-
      // each-success claim is observed rather than asserted at the end.
      seen.push((storedQueue('t1')[0]?.pendingAttachments ?? []).map((p: any) => p.blobId));
      return uploaded(file.name);
    });

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());

    // In order, and the second upload starts with the first already resolved.
    expect(api.uploadChatAttachment.mock.calls.map((c: any[]) => c[0].name)).toEqual([
      'one.m4a',
      'two.m4a',
    ]);
    expect(seen).toEqual([['b1', 'b2'], ['b2']]);
    // Each blob dropped as soon as its path was stored.
    expect(db.deleteBlob.mock.calls.map((c: any[]) => c[0])).toEqual(['b1', 'b2']);
    // And the POST carries the two host paths, not a null.
    const [, , paths, names] = api.sendChatMessage.mock.calls[0];
    expect(paths).toEqual(['/host/inbox/one.m4a', '/host/inbox/two.m4a']);
    expect(names).toEqual(['one.m4a', 'two.m4a']);
    s.teardown();
  });

  it('does not re-upload a file an interrupted drain had already landed', async () => {
    // The session that stored this got the first file up and was killed before
    // the second: exactly what the persist-after-each step is for.
    seedQueue('t1', [
      {
        cid: 1,
        text: 'two files',
        attachments: [uploaded('one.m4a'), pendingChip('b2', 'two.m4a')],
        pendingAttachments: [{ blobId: 'b2', name: 'two.m4a', mimeType: 'audio/mp4', size: 1024 }],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
        idempotencyKey: 'key-1',
      },
    ]);
    db.getBlob.mockResolvedValue(heldBlob('two.m4a'));
    api.uploadChatAttachment.mockImplementation(async (file: File) => uploaded(file.name));

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());

    expect(api.uploadChatAttachment).toHaveBeenCalledTimes(1);
    expect(api.uploadChatAttachment.mock.calls[0][0].name).toBe('two.m4a');
    expect(api.sendChatMessage.mock.calls[0][2]).toEqual([
      '/host/inbox/one.m4a',
      '/host/inbox/two.m4a',
    ]);
    s.teardown();
  });

  it('keeps the message and its bytes when an upload finds no server', async () => {
    seedQueue('t1', [
      {
        cid: 1,
        text: 'held',
        attachments: [pendingChip('b1', 'memo.m4a')],
        pendingAttachments: [{ blobId: 'b1', name: 'memo.m4a', mimeType: 'audio/mp4', size: 1024 }],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
      },
    ]);
    db.getBlob.mockResolvedValue(heldBlob('memo.m4a'));
    api.uploadChatAttachment.mockRejectedValue(new api.UploadUnreachableError('no server'));

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(api.uploadChatAttachment).toHaveBeenCalled());

    // Nothing was decided about this message, so it goes back to waiting.
    expect(rowFor(s, 'held')?.sendState).toBe('queued');
    expect(api.sendChatMessage).not.toHaveBeenCalled();
    const stored = storedQueue('t1');
    expect(stored).toHaveLength(1);
    expect(stored[0].pendingAttachments).toHaveLength(1);
    // The bytes are the message; they are not dropped on a gap.
    expect(db.deleteBlob).not.toHaveBeenCalled();
    s.teardown();
  });

  it('fails the whole row and drops the bytes when the server refuses a file', async () => {
    seedQueue('t1', [
      {
        cid: 1,
        text: 'a video, apparently',
        attachments: [pendingChip('b1', 'clip.mov'), pendingChip('b2', 'clip2.mov')],
        pendingAttachments: [
          { blobId: 'b1', name: 'clip.mov', mimeType: 'video/quicktime', size: 1024 },
          { blobId: 'b2', name: 'clip2.mov', mimeType: 'video/quicktime', size: 1024 },
        ],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
      },
    ]);
    db.getBlob.mockResolvedValue(heldBlob('clip.mov'));
    api.uploadChatAttachment.mockRejectedValue(new Error('upload failed (413)'));

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(rowFor(s, 'a video, apparently')?.sendState).toBe('failed'));

    const row = rowFor(s, 'a video, apparently');
    expect(row?.sendError).toContain('413');
    // No Retry: re-POSTing cannot change a verdict on the file. The row keeps
    // the text on screen, which is the whole of what is left of it — the spec
    // says Edit recovers it, and no failed row has ever offered Edit.
    expect(row?.retryable).toBe(false);
    expect(api.sendChatMessage).not.toHaveBeenCalled();
    // Neither the entry nor the bytes are kept: holding them forever is the
    // wrong answer to a verdict that will not change.
    expect(storedQueue('t1')).toEqual([]);
    expect(db.deleteBlob.mock.calls.map((c: any[]) => c[0]).sort()).toEqual(['b1', 'b2']);
    s.teardown();
  });

  it('fails the row rather than sending a message without the file it was about', async () => {
    // The bytes were evicted between queueing and draining — whole-origin
    // eviction is exactly what the Stage 6 matrix exists to measure.
    seedQueue('t1', [
      {
        cid: 1,
        text: 'listen to this',
        attachments: [pendingChip('b1', 'memo.m4a')],
        pendingAttachments: [{ blobId: 'b1', name: 'memo.m4a', mimeType: 'audio/mp4', size: 1024 }],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
      },
    ]);
    db.getBlob.mockResolvedValue(null);

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(rowFor(s, 'listen to this')?.sendState).toBe('failed'));

    expect(api.uploadChatAttachment).not.toHaveBeenCalled();
    expect(api.sendChatMessage).not.toHaveBeenCalled();
    expect(storedQueue('t1')).toEqual([]);
    s.teardown();
  });

  it('deletes the held bytes when the queued message is removed', async () => {
    const s = await freshSession();
    await s.init();
    conn.setOnline(false);
    await s.send('take that back', [pendingChip('b1', 'memo.m4a') as any]);
    const cid = rowFor(s, 'take that back')?.cid as number;

    s.removeQueued(cid);

    expect(db.deleteBlob).toHaveBeenCalledWith('b1');
    expect(storedQueue('t1')).toEqual([]);
    s.teardown();
  });

  it('keeps the held bytes when the queued message is edited back into the composer', async () => {
    const s = await freshSession();
    await s.init();
    conn.setOnline(false);
    await s.send('let me rephrase', [pendingChip('b1', 'memo.m4a') as any]);
    const cid = rowFor(s, 'let me rephrase')?.cid as number;

    s.editQueued(cid);

    // The chip goes back to the composer, so its bytes are still the only copy.
    expect(db.deleteBlob).not.toHaveBeenCalled();
    expect(get(s.sendReturned).attachments?.[0]).toMatchObject({ pendingBlobId: 'b1' });
    s.teardown();
  });

  it('queues a chip held offline even after the connection is back', async () => {
    // The resolution lives in the drain, so a chip staged offline goes through
    // the queue whatever the connection did between holding it and Send.
    db.getBlob.mockResolvedValue(heldBlob('memo.m4a'));
    api.uploadChatAttachment.mockImplementation(async (file: File) => uploaded(file.name));
    const s = await freshSession();
    await s.init();

    await s.send('back already', [pendingChip('b1', 'memo.m4a') as any]);

    await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());
    expect(api.uploadChatAttachment).toHaveBeenCalledTimes(1);
    expect(api.sendChatMessage.mock.calls[0][2]).toEqual(['/host/inbox/memo.m4a']);
    // Drained clean: nothing left waiting.
    expect(storedQueue('t1')).toEqual([]);
    s.teardown();
  });

  it('holds the message when Stop is tapped while its file is going up', async () => {
    // The one-attachment case, which is the voice note this feature is for:
    // the last upload finishes and the pending list empties, so a check that
    // ran only when a file remained would never see this Stop at all.
    const upload = deferred<Record<string, unknown>>();
    seedQueue('t1', [
      {
        cid: 1,
        text: 'never mind',
        attachments: [pendingChip('b1', 'memo.m4a')],
        pendingAttachments: [{ blobId: 'b1', name: 'memo.m4a', mimeType: 'audio/mp4', size: 1024 }],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
      },
    ]);
    db.getBlob.mockResolvedValue(heldBlob('memo.m4a'));
    api.uploadChatAttachment.mockReturnValue(upload.promise);

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(api.uploadChatAttachment).toHaveBeenCalled());
    await s.cancel();
    upload.resolve(uploaded('memo.m4a'));

    await vi.waitFor(() => expect(rowFor(s, 'never mind')?.sendState).toBe('queued'));
    expect(api.sendChatMessage).not.toHaveBeenCalled();
    expect(rowFor(s, 'never mind')?.queueHeld).toBe(true);
    expect(storedQueue('t1')[0].held).toBe(true);

    // And the latch is consumed: pressing Send on the held row sends it, where
    // a Stop left armed would hold it again on every attempt, for good.
    api.uploadChatAttachment.mockResolvedValue(uploaded('memo.m4a'));
    const cid = rowFor(s, 'never mind')?.cid as number;
    await s.releaseQueued(cid);
    await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());
    s.teardown();
  });

  it('refuses a second send of a message whose files are still uploading', async () => {
    // A room switch drops a row that is mid-send — it is not client-only — and
    // the switch back rebuilds it from the entry, which is still queued, then
    // drains at the foot of the load. Without a claim the second resolution
    // races the first over one entry's blobs, and whichever reads after the
    // other's delete fails a message whose upload had in fact succeeded.
    const upload = deferred<Record<string, unknown>>();
    seedQueue('t1', [
      {
        cid: 1,
        text: 'one at a time',
        attachments: [pendingChip('b1', 'memo.m4a')],
        pendingAttachments: [{ blobId: 'b1', name: 'memo.m4a', mimeType: 'audio/mp4', size: 1024 }],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
      },
    ]);
    db.getBlob.mockResolvedValue(heldBlob('memo.m4a'));
    api.uploadChatAttachment.mockReturnValue(upload.promise);

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(api.uploadChatAttachment).toHaveBeenCalled());

    // The room switch and the switch back, which is what re-enters the drain.
    await s.selectRoom(2);
    await s.selectRoom(1);

    upload.resolve(uploaded('memo.m4a'));
    await vi.waitFor(() => expect(api.sendChatMessage).toHaveBeenCalled());
    expect(api.uploadChatAttachment).toHaveBeenCalledTimes(1);
    expect(api.sendChatMessage).toHaveBeenCalledTimes(1);
    s.teardown();
  });

  it('does not let a parked upload claim a server row for a message never POSTed', async () => {
    // `parkedAfterPost` is what lets a row that reached the wire adopt the
    // server's copy of itself by body match, on the stream and on a history
    // rebuild alike. An upload that failed *ahead* of the message POST reached
    // no wire, so nothing of this message can be on the server — and a body
    // match against somebody else's identical send is exactly what the set is
    // bounded to prevent. Driven through the rebuild rather than the stream,
    // which is the same adoption and does not need a poll to fire.
    seedQueue('t1', [
      {
        cid: 1,
        text: 'twin',
        attachments: [pendingChip('b1', 'memo.m4a')],
        pendingAttachments: [{ blobId: 'b1', name: 'memo.m4a', mimeType: 'audio/mp4', size: 1024 }],
        held: false,
        queuedAt: Date.now(),
        reason: 'offline',
      },
    ]);
    db.getBlob.mockResolvedValue(heldBlob('memo.m4a'));
    api.uploadChatAttachment.mockRejectedValue(new api.UploadUnreachableError('no server'));

    const s = await freshSession();
    await s.init();
    await vi.waitFor(() => expect(rowFor(s, 'twin')?.sendState).toBe('queued'));

    // A row with the same body turns up in the room's own history: another
    // client's send, since ours never left.
    api.getRoomMessages.mockResolvedValue(
      history([
        {
          role: 'user',
          text: 'twin',
          created_at: '2026-08-30T11:00:00Z',
          msg_id: 900,
          starred: false,
          room_token: 't1',
        } as ChatHistory['messages'][number],
      ]),
    );
    await s.selectRoom(2);
    await s.selectRoom(1);

    const twins = get(s.messages).filter((m) => m.text === 'twin');
    expect(twins).toHaveLength(2);
    expect(twins.filter((m) => m.sendState === 'queued')).toHaveLength(1);
    expect(storedQueue('t1')).toHaveLength(1);
    s.teardown();
  });

  it('tells the collector which blobs its queue still names', async () => {
    seedQueue('t1', [
      {
        cid: 1,
        text: 'waiting',
        attachments: [pendingChip('b-live', 'memo.m4a')],
        pendingAttachments: [
          { blobId: 'b-live', name: 'memo.m4a', mimeType: 'audio/mp4', size: 1024 },
        ],
        held: true,
        queuedAt: Date.now(),
        reason: 'offline',
      },
    ]);
    const s = await freshSession();
    await s.init();

    await vi.waitFor(() => expect(db.pruneOffline).toHaveBeenCalled());
    const referenced = db.pruneOffline.mock.calls[0][1] as Set<string>;
    expect(referenced).toBeInstanceOf(Set);
    expect(referenced.has('b-live')).toBe(true);
    s.teardown();
  });
});

/**
 * The composer with no connection (ISSUE-202).
 *
 * A file attached offline cannot be POSTed, so its bytes are held in IndexedDB
 * and the chip is staged unresolved; the outbox uploads it when there is a
 * connection to upload it on. What is pinned here is that the branch is taken
 * at all, that a refused hold says so rather than staging a chip with no bytes
 * behind it, and that removing a pending chip takes its bytes with it.
 *
 * `useRecorder` is deliberately not exercised: a voice memo reaches `upload()`
 * exactly as a picked file does, so the recorder needs no offline branch and
 * has none.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(),
  chatConfigOnce: vi.fn(),
  // The double has to carry every class the product does `instanceof`
  // against, or the property read throws inside the branch instead of
  // answering it.
  AuthError: class AuthError extends Error {},
  UploadUnreachableError: class UploadUnreachableError extends Error {},
}));

const picked = vi.hoisted(() => ({
  nativePickersAvailable: vi.fn(() => true),
  takePhoto: vi.fn(async () => []),
  pickPhotos: vi.fn(async () => []),
  pickDocuments: vi.fn(async () => []),
  // Not a seam — the real one, so a File handed to upload() behaves the way it
  // does in the app.
  pickedFromFile: (f: File) => ({ name: f.name, type: f.type, size: f.size, blob: f }),
  fileFromPicked: vi.fn(async (p: { blob?: File }) => p.blob ?? null),
}));
vi.mock('$lib/platform/nativePicker', () => picked);

const db = vi.hoisted(() => ({
  putBlob: vi.fn(),
  deleteBlob: vi.fn(),
  getBlob: vi.fn(),
  listBlobIds: vi.fn(),
  hasHeadroom: vi.fn(),
  MAX_PENDING_BLOB_BYTES: 10 * 1024 * 1024,
}));
vi.mock('$lib/offline/db', () => db);

const conn = vi.hoisted(() => {
  let value = true;
  return {
    online: {
      subscribe(fn: (v: boolean) => void) {
        fn(value);
        return () => {};
      },
    },
    setOnline(next: boolean) {
      value = next;
    },
    noteTransport: vi.fn(),
    probe: vi.fn(),
    startConnectivity: vi.fn(() => () => {}),
  };
});
vi.mock('$lib/stores/connectivity', () => conn);

import {
  uploadChatAttachment,
  fetchChatCommands,
  chatConfigOnce,
  UploadUnreachableError,
} from '$lib/api';
import { pickPhotos } from '$lib/platform/nativePicker';
import { resetCommandCatalogue } from './autocomplete/providers';
import Composer from './Composer.svelte';

const upload = uploadChatAttachment as ReturnType<typeof vi.fn>;
const photos = pickPhotos as ReturnType<typeof vi.fn>;
const chatConfig = chatConfigOnce as ReturnType<typeof vi.fn>;

/** A File that claims a size without allocating it. */
function sizedFile(name: string, bytes: number, type = 'audio/mp4'): File {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}

const btn = (c: HTMLElement, label: string) =>
  c.querySelector(`[aria-label="${label}"]`) as HTMLButtonElement | null;

/** Pick `files` through the photo row, which is the shortest path to upload(). */
async function pick(container: HTMLElement, files: unknown[]) {
  photos.mockResolvedValue(files);
  await fireEvent.click(btn(container, 'Attach file')!);
  await fireEvent.click(btn(container, 'Photo Library')!);
  // Until the in-flight marker clears: holding a file is several awaits deep
  // (the headroom question, the read, the write) and a fixed number of ticks
  // would silently start asserting against a half-finished batch.
  await vi.waitFor(() => expect(container.querySelector('.attach-chip.uploading')).toBeNull());
  await tick();
}

beforeEach(() => {
  window.matchMedia = ((q: string) => ({
    matches: false,
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
  resetCommandCatalogue();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockReset();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
    commands: [],
    model_aliases: [],
  });
  upload.mockReset();
  photos.mockReset();
  db.putBlob.mockReset();
  db.deleteBlob.mockReset();
  picked.fileFromPicked.mockReset();
  picked.fileFromPicked.mockImplementation(async (p: { blob?: File }) => p.blob ?? null);
  db.putBlob.mockResolvedValue(true);
  db.deleteBlob.mockResolvedValue(undefined);
  db.hasHeadroom.mockReset();
  db.hasHeadroom.mockResolvedValue(true);
  chatConfig.mockReset();
  chatConfig.mockResolvedValue({
    max_prompt_chars: 32000,
    max_attachment_mb: 25,
    attachment_extensions: ['jpg', 'jpeg', 'png', 'pdf', 'webm', 'm4a'],
    client_poll_interval_ms: 1500,
  });
  conn.setOnline(false);
});

afterEach(() => {
  cleanup();
  conn.setOnline(true);
});

describe('Composer with no connection', () => {
  it('holds the bytes and stages a pending chip instead of uploading', async () => {
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [sizedFile('memo.m4a', 4096)]);

    expect(upload).not.toHaveBeenCalled();
    expect(db.putBlob).toHaveBeenCalledTimes(1);
    const [blobId, bytes, meta] = db.putBlob.mock.calls[0];
    expect(blobId).toEqual(expect.any(String));
    expect(bytes).toBeInstanceOf(ArrayBuffer);
    expect(meta).toEqual({ name: 'memo.m4a', mimeType: 'audio/mp4', size: 4096 });

    const chip = container.querySelector('.attach-chip');
    expect(chip?.textContent).toContain('memo.m4a');
    // Muted the way an in-flight upload's chip is: the file is staged, it is
    // just not anywhere yet.
    expect(chip?.classList.contains('pending')).toBe(true);
  });

  it('hands a pending chip to the send, with no path and a blob to find it by', async () => {
    const onSend = vi.fn();
    const { container } = render(Composer, { onSend });
    await tick();
    await pick(container, [sizedFile('memo.m4a', 4096)]);

    await fireEvent.click(btn(container, 'Send')!);

    expect(onSend).toHaveBeenCalledTimes(1);
    const [text, attachments] = onSend.mock.calls[0];
    expect(text).toBe('');
    expect(attachments).toEqual([
      {
        path: null,
        pendingBlobId: db.putBlob.mock.calls[0][0],
        name: 'memo.m4a',
        size: 4096,
        mimeType: 'audio/mp4',
      },
    ]);
  });

  it('refuses a file too large to hold, before reading it', async () => {
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [sizedFile('clip.mov', db.MAX_PENDING_BLOB_BYTES + 1)]);

    expect(db.putBlob).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-chip')).toBeNull();
    expect(container.querySelector('.attach-error')?.textContent).toContain(
      'Too large to hold offline',
    );
  });

  it('says so when the store refuses the write, and stages nothing', async () => {
    // The shared total, the origin's headroom, or an IndexedDB this browser
    // will not give us — one sentence for all three, because from here they
    // are one fact.
    db.putBlob.mockResolvedValue(false);
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [sizedFile('memo.m4a', 4096)]);

    expect(container.querySelector('.attach-chip')).toBeNull();
    expect(container.querySelector('.attach-error')?.textContent).toContain(
      'Too large to hold offline',
    );
  });

  it('refuses a type the server does not take, without holding it either', async () => {
    // The server's own limits still apply offline where they are known: there
    // is no point holding a file for hours to have it refused on arrival.
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [sizedFile('payload.exe', 1024, 'application/octet-stream')]);

    expect(db.putBlob).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')?.textContent).toContain('.exe');
  });

  it('drops the held bytes when the chip is removed', async () => {
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();
    await pick(container, [sizedFile('memo.m4a', 4096)]);
    const blobId = db.putBlob.mock.calls[0][0];

    await fireEvent.click(btn(container, 'Remove memo.m4a')!);

    expect(container.querySelector('.attach-chip')).toBeNull();
    expect(db.deleteBlob).toHaveBeenCalledWith(blobId);
  });

  it('holds a file whose own upload discovered the gap', async () => {
    // The first file attached after the signal dies. `online` is only believed
    // false once something has observed a failure, so this one takes the
    // upload path — and for a voice memo the argument to `upload()` is the only
    // copy of the recording there is. Losing it here is losing the recording.
    conn.setOnline(true);
    upload.mockRejectedValue(new UploadUnreachableError('no server'));
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [sizedFile('memo.m4a', 4096)]);

    expect(upload).toHaveBeenCalledTimes(1);
    expect(db.putBlob).toHaveBeenCalledTimes(1);
    const chip = container.querySelector('.attach-chip');
    expect(chip?.textContent).toContain('memo.m4a');
    expect(chip?.classList.contains('pending')).toBe(true);
    expect(container.querySelector('.attach-error')).toBeNull();
  });

  it('still reports an upload the server itself refused', async () => {
    // The control on the fall-through above: only a gap is held. A 413 is a
    // verdict, and holding the file would park a message the server has
    // already said it will not take.
    conn.setOnline(true);
    upload.mockRejectedValue(new Error('upload failed (413)'));
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [sizedFile('memo.m4a', 4096)]);

    expect(db.putBlob).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-chip')).toBeNull();
    expect(container.querySelector('.attach-error')?.textContent).toContain('413');
  });

  it('says a file could not be read rather than calling it too large', async () => {
    // A native pick whose path could not be read back is not a size refusal,
    // and telling the user to attach it when they are online would be advice
    // about the wrong problem.
    picked.fileFromPicked.mockResolvedValue(null);
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [{ name: 'scan.pdf', type: 'application/pdf', size: 2048 }]);

    expect(db.putBlob).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')?.textContent).toContain(
      'Couldn’t read scan.pdf',
    );
  });

  it('drops the held bytes when the room changes under the chip', async () => {
    // The composer is mounted once and clears its chips on a room switch. It
    // is the last thing that knows which blobs those were: the collector
    // reconciles against the send queue, and this file was never queued.
    const { container, rerender } = render(Composer, { onSend: vi.fn(), draftKey: 'u:room:t1' });
    await tick();
    await pick(container, [sizedFile('memo.m4a', 4096)]);
    const blobId = db.putBlob.mock.calls[0][0];

    await rerender({ onSend: vi.fn(), draftKey: 'u:room:t2' });
    await tick();

    expect(container.querySelector('.attach-chip')).toBeNull();
    expect(db.deleteBlob).toHaveBeenCalledWith(blobId);
  });

  it('uploads as it always did once there is a connection', async () => {
    // The control on all of the above: the branch is the connection's, not a
    // new default.
    conn.setOnline(true);
    upload.mockResolvedValue({ path: 'inbox/memo.m4a', name: 'memo.m4a', size: 4096 });
    const { container } = render(Composer, { onSend: vi.fn() });
    await tick();

    await pick(container, [sizedFile('memo.m4a', 4096)]);

    expect(db.putBlob).not.toHaveBeenCalled();
    expect(upload).toHaveBeenCalledTimes(1);
    expect(container.querySelector('.attach-chip')?.classList.contains('pending')).toBe(false);
  });
});

import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';
import { tick } from 'svelte';

vi.mock('$lib/api', () => ({
  uploadChatAttachment: vi.fn(),
  fetchChatCommands: vi.fn(),
  chatConfigOnce: vi.fn(),
  // Not used by any test here, and it has to be on the mock all the same:
  // `upload()`'s catch reads `e instanceof UploadUnreachableError` on every
  // failed upload, and vitest throws on a missing export from a factory mock
  // rather than answering undefined. That throw escapes into an unhandled
  // rejection, which fails the run without failing a test.
  // The double has to carry every class the product does `instanceof`
  // against, or the property read throws inside the branch instead of
  // answering it.
  AuthError: class AuthError extends Error {},
  UploadUnreachableError: class extends Error {},
}));

// The pickers have their own unit tests (nativePicker.test.ts). Here the seam
// is what matters: which one a row reaches for, and whether the menu is offered
// at all.
vi.mock('$lib/platform/nativePicker', () => ({
  nativePickersAvailable: vi.fn(() => true),
  takePhoto: vi.fn(async () => []),
  pickPhotos: vi.fn(async () => []),
  pickDocuments: vi.fn(async () => []),
  // Not a seam — the real one, so a File handed to upload() behaves the way it
  // does in the app.
  pickedFromFile: (f: File) => ({ name: f.name, type: f.type, size: f.size, blob: f }),
}));

import { uploadChatAttachment, fetchChatCommands, chatConfigOnce } from '$lib/api';
import {
  nativePickersAvailable,
  takePhoto,
  pickPhotos,
  pickDocuments,
} from '$lib/platform/nativePicker';
import { resetCommandCatalogue } from './autocomplete/providers';
import { readDraft, writeDraft, DRAFT_STORAGE_KEY, MAX_DRAFT_CHARS } from '$lib/stores/drafts';
import { IME_COMMIT_GRACE_MS } from '$lib/platform/input';
import Composer, { DRAFT_SAVE_DEBOUNCE_MS } from './Composer.svelte';

const upload = uploadChatAttachment as ReturnType<typeof vi.fn>;
const chatConfig = chatConfigOnce as ReturnType<typeof vi.fn>;
const hasNative = nativePickersAvailable as ReturnType<typeof vi.fn>;

/** A File that claims a size without allocating it. */
function sizedFile(name: string, bytes: number, type = 'image/jpeg'): File {
  const file = new File(['x'], name, { type });
  Object.defineProperty(file, 'size', { value: bytes });
  return file;
}
const native = {
  camera: takePhoto as ReturnType<typeof vi.fn>,
  photos: pickPhotos as ReturnType<typeof vi.fn>,
  documents: pickDocuments as ReturnType<typeof vi.fn>,
};

/** jsdom leaves scrollHeight at 0, so autoGrow can't tell one line from many.
 *  Feed it a height we control to exercise the wrap threshold.
 *
 *  `narrowScrollHeight` models the width-dependent case: the composer measures
 *  wrapping with the field's wrapper pinned to its single-row width (an inline
 *  flex-basis), so a test can return a different height for that measurement
 *  than for the field's natural width. */
let fakeScrollHeight = 20;
let narrowScrollHeight: number | null = null;
Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
  configurable: true,
  get(this: HTMLTextAreaElement) {
    const pinned = (this.closest('.ta-wrap') as HTMLElement | null)?.style.flexBasis;
    return pinned && narrowScrollHeight !== null ? narrowScrollHeight : fakeScrollHeight;
  },
});

/** jsdom reports every box as 0×0, which makes the single-row width
 *  uncomputable. Give the three measured elements a size. */
Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
  configurable: true,
  get(this: HTMLElement) {
    return this.classList?.contains('composer-row') ? 400 : 0;
  },
});
Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
  configurable: true,
  get(this: HTMLElement) {
    if (this.classList?.contains('plus')) return 36;
    if (this.classList?.contains('tools')) return 72;
    return 0;
  },
});

class FakeMediaRecorder {
  static isTypeSupported = () => true;
  static last: FakeMediaRecorder | null = null;
  ondataavailable: ((e: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  mimeType = 'audio/webm';
  constructor() {
    FakeMediaRecorder.last = this;
  }
  start() {}
  stop() {
    this.ondataavailable?.({ data: new Blob(['x'], { type: this.mimeType }) });
    this.onstop?.();
  }
}

function enableMic() {
  (globalThis as Record<string, unknown>).MediaRecorder = FakeMediaRecorder;
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }) },
  });
}

/** Which keyboard the composer thinks it is talking to. */
function softKeyboard(on: boolean) {
  window.matchMedia = ((q: string) => ({
    // `on && …`, not `on === …`: the equality form answered **true** to every
    // non-coarse query when `on` was false, which was inert only while nothing
    // else in this file's tree asked `matchMedia` anything.
    matches: on && q.includes('coarse'),
    media: q,
    addEventListener: () => {},
    removeEventListener: () => {},
  })) as unknown as typeof window.matchMedia;
}

afterEach(() => {
  cleanup();
  delete (globalThis as Record<string, unknown>).MediaRecorder;
  Object.defineProperty(globalThis.navigator, 'mediaDevices', {
    configurable: true,
    value: undefined,
  });
  // The backgrounding test redefines this and cannot restore it itself without
  // the assertion coming first. Left alone it stayed 'hidden' for every test
  // after it in the file — a leak nothing would report until something started
  // depending on the value.
  Object.defineProperty(document, 'visibilityState', {
    configurable: true,
    get: () => 'visible',
  });
  fakeScrollHeight = 20;
  narrowScrollHeight = null;
});

beforeEach(() => {
  // `softKeyboard(on)` replaces `window.matchMedia` outright and nothing put it
  // back, so every test after one that asked for a coarse pointer inherited it.
  // That was inert while nothing but the post-send blur read the modality; the
  // Enter-to-send rule reads it too, so the default has to be stated.
  softKeyboard(false);
  resetCommandCatalogue();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockReset();
  (fetchChatCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
    commands: [],
    model_aliases: [],
  });
  upload.mockReset();
  upload.mockResolvedValue({ path: 'inbox/voice.webm', name: 'voice.webm', size: 12 });
  chatConfig.mockReset();
  chatConfig.mockResolvedValue({
    max_prompt_chars: 32000,
    max_attachment_mb: 25,
    attachment_extensions: ['jpg', 'jpeg', 'png', 'pdf', 'webm'],
    client_poll_interval_ms: 1500,
  });
});

function mount(props: Record<string, unknown> = {}) {
  const utils = render(Composer, { onSend: vi.fn(), ...props });
  const textarea = utils.container.querySelector('textarea') as HTMLTextAreaElement;
  return { ...utils, textarea };
}

async function type(textarea: HTMLTextAreaElement, value: string) {
  textarea.value = value;
  textarea.selectionStart = textarea.selectionEnd = value.length;
  await fireEvent.input(textarea);
  await tick();
}

const btn = (c: HTMLElement, label: string) =>
  c.querySelector(`[aria-label="${label}"]`) as HTMLButtonElement | null;

const menu = (c: HTMLElement) => c.querySelector('[role="menu"]');

const picker = (c: HTMLElement, kind: string) =>
  c.querySelector(`input[data-picker="${kind}"]`) as HTMLInputElement;

beforeEach(() => {
  hasNative.mockReturnValue(true);
  for (const fn of Object.values(native)) {
    fn.mockReset();
    fn.mockResolvedValue([]);
  }
});

describe('Composer attachment menu', () => {
  it('opens our own menu rather than the system sheet', async () => {
    const { container } = mount();
    expect(menu(container)).toBeNull();

    await fireEvent.click(btn(container, 'Attach file')!);

    expect(menu(container)).toBeTruthy();
    for (const label of ['Photo Library', 'Take Photo', 'Choose File']) {
      expect(btn(container, label)).toBeTruthy();
    }
  });

  it('reaches no file input just to show the menu', async () => {
    // The whole point. WebKit's sheet is what takes the keyboard down, and it
    // goes up the moment a file input is activated — so the tap that opens the
    // menu must not touch one.
    const { container } = mount();
    const opened = vi.fn();
    for (const input of container.querySelectorAll('input[type="file"]')) {
      input.addEventListener('click', opened);
    }

    await fireEvent.click(btn(container, 'Attach file')!);

    expect(opened).not.toHaveBeenCalled();
  });

  it('skips the menu entirely in a plain browser', async () => {
    // Without native pickers every row would end at WebKit's sheet anyway, so
    // the menu would be a step added rather than removed. The button goes
    // straight to the file input, exactly as it did before the menu existed.
    hasNative.mockReturnValue(false);
    const { container } = mount();
    const open = vi.spyOn(picker(container, 'file'), 'click');

    await fireEvent.click(btn(container, 'Attach file')!);

    expect(menu(container)).toBeNull();
    expect(open).toHaveBeenCalled();
  });

  it('keeps the field focused when a menu row is tapped', async () => {
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();
    await fireEvent.click(btn(container, 'Attach file')!);

    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    btn(container, 'Photo Library')!.dispatchEvent(down);

    expect(down.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(textarea);
  });

  it('sits inside the composer, where the global dismiss leaves it alone', async () => {
    // installKeyboardDismiss exempts `.composer` — a menu rendered anywhere else
    // would drop the keyboard on the way to being tapped.
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);
    expect(menu(container)!.closest('.composer')).toBeTruthy();
  });

  it('sends each row to the source it names', async () => {
    // The row landing where it says it will is the whole reason for the native
    // pickers — routing these back through a file input would put our menu in
    // front of WebKit's rather than instead of it.
    const rows: [string, ReturnType<typeof vi.fn>][] = [
      ['Photo Library', native.photos],
      ['Take Photo', native.camera],
      ['Choose File', native.documents],
    ];
    for (const [label, fn] of rows) {
      const { container, unmount } = mount();
      await fireEvent.click(btn(container, 'Attach file')!);
      await fireEvent.click(btn(container, label)!);
      expect(fn, label).toHaveBeenCalled();
      unmount();
    }
  });

  it('uploads what the picker hands back', async () => {
    native.photos.mockResolvedValue([new File(['x'], 'a.jpg', { type: 'image/jpeg' })]);
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).toHaveBeenCalled();
  });

  it('refuses a file bigger than the server takes, without uploading it', async () => {
    // The server answers 413 only after reading the whole body, so without this
    // the user waits out an upload of a file that was never going to land. The
    // limit is the server's own, read from /chat/config.
    native.photos.mockResolvedValue([sizedFile('huge.jpg', 30 * 1024 * 1024)]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')?.textContent).toContain('25 MB');
  });

  it('refuses a type the server does not accept', async () => {
    native.documents.mockResolvedValue([
      sizedFile('payload.exe', 1024, 'application/octet-stream'),
    ]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Choose File')!);
    await tick();

    expect(upload).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')?.textContent).toContain('.exe');
  });

  it('carries on with the files that do fit', async () => {
    // One refusal in a batch is not a reason to drop the rest.
    native.photos.mockResolvedValue([
      sizedFile('huge.jpg', 30 * 1024 * 1024),
      sizedFile('fine.jpg', 1024),
    ]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).toHaveBeenCalledTimes(1);
    expect((upload.mock.calls[0][0] as File).name).toBe('fine.jpg');
  });

  it('lets the server decide when the limits never arrived', async () => {
    // /chat/config is best-effort. Failing to reach it must not turn into a
    // client-side refusal of files the server would have taken.
    chatConfig.mockRejectedValue(new Error('offline'));
    native.photos.mockResolvedValue([sizedFile('huge.jpg', 900 * 1024 * 1024)]);
    const { container } = mount();
    await tick();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).toHaveBeenCalledTimes(1);
  });

  it('uploads nothing when the pick was cancelled', async () => {
    // A cancel comes back as an empty list rather than an error, so there is
    // nothing to report and nothing to send.
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);
    await tick();

    expect(upload).not.toHaveBeenCalled();
    expect(container.querySelector('.attach-error')).toBeNull();
  });

  it('says so when the picker itself fails', async () => {
    native.documents.mockRejectedValue(new Error('no picker'));
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Choose File')!);
    await tick();

    expect(container.querySelector('.attach-error')?.textContent).toContain('picker');
  });

  it('closes the menu as the picker opens', async () => {
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.click(btn(container, 'Photo Library')!);

    expect(menu(container)).toBeNull();
  });

  it('closes on Escape', async () => {
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    await fireEvent.keyDown(window, { key: 'Escape' });

    expect(menu(container)).toBeNull();
  });

  it('closes on a tap outside it', async () => {
    const { container } = mount();
    await fireEvent.click(btn(container, 'Attach file')!);

    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
    await tick();

    expect(menu(container)).toBeNull();
  });

  it('closes rather than reopening when attach is tapped again', async () => {
    // The outside-tap listener sees the attach button's own pointerdown first.
    // Closing there and reopening on the click would leave it stuck open.
    const { container } = mount();
    const attach = btn(container, 'Attach file')!;
    await fireEvent.click(attach);

    attach.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
    await fireEvent.click(attach);

    expect(menu(container)).toBeNull();
  });

  it('says whether it is open', async () => {
    const { container } = mount();
    const attach = btn(container, 'Attach file')!;
    expect(attach.getAttribute('aria-expanded')).toBe('false');
    await fireEvent.click(attach);
    expect(attach.getAttribute('aria-expanded')).toBe('true');
  });
});

describe('Composer send control', () => {
  it('is disabled with an empty field and enables once there is text', async () => {
    const { container, textarea } = mount();
    expect(btn(container, 'Send')!.disabled).toBe(true);
    await type(textarea, 'hello');
    expect(btn(container, 'Send')!.disabled).toBe(false);
  });

  it('stays disabled for whitespace-only input', async () => {
    const { container, textarea } = mount();
    await type(textarea, '   ');
    expect(btn(container, 'Send')!.disabled).toBe(true);
  });

  it('sends the trimmed text and clears the field', async () => {
    const onSend = vi.fn();
    const { container, textarea } = mount({ onSend });
    await type(textarea, '  hi there  ');
    await fireEvent.click(btn(container, 'Send')!);
    expect(onSend).toHaveBeenCalledWith('hi there', [], null);
    expect(textarea.value).toBe('');
  });

  it('leaves the return key labelled as a return key', () => {
    // The hint is only read by a soft keyboard, and there the return key still
    // inserts a newline — Enter-to-send is the hardware-keyboard rule. Labelling
    // it "send" would promise the opposite of what it does on the one device
    // that can see the label.
    const { textarea } = mount();
    expect(textarea.getAttribute('enterkeyhint')).not.toBe('send');
  });

  it('sends on a bare Enter', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'hi');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).toHaveBeenCalledWith('hi', [], null);
    // The newline must not also be inserted.
    expect(notPrevented).toBe(false);
  });

  it('leaves Shift+Enter a newline', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'first line');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true });

    expect(onSend).not.toHaveBeenCalled();
    // Left to the browser — the default action is what inserts the newline.
    expect(notPrevented).toBe(true);
  });

  it('leaves Alt+Enter a newline too', async () => {
    // Only Shift is the documented newline chord, but a modified Enter is never
    // a plain send: the send path takes the unmodified key and the Cmd/Ctrl one.
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'first line');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter', altKey: true });

    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
  });

  it('leaves a bare Enter a newline on a soft keyboard', async () => {
    // There is no cheap Shift on a phone, and the send button is right there —
    // so the return key keeps inserting a newline, which is also what the
    // `enterkeyhint` above promises.
    softKeyboard(true);
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'first line');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
  });

  it('does not send on the Enter that closes an IME composition', async () => {
    // Committing a candidate is an Enter like any other from the DOM's point of
    // view, so sending on it would post a half-typed word for anyone typing
    // Japanese, Korean or Chinese.
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'にほん');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter', isComposing: true });

    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
  });

  it('does not send on the legacy 229 composition keydown either', async () => {
    // Some IMEs report the composing keydown as keyCode 229 with `isComposing`
    // unset, which is the shape the flag was introduced to replace.
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'にほん');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter', keyCode: 229 });

    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
  });

  it('does not send on the Enter WebKit reports after compositionend', async () => {
    // WebKit can dispatch `compositionend` *before* the keydown that confirmed
    // the candidate, which then carries neither mark — so the event alone
    // cannot answer the question and a short window after the event stands in.
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'にほん');
    await fireEvent.compositionEnd(textarea);

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
  });

  it('sends on the Enter after that window has passed', async () => {
    // The window is a stand-in for a missing flag, not a cooldown on the key.
    vi.useFakeTimers();
    try {
      const onSend = vi.fn();
      const { textarea } = mount({ onSend });
      await type(textarea, 'にほん');
      await fireEvent.compositionEnd(textarea);
      vi.advanceTimersByTime(IME_COMMIT_GRACE_MS);
      await fireEvent.keyDown(textarea, { key: 'Enter' });
      expect(onSend).toHaveBeenCalledWith('にほん', [], null);
    } finally {
      vi.useRealTimers();
    }
  });

  it('sends on Enter while a turn is running, for the queue to take', async () => {
    // The mode gate used to refuse every keyboard send while a turn ran,
    // because a second `runTurn` in one room shares an echo buffer whose drain
    // releases the first turn's frames before its task id exists. ISSUE-238
    // serializes into that same entry point instead of refusing at the key, so
    // the key does what it says and the store queues what it produced.
    const onSend = vi.fn();
    const { textarea } = mount({ onSend, busy: true, queueing: true, onCancel: () => {} });
    await type(textarea, 'let me in');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).toHaveBeenCalledWith('let me in', [], null);
    expect(notPrevented).toBe(false);
  });

  it('leaves Enter a newline when the queue has no room left', async () => {
    // The one refusal that replaced the mode gate. What a refusal must never
    // do is eat the key — the send key silently doing nothing is the failure
    // the user has no way to read — so it falls through to the browser, which
    // writes the newline, and the reason is on screen beside the field.
    const onSend = vi.fn();
    const { container, textarea } = mount({
      onSend,
      busy: true,
      queueing: true,
      queueFull: true,
      onCancel: () => {},
    });
    await type(textarea, 'the eleventh');

    const notPrevented = await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).not.toHaveBeenCalled();
    expect(notPrevented).toBe(true);
    expect(textarea.value).toBe('the eleventh');
    expect(container.querySelector('.notice-row')?.textContent).toContain(
      'Too many messages waiting to send',
    );
  });

  it('does not send on Enter while a voice message is recording', async () => {
    // The send button is deliberately swapped out for Discard/Finish here and
    // the field sits under an opaque overlay, so a key that sent would post
    // text the user cannot see and strand the memo in the next message.
    enableMic();
    const onSend = vi.fn();
    const { container, textarea } = mount({ onSend });
    await type(textarea, 'hold on');
    await fireEvent.click(btn(container, 'Record voice message')!);
    await tick();
    await tick();
    expect(btn(container, 'Finish recording')).toBeTruthy();

    await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).not.toHaveBeenCalled();
  });

  it('does not send on Enter while an attachment is still uploading', async () => {
    // The file is part of this message. Sending without it posts the text alone
    // and leaves the chip in the composer for whatever is typed next, which
    // reads as the file having been sent.
    let release: (v: unknown) => void = () => {};
    upload.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const onSend = vi.fn();
    const { container, textarea } = mount({ onSend });
    await type(textarea, 'see attached');
    const input = picker(container, 'file');
    Object.defineProperty(input, 'files', { value: [sizedFile('a.png', 10)] });
    await fireEvent.change(input);
    await tick();

    await fireEvent.keyDown(textarea, { key: 'Enter' });

    expect(onSend).not.toHaveBeenCalled();
    // And the button agrees with the key, rather than the two disagreeing about
    // whether the message is ready.
    expect(btn(container, 'Send')!.disabled).toBe(true);

    release({ path: 'inbox/a.png', name: 'a.png', size: 10 });
    await tick();
    await tick();
    expect(btn(container, 'Send')!.disabled).toBe(false);
  });

  it('sends on Cmd+Enter', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('hi', [], null);
  });

  it('sends on Ctrl+Enter', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend });
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true });
    expect(onSend).toHaveBeenCalledWith('hi', [], null);
  });

  it('drops the keyboard after a send on a touch device', async () => {
    // The reply arrives behind the keyboard otherwise, and getting it out of
    // the way was a second deliberate gesture every time.
    softKeyboard(true);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(document.activeElement).not.toBe(textarea);
  });

  it('drops it after the send button too, not just the return key', async () => {
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.click(btn(container, 'Send')!);
    expect(document.activeElement).not.toBe(textarea);
  });

  it('holds the keyboard up while a tap on send is still resolving', async () => {
    // The two-tap send. iOS takes focus off the field when a button takes the
    // tap, and the keyboard leaving reflows the composer down out from under
    // the finger — so the click that follows is hit-tested against the new
    // layout and lands on nothing. The first tap dismissed the keyboard and
    // sent nothing; the send needed a second one. Suppressing the default
    // focus shift keeps the field focused through the click, and submit()
    // drops the keyboard itself once the message has actually gone.
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');

    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    btn(container, 'Send')!.dispatchEvent(down);

    expect(down.defaultPrevented).toBe(true);
    expect(document.activeElement).toBe(textarea);
  });

  it('holds it up for the other composer tools too', async () => {
    // Same reflow, and the attach sheet or the mic would open against a moving
    // target for the same reason.
    enableMic();
    softKeyboard(true);
    const { container, textarea } = mount();
    textarea.focus();

    for (const label of ['Attach file', 'Record voice message']) {
      const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
      btn(container, label)!.dispatchEvent(down);
      expect(down.defaultPrevented).toBe(true);
    }
    expect(document.activeElement).toBe(textarea);
  });

  it('leaves a tap on the field itself alone', async () => {
    // Only the buttons suppress the focus shift. The textarea needs its own
    // mousedown default — that is what places the caret.
    softKeyboard(true);
    const { textarea } = mount();

    const down = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    textarea.dispatchEvent(down);

    expect(down.defaultPrevented).toBe(false);
  });

  it('keeps focus where there is a hardware keyboard', async () => {
    // On a desktop the next message is typed straight away, and re-focusing is
    // a mouse trip the user did not ask for.
    softKeyboard(false);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true });
    expect(document.activeElement).toBe(textarea);
  });

  it('keeps focus when Enter did not send', async () => {
    softKeyboard(true);
    const { textarea } = mount();
    textarea.focus();
    await type(textarea, 'hi');
    await fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true });
    expect(document.activeElement).toBe(textarea);
  });

  it('adds a stop control beside Send while a task is running', () => {
    const onCancel = vi.fn();
    const { container } = mount({ busy: true, onCancel });
    expect(btn(container, 'Send')).toBeTruthy();
    expect(btn(container, 'Stop')).toBeTruthy();
  });

  it('keeps the same Send element when a turn starts under it', async () => {
    const onCancel = vi.fn();
    const { container, rerender } = mount({ busy: false, onCancel });
    const before = btn(container, 'Send');
    await rerender({ busy: true, onCancel });
    // iOS re-hit-tests when it delivers a tap's synthesized click, so a Send
    // that was destroyed and rebuilt — or merely moved — would hand the tap to
    // whatever took its place. Stop is what comes and goes, to Send's left.
    expect(btn(container, 'Send')).toBe(before);
    expect(btn(container, 'Stop')).not.toBe(before);
  });

  it('has no mode left to flip, so a duplicate tap still sends', async () => {
    // What replaced `MODE_FLIP_GUARD_MS`. A tap can be delivered twice (a
    // compat mouse event after the touch) and the second delivery lands after
    // `busy` has flipped — which, on one two-mode control, read as the
    // opposite command and cancelled the task it had just started. One button
    // per meaning is what makes that unrepresentable, and it costs no window
    // in which a genuine second tap is dropped.
    const onSend = vi.fn();
    const onCancel = vi.fn();
    const { container, textarea, rerender } = mount({ onSend, onCancel, busy: false });
    await type(textarea, 'hi');
    const control = btn(container, 'Send')!;
    await fireEvent.click(control);
    expect(onSend).toHaveBeenCalledTimes(1);

    // The parent flips busy inside that same click; the duplicate delivery
    // lands on the same element, which still says Send.
    await rerender({ onSend, onCancel, busy: true, queueing: true });
    await type(textarea, 'again');
    await fireEvent.click(control);
    expect(onCancel).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledTimes(2);
  });

  it('allows two deliberate sends in quick succession', async () => {
    const onSend = vi.fn();
    const { container, textarea } = mount({ onSend });
    await type(textarea, 'one');
    await fireEvent.click(btn(container, 'Send')!);
    await type(textarea, 'two');
    await fireEvent.click(btn(container, 'Send')!);
    expect(onSend).toHaveBeenCalledTimes(2);
  });
});

describe('Composer layout', () => {
  it('keeps one row while the text fits and drops the controls below once it wraps', async () => {
    const { container, textarea } = mount();
    const row = container.querySelector('.composer-row')!;
    expect(row.classList.contains('multiline')).toBe(false);

    fakeScrollHeight = 80;
    await type(textarea, 'a long message that wraps over several lines');
    expect(row.classList.contains('multiline')).toBe(true);

    // Sending empties the field, so the bar collapses back to one row.
    fakeScrollHeight = 20;
    await fireEvent.click(btn(container, 'Send')!);
    await tick();
    await Promise.resolve();
    await tick();
    expect(row.classList.contains('multiline')).toBe(false);
  });

  it('stays wrapped when the text only fits because wrapping widened the field', async () => {
    // The regression: wrapping moves the controls off the field's row, so the
    // field gets wider and the text no longer wraps — measured naively that
    // flips the layout back, and the field alternates one/two rows on every
    // keystroke. Here the text wraps at the single-row width (80) but fits at
    // the full width (20), which is exactly that boundary.
    narrowScrollHeight = 80;
    fakeScrollHeight = 20;

    const { container, textarea } = mount();
    const row = container.querySelector('.composer-row')!;

    for (const value of ['aaaa', 'aaaab', 'aaaabc', 'aaaabcd']) {
      await type(textarea, value);
      expect(row.classList.contains('multiline')).toBe(true);
    }
  });
});

describe('Composer voice message', () => {
  it('hides the mic when the browser cannot record', () => {
    const { container } = mount();
    expect(btn(container, 'Record voice message')).toBeNull();
  });

  it('records, uploads the audio as an attachment, and sends it with the message', async () => {
    enableMic();
    const onSend = vi.fn();
    const { container } = mount({ onSend });

    await fireEvent.click(btn(container, 'Record voice message')!);
    await tick();
    await Promise.resolve();
    await tick();

    // Recording state: readout up, mic replaced by discard + finish.
    expect(container.querySelector('.rec-overlay')).toBeTruthy();
    expect(btn(container, 'Record voice message')).toBeNull();
    expect(btn(container, 'Discard recording')).toBeTruthy();

    await fireEvent.click(btn(container, 'Finish recording')!);
    await tick();
    await Promise.resolve();
    await tick();

    expect(upload).toHaveBeenCalledTimes(1);
    expect((upload.mock.calls[0][0] as File).name).toMatch(/\.webm$/);
    expect(container.querySelector('.rec-overlay')).toBeNull();

    // An audio-only message is sendable with no text at all.
    expect(btn(container, 'Send')!.disabled).toBe(false);
    await fireEvent.click(btn(container, 'Send')!);
    // The upload's whole answer is handed on, `workspace_path` included, so the
    // chip can link at the file endpoint without a second round trip.
    expect(onSend).toHaveBeenCalledWith(
      '',
      [{ path: 'inbox/voice.webm', name: 'voice.webm', size: 12 }],
      null,
    );
  });

  it('discarding a recording uploads nothing', async () => {
    enableMic();
    const { container } = mount();
    await fireEvent.click(btn(container, 'Record voice message')!);
    await tick();
    await Promise.resolve();
    await tick();
    await fireEvent.click(btn(container, 'Discard recording')!);
    await tick();
    expect(upload).not.toHaveBeenCalled();
    expect(btn(container, 'Record voice message')).toBeTruthy();
  });
});

describe('Composer drafts', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('restores the draft held under its key', async () => {
    writeDraft('room:3', 'half a thought');
    const { textarea } = mount({ draftKey: 'room:3' });
    await tick();
    expect(textarea.value).toBe('half a thought');
    // Restored text is sendable straight away — no keystroke needed first.
    expect(btn(document.body, 'Send')!.disabled).toBe(false);
  });

  it('survives leaving the page and coming back', async () => {
    // The reported symptom, end to end: type, go and look at another section,
    // come back. Client-side navigation destroys the whole chat page, so the
    // round trip is an unmount and a fresh mount rather than anything subtler.
    const first = mount({ draftKey: 'room:3' });
    await type(first.textarea, 'back in a moment');
    first.unmount();

    const { textarea } = mount({ draftKey: 'room:3' });
    await tick();
    expect(textarea.value).toBe('back in a moment');
  });

  it('holds what was typed when it unmounts', async () => {
    const { textarea, unmount } = mount({ draftKey: 'room:3' });
    await type(textarea, 'mid-sentence');
    unmount();
    expect(readDraft('room:3')).toBe('mid-sentence');
  });

  it('holds what was typed once the debounce elapses', async () => {
    vi.useFakeTimers();
    try {
      const { textarea } = mount({ draftKey: 'room:3' });
      await type(textarea, 'still typing');
      expect(readDraft('room:3')).toBe('');
      vi.advanceTimersByTime(DRAFT_SAVE_DEBOUNCE_MS);
      expect(readDraft('room:3')).toBe('still typing');
    } finally {
      vi.useRealTimers();
    }
  });

  it('persists nothing without a key', async () => {
    const { textarea, unmount } = mount();
    await type(textarea, 'no room to attribute this to');
    unmount();
    expect(localStorage.getItem(DRAFT_STORAGE_KEY)).toBeNull();
  });

  it('swaps drafts when the key changes, without leaking one into the other', async () => {
    writeDraft('room:9', 'for B');
    const { textarea, rerender } = mount({ draftKey: 'room:3' });
    await type(textarea, 'for A');

    await rerender({ draftKey: 'room:9' });
    await tick();
    expect(textarea.value).toBe('for B');
    expect(readDraft('room:3')).toBe('for A');

    await rerender({ draftKey: 'room:3' });
    await tick();
    expect(textarea.value).toBe('for A');
  });

  it('empties the field for a room that has no draft', async () => {
    const { textarea, rerender } = mount({ draftKey: 'room:3' });
    await type(textarea, 'for A');

    await rerender({ draftKey: 'room:9' });
    await tick();
    expect(textarea.value).toBe('');
  });

  it('carries text typed before a room resolved into that room', async () => {
    // The composer renders while the room list is still in flight, so the key
    // arrives after the first keystroke could. There is no earlier room to
    // attribute the text to, so it belongs to the one that lands.
    const { textarea, rerender } = mount({ draftKey: null });
    await type(textarea, 'typed during the load');

    await rerender({ draftKey: 'room:3' });
    await tick();
    expect(textarea.value).toBe('typed during the load');
    expect(readDraft('room:3')).toBe('typed during the load');
  });

  it('never lets a keystroke during the load replace the room own draft', async () => {
    // The destructive half of the case above. The field is empty and unkeyed
    // for two round trips on the way back to /chat, so a returning user sees
    // no draft, types, and the draft they came back for would be overwritten
    // by that keystroke — with no undo and no notice.
    writeDraft('room:3', 'what they came back for');
    const { textarea, rerender } = mount({ draftKey: null });
    await type(textarea, 'x');

    await rerender({ draftKey: 'room:3' });
    await tick();
    expect(textarea.value).toBe('what they came back for');
    expect(readDraft('room:3')).toBe('what they came back for');
  });

  it('clears the field when the room goes away under it', async () => {
    // Deleting or archiving the open room drops the key while leaving the view
    // on 'room', so the composer stays mounted. Leaving the departed room's
    // text in the field is how it reaches the next room.
    const { textarea, rerender } = mount({ draftKey: 'room:3' });
    await type(textarea, 'for A');

    await rerender({ draftKey: null });
    await tick();
    expect(textarea.value).toBe('');
    expect(readDraft('room:3')).toBe('for A');
  });

  it('does not carry a departed room text into the next room', async () => {
    // The leak the whole mechanism exists to stop, by the route that does not
    // pass through a room-to-room switch: delete the open room, pick another.
    writeDraft('room:9', 'B own draft');
    const { textarea, rerender } = mount({ draftKey: 'room:3' });
    await type(textarea, 'PRIVATE to room 3');

    await rerender({ draftKey: null });
    await tick();
    await rerender({ draftKey: 'room:9' });
    await tick();

    expect(textarea.value).toBe('B own draft');
    expect(readDraft('room:9')).toBe('B own draft');
  });

  it('does not carry attachments into the next room', async () => {
    // Not drafted, but they must not ride along either: re-picking a file
    // costs a tap, posting one to the wrong room does not undo.
    upload.mockResolvedValue({ path: 'inbox/secret.txt', name: 'secret.txt', size: 4 });
    const { container, rerender } = mount({ draftKey: 'room:3' });
    const input = picker(container, 'file');
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'secret.txt')] });
    await fireEvent.change(input);
    await tick();
    expect(container.querySelectorAll('.attach-chip')).toHaveLength(1);

    await rerender({ draftKey: 'room:9' });
    await tick();
    expect(container.querySelectorAll('.attach-chip')).toHaveLength(0);
  });

  it('holds what was typed when the app is backgrounded', async () => {
    // A backgrounded iOS app fires visibilitychange and may never fire
    // anything again; pagehide does not cover it and the destroy flush is
    // never reached.
    const { textarea } = mount({ draftKey: 'room:3' });
    await type(textarea, 'still typing');
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'hidden',
    });
    await fireEvent(document, new Event('visibilitychange'));
    expect(readDraft('room:3')).toBe('still typing');
  });

  it('has a visible document again, so the test above cannot leak', () => {
    expect(document.visibilityState).toBe('visible');
  });

  it('settles only the room the ack names, leaving another send in flight alone', async () => {
    // Two sends can be open at once — leaving a room resets the session's
    // status to idle, which un-gates the composer — and a settle signal with
    // no message identity let whichever acked first drop the other's draft
    // while its own POST was still open, destroying the only copy of it.
    const { textarea, rerender } = mount({ draftKey: 'room:3', sendSettled: { n: 0, key: null } });
    await type(textarea, 'message for three');
    await fireEvent.click(btn(document.body, 'Send')!);

    await rerender({ draftKey: 'room:9' });
    await tick();
    await type(textarea, 'message for nine');
    await fireEvent.click(btn(document.body, 'Send')!);

    // Room 3's POST was slower, so its ack lands second — with room 9 on screen.
    await rerender({ sendSettled: { n: 1, key: 'room:3' } });
    await tick();

    expect(readDraft('room:3')).toBe('');
    expect(readDraft('room:9')).toBe('message for nine');
  });

  it('does not put an unacked message back in the field on returning to its room', async () => {
    // `submit` stores the text under the key so a reload can recover it, which
    // made coming back to the room show a message the user had already sent as
    // unsent text — one Enter from sending it twice — and the ack then read
    // that as newly typed and declined to clear it, so it stayed.
    const { textarea, rerender } = mount({ draftKey: 'room:3', sendSettled: { n: 0, key: null } });
    await type(textarea, 'already gone');
    await fireEvent.click(btn(document.body, 'Send')!);

    await rerender({ draftKey: 'room:9' });
    await tick();
    await rerender({ draftKey: 'room:3' });
    await tick();
    expect(textarea.value).toBe('');

    await rerender({ sendSettled: { n: 1, key: 'room:3' } });
    await tick();
    expect(readDraft('room:3')).toBe('');
  });

  it('sends on the send chord while a turn is running', async () => {
    // The chord was the one entry into a send that did not consult the mode
    // gate, so holding it started a second turn in a room that already had one.
    // It consults `wouldSend()` like every other send path, and what that
    // refuses now is a full queue rather than a running turn.
    const onSend = vi.fn();
    const { textarea } = mount({ onSend, busy: true, queueing: true, onCancel: () => {} });
    await type(textarea, 'let me in');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('let me in', [], null);
  });

  it('still sends on the chord when the button would have sent', async () => {
    const onSend = vi.fn();
    const { textarea } = mount({ onSend, busy: false, onCancel: () => {} });
    await type(textarea, 'go');
    await fireEvent.keyDown(textarea, { key: 'Enter', metaKey: true });
    expect(onSend).toHaveBeenCalledWith('go', [], null);
  });

  it('holds the draft through the send and drops it on the ack', async () => {
    // The drop waits for the backend, because until then the stored draft is
    // the only copy of this text that survives a reload — the failed row does
    // not. Dropping it on submit is what made an outage take the message away
    // as well as report it unsent.
    const onSend = vi.fn();
    const { textarea, rerender } = mount({
      draftKey: 'room:3',
      onSend,
      sendSettled: { n: 0, key: null },
    });
    await type(textarea, 'going out');

    await fireEvent.click(btn(document.body, 'Send')!);
    expect(onSend).toHaveBeenCalled();
    expect(textarea.value).toBe('');
    expect(readDraft('room:3')).toBe('going out');

    await rerender({ sendSettled: { n: 1, key: 'room:3' } });
    await tick();
    expect(readDraft('room:3')).toBe('');
  });

  it('drops the draft on submit when the message is being queued', async () => {
    // A queued message is written to its own persisted queue the moment it is
    // accepted, so the queue — not the draft — is the copy that survives a
    // reload. Holding it here as well would put a second copy of the same text
    // in a slot that fits one: several queued messages in a room would fight
    // over it, and the first ack would drop the last one's stored copy.
    const onSend = vi.fn();
    const { textarea, unmount } = mount({
      draftKey: 'room:3',
      onSend,
      busy: true,
      queueing: true,
      onCancel: () => {},
      sendSettled: { n: 0, key: null },
    });
    await type(textarea, 'the next thing');

    await fireEvent.click(btn(document.body, 'Send')!);
    expect(onSend).toHaveBeenCalledWith('the next thing', [], null);
    expect(textarea.value).toBe('');
    // No ack is waited for, and none is coming for a whole turn.
    expect(readDraft('room:3')).toBe('');

    // And the departure flush does not put it back.
    unmount();
    expect(readDraft('room:3')).toBe('');
  });

  it('holds the draft of an ordinary send whose own POST flips busy under it', async () => {
    // The real parent flips `busy` — and with it `queueing` — synchronously
    // inside `onSend`: the store's `send()` reaches `status.set('sending')`
    // before it returns. Reading the prop *after* that call therefore reported
    // every ordinary send from an idle room as queued, cleared the draft on
    // submit and wrote no `unsettledSends` entry — leaving `settleDraft` and
    // `switchDraft`'s in-flight refusal both inert, with the durability
    // mechanism switched off and nothing saying so.
    //
    // A bare `vi.fn()` cannot model that, which is why every other draft test
    // in this file passes either way. `rerender` assigns into the same
    // reactive props object synchronously, so this reproduces the flip.
    const utils: { rerender?: (p: Record<string, unknown>) => Promise<void> } = {};
    const onSend = vi.fn(() => {
      void utils.rerender?.({ busy: true, queueing: true, onCancel: () => {} });
    });
    const rendered = mount({
      draftKey: 'room:3',
      onSend,
      busy: false,
      queueing: false,
      sendSettled: { n: 0, key: null },
    });
    utils.rerender = rendered.rerender;
    await type(rendered.textarea, 'going out');

    await fireEvent.click(btn(document.body, 'Send')!);
    expect(onSend).toHaveBeenCalled();
    // Held, not dropped: this send has no durable copy until the ack.
    expect(readDraft('room:3')).toBe('going out');

    // And the ack still recognises it, which is the half that needs the map
    // entry the queued branch would have skipped.
    await rendered.rerender({ sendSettled: { n: 1, key: 'room:3' } });
    await tick();
    expect(readDraft('room:3')).toBe('');
  });

  it('leaves an unacked send its draft when a second message is queued behind it', async () => {
    // The one case where an enqueue must not clear the slot: it belongs to a
    // message whose POST is still open, whose stored copy is the only one that
    // survives a reload if that send then fails.
    const { textarea, rerender } = mount({
      draftKey: 'room:3',
      sendSettled: { n: 0, key: null },
    });
    await type(textarea, 'the message that matters');
    await fireEvent.click(btn(document.body, 'Send')!);
    expect(readDraft('room:3')).toBe('the message that matters');

    await rerender({ busy: true, queueing: true, onCancel: () => {} });
    await tick();
    await type(textarea, 'and then this one');
    await fireEvent.click(btn(document.body, 'Send')!);

    expect(readDraft('room:3')).toBe('the message that matters');
  });

  it('settles a send whose text was too long to store whole', async () => {
    // The draft store caps one draft, so what it holds for an over-long
    // message is a prefix. Both tests that recognise the message again compare
    // against the stored copy, so recording the full text instead left the ack
    // unable to match it: the draft was never cleared, and coming back to the
    // room restored an already-sent message into the field as unsent text.
    const { textarea, rerender } = mount({
      draftKey: 'room:3',
      sendSettled: { n: 0, key: null },
    });
    await type(textarea, 'y'.repeat(MAX_DRAFT_CHARS + 500));

    await fireEvent.click(btn(document.body, 'Send')!);
    expect(readDraft('room:3').length).toBe(MAX_DRAFT_CHARS);

    // Away from the room, so the ack has to judge by what is stored rather
    // than by the field — which is the comparison the clamp breaks.
    await rerender({ draftKey: 'room:9' });
    await tick();
    await rerender({ sendSettled: { n: 1, key: 'room:3' } });
    await tick();
    expect(readDraft('room:3')).toBe('');
  });

  it('does not restore an over-long message that is still in flight', async () => {
    const { textarea, rerender } = mount({
      draftKey: 'room:3',
      sendSettled: { n: 0, key: null },
    });
    await type(textarea, 'z'.repeat(MAX_DRAFT_CHARS + 500));
    await fireEvent.click(btn(document.body, 'Send')!);

    await rerender({ draftKey: 'room:9' });
    await tick();
    await rerender({ draftKey: 'room:3' });
    await tick();
    expect(textarea.value).toBe('');
  });

  it('holds the draft through a send that never lands', async () => {
    // No ack, so no drop: the text is still recoverable after a reload, which
    // is the one thing that outlives the failed row in the transcript.
    const { textarea, unmount } = mount({ draftKey: 'room:3', sendSettled: { n: 0, key: null } });
    await type(textarea, 'never arrived');
    await fireEvent.click(btn(document.body, 'Send')!);

    // Every departure flushes the field, and the field is empty now — so this
    // is also the assertion that the flush knows to leave the draft alone.
    unmount();
    expect(readDraft('room:3')).toBe('never arrived');
  });

  it('leaves a draft typed after the send alone when the ack lands', async () => {
    const { textarea, rerender, unmount } = mount({
      draftKey: 'room:3',
      sendSettled: { n: 0, key: null },
    });
    await type(textarea, 'first message');
    await fireEvent.click(btn(document.body, 'Send')!);
    await type(textarea, 'second, still being written');

    await rerender({ sendSettled: { n: 1, key: 'room:3' } });
    await tick();

    // The ack settles the message that was sent, not the one being typed: the
    // field keeps it, and the draft is not cleared out from under it.
    expect(textarea.value).toBe('second, still being written');
    expect(readDraft('room:3')).not.toBe('');
    // The new text is still behind its debounce, so flush it the way leaving
    // the page would and check what lands.
    unmount();
    expect(readDraft('room:3')).toBe('second, still being written');
  });

  it('carries text typed after the room went away into the next room', async () => {
    // The field was cleared of the departed room's text, so what follows is
    // unattributed in exactly the way the page-load case is. A one-shot flag
    // used to suppress this carry for the life of the component.
    const { textarea, rerender } = mount({ draftKey: 'room:3' });
    await type(textarea, 'PRIVATE to room 3');

    await rerender({ draftKey: null });
    await tick();
    expect(textarea.value).toBe('');

    await type(textarea, 'typed with nowhere to go');
    await rerender({ draftKey: 'room:9' });
    await tick();

    expect(textarea.value).toBe('typed with nowhere to go');
    expect(readDraft('room:9')).toBe('typed with nowhere to go');
    // And the departed room's own text stayed where it was.
    expect(readDraft('room:3')).toBe('PRIVATE to room 3');
  });
});

describe('Composer uploads across a room switch', () => {
  it('drops a chip whose upload resolved after the room changed', async () => {
    // `upload` is async and appends with no notion of where it started, while
    // the room switch clears the list synchronously and cannot see a promise
    // in flight — so a large file picked in one room landed its chip in the
    // next, and sending from there posted it to the wrong room.
    let release: (v: unknown) => void = () => {};
    upload.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const { container, rerender } = mount({ draftKey: 'room:3' });
    const input = picker(container, 'file');
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'big.bin')] });
    await fireEvent.change(input);
    await tick();

    await rerender({ draftKey: 'room:9' });
    await tick();

    release({ path: 'inbox/big.bin', name: 'big.bin', size: 4 });
    await tick();
    await tick();

    expect(container.querySelectorAll('.attach-chip')).toHaveLength(0);
  });

  it('does not leave the upload counter stuck after a stale resolve', async () => {
    // The switch resets the counter to 0, so a stale decrement would drive it
    // negative and the composer would report an upload in progress for good.
    let release: (v: unknown) => void = () => {};
    upload.mockReturnValue(
      new Promise((res) => {
        release = res;
      }),
    );
    const { container, rerender } = mount({ draftKey: 'room:3' });
    const input = picker(container, 'file');
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'big.bin')] });
    await fireEvent.change(input);
    await tick();
    expect(container.querySelector('.uploading')).not.toBeNull();

    await rerender({ draftKey: 'room:9' });
    await tick();
    release({ path: 'inbox/big.bin', name: 'big.bin', size: 4 });
    await tick();
    await tick();

    expect(container.querySelector('.uploading')).toBeNull();
  });

  it('does not show a stale upload error in the room that inherited the composer', async () => {
    let reject: (e: unknown) => void = () => {};
    upload.mockReturnValue(
      new Promise((_res, rej) => {
        reject = rej;
      }),
    );
    const { container, rerender } = mount({ draftKey: 'room:3' });
    const input = picker(container, 'file');
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'big.bin')] });
    await fireEvent.change(input);
    await tick();

    await rerender({ draftKey: 'room:9' });
    await tick();
    reject(new Error('upload blew up'));
    await tick();
    await tick();

    expect(container.textContent).not.toContain('upload blew up');
  });
});

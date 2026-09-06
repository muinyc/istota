/**
 * The chat page's half of not jumping while an image loads.
 *
 * `lib/markdown/index.test.ts` pins that a `#w=&h=` hint becomes `width` /
 * `height` on the tag; this pins that the attributes survive the page's own
 * render, and that the scroller re-pins when an image without a hint finishes
 * loading underneath a reader who is at the bottom. The two halves fail
 * independently: a renderer that emits the attributes into a page that strips
 * them reserves nothing, and neither does a page that never listens for the
 * load it cannot predict.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';

// Same mock, same reason, as `imageLightbox.svelte.test.ts`: the renderer
// admits an image only for a src starting `${base}/api/chat/files?`.
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));

vi.mock('$lib/stores/chat', async () => {
  const { writable } = await import('svelte/store');
  const stores: Record<string, unknown> = {
    rooms: writable([]),
    activeRoomId: writable(null),
    messages: writable([]),
    status: writable('idle'),
    loaded: writable(true),
    hasMore: writable(false),
    loadingOlder: writable(false),
    view: writable('room'),
    scrollTarget: writable(null),
    sendSettled: writable({ n: 0, token: null }),
    sendReturned: writable({ n: 0, token: null, text: '', attachments: [] }),
    outboundDrafts: writable([]),
    externalTurnDisplay: writable('full'),
    offlineTranscript: writable(false),
    queuedCounts: writable({}),
  };
  const session = new Proxy(stores, {
    get: (target, key: string) => (target[key] ??= vi.fn(async () => undefined)),
  });
  return { getChatSession: () => session };
});

import { getChatSession } from '$lib/stores/chat';
import Page from './+page.svelte';
import Harness from '$lib/currentUserHarness.test.svelte';
import type { User } from '$lib/api';

const person: User = {
  username: 'alice',
  display_name: 'Alice',
  bot_name: 'Istota',
  is_admin: false,
  features: {
    chat: true,
    feeds: false,
    location: false,
    money: false,
    health: false,
    briefings: false,
    google_workspace: false,
    google_workspace_enabled: false,
    admin: false,
  },
};

const SRC = '/istota/api/chat/files?path=%2FUsers%2Falice%2Fistota%2Fradar.png';

/** The mocked session is module-lived, so each test seeds every field it reads. */
function seedTranscript(body: string) {
  const session = getChatSession() as unknown as Record<string, { set: (v: unknown) => void }>;
  session.rooms.set([
    {
      id: 1,
      token: 't1',
      name: 'Room 1',
      archived: false,
      created_at: '',
      updated_at: '',
      origin: 'web',
      unread_count: 0,
    },
  ]);
  session.activeRoomId.set(1);
  session.view.set('room');
  session.queuedCounts.set({});
  session.offlineTranscript.set(false);
  session.messages.set([
    {
      cid: 1,
      role: 'assistant',
      text: '',
      segments: [{ kind: 'text', id: 't0', text: body, settled: true }],
      streaming: false,
    },
  ]);
}

/**
 * jsdom lays nothing out, so the scroller's metrics are 0 and every offset
 * reads as the bottom. These are the numbers the page's arithmetic needs:
 * a scroller taller than its viewport, with a settable `scrollTop`.
 */
function measure(list: HTMLElement, { scrollHeight = 900, clientHeight = 300 } = {}) {
  Object.defineProperty(list, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(list, 'clientHeight', { value: clientHeight, configurable: true });
}

const renderPage = () => render(Harness, { component: Page, user: person });
const transcript = (container: HTMLElement) =>
  container.querySelector<HTMLElement>('[role="log"]')!;

beforeEach(() => {
  // A fetch that never settles moves no state under the test.
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('an image the model sized', () => {
  it('reaches the DOM with both attributes, so the box is reserved', async () => {
    seedTranscript(`![Radar](${SRC}#w=1439&h=812)`);
    const { container } = renderPage();
    const img = await waitFor(() => {
      const el = container.querySelector<HTMLImageElement>('img.md-image');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(img.getAttribute('width')).toBe('1439');
    expect(img.getAttribute('height')).toBe('812');
  });
});

describe('an image the model did not size', () => {
  it('re-pins the transcript when it loads under a reader at the bottom', async () => {
    seedTranscript(`![Radar](${SRC})`);
    const { container } = renderPage();
    const img = await waitFor(() => {
      const el = container.querySelector<HTMLImageElement>('img.md-image');
      expect(el).not.toBeNull();
      return el!;
    });
    const list = transcript(container);
    measure(list);
    // The mount's own pin already ran against an unmeasured scroller; start
    // from the top so the re-pin is the only thing that could move it.
    list.scrollTop = 0;

    await fireEvent.load(img);

    expect(list.scrollTop).toBe(900);
  });

  it('leaves the viewport alone when the reader has scrolled up', async () => {
    seedTranscript(`![Radar](${SRC})`);
    const { container } = renderPage();
    const img = await waitFor(() => {
      const el = container.querySelector<HTMLImageElement>('img.md-image');
      expect(el).not.toBeNull();
      return el!;
    });
    const list = transcript(container);
    measure(list);
    list.scrollTop = 100;
    // The latch is only resampled on a real scroll, which is what reading back
    // through history produces.
    await fireEvent.scroll(list);

    await fireEvent.load(img);

    expect(list.scrollTop).toBe(100);
  });
});

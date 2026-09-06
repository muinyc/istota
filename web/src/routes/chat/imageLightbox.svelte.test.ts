/**
 * The chat page's half of click-to-zoom.
 *
 * `Message.image.svelte.test.ts` pins what the message reports; this pins that
 * the page listens. The two halves fail independently and only one of them is
 * visible from the component: a page that never passes `onImageOpen`, or mounts
 * no `<Lightbox>`, leaves every image inert with all of that file green.
 *
 * One instance for the page, as on the feeds route, and rendered
 * unconditionally — the component's own `{#if}` is inside it, and its gesture
 * teardown assumes it is never unmounted between two opens.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent, waitFor } from '@testing-library/svelte';

// The renderer admits an image only for a src starting `${base}/api/chat/files?`,
// so the page renders no `<img>` at all under the `''` the vitest stub answers
// with. Same mock, same reason, as `lib/markdown/index.test.ts`.
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

function seedTranscript() {
  // The mocked session is module-lived, so every field a test reads has to be
  // set by every test that runs — see `offlineBanner.svelte.test.ts`.
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
      segments: [{ kind: 'text', id: 't0', text: `![Radar](${SRC})`, settled: true }],
      streaming: false,
    },
  ]);
}

const renderPage = () => render(Harness, { component: Page, user: person });

/** The lightbox's own image, which exists only while it is open. */
const lightboxImg = () => document.querySelector<HTMLImageElement>('.lightbox img');

beforeEach(() => {
  // Every request the page makes reports to the connectivity store, so a stub
  // that answered would move state under the test; one that never settles
  // reports nothing.
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise<Response>(() => {})),
  );
  seedTranscript();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('click to zoom, on the page', () => {
  it('draws the transcript image and opens nothing until it is clicked', async () => {
    const { container } = renderPage();
    await waitFor(() => expect(container.querySelector('img.md-image')).not.toBeNull());
    expect(lightboxImg()).toBeNull();
  });

  it('opens the page lightbox on the image that was clicked', async () => {
    const { container } = renderPage();
    await waitFor(() => expect(container.querySelector('img.md-image')).not.toBeNull());

    await fireEvent.click(container.querySelector('img.md-image')!);

    await waitFor(() => expect(lightboxImg()).not.toBeNull());
    expect(lightboxImg()!.getAttribute('src')).toBe(SRC);
  });

  it('closes again, so the overlay is not a trap', async () => {
    // `onClose` is the page's, and a no-op there would leave the lightbox over
    // the transcript with the component's own Escape handler doing nothing.
    const { container } = renderPage();
    await waitFor(() => expect(container.querySelector('img.md-image')).not.toBeNull());
    await fireEvent.click(container.querySelector('img.md-image')!);
    await waitFor(() => expect(lightboxImg()).not.toBeNull());

    await fireEvent.keyDown(document, { key: 'Escape' });

    await waitFor(() => expect(lightboxImg()).toBeNull());
  });
});

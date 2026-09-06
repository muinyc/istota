import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, cleanup, fireEvent } from '@testing-library/svelte';

// The deployment's real base path. `vitest-stubs/app-paths.ts` answers `''`,
// and the markdown renderer only draws an `<img>` for a src starting
// `${base}/api/chat/files?` — so under the stub every URL below would have to
// be written without the prefix the production renderer actually matches, and
// the test would stop resembling what ships. Same mock, same reason, as
// `lib/markdown/index.test.ts`.
vi.mock('$app/paths', () => ({ base: '/istota', assets: '' }));

import { SUBSTANTIAL_TEXT_CHARS, type ChatMessage, type Segment } from '$lib/stores/segments';
import Message from './Message.svelte';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const noop = () => {};
const base = { onConfirm: noop, onReject: noop };

/** A chat-files URL, which is the only shape the renderer draws an image for. */
function fileUrl(name: string): string {
  return `/istota/api/chat/files?path=%2FUsers%2Falice%2Fistota%2F${name}`;
}

function text(id: string, body: string): Segment {
  return { kind: 'text', id, text: body, settled: true };
}

/** An assistant turn whose body is the given markdown blocks, in order. */
function assistant(...blocks: string[]): ChatMessage {
  return {
    cid: 1,
    role: 'assistant',
    text: '',
    segments: blocks.map((b, i) => text(`t${i}`, b)),
    streaming: false,
  };
}

function images(container: HTMLElement): HTMLImageElement[] {
  return Array.from(container.querySelectorAll('img.md-image'));
}

/** Capture what `onImageOpen` was called with, in call order. */
function recorder() {
  const calls: Array<{ images: string[]; index: number }> = [];
  return {
    calls,
    onImageOpen: (imgs: string[], index: number) => calls.push({ images: imgs, index }),
  };
}

describe('inline image → lightbox', () => {
  it('opens the clicked image with this message’s list and its index', async () => {
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(`Here it is.\n\n![Radar](${fileUrl('radar.png')})`),
      onImageOpen: rec.onImageOpen,
    });

    const img = images(container)[0];
    expect(img).toBeTruthy();

    await fireEvent.click(img);

    expect(rec.calls).toEqual([{ images: [fileUrl('radar.png')], index: 0 }]);
  });

  it('positions the lightbox on the image that was clicked, not the first', async () => {
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(`![One](${fileUrl('one.png')})\n\n![Two](${fileUrl('two.png')})`),
      onImageOpen: rec.onImageOpen,
    });

    await fireEvent.click(images(container)[1]);

    expect(rec.calls).toEqual([{ images: [fileUrl('one.png'), fileUrl('two.png')], index: 1 }]);
  });

  it('collects across the whole message, not just the block that was clicked', async () => {
    // The body renders as several groups — the delegation sits on the message's
    // content column rather than on one block, and this is what says so.
    const rec = recorder();
    // Only the *final* text segment renders unconditionally; an earlier one has
    // to clear the substantial-prose threshold or `renderGroups` drops it as
    // lead-in narration, and the message would come out with one image in it.
    const lead = 'x'.repeat(SUBSTANTIAL_TEXT_CHARS);
    const { container } = render(Message, {
      ...base,
      message: assistant(
        `${lead}\n\n![One](${fileUrl('one.png')})`,
        `Second block.\n\n![Two](${fileUrl('two.png')})`,
      ),
      onImageOpen: rec.onImageOpen,
    });

    expect(images(container)).toHaveLength(2);
    await fireEvent.click(images(container)[0]);

    expect(rec.calls).toEqual([{ images: [fileUrl('one.png'), fileUrl('two.png')], index: 0 }]);
  });

  it('ignores a click on the text around the image', async () => {
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(`Here it is.\n\n![Radar](${fileUrl('radar.png')})`),
      onImageOpen: rec.onImageOpen,
    });

    const body = container.querySelector('.body.markdown');
    expect(body).toBeTruthy();
    await fireEvent.click(body!);
    await fireEvent.click(container.querySelector('.content')!);

    expect(rec.calls).toEqual([]);
  });

  it('does not throw when mounted without the handler', async () => {
    const { container } = render(Message, {
      ...base,
      message: assistant(`![Radar](${fileUrl('radar.png')})`),
    });

    const img = images(container)[0];
    expect(img).toBeTruthy();
    await expect(fireEvent.click(img)).resolves.not.toThrow();
  });

  it('leaves an image the model wrapped in a link to the anchor', async () => {
    // `[![alt](ours)](https://elsewhere)`: the anchor navigates, so firing the
    // lightbox as well means two things happen at once. The renderer withholds
    // the button affordance from this shape (`md-image-linked`); the handler
    // has to withhold the click, and that is what this pins.
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(`[![Radar](${fileUrl('radar.png')})](https://example.invalid/page)`),
      onImageOpen: rec.onImageOpen,
    });

    const img = images(container)[0];
    expect(img).toBeTruthy();
    expect(img.className).toContain('md-image-linked');
    expect(img.closest('a')).toBeTruthy();

    await fireEvent.click(img);

    expect(rec.calls).toEqual([]);
  });

  it('keeps a linked image out of the list, so the index still lines up', async () => {
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(
        `[![Linked](${fileUrl('linked.png')})](https://example.invalid/page)\n\n` +
          `![Plain](${fileUrl('plain.png')})`,
      ),
      onImageOpen: rec.onImageOpen,
    });

    expect(images(container)).toHaveLength(2);
    const plain = images(container).find((el) => !el.closest('a'))!;
    await fireEvent.click(plain);

    expect(rec.calls).toEqual([{ images: [fileUrl('plain.png')], index: 0 }]);
  });
});

describe('inline image keyboard activation', () => {
  // The renderer emits `role="button"` / `tabindex="0"` precisely so this path
  // exists: the body is `{@html}`, so there is no element to make focusable
  // from the Svelte side.
  it('announces an admitted image as a focusable button', () => {
    const { container } = render(Message, {
      ...base,
      message: assistant(`![Radar](${fileUrl('radar.png')})`),
      onImageOpen: noop,
    });

    const img = images(container)[0];
    expect(img.getAttribute('role')).toBe('button');
    expect(img.getAttribute('tabindex')).toBe('0');
  });

  it.each(['Enter', ' '])('opens on %s, and claims the key', async (key) => {
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(`![Radar](${fileUrl('radar.png')})`),
      onImageOpen: rec.onImageOpen,
    });

    // `fireEvent` hands back `dispatchEvent`'s answer, so `false` is
    // `preventDefault` — which is what stops Space scrolling the transcript out
    // from under the image it is about to open.
    const notCancelled = await fireEvent.keyDown(images(container)[0], { key });

    expect(rec.calls).toEqual([{ images: [fileUrl('radar.png')], index: 0 }]);
    expect(notCancelled).toBe(false);
  });

  it('leaves other keys alone', async () => {
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(`![Radar](${fileUrl('radar.png')})`),
      onImageOpen: rec.onImageOpen,
    });

    await fireEvent.keyDown(images(container)[0], { key: 'a' });
    await fireEvent.keyDown(images(container)[0], { key: 'Tab' });

    expect(rec.calls).toEqual([]);
  });

  it('does not claim the key on a linked image', async () => {
    // Enter is how a keyboard follows the anchor this image sits in, so this
    // asserts the key is left alone as well as the lightbox left shut. It is
    // also what pins the refusal in `eligibleImage` on its own: dropping only
    // the list filter would still leave the call unmade here, because the
    // clicked image would not be found in a list it was excluded from.
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: assistant(`[![Radar](${fileUrl('radar.png')})](https://example.invalid/page)`),
      onImageOpen: rec.onImageOpen,
    });

    const notCancelled = await fireEvent.keyDown(images(container)[0], { key: 'Enter' });

    expect(rec.calls).toEqual([]);
    expect(notCancelled).toBe(true);
  });
});

describe('inline image in a command row', () => {
  // A `!command` body goes through the same renderer as an answer, so an
  // admitted image there announces itself as a button too. The delegation is on
  // that row's content column for the same reason it is on a turn's.
  it('opens from a system row', async () => {
    const rec = recorder();
    const { container } = render(Message, {
      ...base,
      message: {
        cid: 2,
        role: 'system',
        text: `![Chart](${fileUrl('chart.png')})`,
        segments: [],
        streaming: false,
      } as ChatMessage,
      onImageOpen: rec.onImageOpen,
    });

    const img = images(container)[0];
    expect(img).toBeTruthy();
    await fireEvent.click(img);

    expect(rec.calls).toEqual([{ images: [fileUrl('chart.png')], index: 0 }]);
  });
});

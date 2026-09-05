/**
 * The lightbox's gesture wiring: what a finger on the image does to the
 * transform, and which gestures still close the overlay.
 *
 * jsdom lays nothing out, so every rect here is zero and the translation
 * clamps to 0 on both axes — the scale is what these assert on. The
 * translation arithmetic is covered in `imageZoom.test.ts`, against real
 * numbers.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, cleanup, fireEvent, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import { GHOST_CLICK_MS, GHOST_CLICK_SLOP } from '$lib/imageZoom';

import Lightbox from './Lightbox.svelte';

const IMAGES = ['https://example.com/a.jpg', 'https://example.com/b.jpg'];

/** Whatever the overlay is covering, planted by `pageBeneath`. */
const planted: HTMLElement[] = [];

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  planted.splice(0).forEach((el) => el.remove());
  cleanup();
});

function open(onClose = vi.fn()) {
  const { container, rerender } = render(Lightbox, { images: IMAGES, index: 0, onClose });
  return {
    onClose,
    container,
    img: container.querySelector('img') as HTMLImageElement,
    backdrop: container.querySelector('.lightbox') as HTMLElement,
    /**
     * What the only caller does when `onClose` fires: the index goes null and
     * the overlay stops rendering. Whatever it was covering is then what a
     * click at that point is hit-tested against.
     */
    close: () => rerender({ images: IMAGES, index: null, onClose }),
  };
}

/**
 * jsdom implements no PointerEvent, and the repo's existing gesture tests
 * (`platform/input.test.ts`) drive pointer handlers with a MouseEvent. Pinch
 * needs the pointer id as well, which no MouseEvent init carries.
 */
function pointer(
  type: string,
  id: number,
  x: number,
  y: number,
  pointerType = 'touch',
  button = 0,
): MouseEvent {
  const e = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: x,
    clientY: y,
    button,
  });
  Object.defineProperty(e, 'pointerId', { value: id });
  Object.defineProperty(e, 'pointerType', { value: pointerType });
  return e;
}

/**
 * Two fingers going from `from` px apart to `to` px apart, around one center.
 *
 * Awaits a tick, like every gesture here: Svelte batches the DOM write, so the
 * transform an assertion reads is the pre-gesture one without it. Nothing about
 * that is specific to pointer events — it is why `fireEvent` is awaited too.
 */
async function pinch(el: Element, from: number, to: number): Promise<void> {
  el.dispatchEvent(pointer('pointerdown', 1, 500 - from / 2, 400));
  el.dispatchEvent(pointer('pointerdown', 2, 500 + from / 2, 400));
  el.dispatchEvent(pointer('pointermove', 1, 500 - to / 2, 400));
  el.dispatchEvent(pointer('pointermove', 2, 500 + to / 2, 400));
  el.dispatchEvent(pointer('pointerup', 1, 500 - to / 2, 400));
  el.dispatchEvent(pointer('pointerup', 2, 500 + to / 2, 400));
  await tick();
}

/** The `scale(N)` factor currently on the image, or 1 when it carries none. */
function scaleOf(img: HTMLElement): number {
  const match = /scale\(([\d.]+)\)/.exec(img.style.transform);
  return match ? Number(match[1]) : 1;
}

async function tap(el: Element, x = 500, y = 400, id = 1, pointerType = 'touch'): Promise<void> {
  el.dispatchEvent(pointer('pointerdown', id, x, y, pointerType));
  el.dispatchEvent(pointer('pointerup', id, x, y, pointerType));
  await tick();
}

/**
 * Run out anything that might have been armed on a timer.
 *
 * Nothing here arms one any more, which is exactly why every negative
 * assertion below calls this: a `not.toHaveBeenCalled` that never moves the
 * clock passes just as well against a dismissal deferred behind a double-tap
 * window, which is what this component used to do.
 */
function settle(): void {
  vi.advanceTimersByTime(1000);
}

/** A press and release of a mouse button that is not the primary one. */
async function buttonPress(el: Element, button: number, x = 500, y = 400): Promise<void> {
  el.dispatchEvent(pointer('pointerdown', 1, x, y, 'mouse', button));
  el.dispatchEvent(pointer('pointerup', 1, x, y, 'mouse', button));
  await tick();
}

/**
 * A page the overlay is covering, with one thing on it that can be activated.
 *
 * A `<button>` rather than a bare div: what the lightbox actually covers is a
 * feed card and a reader backdrop, both of which act on a click.
 */
function pageBeneath(): { el: HTMLElement; activated: ReturnType<typeof vi.fn> } {
  const el = document.createElement('button');
  const activated = vi.fn();
  el.addEventListener('click', activated);
  document.body.appendChild(el);
  planted.push(el);
  return { el, activated };
}

/**
 * The `click` a tap leaves behind.
 *
 * jsdom synthesizes none from pointer events, so the second half of what a
 * browser does with a tap is written out: a `click` at the point the finger
 * lifted from, dispatched once the overlay has had its chance to close and be
 * removed. Its target is therefore whatever was underneath.
 */
function clickAt(el: Element, x: number, y: number): void {
  el.dispatchEvent(
    new MouseEvent('click', { bubbles: true, cancelable: true, clientX: x, clientY: y }),
  );
}

describe('pinch to zoom', () => {
  it('scales the image up when two fingers spread', async () => {
    const { img } = open();
    await pinch(img, 100, 300);
    expect(scaleOf(img)).toBeCloseTo(3, 5);
  });

  it('scales back down when they pinch in, and stops at the fit scale', async () => {
    const { img } = open();
    await pinch(img, 100, 300);
    expect(scaleOf(img)).toBeCloseTo(3, 5);

    await pinch(img, 300, 200);
    expect(scaleOf(img)).toBeCloseTo(2, 5);

    // Past the fit scale the clamp takes over rather than shrinking further.
    await pinch(img, 300, 30);
    expect(scaleOf(img)).toBe(1);
  });

  it('sees a pinch with one finger on the backdrop', async () => {
    // A fitted image is letterboxed, so one finger of a real pinch routinely
    // lands beside it rather than on it.
    const { img, backdrop } = open();
    backdrop.dispatchEvent(pointer('pointerdown', 1, 450, 400));
    img.dispatchEvent(pointer('pointerdown', 2, 550, 400));
    backdrop.dispatchEvent(pointer('pointermove', 1, 350, 400));
    img.dispatchEvent(pointer('pointermove', 2, 650, 400));
    await tick();
    expect(scaleOf(img)).toBeCloseTo(3, 5);
  });

  it('does not close the overlay when a pinch ends', async () => {
    const { onClose, img } = open();
    await pinch(img, 100, 300);
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('tapping', () => {
  it('closes on a single tap at the fit scale, out of the pointerup', async () => {
    // No timer is run out here, and that is the assertion: while a tap on the
    // image had to sit out a double-tap window before it could mean anything,
    // this dismissal was a visible third of a second late — against a tap on
    // the backdrop, three lines down, which was always instant.
    const { onClose, img } = open();
    await tap(img);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('closes on a tap on the backdrop', async () => {
    const { onClose, backdrop } = open();
    await tap(backdrop);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('does not close on a single tap while zoomed', async () => {
    const { onClose, img } = open();
    await pinch(img, 100, 300);
    await tap(img);
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on a tap on the backdrop even while zoomed', async () => {
    // The one way out that a zoomed image does not take away, so it has to
    // survive the guard above it.
    const { onClose, img, backdrop } = open();
    await pinch(img, 100, 300);
    await tap(backdrop);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('does not zoom on a second tap', async () => {
    // Double-tap-to-zoom is gone; pinch, the trackpad and the zoom keys are
    // what zoom now, and a second tap is just another dismissal.
    const { img } = open();
    await tap(img);
    await tap(img);
    expect(scaleOf(img)).toBe(1);
  });

  it('does not close after a drag that ends on the image', async () => {
    const { onClose, img } = open();
    await pinch(img, 100, 300);
    img.dispatchEvent(pointer('pointerdown', 3, 500, 400));
    img.dispatchEvent(pointer('pointermove', 3, 560, 430));
    img.dispatchEvent(pointer('pointerup', 3, 560, 430));
    await tick();
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not close on a drag at the fit scale', async () => {
    // A swipe across a fitted image has nothing to pan, but it is still a
    // swipe: delivering it as a tap dismissed the overlay under the finger.
    const { onClose, img } = open();
    img.dispatchEvent(pointer('pointerdown', 1, 400, 400));
    img.dispatchEvent(pointer('pointermove', 1, 520, 400));
    img.dispatchEvent(pointer('pointerup', 1, 520, 400));
    await tick();
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not close on a two-finger tap that never moves', async () => {
    const { onClose, img } = open();
    img.dispatchEvent(pointer('pointerdown', 1, 460, 400));
    img.dispatchEvent(pointer('pointerdown', 2, 540, 400));
    img.dispatchEvent(pointer('pointerup', 1, 460, 400));
    img.dispatchEvent(pointer('pointerup', 2, 540, 400));
    await tick();
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('leaves the transform alone when a pinch is cancelled', async () => {
    const { onClose, img } = open();
    await pinch(img, 100, 300);
    const held = scaleOf(img);

    img.dispatchEvent(pointer('pointerdown', 5, 480, 400));
    img.dispatchEvent(pointer('pointerdown', 6, 520, 400));
    img.dispatchEvent(pointer('pointercancel', 5, 480, 400));
    img.dispatchEvent(pointer('pointercancel', 6, 520, 400));
    await tick();

    expect(scaleOf(img)).toBe(held);
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('a button that is not the primary one', () => {
  it('does not close when the image is right-clicked', async () => {
    // The browser raises its own menu on the image — save it, copy it, open it
    // in a tab — and the overlay dismissing out from under that menu is what
    // made saving an image impossible.
    const { onClose, img } = open();
    await buttonPress(img, 2);
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not close when the backdrop is right-clicked', async () => {
    const { onClose, backdrop } = open();
    await buttonPress(backdrop, 2);
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not close on a middle click', async () => {
    const { onClose, img } = open();
    await buttonPress(img, 1);
    settle();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('does not end a gesture the primary button began', async () => {
    // A mouse reuses one pointer id across its buttons, so a right button
    // released while the left is held reaches `onPointerUp` carrying the id of
    // the press that is still down.
    const { onClose, img } = open();
    img.dispatchEvent(pointer('pointerdown', 1, 500, 400, 'mouse'));
    img.dispatchEvent(pointer('pointerup', 1, 500, 400, 'mouse', 2));
    await tick();
    settle();
    expect(onClose).not.toHaveBeenCalled();

    img.dispatchEvent(pointer('pointerup', 1, 500, 400, 'mouse'));
    await tick();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('leaves a later primary tap working', async () => {
    const { onClose, img } = open();
    await buttonPress(img, 2);
    await tap(img);
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe('the click a tap leaves behind', () => {
  it('does not reach the page under a backdrop tap that dismissed the overlay', async () => {
    const { el, activated } = pageBeneath();
    const { onClose, backdrop, container, close } = open();

    await tap(backdrop, 500, 400);
    expect(onClose).toHaveBeenCalledOnce();
    await close();
    // The condition the defect needs: the overlay is gone by the time the
    // click is dispatched, so nothing of it is left to absorb the click.
    expect(container.querySelector('.lightbox')).toBeNull();

    clickAt(el, 500, 400);
    expect(activated).not.toHaveBeenCalled();
  });

  it('does not reach it after a tap on the image either', async () => {
    // Both halves of the gesture dismiss out of the same `pointerup` now, so
    // both strand a click over whatever the overlay was covering.
    const { el, activated } = pageBeneath();
    const { img, close } = open();

    await tap(img, 500, 400);
    await close();

    clickAt(el, 500, 400);
    expect(activated).not.toHaveBeenCalled();
  });

  it('is still claimed after the overlay has dropped its gesture state', async () => {
    // Closing runs `resetGestures` — through the caller's index going null,
    // and through the nav path used here — before the click lands. A claim
    // cleared there would hand the click straight back to what was covered.
    const { el, activated } = pageBeneath();
    const { backdrop } = open();

    await tap(backdrop, 500, 400);
    await fireEvent.keyDown(document, { key: 'ArrowRight' });

    clickAt(el, 500, 400);
    expect(activated).not.toHaveBeenCalled();
  });

  it('claims nothing for a mouse, whose click cannot be handed on', async () => {
    // A mouse click is targeted from the press and the release, both of which
    // landed on the overlay, so it never reaches what the overlay covered.
    // Claiming there would leave behind a claim no click ever spends.
    const { el, activated } = pageBeneath();
    const { onClose, backdrop, close } = open();

    await tap(backdrop, 500, 400, 1, 'mouse');
    expect(onClose).toHaveBeenCalledOnce();
    await close();

    clickAt(el, 500, 400);
    expect(activated).toHaveBeenCalledOnce();
  });

  it('takes one that lands within the slop of the release', async () => {
    const { el, activated } = pageBeneath();
    const { backdrop, close } = open();

    await tap(backdrop, 500, 400);
    await close();

    clickAt(el, 500 + GHOST_CLICK_SLOP, 400);
    expect(activated).not.toHaveBeenCalled();
  });

  it('leaves one that lands past the slop alone', async () => {
    const { el, activated } = pageBeneath();
    const { backdrop, close } = open();

    await tap(backdrop, 500, 400);
    await close();

    clickAt(el, 500 + GHOST_CLICK_SLOP + 1, 400);
    expect(activated).toHaveBeenCalledOnce();
  });

  it('leaves one that arrives too late alone', async () => {
    const { el, activated } = pageBeneath();
    const { backdrop, close } = open();

    await tap(backdrop, 500, 400);
    await close();
    vi.advanceTimersByTime(GHOST_CLICK_MS + 1);

    clickAt(el, 500, 400);
    expect(activated).toHaveBeenCalledOnce();
  });

  it('leaves the nav buttons working while a claim is outstanding', async () => {
    const { backdrop, container } = open();

    await tap(backdrop, 500, 400);
    await fireEvent.click(screen.getByLabelText('Next image'));

    expect((container.querySelector('img') as HTMLImageElement).src).toBe(IMAGES[1]);
  });
});

describe('navigation', () => {
  it('drops the zoom when moving to the next image', async () => {
    const { img, container } = open();
    await pinch(img, 100, 300);
    expect(scaleOf(img)).toBeGreaterThan(1);
    await fireEvent.click(screen.getByLabelText('Next image'));
    expect(scaleOf(container.querySelector('img') as HTMLElement)).toBe(1);
  });

  it('drops the zoom when the keyboard moves to the previous image', async () => {
    const { img } = open();
    await pinch(img, 100, 300);
    await fireEvent.keyDown(document, { key: 'ArrowLeft' });
    expect(scaleOf(img)).toBe(1);
  });
});

describe('keyboard', () => {
  it('zooms in and out on the zoom keys', async () => {
    const { img } = open();
    await fireEvent.keyDown(document, { key: '+' });
    const zoomedIn = scaleOf(img);
    expect(zoomedIn).toBeGreaterThan(1);

    await fireEvent.keyDown(document, { key: '+' });
    expect(scaleOf(img)).toBeGreaterThan(zoomedIn);

    await fireEvent.keyDown(document, { key: '-' });
    expect(scaleOf(img)).toBeCloseTo(zoomedIn, 5);
  });

  it('goes back to the fit scale on 0', async () => {
    const { img } = open();
    await fireEvent.keyDown(document, { key: '+' });
    await fireEvent.keyDown(document, { key: '0' });
    expect(scaleOf(img)).toBe(1);
  });

  it('still closes on Escape while zoomed', async () => {
    const { onClose, img } = open();
    await pinch(img, 100, 300);
    await fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledOnce();
  });
});

describe('trackpad and mouse wheel', () => {
  it('zooms on a pinch-to-zoom wheel event', async () => {
    const { img } = open();
    img.dispatchEvent(
      new WheelEvent('wheel', { bubbles: true, cancelable: true, ctrlKey: true, deltaY: -100 }),
    );
    await tick();
    expect(scaleOf(img)).toBeGreaterThan(1);
  });

  it('leaves a plain scroll alone', async () => {
    const { img } = open();
    img.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: -100 }));
    await tick();
    expect(scaleOf(img)).toBe(1);
  });
});

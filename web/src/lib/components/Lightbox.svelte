<script lang="ts">
  import { onMount } from 'svelte';
  import { ChevronLeft, ChevronRight } from 'lucide-svelte';
  import {
    FIT,
    GHOST_CLICK_MS,
    GHOST_CLICK_SLOP,
    KEY_ZOOM_STEP,
    TAP_SLOP,
    WHEEL_IDLE_MS,
    applyGesture,
    distance,
    isZoomed,
    midpoint,
    normalizeWheelDelta,
    wheelScaleFactor,
    type Geometry,
    type Point,
    type ZoomState,
  } from '$lib/imageZoom';

  let {
    images = [],
    index = null,
    onClose,
  }: {
    images?: string[];
    index?: number | null;
    onClose: () => void;
  } = $props();

  let current = $state<number | null>(null);
  let zoom = $state<ZoomState>(FIT);
  let smooth = $state(false);
  let imgEl = $state<HTMLImageElement | null>(null);
  let backdropEl = $state<HTMLDivElement | null>(null);

  $effect(() => {
    current = index;
    resetGestures();
  });

  /**
   * The live pointers, and the state the current gesture started from.
   *
   * Plain variables rather than `$state`: nothing renders from them, and a
   * pinch writes them on every pointermove.
   */
  const pointers = new Map<number, Point>();
  let gestureStart: { state: ZoomState; anchor: Point; spread: number } | null = null;
  /** Has this gesture stopped being a tap — by travelling, or by multi-touch? */
  let dragged = false;
  /** Where the gesture began, which is what decides what a tap ending it means. */
  let tapTarget: 'image' | 'backdrop' = 'backdrop';
  /**
   * How a tap ended: where the pointer came up, and whether the browser will
   * follow it with a click of its own.
   *
   * The point is the release rather than the last position in `pointers`,
   * because that is where a synthesized click is put — and the two are not the
   * same, since nothing records the `pointerup` coordinates into that map.
   *
   * `synthesized` is false for a mouse, whose click is targeted from the press
   * and the release, both of which landed on this overlay; it can therefore
   * never be delivered to what the overlay was covering, however fast the
   * overlay goes. Measured in Chrome: closing on `pointerup` with a mouse
   * dispatches no click at all. So there is nothing to claim on that path, and
   * claiming anyway would only leave a claim behind that no click ever spends.
   */
  interface Release {
    point: Point;
    synthesized: boolean;
  }

  /**
   * The click of the tap that dismissed the overlay, before it has arrived.
   *
   * Deliberately **not** cleared by `resetGestures`, unlike everything above
   * it: this belongs to the input device's own timeline rather than to the
   * image on screen, and the close it follows runs `resetGestures` before the
   * click lands. Clearing it there would hand the click straight back to
   * whatever the overlay was covering, which is the whole defect.
   */
  let claimedTap: { at: number; point: Point } | null = null;
  let wheelGesture: { state: ZoomState; anchor: Point; factor: number } | null = null;
  let wheelIdleTimer: ReturnType<typeof setTimeout> | null = null;

  /**
   * Drop every scrap of gesture state and go back to the fit scale.
   *
   * Everything here has to be cleared together, and this is the only place that
   * does it, because the component is **never unmounted**: the one caller
   * (`routes/feeds/+page.svelte`) renders `<Lightbox>` unconditionally and the
   * `{#if}` that hides it is inside. So the teardown in `onMount` does not run
   * between an open and the next one, and anything left behind here outlives
   * the image it belonged to — a wheel burst carries its start state onto the
   * next image, and a pointer id that never got its `pointerup` makes every
   * later tap look like half a pinch.
   */
  function resetGestures() {
    zoom = FIT;
    smooth = false;
    endWheelGesture();
    pointers.clear();
    gestureStart = null;
    dragged = false;
  }

  function endWheelGesture() {
    wheelGesture = null;
    if (wheelIdleTimer === null) return;
    clearTimeout(wheelIdleTimer);
    wheelIdleTimer = null;
  }

  function next(e?: Event) {
    e?.stopPropagation();
    if (current === null || images.length === 0) return;
    current = (current + 1) % images.length;
    resetGestures();
  }

  function prev(e?: Event) {
    e?.stopPropagation();
    if (current === null || images.length === 0) return;
    current = (current - 1 + images.length) % images.length;
    resetGestures();
  }

  function handleKeydown(e: KeyboardEvent) {
    if (current === null) return;
    if (e.key === 'Escape') onClose();
    else if (e.key === 'ArrowRight') next();
    else if (e.key === 'ArrowLeft') prev();
    else if (e.key === '+' || e.key === '=') zoomFromCenter(KEY_ZOOM_STEP);
    else if (e.key === '-' || e.key === '_') zoomFromCenter(1 / KEY_ZOOM_STEP);
    else if (e.key === '0') resetGestures();
    else return;
    // Only the zoom keys are ours alone. Escape and the arrows are also bound
    // by the feed reader underneath, on `document`, and it registers first — so
    // claiming them here would suppress the browser's default without stopping
    // the other handler, which is a promise this cannot keep.
    if (e.key !== 'Escape' && e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') {
      e.preventDefault();
    }
  }

  /**
   * Zoom without a pointer to anchor on.
   *
   * The keyboard is the only way in for anyone who cannot pinch or hold a
   * mouse, and it is also what the `+` / `-` / `0` keys do in every image
   * viewer. The centre of the overlay stands in for the anchor, which is where
   * a gesture-less zoom is expected to grow from.
   */
  function zoomFromCenter(factor: number) {
    const geom = geometry();
    if (!geom) return;
    smooth = true;
    zoom = applyGesture(
      zoom,
      { scaleFactor: factor, anchorStart: geom.origin, anchorNow: geom.origin },
      geom,
    );
  }

  /**
   * Eat the `click` of the tap that dismissed the overlay.
   *
   * A tap on the backdrop closes at once, so by the time the browser dispatches
   * that tap's `click` the overlay is gone and the click is hit-tested against
   * whatever it was covering: the feed card behind opens, or the reader
   * underneath closes, from a tap aimed at a darkened area precisely because
   * there was nothing there to hit. Both halves of the gesture do this, since
   * a tap on the image dismisses out of the same `pointerup`.
   *
   * Bounded by time and by distance rather than swallowing the next click
   * outright, so the only one it can take is the one belonging to the tap that
   * claimed it — a deliberate tap elsewhere, or a later one, goes through.
   * Both halves are needed: `stopPropagation` covers what a handler would do
   * with the click, `preventDefault` covers what the browser would do with it
   * on its own, such as following a link.
   *
   * It covers the `click` alone. A touch also synthesizes `mousedown` and
   * `mouseup` at the same point, so anything underneath that acts on those —
   * nothing on this route today — still hears them, and a button underneath
   * can still take focus.
   */
  function swallowClaimedClick(e: MouseEvent) {
    if (claimedTap === null) return;
    const { at, point } = claimedTap;
    claimedTap = null;
    if (Date.now() - at > GHOST_CLICK_MS) return;
    if (distance(point, { x: e.clientX, y: e.clientY }) > GHOST_CLICK_SLOP) return;
    e.preventDefault();
    e.stopPropagation();
  }

  onMount(() => {
    document.addEventListener('keydown', handleKeydown);
    // Capture, so it runs before the handlers on whatever is underneath.
    document.addEventListener('click', swallowClaimedClick, true);
    return () => {
      document.removeEventListener('keydown', handleKeydown);
      document.removeEventListener('click', swallowClaimedClick, true);
      endWheelGesture();
    };
  });

  /**
   * What the transform arithmetic needs, measured off the live elements.
   *
   * Nothing here reads a transformed box. `offsetWidth`/`offsetHeight` are
   * layout sizes, so they are the fit size directly rather than a rect that has
   * to be divided by the current scale — which also means a measurement taken
   * while a `smooth` transition is still running is not the interpolated one.
   *
   * The backdrop is the box the image is centred in (`position: fixed`, inset
   * 0, flex-centred), so it gives both the origin and the viewport, and gives
   * them consistently. `window.innerHeight` would be neither: it reports the
   * *visual* viewport, which on iOS diverges from the layout viewport the image
   * is actually laid out in — the divergence this app already carries
   * `--app-height` to work around.
   */
  function geometry(): Geometry | null {
    if (!imgEl || !backdropEl) return null;
    const box = backdropEl.getBoundingClientRect();
    return {
      origin: { x: box.left + box.width / 2, y: box.top + box.height / 2 },
      fit: { width: imgEl.offsetWidth, height: imgEl.offsetHeight },
      viewport: { width: box.width, height: box.height },
    };
  }

  /** Restart the gesture from wherever the fingers are now. */
  function beginGesture() {
    const points = [...pointers.values()];
    if (points.length === 0) {
      gestureStart = null;
      return;
    }
    gestureStart =
      points.length >= 2
        ? {
            state: zoom,
            anchor: midpoint(points[0], points[1]),
            spread: distance(points[0], points[1]),
          }
        : { state: zoom, anchor: points[0], spread: 0 };
  }

  /** The nav buttons keep their own behaviour; a tap on one is not a gesture. */
  function isControl(target: EventTarget | null): boolean {
    return target instanceof Element && target.closest('.controls') !== null;
  }

  function onPointerDown(e: PointerEvent) {
    if (isControl(e.target)) return;
    // A right-click is a request for the browser's own menu on the image —
    // save it, copy it, open it in a tab — and a middle or side click asks for
    // something else again. None of them is a tap, and letting one begin a
    // gesture dismissed the overlay out from under the menu it had just
    // raised. Touch and pen report button 0, so this costs them nothing.
    if (e.button !== 0) return;
    endWheelGesture();
    smooth = false;
    backdropEl?.setPointerCapture?.(e.pointerId);
    if (pointers.size === 0) {
      dragged = false;
      tapTarget = e.target === imgEl ? 'image' : 'backdrop';
    } else {
      // A second finger means a pinch, and a pinch is never a tap — even one
      // that lands and lifts without moving.
      dragged = true;
    }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    beginGesture();
  }

  function onPointerMove(e: PointerEvent) {
    if (!pointers.has(e.pointerId) || !gestureStart) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });

    const geom = geometry();
    if (!geom) return;
    const points = [...pointers.values()];

    if (points.length >= 2) {
      // A pinch is a scale and a two-finger pan at once: the midpoint carries
      // the pan, the spread carries the scale.
      const spread = distance(points[0], points[1]);
      dragged = true;
      zoom = applyGesture(
        gestureStart.state,
        {
          scaleFactor: gestureStart.spread > 0 ? spread / gestureStart.spread : 1,
          anchorStart: gestureStart.anchor,
          anchorNow: midpoint(points[0], points[1]),
        },
        geom,
      );
      e.preventDefault();
      return;
    }

    // Travel ends the tap whatever the scale. Only the panning is conditional:
    // at the fit scale there is nowhere to pan to, but a swipe is still a
    // swipe and must not be delivered as a tap — which at the fit scale is
    // what closes the overlay.
    if (distance(gestureStart.anchor, points[0]) > TAP_SLOP) dragged = true;
    if (!isZoomed(gestureStart.state)) return;
    zoom = applyGesture(
      gestureStart.state,
      { scaleFactor: 1, anchorStart: gestureStart.anchor, anchorNow: points[0] },
      geom,
    );
    e.preventDefault();
  }

  function onPointerUp(e: PointerEvent) {
    // The same filter as the press, and needed separately: a mouse reuses one
    // pointer id across its buttons, so a right button released while the left
    // is held would otherwise end a gesture it never began. `pointercancel`
    // reports no button at all, so it is matched on type rather than value.
    if (e.type === 'pointerup' && e.button !== 0) return;
    const lifted = pointers.get(e.pointerId);
    pointers.delete(e.pointerId);
    if (backdropEl?.hasPointerCapture?.(e.pointerId)) backdropEl.releasePointerCapture(e.pointerId);

    if (pointers.size > 0) {
      // One finger of a pinch lifted; carry on panning with what is left.
      beginGesture();
      return;
    }
    gestureStart = null;
    if (e.type === 'pointercancel' || dragged || !lifted) return;
    handleTap({
      point: { x: e.clientX, y: e.clientY },
      synthesized: e.pointerType !== 'mouse',
    });
  }

  /**
   * A tap, once it is known not to be a drag: dismiss, out of the `pointerup`
   * that ended it.
   *
   * There is no double tap on the image, and that is what lets this be
   * immediate. While there was one, the two gestures were identical until the
   * second tap either arrived or did not, so a tap on the image had to sit out
   * that window before it could mean anything — which the backdrop never did,
   * leaving one dismissal instant and the other visibly late. Pinch, the
   * trackpad and the zoom keys all still zoom, so nothing was lost by dropping
   * the gesture that made the wait necessary.
   *
   * The one exception is a tap on a zoomed image, which does nothing. A
   * stationary finger there is far likelier to be a misplaced one during an
   * inspection than a dismissal, and the ways out — pinching back to fit,
   * Escape, the backdrop where the image does not cover it — all remain. A tap
   * on the backdrop dismisses whatever the image is doing.
   */
  function handleTap(release: Release) {
    if (tapTarget === 'image' && isZoomed(zoom)) return;
    dismiss(release);
  }

  /**
   * Go, and claim the click the tap that dismissed us is about to leave behind.
   *
   * The claim is stamped here rather than where the tap landed, because the
   * window it opens is measured from the moment the overlay stops being able
   * to absorb its own click.
   *
   * Only a tap that actually dismisses claims anything. A tap that leaves the
   * overlay up strands no click: that one lands on the overlay itself and dies
   * there, and claiming it would only mean the overlay swallowing its own
   * clicks — which would make any future handler on the image or the backdrop
   * dead on the tap path alone.
   */
  function dismiss(release: Release) {
    claimedTap = release.synthesized ? { at: Date.now(), point: release.point } : null;
    onClose();
  }

  /**
   * A trackpad pinch arrives here rather than as pointer events, with `ctrlKey`
   * set by the platform. A plain scroll is left alone: over a modal it means
   * the page behind, not this image.
   *
   * The burst is treated as one gesture, with the factors multiplied and
   * applied to the state it started from, because `applyGesture` must not be
   * fed its own output — every call re-clamps, so accumulating frame by frame
   * would not come back to where it started after a pinch that ran the image
   * into an edge. A wheel has no finger to lift, so the gesture ends on idle.
   */
  function onWheel(e: WheelEvent) {
    if (!e.ctrlKey && !e.metaKey) return;
    const geom = geometry();
    if (!geom) return;

    const at = { x: e.clientX, y: e.clientY };
    if (!wheelGesture) wheelGesture = { state: zoom, anchor: at, factor: 1 };
    wheelGesture.factor *= wheelScaleFactor(
      normalizeWheelDelta(e.deltaY, e.deltaMode, geom.viewport.height),
    );

    smooth = false;
    zoom = applyGesture(
      wheelGesture.state,
      { scaleFactor: wheelGesture.factor, anchorStart: wheelGesture.anchor, anchorNow: at },
      geom,
    );
    e.preventDefault();

    if (wheelIdleTimer !== null) clearTimeout(wheelIdleTimer);
    wheelIdleTimer = setTimeout(endWheelGesture, WHEEL_IDLE_MS);
  }
</script>

{#if current !== null && images.length > 0}
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <div
    class="lightbox open"
    bind:this={backdropEl}
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerUp}
    onpointercancel={onPointerUp}
    onwheel={onWheel}
  >
    <img
      bind:this={imgEl}
      src={images[current]}
      alt=""
      draggable="false"
      class:zoomed={isZoomed(zoom)}
      style:transform="translate({zoom.x}px, {zoom.y}px) scale({zoom.scale})"
      style:transition={smooth ? 'transform 180ms ease-out' : 'none'}
    />
    {#if images.length > 1}
      <div class="controls">
        <button class="nav" onclick={prev} aria-label="Previous image">
          <ChevronLeft size={24} />
        </button>
        <div class="counter">{current + 1} / {images.length}</div>
        <button class="nav" onclick={next} aria-label="Next image">
          <ChevronRight size={24} />
        </button>
      </div>
    {/if}
  </div>
{/if}

<style>
  .lightbox {
    position: fixed;
    inset: 0;
    z-index: var(--z-lightbox);
    /* design-lint-allow: fixed chrome — the lightbox is a theme-invariant dark
       overlay over the image; darkening is the whole point of the surface. */
    background: rgba(0, 0, 0, 0.9);
    display: flex;
    justify-content: center;
    align-items: center;
    cursor: zoom-out;
    /* Clip the zoomed image to the overlay, so it cannot paint over the device
       insets or the control bar below it. */
    overflow: hidden;
    /* The gesture is ours, so the browser must not claim it first: without
       this, a pinch scrolls and zooms the page instead. It goes on the
       backdrop rather than the image because a pinch routinely puts one finger
       on the letterboxing beside a fitted image, and that finger has to reach
       the same handler — a gesture the backdrop does not see is delivered as a
       one-finger swipe on the image. */
    touch-action: none;
  }
  .lightbox img {
    max-width: 90vw;
    /* Leave room for the bottom control bar so it never covers the image, and
		   for the device insets — 5vh of slack is under the Dynamic Island's height
		   on a phone, so without this the top of a tall image sits behind it. */
    max-height: calc(90dvh - 4rem - var(--safe-top) - var(--safe-bottom));
    object-fit: contain;
    /* A drag across an image is a native image-drag on a desktop browser and a
       text selection on the way out of one; a long press in a WKWebView raises
       the system callout sheet. Each of the three interrupts a pan. */
    user-select: none;
    -webkit-user-drag: none;
    -webkit-touch-callout: none;
  }
  .lightbox img.zoomed {
    cursor: grab;
  }
  /* Full-bleed overlay, so its controls carry their own safe-area offsets. */
  .controls {
    position: absolute;
    bottom: max(1rem, var(--safe-bottom));
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: default;
  }
  .nav {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2.5rem;
    height: 2.5rem;
    border: none;
    border-radius: 50%;
    /* design-lint-allow-begin: fixed chrome — the lightbox is a theme-invariant
       dark overlay over the image, so its controls stay dark-on-white in both
       themes. */
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    /* design-lint-allow-end */
    cursor: pointer;
    transition: background 120ms;
  }
  .nav:hover {
    /* design-lint-allow: fixed chrome — see .nav above. */
    background: rgba(0, 0, 0, 0.75);
  }
  .counter {
    padding: var(--space-1) var(--space-2);
    /* design-lint-allow-begin: fixed chrome — see .nav above. */
    background: rgba(0, 0, 0, 0.5);
    color: #fff;
    /* design-lint-allow-end */
    font-size: 0.8rem;
    border-radius: var(--radius-sm);
    pointer-events: none;
    white-space: nowrap;
  }
</style>

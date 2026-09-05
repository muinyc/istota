/**
 * The zoom transform behind the lightbox.
 *
 * All of it is arithmetic over screen coordinates: a caller measures the
 * element and the viewport, hands the numbers in, and gets the next transform
 * back. Nothing here touches the DOM, which is what makes the interesting part
 * — anchoring a pinch so the content under the fingers stays put, and holding
 * the image against the viewport edge — testable without a layout engine.
 *
 * The state maps onto `transform: translate(x, y) scale(scale)` with the
 * default centre origin. CSS applies that right to left, so the translation is
 * in the parent's own pixels rather than scaled ones, and `x`/`y` are simply
 * how far the image's centre has moved from where layout put it.
 */

export interface Point {
  x: number;
  y: number;
}

export interface Size {
  width: number;
  height: number;
}

export interface ZoomState {
  scale: number;
  x: number;
  y: number;
}

/**
 * What the caller measured.
 *
 * `origin` is the centre of the image as laid out, before any transform — not
 * the centre of its current bounding rect, which moves as it pans. `fit` is its
 * size at scale 1, and `viewport` is the box it is being held inside.
 */
export interface Geometry {
  origin: Point;
  fit: Size;
  viewport: Size;
}

/** The image as laid out: filling its box, centred, untransformed. */
export const FIT: ZoomState = { scale: 1, x: 0, y: 0 };

export const MIN_SCALE = 1;

/**
 * Far enough in to read small type on a screenshot. Past this a phone photo is
 * mostly interpolation, and the pan bounds get long enough to feel unmoored.
 */
export const MAX_SCALE = 6;

/**
 * One press of the keyboard's zoom key.
 *
 * Coarser than a wheel notch: a key repeat is slower than a scroll, so the
 * same step would take too many presses to cross the range.
 */
export const KEY_ZOOM_STEP = 1.4;

/** Movement past this ends a tap and makes the gesture a drag. */
export const TAP_SLOP = 8;

/**
 * How long after an overlay closes the click of the tap that closed it may
 * still arrive.
 *
 * A tap delivers its pointer events at once and its `click` afterwards — on a
 * touch screen tens to hundreds of milliseconds afterwards, hit-tested where
 * the finger was rather than against whatever the pointer events reached. The
 * window covers that gap, and is measured from the close rather than from the
 * tap because until then the overlay is still there to absorb its own click.
 *
 * The claim is spent by the first click to arrive, so this length only bounds
 * a claim that no click ever came for. The cost of it is a deliberate tap at
 * the same spot inside the window, which is swallowed with the ghost.
 */
export const GHOST_CLICK_MS = 400;

/**
 * How far that click may land from the pointer's release and still be
 * recognized as belonging to it.
 *
 * Deliberately not `TAP_SLOP`, which bounds a different thing: how far a
 * finger may travel and still have been a tap. A browser may position the
 * click at either end of that travel, so this has to cover the whole of it
 * with room left for rounding — a click landing outside takes the claim with
 * it and reaches the page underneath.
 */
export const GHOST_CLICK_SLOP = 24;

export function distance(a: Point, b: Point): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export function midpoint(a: Point, b: Point): Point {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export function isZoomed(state: ZoomState): boolean {
  return state.scale > MIN_SCALE;
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/**
 * How far the image may travel from centre on one axis before an edge of it
 * would come inside the viewport.
 *
 * Zero when the scaled image still fits, which is what pins it centred at the
 * fit scale rather than letting a stray drag slide it around.
 */
function slack(scaled: number, viewport: number): number {
  return Math.max(0, (scaled - viewport) / 2);
}

/**
 * Collapse a negative zero.
 *
 * Clamping a negative offset against a bound of 0 yields `-0`, which compares
 * equal to 0 everywhere except `Object.is` — and so in vitest's `toEqual`. It
 * reaches CSS harmlessly, but it makes two states that mean the same thing
 * distinguishable, which is a trap for anything comparing them.
 */
function zeroed(value: number): number {
  return value === 0 ? 0 : value;
}

/** Hold the image against the viewport rather than letting it drift off. */
export function clampTranslation(state: ZoomState, geometry: Geometry): ZoomState {
  const maxX = slack(geometry.fit.width * state.scale, geometry.viewport.width);
  const maxY = slack(geometry.fit.height * state.scale, geometry.viewport.height);
  return {
    scale: state.scale,
    x: zeroed(clamp(state.x, -maxX, maxX)),
    y: zeroed(clamp(state.y, -maxY, maxY)),
  };
}

export interface Gesture {
  /** How much bigger the gesture is asking the image to be, since it began. */
  scaleFactor: number;
  /** Where the gesture started — the pinch midpoint, the finger, the cursor. */
  anchorStart: Point;
  /** Where it is now. Equal to `anchorStart` for a gesture that only scales. */
  anchorNow: Point;
}

/**
 * The next transform, given the state the gesture started from.
 *
 * One function covers pinch, pan, wheel and double tap, because they differ
 * only in what they put in the `Gesture`: a pan is a scale factor of 1 with a
 * moving anchor, a double tap is a fixed anchor with a factor that lands on a
 * chosen scale.
 *
 * Always call it with the state as it was when the gesture *began*, not the
 * state from the previous move. Accumulating frame by frame drifts, because
 * every frame re-rounds and re-clamps — and a pinch that hits the scale cap and
 * comes back would not return along the path it took out.
 *
 * The anchoring is: the point under the fingers must land on the same content
 * afterwards. Solving `anchorNow - c1 = (anchorStart - c0) * factor` for the new
 * centre `c1` gives the line below. The factor it uses is the one left after
 * clamping the scale, not the one asked for — anchoring on the raw factor while
 * capping the scale flings the image off screen the moment a fast pinch
 * overshoots the cap.
 */
export function applyGesture(start: ZoomState, gesture: Gesture, geometry: Geometry): ZoomState {
  const scale = clamp(start.scale * gesture.scaleFactor, MIN_SCALE, MAX_SCALE);
  const factor = scale / start.scale;

  const centerX = geometry.origin.x + start.x;
  const centerY = geometry.origin.y + start.y;

  const nextCenterX = gesture.anchorNow.x - (gesture.anchorStart.x - centerX) * factor;
  const nextCenterY = gesture.anchorNow.y - (gesture.anchorStart.y - centerY) * factor;

  return clampTranslation(
    { scale, x: nextCenterX - geometry.origin.x, y: nextCenterY - geometry.origin.y },
    geometry,
  );
}

/** `WheelEvent.DOM_DELTA_LINE`, spelled out so no caller needs the event. */
const DELTA_LINE = 1;
/** `WheelEvent.DOM_DELTA_PAGE`. */
const DELTA_PAGE = 2;

/** Roughly one line of text, for a browser that measures a wheel in lines. */
const LINE_HEIGHT_PX = 16;

/**
 * A wheel delta in pixels, whatever unit the browser reported it in.
 *
 * `deltaY` is only pixels when `deltaMode` says so. Firefox reports a mouse
 * wheel in *lines* — about 3 per notch — so treating the number as pixels makes
 * a notch a 2% step and puts the whole zoom range some forty notches away. A
 * trackpad pinch reports pixels everywhere, which is why the primary case never
 * showed this.
 */
export function normalizeWheelDelta(
  deltaY: number,
  deltaMode: number,
  viewportHeight: number,
): number {
  if (deltaMode === DELTA_LINE) return deltaY * LINE_HEIGHT_PX;
  if (deltaMode === DELTA_PAGE) return deltaY * (viewportHeight || LINE_HEIGHT_PX * 20);
  return deltaY;
}

/**
 * The scale factor a wheel notch asks for, given a delta already in pixels.
 *
 * A trackpad pinch arrives as a `wheel` event with `ctrlKey` set and a small
 * delta per frame; a mouse wheel sends a much larger one per notch. Exponential
 * rather than linear so each notch is the same proportional step whatever the
 * current scale, and capped per event so one coarse notch cannot cross the whole
 * range.
 */
export function wheelScaleFactor(deltaY: number): number {
  return Math.exp(-clamp(deltaY, -WHEEL_DELTA_CAP, WHEEL_DELTA_CAP) / 120);
}

/** The most one event may contribute, in pixels. */
export const WHEEL_DELTA_CAP = 50;

/**
 * How long a wheel gesture stays open after its last event.
 *
 * A trackpad pinch is a burst of events, and `applyGesture` has to be called
 * with the state the burst started from — so the burst needs an end, and a
 * wheel has no equivalent of a finger lifting.
 */
export const WHEEL_IDLE_MS = 140;

/**
 * One `AuthError`, not two.
 *
 * `$lib/api` and `$lib/money/api` each declared a byte-identical class and each
 * exported it, so `e instanceof AuthError` was `false` across the boundary. It
 * was latent — no file imported from both — and a workaround for it had already
 * been written in `stores/chat.ts` on the stale premise that `api.ts` did not
 * export the class at all.
 *
 * This is an identity assertion rather than a parity one, and deliberately: the
 * two are now one implementation, so there is nothing left to compare. What it
 * pins is that they stay one. Its control is re-declaring the class in
 * `money/api.ts`, which turns the first case below red.
 */

import { describe, expect, it } from 'vitest';

import { AuthError as ApiAuthError } from '$lib/api';
import { AuthError as MoneyAuthError } from '$lib/money/api';

describe('AuthError', () => {
  it('is the same class on both sides of the money boundary', () => {
    expect(MoneyAuthError).toBe(ApiAuthError);
  });

  it('is recognised by instanceof across the boundary', () => {
    expect(new MoneyAuthError()).toBeInstanceOf(ApiAuthError);
    expect(new ApiAuthError()).toBeInstanceOf(MoneyAuthError);
  });

  it('still sets the name the retired workaround matched on', () => {
    // `stores/chat.ts` no longer reads it, but the login redirect in
    // `routes/+layout.svelte` and anything catching across a bundle boundary
    // may, so removing it is a separate decision from removing the workaround.
    expect(new ApiAuthError().name).toBe('AuthError');
  });
});

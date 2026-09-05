import adapter from '@sveltejs/adapter-static';
import { relative, sep } from 'node:path';

/** @type {import('@sveltejs/kit').Config} */
const config = {
  compilerOptions: {
    // defaults to rune mode for the project, except for `node_modules`. Can be removed in svelte 6.
    runes: ({ filename }) => {
      const relativePath = relative(import.meta.dirname, filename);
      const pathSegments = relativePath.toLowerCase().split(sep);
      const isExternalLibrary = pathSegments.includes('node_modules');

      return isExternalLibrary ? undefined : true;
    },
  },
  kit: {
    // adapter-auto only supports some environments, see https://svelte.dev/docs/kit/adapter-auto for a list.
    // If your environment is not supported, or you settled on a specific environment, switch out the adapter.
    // See https://svelte.dev/docs/kit/adapters for more information about adapters.
    adapter: adapter(),
    paths: {
      base: '/istota',
    },
    serviceWorker: {
      // Kit registers `src/service-worker.ts` for every visitor by default,
      // which is the one thing this must not do (ISSUE-202). The worker is for
      // the iOS shell's cold launch with no connection; a service worker is
      // also the one client artifact that can pin a continuously deployed app
      // to a stale build, so `routes/+layout.svelte` registers it behind
      // `isNativeShell()` and the desktop keeps exactly what it has today.
      register: false,
    },
    version: {
      // ISSUE-428: the default is a build timestamp, which says nothing about
      // which commit produced the bundle. Stamping the checkout's sha makes
      // `_app/version.json` an oracle `doctor`'s `web.build_current` compares
      // against HEAD, so a deployment serving a stale bundle says so instead
      // of looking identical to a current one. The poll below only needs the
      // string to change, so an unstamped build keeps the timestamp default.
      name: process.env.ISTOTA_BUILD_SHA || Date.now().toString(),
      // Poll `_app/version.json` so a long-lived session learns a new build
      // shipped. SvelteKit only reloads on the *next navigation*, which a chat
      // tab left open for days never performs — the root layout turns `updated`
      // into a visible prompt (and auto-reloads when idle). Chiefly for the iOS
      // home-screen PWA, which caches the app shell aggressively enough to keep
      // running a deleted bundle against a current API.
      pollInterval: 300000,
    },
  },
};

export default config;

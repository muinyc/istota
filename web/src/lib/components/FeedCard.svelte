<script lang="ts">
  import { FileText, Play, Star } from 'lucide-svelte';
  import type { FeedEntry } from '$lib/api';
  import { updateEntryStarred } from '$lib/api';
  import { fileKind, inlineMedia, playerUrl, providerLabel } from '$lib/feeds/embed';

  import { markReadDelay } from '$lib/stores/feeds';
  import { notifyError } from '$lib/stores/notices';
  import { formatDate as formatIsoDate } from '$lib/dateFormat';

  let {
    entry,
    onImageClick,
    onViewed,
    onStarToggle,
    onOpen,
  }: {
    entry: FeedEntry;
    onImageClick: (images: string[], index: number) => void;
    onViewed?: (id: number) => void;
    onStarToggle?: (id: number, starred: boolean) => void;
    onOpen?: () => void;
  } = $props();

  // Open the reader on a plain card click, but let the existing interactive
  // targets (image → lightbox, title/permalink → original, star) keep their
  // own behaviour.
  function handleCardClick(e: MouseEvent) {
    if (!onOpen) return;
    if ((e.target as HTMLElement).closest('a, button')) return;
    onOpen();
  }

  function handleCardKey(e: KeyboardEvent) {
    if (!onOpen || e.target !== e.currentTarget) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onOpen();
    }
  }

  async function toggleStar(e: MouseEvent) {
    e.stopPropagation();
    e.preventDefault();
    const next = !entry.starred;
    // Optimistic local update; the parent owns the entries array, so we
    // poke a callback so it can rebroadcast (e.g. exit a starred-only view).
    entry.starred = next;
    try {
      await updateEntryStarred(entry.id, next);
      onStarToggle?.(entry.id, next);
    } catch {
      // Roll back if the server rejected. The rollback is silent on its own —
      // the star simply springs back — so say why.
      entry.starred = !next;
      notifyError("Couldn't update star.");
    }
  }

  const maxGrid = 4;
  const feedSlug = $derived(entry.feed.title.toLowerCase().replace(/[^a-z0-9-]/g, '-'));
  const isImage = $derived(entry.images.length > 0);
  const hiddenCount = $derived(Math.max(0, entry.images.length - maxGrid));
  // Images the server withheld because a newer entry already showed them
  // (reblog of a picture you just scrolled past). Noted rather than silent,
  // so a card that lost all its images doesn't just look empty.
  const repeatCount = $derived(entry.duplicate_image_count ?? 0);
  const galleryCount = $derived(Math.min(entry.images.length, maxGrid));
  const permalink = $derived(entry.url || entry.feed.site_url || '');

  // Playable media (an Are.na Embed block). `playerUrl` is an allowlist over
  // known providers and returns null for anything it can't vouch for — the
  // card then behaves like any other entry and the body's "Watch on …" link
  // (written by the provider) is the way out. Nothing is guessed into an
  // iframe src.
  const player = $derived(playerUrl(entry.embed_url));
  const providerName = $derived(providerLabel(entry.embed_url));
  const playLabel = $derived(`Play video${providerName ? ` on ${providerName}` : ''}`);
  let playing = $state(false);

  // Autoplay is honest here: it only ever follows an explicit click on the
  // play control, never a page load.
  const playerSrc = $derived(player ? `${player}?autoplay=1` : '');

  // A media file we play ourselves — a Mastodon video attachment, a podcast
  // enclosure. `inlineMedia` re-parses the URL rather than trusting it and
  // returns null for anything it can't put in a <video>/<audio> src, in which
  // case the card falls back to whatever it would otherwise have shown.
  // Before ISSUE-356 this URL arrived in `entry.images` and painted an <img>
  // that never decodes. A provider embed still wins: it is the more specific
  // affordance, and it is the one an entry would carry deliberately.
  const media = $derived(player ? null : inlineMedia(entry.media_url, entry.media_type));
  // A lone still that came with the clip becomes its poster rather than a hero
  // of its own — one piece of media, shown once. Several stills are a gallery,
  // and consuming the first as a poster would silently drop the rest, so they
  // are left to render below the player instead. A still that is itself a
  // playable URL is refused: a downgrade to a pre-v7 binary re-files the clip
  // into `images`, and using it as the poster would put the mp4 back in an
  // `<img>`-shaped hole — the exact symptom this fixed.
  const mediaPoster = $derived(
    media && entry.images.length === 1 && !inlineMedia(entry.images[0])
      ? entry.images[0]
      : undefined,
  );
  // Images the player did not consume. Rendered under it, because a card that
  // quietly shows fewer pictures than the entry has is the bug one field over.
  const mediaImages = $derived(media && !mediaPoster ? entry.images : []);

  // An attached document (an Are.na Attachment — nearly always a PDF). Are.na
  // renders a cover page for one, so without this the card is indistinguishable
  // from a photo and its hero click zooms page 1 instead of opening the file.
  // A video wins if an entry somehow carries both, since playing is the more
  // specific affordance.
  const documentUrl = $derived(!player && !media && entry.file_url ? entry.file_url : '');
  const documentKind = $derived(fileKind(documentUrl));

  // The card opens the reader on any click that isn't a link or a button, and
  // the media element is neither. Clicking a player means "play".
  function swallow(e: Event) {
    e.stopPropagation();
  }

  function play(e: MouseEvent) {
    // The hero is normally a lightbox trigger; for a video the click means
    // "play", so stop it before the card's own open handler sees it too.
    e.stopPropagation();
    e.preventDefault();
    playing = true;
  }

  // The Images / Text header chips are *display* toggles, not filters, and this
  // card deliberately knows nothing about them: it always renders its media and
  // body, and the grid hides them with CSS (the .hide-images / .hide-text rules
  // in routes/feeds/+page.svelte). That keeps the toggles desktop-only for free
  // — the rules live in a min-width media query, so on a phone they simply
  // don't apply and everything shows. Conditioning the markup here instead
  // would need JS viewport detection, which nothing else in this app does, and
  // would flash the wrong layout between prerender and hydration. It is also
  // the only way to reach images embedded in the body copy, which arrive as
  // {@html} and can't be conditioned in the template at all.

  const formatDate = (iso: string) =>
    formatIsoDate(iso, { locale: 'en-US', month: 'short', day: 'numeric' });

  function trackView(node: HTMLElement) {
    if (entry.status === 'read' || !onViewed) return;

    let timer: ReturnType<typeof setTimeout> | null = null;
    let done = false;

    const observer = new IntersectionObserver(
      (entries) => {
        const e = entries[0];
        if (e.isIntersecting && !done) {
          timer = setTimeout(() => {
            done = true;
            onViewed!(entry.id);
            observer.disconnect();
          }, $markReadDelay * 1000);
        } else if (timer) {
          clearTimeout(timer);
          timer = null;
        }
      },
      { threshold: 0.5 },
    );

    observer.observe(node);

    return {
      destroy() {
        if (timer) clearTimeout(timer);
        observer.disconnect();
      },
    };
  }
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<article
  class="card {isImage ? 'image' : 'text'} feed-{feedSlug}"
  class:openable={!!onOpen}
  data-published={entry.published_at}
  data-added={entry.created_at}
  use:trackView
  onclick={handleCardClick}
  onkeydown={handleCardKey}
  role={onOpen ? 'button' : undefined}
  tabindex={onOpen ? 0 : undefined}
>
  {#if entry.status === 'read'}
    <span class="seen-pill">SEEN</span>
  {/if}
  {#if player}
    <!-- Playable media. The thumbnail is a play surface rather than a
         lightbox trigger, and the frame replaces it in place on click. -->
    {#if playing}
      <div class="card-player">
        <iframe
          src={playerSrc}
          title={entry.title || 'Embedded video'}
          allow="autoplay; fullscreen; picture-in-picture; encrypted-media"
          sandbox="allow-scripts allow-same-origin allow-presentation allow-popups allow-popups-to-escape-sandbox"
          referrerpolicy="strict-origin-when-cross-origin"
          loading="lazy"
          allowfullscreen
        ></iframe>
      </div>
    {:else}
      <button
        type="button"
        class="card-image card-video"
        class:no-poster={!isImage}
        onclick={play}
        aria-label={playLabel}
      >
        {#if isImage}
          <img src={entry.images[0]} alt={entry.title || ''} loading="lazy" />
        {/if}
        <span class="play-badge"><Play size={26} fill="currentColor" /></span>
      </button>
    {/if}
    {#if entry.title}
      <div class="card-title-overlay">
        {#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
      </div>
    {/if}
    {#if entry.content}
      <div class="card-body"><div class="excerpt prose">{@html entry.content}</div></div>
    {/if}
  {:else if media}
    <!-- A media file we serve ourselves. No poster/play dance: the element is
         the player, `controls` is the play affordance, and `preload="metadata"`
         keeps a grid of cards from pulling whole clips down on scroll. No
         width or height attribute — the stylesheet bounds it, the same rule
         inline <video> in the body copy follows. -->
    <div class="card-media" class:audio={media.kind === 'audio'}>
      {#if media.kind === 'video'}
        <!-- svelte-ignore a11y_media_has_caption -->
        <video
          src={media.url}
          poster={mediaPoster}
          controls
          playsinline
          preload="metadata"
          onclick={swallow}
        ></video>
      {:else}
        <audio src={media.url} controls preload="metadata" onclick={swallow}></audio>
      {/if}
    </div>
    {#if mediaImages.length > 0}
      <!-- Stills the player did not take as its poster. Same gallery the
           image branch below renders, so nothing an entry carries goes
           unshown just because it also carried a clip. -->
      <div class="card-gallery gallery-{Math.min(mediaImages.length, maxGrid)}">
        {#each mediaImages.slice(0, maxGrid) as img, idx}
          <button
            type="button"
            class="card-image{idx === maxGrid - 1 && mediaImages.length > maxGrid
              ? ' gallery-more'
              : ''}"
            onclick={() => onImageClick(mediaImages, idx)}
          >
            <img src={img} alt={entry.title || ''} loading="lazy" />
            {#if idx === maxGrid - 1 && mediaImages.length > maxGrid}
              <span class="gallery-count">+{mediaImages.length - maxGrid + 1}</span>
            {/if}
          </button>
        {/each}
      </div>
    {/if}
    {#if entry.title}
      <div class="card-title-overlay">
        {#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
      </div>
    {/if}
    {#if entry.content}
      <div class="card-body"><div class="excerpt prose">{@html entry.content}</div></div>
    {/if}
  {:else if documentUrl}
    <!-- An attached file. The cover is a link to the document rather than a
         lightbox trigger: zooming page 1 as a picture is a dead end. A real
         <a> keeps middle-click and copy-link working, and the card's own
         click handler already ignores anchors. -->
    <a
      class="card-image card-document"
      class:no-cover={!isImage}
      href={documentUrl}
      target="_blank"
      rel="noopener noreferrer"
      aria-label={`Open ${documentKind}: ${entry.title || 'attached document'}`}
    >
      {#if isImage}
        <img src={entry.images[0]} alt={entry.title || ''} loading="lazy" />
      {:else}
        <FileText size={32} aria-hidden="true" />
      {/if}
      <span class="doc-badge">{documentKind}</span>
    </a>
    {#if entry.title}
      <div class="card-title-overlay">
        {#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
      </div>
    {/if}
    {#if entry.content}
      <div class="card-body"><div class="excerpt prose">{@html entry.content}</div></div>
    {/if}
  {:else if isImage}
    {#if entry.images.length > 1}
      <div class="card-gallery gallery-{galleryCount}">
        {#each entry.images.slice(0, maxGrid) as img, idx}
          <button
            type="button"
            class="card-image{idx === maxGrid - 1 && hiddenCount > 0 ? ' gallery-more' : ''}"
            onclick={() => onImageClick(entry.images, idx)}
          >
            <img src={img} alt={entry.title || ''} loading="lazy" />
            {#if idx === maxGrid - 1 && hiddenCount > 0}
              <span class="gallery-count">+{hiddenCount + 1}</span>
            {/if}
          </button>
        {/each}
      </div>
    {:else}
      <button type="button" class="card-image" onclick={() => onImageClick(entry.images, 0)}>
        <img src={entry.images[0]} alt={entry.title || ''} loading="lazy" />
      </button>
    {/if}
    {#if entry.title}
      <div class="card-title-overlay">
        {#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
      </div>
    {/if}
    {#if entry.content}
      <div class="card-body"><div class="excerpt prose">{@html entry.content}</div></div>
    {/if}
  {:else}
    <div class="card-body">
      {#if entry.title}
        <h3>
          {#if permalink}<a href={permalink}>{entry.title}</a>{:else}{entry.title}{/if}
        </h3>
      {/if}
      {#if entry.content}
        <div class="excerpt prose">{@html entry.content}</div>
      {/if}
    </div>
  {/if}
  <div class="meta">
    <button
      type="button"
      class="star-btn"
      class:starred={entry.starred}
      onclick={toggleStar}
      title={entry.starred ? 'Unstar' : 'Star'}
      aria-label={entry.starred ? 'Unstar entry' : 'Star entry'}
    >
      <Star size={14} fill={entry.starred ? 'currentColor' : 'none'} />
    </button>
    <span class="feed-name">{entry.feed.title}</span>
    {#if repeatCount > 0}
      <span class="repeat-note" title="Already shown by a more recent post">
        {repeatCount} repeat{repeatCount > 1 ? 's' : ''} hidden
      </span>
    {/if}
    {#if entry.published_at}
      {#if permalink}
        <a href={permalink} class="meta-link">
          <time datetime={entry.published_at}>{formatDate(entry.published_at)}</time>
        </a>
      {:else}
        <span class="meta-link">
          <time datetime={entry.published_at}>{formatDate(entry.published_at)}</time>
        </span>
      {/if}
    {/if}
  </div>
</article>

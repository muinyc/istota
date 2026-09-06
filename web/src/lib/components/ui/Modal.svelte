<script lang="ts">
  import type { Snippet } from 'svelte';
  import { Dialog } from 'bits-ui';

  interface Props {
    open: boolean;
    title: string;
    description?: string;
    onOpenChange?: (open: boolean) => void;
    children: Snippet;
    footer?: Snippet;
    width?: string;
    /**
     * Panel height. `auto` (the default) sizes to the content, capped by the
     * viewport. A dialog whose content is a list rather than a form wants the
     * height it can have — pass `100dvh` and the panel's own max-height caps
     * it to the safe box.
     */
    height?: string;
  }

  let {
    open = $bindable(false),
    title,
    description,
    onOpenChange,
    children,
    footer,
    width = '420px',
    height = 'auto',
  }: Props = $props();
</script>

<Dialog.Root bind:open {onOpenChange}>
  <Dialog.Portal>
    <Dialog.Overlay class="ui-modal-overlay" />
    <Dialog.Content
      class="ui-modal-content"
      style="--modal-width: {width}; --modal-height: {height}"
    >
      <Dialog.Title class="ui-modal-title">{title}</Dialog.Title>
      {#if description}
        <Dialog.Description class="ui-modal-description">{description}</Dialog.Description>
      {/if}
      <div class="ui-modal-body">{@render children()}</div>
      {#if footer}
        <div class="ui-modal-footer">{@render footer()}</div>
      {/if}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>

<style>
  :global(.ui-modal-overlay) {
    position: fixed;
    inset: 0;
    background: var(--scrim-bg);
    z-index: var(--z-modal);
  }
  :global(.ui-modal-content) {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    padding: var(--space-4);
    width: var(--modal-width, 420px);
    height: var(--modal-height, auto);
    /* Column so the body is the part that scrolls: the title (and the footer,
       which holds the actions) stay put instead of scrolling away from a long
       list. Inert at auto height, where the body never has to shrink. */
    display: flex;
    flex-direction: column;
    /* The panel is pinned to the viewport centre rather than laid out inside a
		   padded backdrop, so it can't use .overlay-safe — the insets come off its
		   caps instead. Subtracting both ends of each axis keeps a full-height modal
		   inside the safe box once it is centred, and dvh tracks a collapsing mobile
		   browser toolbar the way the body's height does. Inert where insets are 0. */
    max-width: calc(100vw - 2rem - var(--safe-left) - var(--safe-right));
    max-height: calc(100dvh - 2rem - var(--safe-top) - var(--safe-bottom));
    overflow: auto;
    z-index: var(--z-modal-panel);
    outline: none;
  }
  :global(.ui-modal-title) {
    font-size: var(--text-base);
    font-weight: 600;
    margin: 0 0 var(--space-2);
    color: var(--text-primary);
  }
  :global(.ui-modal-description) {
    font-size: var(--text-sm);
    color: var(--text-muted);
    margin: 0 0 var(--space-3);
  }
  /* min-height: 0 or the body refuses to shrink below its content and the
     panel overflows its own max-height instead of scrolling here. */
  :global(.ui-modal-body) {
    min-height: 0;
    overflow: auto;
    /* A scroll container clips at its padding box, and rings are drawn outside
       the box they belong to — a selection or focus ring on the first item of a
       flush-left row (the room colour picker) lost its left edge to that. The
       bleed gives the clip box a few px on each side; the negative margin takes
       them back out of the panel's own padding, so nothing moves. */
    padding-inline: 4px;
    margin-inline: -4px;
  }
  :global(.ui-modal-footer) {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-2);
    margin-top: var(--space-4);
    padding-top: var(--space-3);
    border-top: 1px solid var(--border-subtle);
    flex-shrink: 0;
  }
</style>

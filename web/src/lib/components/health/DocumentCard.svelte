<script lang="ts">
  import { KebabMenu, type KebabItem } from '$lib/components/ui';
  import type { DocumentEntity, HealthDocument } from '$lib/api';
  import { documentName, formatBytes, mimeLabel } from '$lib/health/documents';
  import { formatDate } from '$lib/dateFormat';

  interface Props {
    doc: HealthDocument;
    /** The record this card is being shown under, if any. */
    entityType?: DocumentEntity;
    entityId?: number;
    /** Omitted on a read-only surface (no detach target). */
    onDetach?: (doc: HealthDocument) => void;
    onDelete?: (doc: HealthDocument) => void;
  }

  let { doc, entityType, entityId, onDetach, onDelete }: Props = $props();

  const name = $derived(documentName(doc));

  const menu = $derived.by(() => {
    const items: KebabItem[] = [{ label: 'Open', href: doc.url }];
    if (onDetach && entityType && entityId) {
      items.push({ label: 'Detach', onSelect: () => onDetach(doc) });
    }
    if (onDelete) {
      items.push({ label: 'Delete', danger: true, onSelect: () => onDelete(doc) });
    }
    return items;
  });
</script>

<li class="doc-card">
  <div class="card-head">
    <!-- The whole card is not a link: the kebab lives inside it, and a
         nested interactive element inside an anchor is not addressable. -->
    <a class="name" href={doc.url} title={name}>{name}</a>
    <KebabMenu items={menu} ariaLabel="Document actions" />
  </div>
  <div class="tags">
    <span class="tag">{mimeLabel(doc.mime)}</span>
    <span class="tag source-{doc.source}">{doc.source}</span>
  </div>
  <div class="doc-meta">
    <span>{formatBytes(doc.byte_size)}</span>
    <span>{formatDate(doc.created_at)}</span>
  </div>
  {#if doc.notes}
    <p class="notes">{doc.notes}</p>
  {/if}
</li>

<style>
  .doc-card {
    padding: var(--space-3) var(--space-4);
    background: var(--surface-card);
    border: 1px solid var(--border-default);
    border-radius: var(--radius-card);
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    min-width: 0;
  }

  /* flex-start: a long document name wraps, and a centred row would float
     the kebab against the middle of it. */
  .card-head {
    align-items: flex-start;
  }

  .name {
    margin: 0;
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--accent-blue);
    line-height: 1.35;
    text-decoration: none;
    /* A long scanner-generated filename must not widen its grid column. */
    overflow-wrap: anywhere;
    min-width: 0;
  }

  .name:hover {
    text-decoration: underline;
  }

  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .tag {
    font-size: var(--text-2xs);
    color: var(--text-muted);
    background: var(--surface-raised);
    border-radius: var(--radius-pill);
    padding: 0.05rem var(--space-2);
  }

  .doc-meta {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    font-size: var(--text-xs);
    color: var(--text-dim);
  }

  .notes {
    margin: 0;
    font-size: var(--text-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }
</style>

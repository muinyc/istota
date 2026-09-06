/**
 * Presentation helpers for health documents.
 *
 * Kept out of the components so the copy that has to be exactly right —
 * how many *other* records a delete would strip a document from — is unit
 * testable without mounting anything.
 */

import type {
  Diagnosis,
  DocumentEntity,
  DocumentLink,
  Encounter,
  HealthDocument,
  Immunization,
} from '$lib/api';
import type { SelectOption } from '$lib/components/ui';
import { conditionOptionLabel, encounterOptionLabel } from './conditions';

/** Human file size. Binary units, one decimal above KB. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 10 || unit === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded} ${units[unit]}`;
}

/**
 * The warning under a delete confirmation.
 *
 * `otherLinks` counts links to records *other* than the one the user is
 * deleting from — a document attached only here has no extra consequence,
 * so it gets no sentence at all rather than a "0 other records" one.
 */
export function otherRecordsWarning(otherLinks: number): string {
  if (otherLinks <= 0) return '';
  if (otherLinks === 1) {
    return 'This document is also attached to 1 other record. Deleting removes it everywhere.';
  }
  return `This document is also attached to ${otherLinks} other records. Deleting removes it everywhere.`;
}

/**
 * The warning under a delete confirmation on the Documents view.
 *
 * Distinct from `otherRecordsWarning` because the question is different: there
 * the user is deleting *from* one record and wants to know what else goes with
 * it, so a document attached only there needs no sentence. Here they are
 * deleting the document itself from a list of all of them, and every record it
 * is attached to loses it — including the one, which is exactly the case they
 * cannot see from this table.
 */
export function attachedRecordsWarning(links: number): string {
  if (links <= 0) return 'It is not attached to any record.';
  if (links === 1) return 'It is attached to 1 record, which will lose it.';
  return `It is attached to ${links} records, which will lose it.`;
}

const ENTITY_LABELS: Record<DocumentEntity, string> = {
  encounter: 'Visit',
  diagnosis: 'Condition',
  immunization: 'Vaccination',
};

/**
 * What a link points at, in the user's vocabulary rather than the schema's.
 * The tables are `encounters` / `diagnoses` / `immunizations`, but the pages
 * they back are called Visits, Conditions and Immunizations.
 */
export function entityTypeLabel(entityType: string): string {
  return ENTITY_LABELS[entityType as DocumentEntity] ?? entityType;
}

/** Short label for the MIME chip — `application/pdf` reads as noise. */
export function mimeLabel(mime: string): string {
  if (mime === 'application/pdf') return 'PDF';
  if (mime === 'text/plain') return 'Text';
  if (mime.startsWith('image/')) return mime.slice('image/'.length).toUpperCase();
  return mime;
}

/** Display name, preferring what the user actually uploaded. */
export function documentName(doc: HealthDocument): string {
  return doc.original_filename || doc.filename;
}

/**
 * The page size each list on the Documents view asks for, and the ceiling on
 * how many pages it will walk.
 *
 * One size per list, because the caps are not the same and a `limit` above an
 * endpoint's own ceiling is a 422 rather than a clamp: FastAPI validates the
 * query parameter before the handler runs, so the request never reaches the
 * paging logic to be corrected. A single shared size is what broke this view
 * in production — the page asked all four lists for 1000, `/encounters` and
 * `/diagnoses` cap at 500, and the two rejections failed the `Promise.all`
 * that loads the page, so the whole view rendered "Health API error: 422"
 * however few documents were stored.
 *
 * Each value is that endpoint's declared ceiling, so a store under it is one
 * request. `tests/test_health_page_limits.py` reads these numbers back and
 * compares them against the routes, since nothing else stops the server
 * lowering a cap and putting the 422 straight back.
 *
 * The page ceiling exists because a paging loop against a server that stops
 * advancing is an infinite one; 20 pages is well past any real health store,
 * and hitting it is reported rather than hidden.
 */
export const PAGE_SIZES = {
  documents: 1000,
  encounters: 500,
  diagnoses: 500,
  immunizations: 2000,
} as const;
export const MAX_PAGES = 20;

/** What a paged fetch returned, and whether it ran out of patience. */
export interface PagedResult<T> {
  items: T[];
  /** True when the ceiling was reached with a full page still coming back. */
  truncated: boolean;
}

/**
 * Walk a paged list endpoint to the end.
 *
 * The Documents view states a total and derives an "unattached" count from
 * what it holds, so a silently truncated list is not merely an incomplete
 * table — it is a wrong number presented as a fact. Either the loop reaches
 * the end, or `truncated` says it did not and the page says so too.
 *
 * A short page ends it. A page that comes back empty also ends it, which is
 * what stops a server returning the same full page forever from spinning
 * until the ceiling; the ceiling is the backstop for the case where it does
 * keep returning full pages.
 */
export async function fetchAllPages<T>(
  fetchPage: (offset: number, limit: number) => Promise<T[]>,
  {
    pageSize = PAGE_SIZES.documents,
    maxPages = MAX_PAGES,
  }: { pageSize?: number; maxPages?: number } = {},
): Promise<PagedResult<T>> {
  const items: T[] = [];
  for (let page = 0; page < maxPages; page += 1) {
    const batch = await fetchPage(items.length, pageSize);
    items.push(...batch);
    if (batch.length < pageSize) return { items, truncated: false };
  }
  return { items, truncated: true };
}

/** The records a document could be attached to, by type. */
export interface AttachPool {
  encounters: Encounter[];
  diagnoses: Diagnosis[];
  immunizations: Immunization[];
}

/**
 * Options for the attach picker: records of one type this document is not
 * already on.
 *
 * The filter is on the `(entity_type, entity_id)` pair, never on the id alone.
 * `document_links` is polymorphic, so encounter 1 and diagnosis 1 are two
 * different records that happen to share a number — filtering by id would hide
 * a condition from the picker because a *visit* with the same id was already
 * attached, and there is nothing on the screen that would explain it.
 */
export function attachOptions(
  entityType: DocumentEntity,
  pool: AttachPool,
  links: DocumentLink[],
  formatDate: (iso: string) => string,
): SelectOption[] {
  const taken = new Set(links.filter((l) => l.entity_type === entityType).map((l) => l.entity_id));
  const free = <T extends { id: number }>(xs: T[]) => xs.filter((x) => !taken.has(x.id));
  if (entityType === 'encounter') {
    return free(pool.encounters).map((e) => ({
      value: String(e.id),
      label: encounterOptionLabel(e, formatDate, (t) => t),
    }));
  }
  if (entityType === 'diagnosis') {
    return free(pool.diagnoses).map((d) => ({
      value: String(d.id),
      label: conditionOptionLabel(d),
    }));
  }
  return free(pool.immunizations).map((i) => ({
    value: String(i.id),
    label: `${formatDate(i.date_given)} · ${i.name}`,
  }));
}

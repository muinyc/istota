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

const SOURCE_LABELS: Record<string, string> = {
  manual: 'Uploaded',
  import: 'Imported',
  lab_panel: 'Lab panel',
};

/**
 * Where a document came from, in the user's vocabulary rather than the
 * column's. `lab_panel` is the stored value and reads as a schema leak under a
 * filename; an unknown source falls through as written rather than being
 * hidden, since a value nothing here knows about is still worth seeing.
 */
export function sourceLabel(source: string): string {
  return SOURCE_LABELS[source] ?? source;
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
 * Which of the Documents view's four walks stopped at the ceiling (ISSUE-441).
 *
 * One flag per list rather than a single boolean, because the four are not one
 * fact: the header count is a statement about the documents and nothing else,
 * while a cut *pool* costs the attach picker rather than the table. A union
 * would make the page disown a document total it reached in full, and a picker
 * showing visits would carry a notice earned by the vaccinations.
 *
 * Written against `AttachPool` rather than as a fourth hand-listed copy of the
 * same key set, so a pool added later cannot be walked and then dropped — which
 * is the defect this type exists to fix.
 */
export type TruncationFlags = { documents: boolean } & Record<keyof AttachPool, boolean>;

/** The pool key each picker type draws from. */
const POOL_FOR: Record<DocumentEntity, keyof AttachPool> = {
  encounter: 'encounters',
  diagnosis: 'diagnoses',
  immunization: 'immunizations',
};

/**
 * A pool's name in the user's vocabulary, plural. `ENTITY_LABELS` above is the
 * singular of the same words; these sentences read about a list, not a record.
 *
 * Declared in the order the picker offers the types, because `POOL_ORDER` below
 * is its key list: a `Record<keyof AttachPool, …>` is exhaustiveness-checked by
 * the compiler, where an `Array<keyof AttachPool>` missing a member is not.
 */
const POOL_LABELS: Record<keyof AttachPool, string> = {
  encounters: 'visits',
  diagnoses: 'conditions',
  immunizations: 'vaccinations',
};

const POOL_ORDER = Object.keys(POOL_LABELS) as Array<keyof AttachPool>;

function joinNames(names: string[]): string {
  if (names.length <= 1) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/**
 * The whole-page notice: what was cut, and what it costs.
 *
 * Two sentences rather than one, because the two truncations have different
 * consequences. A cut documents list means the table is short and the oldest
 * are missing — the ones the 24h orphan sweep is about to take. A cut pool
 * leaves the table correct and shortens the attach picker instead, which is
 * the failure with nothing on screen to explain it: a record that is not among
 * the options looks exactly like one that does not exist, and the user's next
 * move is to create a second copy of it.
 */
export function truncationNotice(flags: TruncationFlags): string {
  const sentences: string[] = [];
  if (flags.documents) {
    sentences.push('There are more documents than this page loads. The oldest are not shown.');
  }
  const cut = POOL_ORDER.filter((pool) => flags[pool]).map((pool) => POOL_LABELS[pool]);
  if (cut.length > 0) {
    sentences.push(
      `There are more ${joinNames(cut)} than this page loads, ` +
        'so the attach picker does not offer every record.',
    );
  }
  return sentences.join(' ');
}

/**
 * The same fact where the user is acting on it.
 *
 * The page banner is behind the modal, so it cannot be the only place this is
 * said. Scoped to the type the picker is currently showing: a notice on all
 * three whenever any one was cut is false for two of them, and a warning that
 * is usually wrong is one the reader learns to skip.
 */
export function attachPoolNotice(entityType: DocumentEntity, flags: TruncationFlags): string {
  if (!flags[POOL_FOR[entityType]]) return '';
  return (
    `There are more ${POOL_LABELS[POOL_FOR[entityType]]} than this page loads, ` +
    'so this list is not complete.'
  );
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

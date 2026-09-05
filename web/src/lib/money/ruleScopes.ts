import type { SelectOption } from '$lib/components/ui';
import type { TransactionRule } from './api';

/**
 * The scope vocabulary the Transactions section's pickers offer.
 *
 * Shared by the rules card and the test card because both have to answer the
 * same question — which ledgers and sources can a rule be written for — and
 * the answer is not the configured list alone. A ledger renamed in the money
 * TOML leaves its rules behind at the old name, in force for nothing, and a
 * picker built from the configuration would make them unreachable: the scope
 * they sit in cannot be selected, so those rules cannot be seen or moved. So
 * every distinct value already in the table is unioned in, the same way
 * `PortfolioClassificationsCard` unions the classes already in use with the
 * canonical vocabulary.
 *
 * The two columns are not symmetrical, and follow the store rather than a
 * preference here. `ledger` is user-typed and matched case-insensitively, so
 * `Personal` and `personal` are one scope and must be one option — offering
 * both would be two entries selecting the same rules. `source` is an
 * `ImportSource.name`, code-owned and matched exactly, so it is deduplicated
 * exactly.
 */

/**
 * "Send no scope parameter" — every scope at once, which is what the list
 * endpoint answers when the key is absent. Distinct from `''`, which is a
 * real stored value meaning "any ledger" and selects only the rules written
 * there; collapsing the two is the one mistake these pickers can make.
 */
export const ALL_SCOPES = '\u0000all';

/**
 * "Nothing picked yet", for the add form, where the widest scope has to be
 * chosen rather than defaulted into: both columns default to `''` server-side
 * and the engine reads `''` as "any", so a create that omits a ledger is a
 * rule silently applying to every ledger and every source.
 *
 * Both sentinels are written as an escape rather than a word because a ledger
 * name comes from the money TOML and a source name from the importer
 * registry, and neither can contain a NUL — a readable `__all__` is a name
 * somebody could actually configure, and the collision would be silent.
 */
export const UNSET_SCOPE = '\u0000unset';

/**
 * The transaction sources that ship, in the order the picker offers them.
 *
 * Only `kind="transactions"` sources belong here; the two positions importers
 * in the same registry never reach a transaction rule. A source added later
 * needs no change — it arrives in the union as soon as one rule names it.
 */
export const KNOWN_SOURCES = ['monarch-api', 'monarch-csv'];

interface ScopeOpts {
  /** Prepend the "every scope" filter entry. Never on a write form. */
  all?: boolean;
}

function anyOption(column: 'ledger' | 'source'): SelectOption {
  return { value: '', label: column === 'ledger' ? 'Any ledger' : 'Any source' };
}

function allOption(column: 'ledger' | 'source'): SelectOption {
  return { value: ALL_SCOPES, label: column === 'ledger' ? 'All ledgers' : 'All sources' };
}

/**
 * Configured ledgers, unioned with every ledger a stored rule names.
 *
 * The configured spelling wins a case-insensitive collision: it is the one
 * the user sees everywhere else in the module, and the store matches either
 * way, so the picker names the ledger rather than whichever rule happened to
 * be written first.
 */
export function ledgerScopeOptions(
  configured: string[],
  rules: TransactionRule[],
  opts: ScopeOpts = {},
): SelectOption[] {
  const seen = new Map<string, string>();
  for (const name of configured) {
    if (name && !seen.has(name.toLowerCase())) seen.set(name.toLowerCase(), name);
  }
  for (const rule of rules) {
    if (rule.ledger && !seen.has(rule.ledger.toLowerCase())) {
      seen.set(rule.ledger.toLowerCase(), rule.ledger);
    }
  }
  const named = [...seen.values()].sort((a, b) => a.localeCompare(b));
  return [
    ...(opts.all ? [allOption('ledger')] : []),
    anyOption('ledger'),
    ...named.map((value) => ({ value, label: value })),
  ];
}

/** The shipped transaction sources, unioned with every source a rule names. */
export function sourceScopeOptions(rules: TransactionRule[], opts: ScopeOpts = {}): SelectOption[] {
  const named = [...new Set([...KNOWN_SOURCES, ...rules.map((r) => r.source)])]
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b));
  return [
    ...(opts.all ? [allOption('source')] : []),
    anyOption('source'),
    ...named.map((value) => ({ value, label: value })),
  ];
}

/** True where a picker value names a scope rather than standing in for none. */
export function isPickedScope(value: string): boolean {
  return value !== ALL_SCOPES && value !== UNSET_SCOPE;
}

/** The scope arguments for a list read: an unpicked column drops its filter. */
export function scopeQuery(ledger: string, source: string): { ledger?: string; source?: string } {
  const query: { ledger?: string; source?: string } = {};
  if (isPickedScope(ledger)) query.ledger = ledger;
  if (isPickedScope(source)) query.source = source;
  return query;
}

/** How a stored scope reads in a row: `''` is a wildcard, not a blank. */
export function scopeLabel(ledger: string, source: string): string {
  return `${ledger || 'any ledger'} · ${source || 'any source'}`;
}

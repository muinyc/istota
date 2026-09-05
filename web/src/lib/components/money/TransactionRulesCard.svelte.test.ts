import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import TransactionRulesCard from './TransactionRulesCard.svelte';
import {
  createTransactionRule,
  deleteTransactionRule,
  getLedgers,
  getTransactionRules,
  updateTransactionRule,
} from '$lib/money/api';
import type { TransactionRule } from '$lib/money/api';

vi.mock('$lib/money/api', () => ({
  getLedgers: vi.fn(),
  getTransactionRules: vi.fn(),
  createTransactionRule: vi.fn(),
  updateTransactionRule: vi.fn(),
  deleteTransactionRule: vi.fn(),
}));

// The real notices store schedules auto-dismiss timers that outlive the test
// environment and surface as post-teardown errors in whichever file runs next.
vi.mock('$lib/stores/notices', () => ({
  notifyError: vi.fn(),
  notifyInfo: vi.fn(),
  notifySuccess: vi.fn(),
}));

const mockedList = vi.mocked(getTransactionRules);
const mockedLedgers = vi.mocked(getLedgers);
const mockedCreate = vi.mocked(createTransactionRule);
const mockedUpdate = vi.mocked(updateTransactionRule);
const mockedDelete = vi.mocked(deleteTransactionRule);

function rule(overrides: Partial<TransactionRule> = {}): TransactionRule {
  return {
    id: 31,
    ledger: 'personal',
    source: 'monarch-api',
    field: 'category',
    match_kind: 'iexact',
    match_value: 'Software',
    action: 'posting_account',
    target: 'Expenses:Business:Software',
    priority: 100,
    enabled: true,
    origin: 'user',
    note: '',
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedLedgers.mockResolvedValue(['personal']);
});

// bits-ui overlays hold a body scroll lock whose reset runs after unmount;
// close anything open before cleanup so it cannot land after jsdom teardown.
afterEach(async () => {
  await fireEvent.keyDown(document.body, { key: 'Escape' });
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
});

async function mount(rules: TransactionRule[]) {
  mockedList.mockResolvedValue({ status: 'ok', rules });
  const utils = render(TransactionRulesCard);
  if (rules.length > 0) await screen.findByText(rules[0].match_value);
  else await screen.findByText('No rules in this scope yet.');
  return utils;
}

/**
 * Pick an option from a bits-ui Select.
 *
 * Items commit on pointerup, not click, and jsdom synthesizes neither from a
 * `click()` — the portfolio card's test found the same thing.
 */
async function pick(triggerName: string, optionName: string) {
  const trigger = screen.getAllByRole('button', { name: triggerName })[0];
  await fireEvent.keyDown(trigger, { key: 'Enter' });
  const option = await screen.findByRole('option', { name: optionName });
  await fireEvent.pointerDown(option);
  await fireEvent.pointerUp(option);
  await fireEvent.click(option);
}

async function openKebab(name: string, item: string) {
  await fireEvent.keyDown(screen.getByLabelText(name), { key: 'Enter' });
  await fireEvent.click(await screen.findByText(item));
}

describe('TransactionRulesCard', () => {
  it('renders the rules in the order the store returned them', async () => {
    // Evaluation order is (priority, id) and the store already sorted on it.
    // Re-sorting here — or rendering a map — would make the list disagree with
    // the pass it claims to describe, which is the section's whole claim.
    await mount([
      rule({ id: 12, priority: 50, match_value: 'Personal', action: 'skip', target: '' }),
      rule({ id: 31, priority: 100, match_value: 'Software' }),
      rule({ id: 44, priority: 900, match_value: 'Groceries', origin: 'seed' }),
    ]);
    const values = screen.getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(values.map((t) => t.includes('Personal'))).toEqual([true, false, false]);
    expect(values[2]).toContain('Groceries');
    expect(document.querySelector('table')).toBeNull();
  });

  it('shows the match, the action and the provenance on a row', async () => {
    await mount([rule({ origin: 'migrated' })]);
    const row = screen.getByText('Software').closest('li');
    expect(row?.textContent).toContain('category');
    expect(row?.textContent).toContain('Expenses:Business:Software');
    expect(row?.textContent).toContain('migrated');
  });

  it('marks a disabled rule, which the editor shows and an import ignores', async () => {
    await mount([rule({ enabled: false })]);
    const row = screen.getByText('Software').closest('li');
    expect(row?.textContent).toContain('disabled');
  });

  it('unions the configured ledgers with the scopes rules already name', async () => {
    // A ledger renamed in the money TOML leaves its rules behind at the old
    // name. A picker built from the configuration alone cannot select that
    // scope, so those rules can be neither seen nor moved.
    mockedLedgers.mockResolvedValue(['personal']);
    await mount([rule({ ledger: 'archive', match_value: 'Software' })]);
    await fireEvent.keyDown(screen.getByRole('button', { name: 'Filter by ledger' }), {
      key: 'Enter',
    });
    expect(await screen.findByRole('option', { name: 'personal' })).toBeTruthy();
    expect(await screen.findByRole('option', { name: 'archive' })).toBeTruthy();
  });

  it('offers one option for a ledger the config and a rule spell differently', async () => {
    // The store matches `ledger` case-insensitively, so `Personal` and
    // `personal` are one scope; two entries would select the same rules.
    mockedLedgers.mockResolvedValue(['personal']);
    await mount([rule({ ledger: 'Personal' })]);
    await fireEvent.keyDown(screen.getByRole('button', { name: 'Filter by ledger' }), {
      key: 'Enter',
    });
    const options = (await screen.findAllByRole('option')).map((o) => o.textContent?.trim());
    expect(options.filter((o) => o?.toLowerCase() === 'personal')).toEqual(['personal']);
  });

  it('refuses to add until a scope is chosen, rather than defaulting to any', async () => {
    // Both scope columns default to '' server-side and the engine reads '' as
    // "any", so a create that omits them is a rule applying to every ledger
    // and every source. The widest scope has to be chosen, not fallen into.
    await mount([]);
    await fireEvent.input(screen.getByLabelText('Match value'), {
      target: { value: 'Software' },
    });
    await fireEvent.input(screen.getByLabelText('Target account'), {
      target: { value: 'Expenses:Business:Software' },
    });
    expect(screen.getByRole('button', { name: 'Add rule' })).toHaveProperty('disabled', true);
    await fireEvent.click(screen.getByRole('button', { name: 'Add rule' }));
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it('refuses to add a mapping rule with no target', async () => {
    await mount([]);
    await pick('Rule ledger', 'personal');
    await pick('Rule source', 'monarch-api');
    await fireEvent.input(screen.getByLabelText('Match value'), {
      target: { value: 'Software' },
    });
    expect(screen.getByRole('button', { name: 'Add rule' })).toHaveProperty('disabled', true);
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it('creates a rule with the scope explicitly on the body', async () => {
    await mount([]);
    await pick('Rule ledger', 'personal');
    await pick('Rule source', 'monarch-api');
    await fireEvent.input(screen.getByLabelText('Match value'), {
      target: { value: ' Software ' },
    });
    await fireEvent.input(screen.getByLabelText('Target account'), {
      target: { value: 'Expenses:Business:Software' },
    });
    mockedCreate.mockResolvedValue({ status: 'ok', rule: rule() });

    await fireEvent.click(screen.getByRole('button', { name: 'Add rule' }));
    expect(mockedCreate).toHaveBeenCalledWith({
      ledger: 'personal',
      source: 'monarch-api',
      field: 'category',
      match_kind: 'iexact',
      match_value: 'Software',
      action: 'posting_account',
      target: 'Expenses:Business:Software',
      priority: 100,
      origin: 'user',
    });
  });

  it('sends only the fields an edit changed', async () => {
    // The route merges a partial onto the stored row and validates the whole
    // merge, so sending the untouched fields back is not wrong — it is a
    // silent overwrite of anything the CLI or the agent changed meanwhile.
    await mount([rule()]);
    await openKebab('Actions for rule 31', 'Edit');
    await fireEvent.input(screen.getByLabelText('Priority'), { target: { value: '10' } });
    mockedUpdate.mockResolvedValue({ status: 'ok', rule: rule({ priority: 10 }) });

    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(mockedUpdate).toHaveBeenCalledWith(31, { priority: 10 });
  });

  it('sends nothing when an edit changed nothing', async () => {
    await mount([rule()]);
    await openKebab('Actions for rule 31', 'Edit');
    await fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(mockedUpdate).not.toHaveBeenCalled();
  });

  it('prefills the add form from Duplicate rather than posting a copy', async () => {
    // A copy of the same scope, match and action hits the unique index, so the
    // only useful duplicate is one the user has changed first.
    await mount([rule()]);
    await openKebab('Actions for rule 31', 'Duplicate');
    expect(screen.getByLabelText('Match value')).toHaveProperty('value', 'Software');
    expect(screen.getByLabelText('Target account')).toHaveProperty(
      'value',
      'Expenses:Business:Software',
    );
    expect(mockedCreate).not.toHaveBeenCalled();
  });

  it('confirms before deleting, and deletes on confirm', async () => {
    await mount([rule()]);
    await openKebab('Actions for rule 31', 'Delete');
    expect(mockedDelete).not.toHaveBeenCalled();
    mockedDelete.mockResolvedValue({ status: 'ok', removed: true });
    await fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(mockedDelete).toHaveBeenCalledWith(31);
  });

  it('reloads the list against the picked scope', async () => {
    await mount([rule()]);
    mockedList.mockClear();
    mockedList.mockResolvedValue({ status: 'ok', rules: [] });
    await pick('Filter by ledger', 'personal');
    expect(mockedList).toHaveBeenCalledWith(expect.objectContaining({ ledger: 'personal' }));
  });

  it('reports a load failure in the card rather than an empty list', async () => {
    mockedList.mockRejectedValue(new Error('money DB not configured'));
    render(TransactionRulesCard);
    expect(await screen.findByText(/money DB not configured/)).toBeTruthy();
  });
});

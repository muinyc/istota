import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import TransactionRuleCoverageCard from './TransactionRuleCoverageCard.svelte';
import { getTransactionRuleCoverage } from '$lib/money/api';
import type { RuleCoverageValue } from '$lib/money/api';

vi.mock('$lib/money/api', () => ({
  getTransactionRuleCoverage: vi.fn(),
}));

vi.mock('$lib/stores/notices', () => ({
  notifyError: vi.fn(),
  notifyInfo: vi.fn(),
  notifySuccess: vi.fn(),
}));

const mockedCoverage = vi.mocked(getTransactionRuleCoverage);

function value(overrides: Partial<RuleCoverageValue> = {}): RuleCoverageValue {
  return {
    value: 'Software',
    count: 12,
    last_seen: '2026-08-30',
    posted_account: 'Expenses:Business:Software',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

afterEach(async () => {
  await fireEvent.keyDown(document.body, { key: 'Escape' });
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
});

async function mount(
  values: RuleCoverageValue[],
  extra: { untraced?: number; field?: string } = {},
) {
  mockedCoverage.mockResolvedValue({
    status: 'ok',
    field: extra.field ?? 'category',
    values,
    ...(extra.untraced === undefined ? {} : { untraced: extra.untraced }),
  });
  const utils = render(TransactionRuleCoverageCard);
  if (values.length > 0) await screen.findByText(values[0].value);
  else await screen.findByText('No imports yet.');
  return utils;
}

describe('TransactionRuleCoverageCard', () => {
  it('sorts the rows falling through to Uncategorized first', async () => {
    // The endpoint orders by count, which buries the short tail of values no
    // rule covers — and that tail is the list this card exists to shorten.
    await mount([
      value({ value: 'Software', count: 12 }),
      value({
        value: 'Widgets',
        count: 2,
        posted_account: 'Expenses:Uncategorized:Widgets',
      }),
      value({ value: 'Groceries', count: 7 }),
    ]);
    const rows = screen.getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(rows[0]).toContain('Widgets');
    expect(rows[1]).toContain('Software');
    expect(rows[2]).toContain('Groceries');
  });

  it('flags a fallthrough row rather than only reordering it', async () => {
    await mount([
      value({ value: 'Widgets', posted_account: 'Expenses:Uncategorized:Widgets' }),
      value(),
    ]);
    const flagged = screen.getByText('Widgets').closest('li');
    expect(flagged?.textContent).toMatch(/no rule/i);
    expect(screen.getByText('Software').closest('li')?.textContent).not.toMatch(/no rule/i);
  });

  it('does not flag the bare Uncategorized-prefixed account of a real rule', async () => {
    // The fallback is `Expenses:Uncategorized` and `Expenses:Uncategorized:…`.
    // A user's own `Expenses:UncategorizedTravel` is a rule doing its job, and
    // a prefix test with no separator would call it a gap forever.
    await mount([value({ value: 'Travel', posted_account: 'Expenses:UncategorizedTravel' })]);
    expect(screen.getByText('Travel').closest('li')?.textContent).not.toMatch(/no rule/i);
  });

  it('shows the count and when the value was last seen', async () => {
    await mount([value()]);
    const row = screen.getByText('Software').closest('li');
    expect(row?.textContent).toContain('12');
    expect(row?.textContent).toContain('2026-08-30');
  });

  it('accounts for the rows synced before rule tracing in one line', async () => {
    // Their source category was never stored and reading it back out of the
    // posted account is lossy, so there is no value to list — only a count.
    await mount([value()], { untraced: 431 });
    expect(screen.getByText(/431 transactions synced before rule tracing/)).toBeTruthy();
  });

  it('says nothing about tracing when nothing is untraced', async () => {
    await mount([value()], { untraced: 0 });
    expect(screen.queryByText(/synced before rule tracing/)).toBeNull();
  });

  it('renders an empty state rather than an empty list', async () => {
    await mount([]);
    expect(screen.getByText('No imports yet.')).toBeTruthy();
  });

  it('re-reads against the account column when the field is switched', async () => {
    // `untraced` counts rows with no source *category* and says nothing about
    // the account column, so it must not follow the switch.
    await mount([value()], { untraced: 5 });
    mockedCoverage.mockResolvedValue({
      status: 'ok',
      field: 'account',
      values: [value({ value: 'Chase Business', posted_account: 'Assets:Bank:Chase' })],
    });
    const trigger = screen.getByRole('button', { name: 'Coverage field' });
    await fireEvent.keyDown(trigger, { key: 'Enter' });
    const option = await screen.findByRole('option', { name: 'Source accounts' });
    await fireEvent.pointerDown(option);
    await fireEvent.pointerUp(option);
    await fireEvent.click(option);

    expect(mockedCoverage).toHaveBeenLastCalledWith(expect.objectContaining({ field: 'account' }));
    expect(await screen.findByText('Chase Business')).toBeTruthy();
    expect(screen.queryByText(/synced before rule tracing/)).toBeNull();
  });

  it('reports a load failure in the card', async () => {
    mockedCoverage.mockRejectedValue(new Error('money DB not configured'));
    render(TransactionRuleCoverageCard);
    expect(await screen.findByText(/money DB not configured/)).toBeTruthy();
  });
});

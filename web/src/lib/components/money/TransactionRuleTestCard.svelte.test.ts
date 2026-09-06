import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, cleanup, screen, fireEvent } from '@testing-library/svelte';
import TransactionRuleTestCard from './TransactionRuleTestCard.svelte';
import { getLedgers, getTransactionRules, testTransactionRule } from '$lib/money/api';
import type { RuleResolution, RuleTraceEntry } from '$lib/money/api';

vi.mock('$lib/money/api', () => ({
  // Same shape as the real one, so `instanceof` and `.status` behave: the
  // constructor takes the parsed error envelope, not a message.
  ApiError: class ApiError extends Error {
    status: number;
    constructor(status: number, payload: { error?: string } | null) {
      super(payload?.error || `API error: ${status}`);
      this.status = status;
    }
  },
  getLedgers: vi.fn(),
  getTransactionRules: vi.fn(),
  testTransactionRule: vi.fn(),
}));

vi.mock('$lib/stores/notices', () => ({
  notifyError: vi.fn(),
  notifyInfo: vi.fn(),
  notifySuccess: vi.fn(),
}));

const mockedTest = vi.mocked(testTransactionRule);
const mockedLedgers = vi.mocked(getLedgers);
const mockedList = vi.mocked(getTransactionRules);

function trace(overrides: Partial<RuleTraceEntry> = {}): RuleTraceEntry {
  return {
    rule_id: 31,
    priority: 100,
    ledger: 'personal',
    source: 'monarch-api',
    field: 'category',
    match_kind: 'iexact',
    match_value: 'Software',
    action: 'posting_account',
    target: 'Expenses:Business:Software',
    origin: 'user',
    outcome: 'applied',
    shadowed_by: null,
    ...overrides,
  };
}

function resolution(overrides: Partial<RuleResolution> = {}): RuleResolution {
  return {
    skip: false,
    posting_account: 'Expenses:Business:Software',
    contra_account: null,
    considered: 3,
    hits: [{ rule_id: 31, action: 'posting_account', target: 'Expenses:Business:Software' }],
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedLedgers.mockResolvedValue(['personal']);
  mockedList.mockResolvedValue({ status: 'ok', rules: [] });
});

afterEach(async () => {
  await fireEvent.keyDown(document.body, { key: 'Escape' });
  cleanup();
  await new Promise((resolve) => setTimeout(resolve, 0));
});

async function mountAndRun(
  body: Partial<{ resolution: RuleResolution; trace: RuleTraceEntry[]; dropped: number[] }> = {},
  fill: Record<string, string> = { Category: 'Software' },
) {
  render(TransactionRuleTestCard);
  await screen.findByRole('button', { name: 'Run' });
  for (const [label, value] of Object.entries(fill)) {
    await fireEvent.input(screen.getByLabelText(label), { target: { value } });
  }
  mockedTest.mockResolvedValue({
    status: 'ok',
    resolution: body.resolution ?? resolution(),
    trace: body.trace ?? [trace()],
    dropped: body.dropped ?? [],
  });
  await fireEvent.click(screen.getByRole('button', { name: 'Run' }));
  await screen.findByText('Resolution');
}

describe('TransactionRuleTestCard', () => {
  it('posts the made-up transaction with an explicit scope', async () => {
    await mountAndRun({}, { Category: ' Software ', Account: 'Chase Business', Tags: 'a, b' });
    expect(mockedTest).toHaveBeenCalledWith({
      ledger: 'personal',
      source: 'monarch-api',
      category: 'Software',
      account: 'Chase Business',
      payee: '',
      notes: '',
      tags: ['a', 'b'],
    });
  });

  it('renders the accounts the pass resolved', async () => {
    await mountAndRun({
      resolution: resolution({ contra_account: 'Assets:Bank:Chase' }),
    });
    expect(screen.getByText('Expenses:Business:Software')).toBeTruthy();
    expect(screen.getByText('Assets:Bank:Chase')).toBeTruthy();
  });

  it('says a slot fell back rather than leaving it blank', async () => {
    // `null` is not "nothing happens" — the import path fills the slot from
    // `Expenses:Uncategorized:{slug}` or the profile's default account, and a
    // blank cell would read as a rule that posts nowhere.
    await mountAndRun({ resolution: resolution({ contra_account: null }) });
    const contra = screen.getByText('Contra account').closest('div');
    expect(contra?.textContent).toContain('falls back');
  });

  it('reports a skip as a skip, not as two empty accounts', async () => {
    await mountAndRun({
      resolution: resolution({
        skip: true,
        posting_account: null,
        contra_account: null,
        hits: [{ rule_id: 12, action: 'skip', target: '' }],
      }),
      trace: [trace({ rule_id: 12, action: 'skip', target: '', match_value: 'Personal' })],
    });
    expect(screen.getByText(/not imported/i)).toBeTruthy();
  });

  it('names the rule that shadowed a match', async () => {
    // The resolution alone cannot express this: the rule matched and filled
    // nothing, so a user editing priorities has to be told which rule held
    // the slot.
    await mountAndRun({
      trace: [
        trace({ rule_id: 31, outcome: 'applied' }),
        trace({ rule_id: 44, priority: 900, outcome: 'shadowed', shadowed_by: 31 }),
      ],
    });
    const row = screen.getByText(/rule 44/).closest('li');
    expect(row?.textContent).toMatch(/shadowed/i);
    expect(row?.textContent).toContain('31');
  });

  it('tells superseded-by-skip apart from shadowed', async () => {
    // Neighbouring outcomes with different causes: `shadowed` is "a
    // higher-priority rule already held this slot", `superseded_by_skip` is
    // "this rule held it, and the pass then short-circuited on a skip".
    await mountAndRun({
      resolution: resolution({ skip: true, posting_account: null }),
      trace: [
        trace({ rule_id: 31, outcome: 'superseded_by_skip' }),
        trace({ rule_id: 12, priority: 200, action: 'skip', target: '', outcome: 'applied' }),
        trace({ rule_id: 44, priority: 900, outcome: 'not_evaluated' }),
      ],
    });
    const superseded = screen.getByText(/rule 31/).closest('li');
    expect(superseded?.textContent).toMatch(/discarded/i);
    expect(superseded?.textContent).not.toMatch(/shadowed/i);
    expect(screen.getByText(/rule 44/).closest('li')?.textContent).toMatch(/not reached/i);
  });

  it('reports the rows the engine could not compile', async () => {
    // Dropping a `skip` widens what gets imported, so this cannot stay in a
    // log line.
    await mountAndRun({ dropped: [77] });
    expect(screen.getByText(/77/)).toBeTruthy();
  });

  it('says the preview covers enabled rules only', async () => {
    await mountAndRun();
    expect(screen.getByText(/enabled rules/i)).toBeTruthy();
  });

  it('explains a 409 as a migration that has not completed', async () => {
    const { ApiError } = await import('$lib/money/api');
    render(TransactionRuleTestCard);
    await screen.findByRole('button', { name: 'Run' });
    await fireEvent.input(screen.getByLabelText('Category'), { target: { value: 'Software' } });
    mockedTest.mockRejectedValue(new ApiError(409, { error: 'migration has not completed' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    expect(await screen.findByText(/migration/i)).toBeTruthy();
  });
});

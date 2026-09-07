import type { AccountRow } from '$lib/money/api';

export interface AccountNode {
  name: string;
  fullName: string;
  balance: string;
  children: AccountNode[];
  depth: number;
}

export function buildTree(rows: AccountRow[], defaultExpanded?: Set<string>): AccountNode[] {
  const root: AccountNode[] = [];
  const nodeMap = new Map<string, AccountNode>();

  for (const row of rows) {
    const parts = row.account.split(':');
    let current = root;
    let path = '';

    for (let i = 0; i < parts.length; i++) {
      path = path ? `${path}:${parts[i]}` : parts[i];
      let node = nodeMap.get(path);
      if (!node) {
        node = {
          name: parts[i],
          fullName: path,
          balance: '',
          children: [],
          depth: i,
        };
        nodeMap.set(path, node);
        current.push(node);
        if (defaultExpanded && i < 1) defaultExpanded.add(path);
      }
      if (i === parts.length - 1) {
        node.balance = row['sum(position)'] || '';
      }
      current = node.children;
    }
  }

  return root;
}

export function shouldInvert(account: string): boolean {
  return /^(Income|Liabilities|Equity):/.test(account);
}

export function invertAmount(s: string): string {
  return s.replace(/-?([\d,]+\.?\d*)/, (m) => {
    if (m.startsWith('-')) return m.slice(1);
    return '-' + m;
  });
}

export function displayBalance(pos: string, account: string): string {
  if (!pos || pos.trim() === '') return '';
  return shouldInvert(account) ? invertAmount(pos) : pos;
}

/**
 * Extract the numeric value from a beancount position string like "1234.56 USD".
 * For multi-commodity positions (containing commas between amounts), returns NaN.
 */
export function parseAmount(s: string): number {
  if (!s || s.trim() === '') return 0;
  const trimmed = s.trim();
  // Multi-commodity: "100 USD, 10 AAPL" — can't reduce to single number
  if (/\d\s+[A-Z].*,/.test(trimmed)) return NaN;
  const match = trimmed.match(/^-?([\d,]+\.?\d*)/);
  if (!match) return 0;
  const num = parseFloat(match[0].replace(/,/g, ''));
  return num;
}

/**
 * Sum balances for a list of account rows, applying sign inversion where appropriate.
 * Returns the total and the currency string (e.g., "USD").
 */
export function sumBalances(rows: AccountRow[]): { total: number; currency: string } {
  let total = 0;
  let currency = '';
  for (const row of rows) {
    const pos = row['sum(position)'] || '';
    const amount = parseAmount(pos);
    if (isNaN(amount)) continue;
    const inv = shouldInvert(row.account) ? -amount : amount;
    total += inv;
    if (!currency) {
      const m = pos.match(/[A-Z]{2,}/);
      if (m) currency = m[0];
    }
  }
  return { total, currency };
}

// Lives in `$lib/format` now, beside the bare `formatDecimal` the six
// components that re-implemented this one were actually after. Re-exported
// here because every money surface imports it from this module.
export { formatAmount, formatDecimal } from '$lib/format';

import { base } from '$app/paths';

// One class, not two. This module declared a byte-identical `AuthError` of its
// own and exported it, so `e instanceof AuthError` was `false` across the two
// modules — latent only because no file imported from both.
import { AuthError } from '$lib/api';

/**
 * A non-OK response, carrying the status and the parsed error envelope.
 *
 * Callers that only render `.message` are unaffected; the work page needs
 * `.status` to tell a 409 conflict from an ordinary failure, and `.payload`
 * to show the current server-side row.
 */
class ApiError extends Error {
  status: number;
  payload: any;

  constructor(status: number, payload: any) {
    super(payload?.error || `API error: ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // `base` is istota's URL prefix (e.g. /istota). Money's routes live under /api/money.
  const resp = await fetch(`${base}/api/money${path}`, {
    ...init,
    credentials: 'same-origin',
  });
  if (resp.status === 401) throw new AuthError();
  if (!resp.ok) {
    let payload: any = null;
    try {
      payload = await resp.json();
    } catch {
      // Non-JSON error body — the status alone has to carry the message.
    }
    throw new ApiError(resp.status, payload);
  }
  return resp.json();
}

export interface AccountRow {
  account: string;
  'sum(position)': string;
}

export interface AccountsResponse {
  status: string;
  accounts: AccountRow[];
}

export interface TransactionRow {
  date: string;
  flag: string;
  payee: string;
  narration: string;
  account: string;
  position: string;
  /** Stable transaction id (beancount `id:` metadata). Empty for un-backfilled legacy rows. */
  id?: string;
}

export interface TransactionsResponse {
  status: string;
  transactions: TransactionRow[];
  total: number;
  page: number;
  per_page: number;
}

export async function getAccounts(opts?: {
  ledger?: string;
  year?: number;
}): Promise<AccountsResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.year) params.set('year', String(opts.year));
  const qs = params.toString();
  return apiFetch<AccountsResponse>(`/accounts${qs ? '?' + qs : ''}`);
}

export async function getTransactions(opts?: {
  ledger?: string;
  account?: string;
  year?: number;
  filter?: string;
  page?: number;
  per_page?: number;
}): Promise<TransactionsResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.account) params.set('account', opts.account);
  if (opts?.year) params.set('year', String(opts.year));
  if (opts?.filter) params.set('filter', opts.filter);
  if (opts?.page) params.set('page', String(opts.page));
  if (opts?.per_page) params.set('per_page', String(opts.per_page));
  const qs = params.toString();
  return apiFetch<TransactionsResponse>(`/transactions${qs ? '?' + qs : ''}`);
}

export interface ReportResponse {
  status: string;
  report_type: string;
  year: number;
  row_count: number;
  results: AccountRow[];
}

export interface CheckResponse {
  status: string;
  message: string;
  error_count: number;
  errors?: string[];
}

export async function getReport(
  type: string,
  opts?: { ledger?: string; year?: number },
): Promise<ReportResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.year) params.set('year', String(opts.year));
  const qs = params.toString();
  return apiFetch<ReportResponse>(`/report/${type}${qs ? '?' + qs : ''}`);
}

export interface CashFlowRow {
  year: string;
  month: string;
  account: string;
  'sum(position)': string;
}

export interface CashFlowResponse {
  status: string;
  report_type: string;
  year: number;
  row_count: number;
  results: CashFlowRow[];
}

export async function getCashFlow(opts?: {
  ledger?: string;
  year?: number;
}): Promise<CashFlowResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.year) params.set('year', String(opts.year));
  const qs = params.toString();
  return apiFetch<CashFlowResponse>(`/report/cash-flow${qs ? '?' + qs : ''}`);
}

export async function checkLedger(opts?: { ledger?: string }): Promise<CheckResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  const qs = params.toString();
  return apiFetch<CheckResponse>(`/check${qs ? '?' + qs : ''}`);
}

export interface PostingRow {
  account: string;
  position: string;
}

export interface PostingsResponse {
  status: string;
  postings: PostingRow[];
}

export async function getPostings(opts: {
  ledger?: string;
  date: string;
  payee: string;
  narration: string;
  account?: string;
  position?: string;
}): Promise<PostingsResponse> {
  const params = new URLSearchParams();
  if (opts.ledger) params.set('ledger', opts.ledger);
  params.set('date', opts.date);
  params.set('payee', opts.payee);
  params.set('narration', opts.narration);
  if (opts.account) params.set('account', opts.account);
  if (opts.position) params.set('position', opts.position);
  return apiFetch<PostingsResponse>(`/postings?${params.toString()}`);
}

export interface EntityRow {
  key: string;
  name: string;
  address: string;
  email: string;
  payment_instructions: string;
  logo: string;
  ar_account: string;
  bank_account: string;
  currency: string;
}

export interface ServiceRow {
  key: string;
  display_name: string;
  rate: number;
  type: string;
  income_account: string;
}

export interface BusinessDefaults {
  currency: string;
  default_entity: string;
  default_ar_account: string;
  default_bank_account: string;
  invoice_output: string;
  next_invoice_number: number;
  notifications: string;
  days_until_overdue: number;
}

export interface BusinessSettingsResponse {
  status: string;
  entities: EntityRow[];
  services: ServiceRow[];
  /** null when the user has no invoicing configuration yet. */
  defaults: BusinessDefaults | null;
}

export async function getBusinessSettings(): Promise<BusinessSettingsResponse> {
  return apiFetch<BusinessSettingsResponse>('/business-settings');
}

export interface ClientRow {
  key: string;
  name: string;
  email: string;
  address: string;
  terms: number | string;
  entity: string;
  entity_name: string;
  schedule: string;
  schedule_day: number;
  ar_account: string;
}

export interface ClientsResponse {
  status: string;
  clients: ClientRow[];
}

export async function getClients(): Promise<ClientsResponse> {
  return apiFetch<ClientsResponse>('/clients');
}

/**
 * Invoicing configuration — the editable side of clients, entities and
 * services.
 *
 * Two rules every caller here depends on:
 *
 * - **Send `""`, never `null`, to clear an optional field.** The store skips
 *   `null` values when merging, so a null silently preserves the old value
 *   while the form shows the field as cleared.
 * - **Omit `bundles` and `separate` entirely.** The merge preserves what's
 *   stored, which is why the client form can leave them out without shipping
 *   a nested-list editor.
 */
export interface ClientConfigRow {
  key: string;
  name: string;
  address: string;
  email: string;
  terms: number | string;
  ar_account: string;
  /** Raw — `''` means "fall back to default_entity", unlike ClientRow.entity. */
  entity: string;
  schedule: string;
  schedule_day: number;
  reminder_days: number;
  notifications: string;
  days_until_overdue: number;
  ledger_posting: boolean;
  bundles: Record<string, unknown>[];
  separate: string[];
}

export type ClientInput = Partial<Omit<ClientConfigRow, 'key' | 'bundles' | 'separate'>>;
export type EntityInput = Partial<Omit<EntityRow, 'key'>>;
export type ServiceInput = Partial<Omit<ServiceRow, 'key'>>;

/**
 * The record-key rule, mirroring `config_store._KEY_RE`.
 *
 * Defined once here and imported by every form: a change to the rule (the
 * lowercase client requirement below arrived that way) otherwise has to be
 * chased through each of them, and a form that drifts rejects a key the
 * server accepts or vice versa.
 */
export const KEY_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
export const KEY_HINT = 'Letters, digits, - and _ only';

/**
 * Client keys are lowercase-only, unlike entities and services.
 *
 * `add_work_entry` stores the client lowercased, so a mixed-case key matches
 * no work entry and every one of that client's rows is skipped at invoice
 * time — the work is silently never billed. The form lowercases as you type so
 * the key you see is the key you get; the server rejects the rest.
 */
export function normalizeClientKey(value: string): string {
  return value.toLowerCase();
}

/** Counts of what pointed at a record — carried on delete responses and 409s. */
export interface ConfigReferences {
  work_entries?: number;
  invoices?: number;
  clients?: string[];
  default_entity?: boolean;
  /** Clients with a blank entity — they bill under whichever one is default. */
  default_for_clients?: number;
  /**
   * Year files holding a row this version can't read. Non-empty means the
   * counts above are lower bounds, so the two strict deletes refuse.
   */
  quarantined?: string[];
}

export interface ConfigDeleteResponse {
  status: string;
  removed: boolean;
  references: ConfigReferences;
}

/**
 * Clients as stored, with no defaults resolved into them.
 *
 * Distinct from `getClients()`, which resolves `entity` and `ar_account`
 * through the business defaults for display. Binding an edit form to the
 * resolved shape would *materialise* the default onto the record on save, so
 * a later change to `default_entity` would stop propagating to a client that
 * never had an explicit one.
 */
export async function getClientConfigs(): Promise<{
  status: string;
  clients: ClientConfigRow[];
}> {
  return apiFetch('/config/clients');
}

function writeJson<T>(path: string, method: string, body: unknown): Promise<T> {
  return apiFetch<T>(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/**
 * PUT a record that must already exist.
 *
 * The routes upsert by default, for `ensure`-style CLI callers. The forms only
 * ever edit a record they just loaded, so `?create=false` makes a key another
 * tab deleted meanwhile a 404 instead of resurrecting a partial record built
 * from this form's fields plus defaults for everything the form doesn't show.
 */
function updateExisting<T>(path: string, body: unknown): Promise<T> {
  return writeJson<T>(`${path}?create=false`, 'PUT', body);
}

export async function createClient(
  key: string,
  input: ClientInput,
): Promise<{ status: string; client: ClientConfigRow }> {
  return writeJson('/config/clients', 'POST', { key, ...input });
}

export async function updateClient(
  key: string,
  input: ClientInput,
): Promise<{ status: string; client: ClientConfigRow }> {
  return updateExisting(`/config/clients/${encodeURIComponent(key)}`, input);
}

export async function deleteClient(key: string): Promise<ConfigDeleteResponse> {
  return apiFetch(`/config/clients/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

export async function createEntity(
  key: string,
  input: EntityInput,
): Promise<{ status: string; company: EntityRow }> {
  return writeJson('/config/companies', 'POST', { key, ...input });
}

export async function updateEntity(
  key: string,
  input: EntityInput,
): Promise<{ status: string; company: EntityRow }> {
  return updateExisting(`/config/companies/${encodeURIComponent(key)}`, input);
}

export async function deleteEntity(key: string): Promise<ConfigDeleteResponse> {
  return apiFetch(`/config/companies/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

export async function createService(
  key: string,
  input: ServiceInput,
): Promise<{ status: string; service: ServiceRow }> {
  return writeJson('/config/services', 'POST', { key, ...input });
}

export async function updateService(
  key: string,
  input: ServiceInput,
): Promise<{ status: string; service: ServiceRow }> {
  return updateExisting(`/config/services/${encodeURIComponent(key)}`, input);
}

export async function deleteService(key: string): Promise<ConfigDeleteResponse> {
  return apiFetch(`/config/services/${encodeURIComponent(key)}`, { method: 'DELETE' });
}

export interface InvoiceRow {
  invoice_number: string;
  client: string;
  client_key: string;
  date: string;
  total: number;
  status: string;
  paid_date?: string;
}

export interface InvoicesResponse {
  status: string;
  invoice_count: number;
  outstanding_count: number;
  invoices: InvoiceRow[];
}

export async function getInvoices(opts?: {
  client?: string;
  show_all?: boolean;
}): Promise<InvoicesResponse> {
  const params = new URLSearchParams();
  if (opts?.client) params.set('client', opts.client);
  if (opts?.show_all) params.set('show_all', 'true');
  const qs = params.toString();
  return apiFetch<InvoicesResponse>(`/invoices${qs ? '?' + qs : ''}`);
}

export interface InvoiceDetailItem {
  description: string;
  detail: string;
  quantity: number;
  rate: number;
  discount: number;
  amount: number;
}

export interface InvoiceDetailsResponse {
  status: string;
  invoice_number: string;
  items: InvoiceDetailItem[];
}

export async function getInvoiceDetails(invoice_number: string): Promise<InvoiceDetailsResponse> {
  const params = new URLSearchParams({ invoice_number });
  return apiFetch<InvoiceDetailsResponse>(`/invoice-details?${params.toString()}`);
}

export interface InvoiceActionResponse {
  status: string;
  invoice_number: string;
  count: number;
  paid_date?: string;
}

/** Mark an invoice paid (sets paid_date; does not post a ledger payment). */
export async function markInvoicePaid(
  invoice_number: string,
  opts?: { paid_date?: string },
): Promise<InvoiceActionResponse> {
  return apiFetch<InvoiceActionResponse>(
    `/invoices/${encodeURIComponent(invoice_number)}/mark-paid`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paid_date: opts?.paid_date }),
    },
  );
}

/** Un-pay an invoice (clears paid_date, keeps the invoice number). */
export async function markInvoicePending(invoice_number: string): Promise<InvoiceActionResponse> {
  return apiFetch<InvoiceActionResponse>(
    `/invoices/${encodeURIComponent(invoice_number)}/mark-pending`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
  );
}

/** URL for the generated invoice PDF — open in a new tab / download. */
export function invoicePdfUrl(invoice_number: string): string {
  return `${base}/api/money/invoices/${encodeURIComponent(invoice_number)}/pdf`;
}

/** Work entries — the input side of invoicing. */
export interface WorkEntryRow {
  /** Stable id. Empty until the backfill runs; such a row isn't editable. */
  uid: string;
  /** 1-based display index. Presentation only — it shifts under concurrent writes. */
  index: number | null;
  /** Content hash, echoed back on write so a stale edit 409s instead of silently reverting. */
  etag: string;
  date: string;
  client: string;
  client_name: string;
  service: string;
  service_name: string;
  service_type: string;
  qty: number | null;
  amount: number | null;
  discount: number;
  description: string;
  entity: string;
  invoice: string;
  paid_date: string | null;
  /** What this entry will bill for, using the same rate rules the invoice uses. */
  computed_amount: number | null;
  editable: boolean;
  /** 'unknown_service' | 'unknown_client' | 'no_uid' */
  warnings: string[];
}

export interface WorkTotals {
  uninvoiced_count: number;
  uninvoiced_amount: number;
  invoiced_count: number;
  paid_count: number;
}

export interface WorkEntriesResponse {
  status: string;
  entries: WorkEntryRow[];
  totals: WorkTotals;
}

export type WorkStatusFilter = 'uninvoiced' | 'invoiced' | 'paid' | 'all';

export interface WorkEntryInput {
  date: string;
  client: string;
  service: string;
  qty?: number | null;
  amount?: number | null;
  discount?: number;
  description?: string;
  entity?: string;
}

export async function getWorkEntries(opts?: {
  client?: string;
  period?: string;
  status?: WorkStatusFilter;
}): Promise<WorkEntriesResponse> {
  const params = new URLSearchParams();
  if (opts?.client) params.set('client', opts.client);
  if (opts?.period) params.set('period', opts.period);
  if (opts?.status) params.set('status', opts.status);
  const qs = params.toString();
  return apiFetch<WorkEntriesResponse>(`/work${qs ? '?' + qs : ''}`);
}

export async function createWorkEntry(
  input: WorkEntryInput,
): Promise<{ status: string; entry: WorkEntryRow }> {
  return apiFetch<{ status: string; entry: WorkEntryRow }>('/work', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}

/** Update an entry by uid. Pass the row's `etag` so a concurrent edit conflicts. */
export async function updateWorkEntry(
  uid: string,
  patch: Partial<WorkEntryInput> & { etag?: string },
): Promise<{ status: string; entry: WorkEntryRow }> {
  return apiFetch<{ status: string; entry: WorkEntryRow }>(`/work/${encodeURIComponent(uid)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

export async function deleteWorkEntry(
  uid: string,
  opts?: { etag?: string },
): Promise<{ status: string; uid: string }> {
  const params = new URLSearchParams();
  if (opts?.etag) params.set('etag', opts.etag);
  const qs = params.toString();
  return apiFetch<{ status: string; uid: string }>(
    `/work/${encodeURIComponent(uid)}${qs ? '?' + qs : ''}`,
    { method: 'DELETE' },
  );
}

export interface TransactionUpdate {
  // Stable id of the transaction to edit.
  id: string;
  // Identifies which posting (leg) to edit when an account repeats.
  old_account?: string;
  old_position?: string;
  // New values.
  new_payee?: string;
  new_narration?: string;
  new_account?: string;
  new_position?: string;
  new_date?: string;
  ledger?: string;
}

/**
 * Edit a transaction, located by its stable `id:` metadata. The backend
 * rewrites the directive in place and re-validates with `bean-check`; an edit
 * that unbalances the entry is rolled back and surfaced as a 422 error.
 */
export async function updateTransaction(payload: TransactionUpdate): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/transactions/update`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export async function getLedgers(): Promise<string[]> {
  const resp = await apiFetch<{ ledgers: string[] }>('/ledgers');
  return resp.ledgers;
}

export interface TaxEstimateResponse {
  status: string;
  tax_year: number;
  quarter: number;
  method: string;
  filing_status: string;
  w2_months: number;
  annualization_months: number;
  se_income_ytd: number;
  se_income_annualized: number;
  w2_income: number;
  w2_income_annualized: number;
  se_tax: number;
  half_se_deduction: number;
  additional_medicare_tax: number;
  federal_agi: number;
  federal_standard_deduction: number;
  federal_taxable_income: number;
  federal_tax: number;
  qbi_deduction: number;
  state_agi: number;
  state_standard_deduction: number;
  state_taxable_income: number;
  /** Non-zero for the states that carry an exemption instead of a deduction. */
  state_personal_exemption: number;
  state_tax: number;
  federal_withholding: number;
  state_withholding: number;
  federal_estimated_paid: number;
  state_estimated_paid: number;
  federal_total_liability: number;
  state_total_liability: number;
  federal_net_due: number;
  state_net_due: number;
  federal_quarterly_amount: number;
  state_quarterly_amount: number;
  quarters_remaining: number;
  /**
   * The payroll figures actually used. Carried so the page's explainers quote
   * the resolved values rather than holding their own copy — the SE-tax panel
   * used to name '$176,100' inline and went stale the moment it moved.
   */
  ss_wage_base: number;
  se_taxable_fraction: number;
  /** Cumulative fraction of the state liability due by each quarter. */
  state_installment_schedule: number[];
  /** Two-letter code, or '' for no state tax. */
  state: string;
  state_name: string;
  /**
   * Which federal figure the state's tax starts from. The page's explainers
   * depend on it: for `federal_taxable_income` the QBI deduction is already
   * inside the base, so saying it does not apply is false.
   */
  state_starts_from: '' | 'federal_agi' | 'federal_taxable_income' | 'gross_compensation';
  /**
   * False with a reason is distinct from a zero liability: no state selected,
   * a state that levies no income tax, and a state we ship no brackets for all
   * produce no figures, and the page says which rather than rendering zeros.
   */
  state_available: boolean;
  state_unavailable_reason: '' | 'no_state' | 'no_income_tax' | 'no_brackets' | 'unknown_state';
  federal_rates: RateProvenance;
  /** Null when there is no state to have rates. */
  state_rates: RateProvenance | null;
}

/** Where a set of figures came from, and whether it can be trusted for the year. */
export interface RateProvenance {
  /** The year actually used — differs from `requested_year` on a fallback. */
  year: number | null;
  requested_year: number | null;
  is_fallback: boolean;
  is_stale: boolean;
  overridden: boolean;
  source: string;
  source_url: string;
  verified_on: string;
}

export interface TaxEstimateInputs {
  method?: string;
  w2_income?: number;
  w2_federal_withholding?: number;
  w2_state_withholding?: number;
  federal_estimated_paid?: number;
  state_estimated_paid?: number;
  w2_months?: number;
}

export async function getTaxEstimate(opts?: {
  ledger?: string;
  method?: string;
}): Promise<TaxEstimateResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  if (opts?.method) params.set('method', opts.method);
  const qs = params.toString();
  return apiFetch<TaxEstimateResponse>(`/tax/estimate${qs ? '?' + qs : ''}`);
}

export async function recalculateTaxEstimate(
  inputs: TaxEstimateInputs,
  opts?: { ledger?: string },
): Promise<TaxEstimateResponse> {
  const params = new URLSearchParams();
  if (opts?.ledger) params.set('ledger', opts.ledger);
  const qs = params.toString();
  return apiFetch<TaxEstimateResponse>(`/tax/estimate${qs ? '?' + qs : ''}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(inputs),
  });
}

// --- Portfolio (positions snapshots) ---

export interface PortfolioSnapshotRow {
  id: number;
  exported_at: string;
  exported_at_estimated: boolean;
  imported_at: string;
  source: string;
  source_file: string | null;
  position_count: number;
  /** Read-time total over non-excluded accounts. */
  total_value: number;
}

export interface PortfolioGroupSlice {
  key: string;
  value: number;
  pct: number;
}

export interface PortfolioAccountSlice extends PortfolioGroupSlice {
  account_id: number;
  group: string;
  account_type: string;
}

export interface PortfolioHolding {
  symbol: string;
  description: string;
  quantity: number | null;
  value: number;
  cost_basis: number | null;
  gain: number | null;
  gain_pct: number | null;
  asset_class: string;
  sub_class: string;
  geography: string;
  accounts: number;
}

export interface PortfolioSummary {
  snapshot_id: number;
  exported_at: string;
  exported_at_estimated: boolean;
  total_value: number;
  position_count: number;
  by_asset_class: PortfolioGroupSlice[];
  by_account: PortfolioAccountSlice[];
  by_account_type: PortfolioGroupSlice[];
  by_group: PortfolioGroupSlice[];
  by_geography: PortfolioGroupSlice[];
  holdings: PortfolioHolding[];
}

export interface PortfolioHistoryPoint {
  snapshot_id: number;
  exported_at: string;
  exported_at_estimated: boolean;
  total: number;
  groups?: Record<string, number>;
}

export interface PortfolioAccount {
  id: number;
  account_name: string;
  account_number: string;
  group: string;
  account_type: string;
  excluded: boolean;
  first_seen_at: string;
  last_seen_at: string;
}

export interface PortfolioClassification {
  symbol: string;
  asset_class: string;
  sub_class: string;
  geography: string;
  /** 'seed' | 'auto' | 'user' | '' (row predating provenance). */
  source: string;
  updated_at: string;
}

export interface PortfolioAutoClassified {
  symbol: string;
  asset_class: string;
  sub_class: string;
  geography: string;
  method: 'lookup' | 'heuristic';
}

export interface PortfolioImportResult {
  status: string;
  snapshot_id?: number;
  exported_at?: string;
  exported_at_estimated?: boolean;
  position_count?: number;
  total_value?: number;
  new_accounts?: string[];
  unclassified_symbols?: string[];
  auto_classified?: PortfolioAutoClassified[];
  warnings?: string[];
  source_file?: string;
  dry_run?: boolean;
  snapshots?: {
    exported_at: string;
    exported_at_estimated: boolean;
    source: string;
    position_count: number;
    total_value: number;
    warnings: string[];
  }[];
  /** Multi-snapshot (fina history) import. */
  imported?: number;
  duplicates?: number;
  results?: PortfolioImportResult[];
  /** Same-day collision. */
  existing?: { id: number; exported_at: string; position_count: number };
}

export interface PortfolioDiffEntry {
  symbol: string;
  account_name: string;
  quantity: number;
  value: number;
}

export interface PortfolioDiffChange {
  symbol: string;
  account_name: string;
  quantity_from: number;
  quantity_to: number;
  value_from: number;
  value_to: number;
}

export interface PortfolioDiff {
  older_id: number;
  newer_id: number;
  opened: PortfolioDiffEntry[];
  closed: PortfolioDiffEntry[];
  changed: PortfolioDiffChange[];
}

export interface PortfolioSymbolHistory {
  symbol: string;
  points: {
    snapshot_id: number;
    exported_at: string;
    quantity: number | null;
    price: number | null;
    value: number | null;
  }[];
}

export async function importPortfolioFile(
  file: File,
  opts?: { dryRun?: boolean; replace?: number; force?: boolean; source?: string },
): Promise<PortfolioImportResult> {
  const params = new URLSearchParams();
  if (opts?.dryRun) params.set('dry_run', '1');
  if (opts?.replace != null) params.set('replace', String(opts.replace));
  if (opts?.force) params.set('force', '1');
  if (opts?.source) params.set('source', opts.source);
  const qs = params.toString();
  const form = new FormData();
  form.append('file', file);
  return apiFetch<PortfolioImportResult>(`/portfolio/import${qs ? '?' + qs : ''}`, {
    method: 'POST',
    body: form,
  });
}

export async function getPortfolioSnapshots(): Promise<{
  status: string;
  snapshots: PortfolioSnapshotRow[];
}> {
  return apiFetch('/portfolio/snapshots');
}

export async function getPortfolioSnapshotSummary(
  id: number,
  opts?: { group?: string },
): Promise<{ status: string; summary: PortfolioSummary }> {
  const params = new URLSearchParams();
  if (opts?.group) params.set('group', opts.group);
  const qs = params.toString();
  return apiFetch(`/portfolio/snapshots/${id}${qs ? '?' + qs : ''}`);
}

export async function deletePortfolioSnapshot(
  id: number,
): Promise<{ status: string; deleted: number }> {
  return apiFetch(`/portfolio/snapshots/${id}`, { method: 'DELETE' });
}

export async function getPortfolioSummary(opts?: {
  group?: string;
}): Promise<{ status: string; summary: PortfolioSummary | null }> {
  const params = new URLSearchParams();
  if (opts?.group) params.set('group', opts.group);
  const qs = params.toString();
  return apiFetch(`/portfolio/summary${qs ? '?' + qs : ''}`);
}

export async function getPortfolioHistory(opts?: {
  groupBy?: 'total' | 'group' | 'account_type' | 'asset_class';
  group?: string;
}): Promise<{ status: string; group_by: string; series: PortfolioHistoryPoint[] }> {
  const params = new URLSearchParams();
  if (opts?.groupBy) params.set('group_by', opts.groupBy);
  if (opts?.group) params.set('group', opts.group);
  const qs = params.toString();
  return apiFetch(`/portfolio/history${qs ? '?' + qs : ''}`);
}

export async function getPortfolioDiff(
  older: number,
  newer: number,
): Promise<{ status: string; diff: PortfolioDiff }> {
  return apiFetch(`/portfolio/diff?older=${older}&newer=${newer}`);
}

export async function getPortfolioSymbolHistory(
  symbol: string,
): Promise<{ status: string; history: PortfolioSymbolHistory }> {
  return apiFetch(`/portfolio/symbols/${encodeURIComponent(symbol)}/history`);
}

export async function getPortfolioAccounts(): Promise<{
  status: string;
  accounts: PortfolioAccount[];
}> {
  return apiFetch('/portfolio/accounts');
}

export async function patchPortfolioAccount(
  id: number,
  fields: { group?: string; account_type?: string; excluded?: boolean },
): Promise<{ status: string; account: PortfolioAccount }> {
  return apiFetch(`/portfolio/accounts/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  });
}

export async function getPortfolioClassifications(): Promise<{
  status: string;
  classifications: PortfolioClassification[];
}> {
  return apiFetch('/portfolio/classifications');
}

export async function putPortfolioClassification(
  symbol: string,
  fields: { asset_class: string; sub_class?: string; geography?: string },
): Promise<{ status: string; classification: PortfolioClassification }> {
  return apiFetch(`/portfolio/classifications/${encodeURIComponent(symbol)}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(fields),
  });
}

export async function autoClassifyPortfolio(): Promise<{
  status: string;
  classified: PortfolioAutoClassified[];
  unresolved: string[];
  // False when the ticker-metadata tier could not be used at all — the
  // optional dependency is missing, the operator turned the lookup off, or
  // every attempt failed. Distinguishes "we tried and could not tell" from
  // "we never asked", which is otherwise invisible from the outside.
  lookups_available?: boolean;
}> {
  return apiFetch('/portfolio/classifications/auto', { method: 'POST' });
}

export async function deletePortfolioClassification(symbol: string): Promise<{ status: string }> {
  return apiFetch(`/portfolio/classifications/${encodeURIComponent(symbol)}`, {
    method: 'DELETE',
  });
}

export { AuthError, ApiError };

// =============================================================================
// Tax configuration
// =============================================================================

export interface TaxSettings {
  filing_status: 'mfj' | 'single';
  tax_year: number;
  /** Two-letter code, or '' for no state tax. */
  state: string;
  w2_income: number;
  w2_federal_withholding: number;
  w2_state_withholding: number;
  federal_estimated_paid: number;
  state_estimated_paid: number;
  enable_qbi_deduction: boolean;
  prior_year_federal_tax: number;
  prior_year_state_tax: number;
}

export interface TaxJurisdiction {
  code: string;
  name: string;
  /** False for the nine states with no broad-based income tax. */
  taxes_income: boolean;
  /** False means selectable but override-driven — say so before they pick. */
  has_bundled_data: boolean;
  note: string;
}

/** One rate field, plus whether it is the user's number or the shipped one. */
export interface ResolvedField<T> {
  value: T;
  overridden: boolean;
}

export interface ResolvedStateRates {
  code: string;
  name: string;
  taxes_income: boolean;
  available: boolean;
  reason: '' | 'no_income_tax' | 'no_brackets' | 'unknown_state';
  starts_from: string;
  installment_schedule: number[] | null;
  standard_deduction: ResolvedField<number | null>;
  brackets: ResolvedField<number[][]>;
  provenance: RateProvenance;
}

export interface ResolvedRates {
  status: string;
  tax_year: number;
  filing_status: string;
  federal: {
    standard_deduction: ResolvedField<number | null>;
    brackets: ResolvedField<number[][]>;
    provenance: RateProvenance;
  };
  payroll: Record<string, ResolvedField<number | null>>;
  /** Null when no state is selected. */
  state: ResolvedStateRates | null;
}

export interface TaxSchedule {
  tax_year: number;
  jurisdiction: string;
  filing_status: string;
  brackets: number[][] | null;
  standard_deduction: number | null;
}

export async function getTaxSettings(): Promise<TaxSettings> {
  const resp = await apiFetch<{ tax: TaxSettings }>('/config/tax');
  return resp.tax;
}

export async function updateTaxSettings(patch: Partial<TaxSettings>): Promise<void> {
  await apiFetch('/config/tax', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

export async function getTaxJurisdictions(): Promise<TaxJurisdiction[]> {
  const resp = await apiFetch<{ jurisdictions: TaxJurisdiction[] }>('/config/tax/jurisdictions');
  return resp.jurisdictions;
}

export async function getResolvedTaxRates(opts?: {
  year?: number;
  filingStatus?: string;
  /** '' is a real selection (no state tax); omit the key to use the saved one. */
  state?: string;
}): Promise<ResolvedRates> {
  const params = new URLSearchParams();
  if (opts?.year) params.set('year', String(opts.year));
  if (opts?.filingStatus) params.set('filing_status', opts.filingStatus);
  if (opts?.state !== undefined) params.set('state', opts.state);
  const qs = params.toString();
  return apiFetch<ResolvedRates>(`/config/tax/resolved${qs ? '?' + qs : ''}`);
}

/**
 * Upsert one override.
 *
 * An omitted key leaves that field alone; an explicit `null` reverts it to the
 * bundled value. `null` cannot mean both, which is why the two are distinct.
 */
export async function putTaxSchedule(
  year: number,
  jurisdiction: string,
  filingStatus: string,
  patch: { brackets?: number[][] | null; standard_deduction?: number | null },
): Promise<void> {
  await apiFetch(`/config/tax/schedules/${year}/${jurisdiction}/${filingStatus}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

export async function deleteTaxSchedule(
  year: number,
  jurisdiction: string,
  filingStatus: string,
): Promise<void> {
  await apiFetch(`/config/tax/schedules/${year}/${jurisdiction}/${filingStatus}`, {
    method: 'DELETE',
  });
}

export async function putTaxYearRates(
  year: number,
  patch: Record<string, number | null>,
): Promise<void> {
  await apiFetch(`/config/tax/years/${year}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

// =============================================================================
// Transaction rules
// =============================================================================
//
// `''` is a real value on both scope columns and means "any", so every scope
// argument below is tested with `!== undefined` rather than for truthiness:
// `?ledger=` selects the rules written at the any-ledger scope, while omitting
// the key drops the filter entirely. Collapsing the two is the one mistake
// this client can make on its own.

export type RuleField = 'category' | 'account' | 'payee' | 'notes' | 'tag';
export type RuleMatchKind = 'exact' | 'iexact' | 'contains';
export type RuleAction = 'posting_account' | 'contra_account' | 'skip';

export interface TransactionRule {
  id: number;
  /** '' means any ledger. */
  ledger: string;
  /** An `ImportSource.name` — 'monarch-api', 'monarch-csv' — or '' for any. */
  source: string;
  field: RuleField;
  match_kind: RuleMatchKind;
  match_value: string;
  action: RuleAction;
  /** A beancount account, and '' for a `skip`, which takes no target. */
  target: string;
  /** Evaluation order, low first; the id breaks a tie. */
  priority: number;
  enabled: boolean;
  /** 'seed' | 'migrated' | 'user' | '' (a row written before provenance). */
  origin: string;
  note: string;
  created_at: string;
  updated_at: string;
}

/**
 * The fields a create sends.
 *
 * `ledger` and `source` are required here as well as server-side, because the
 * widest scope has to be chosen rather than defaulted into: both columns
 * default to '' and the engine reads '' as "any", so an omitted ledger is a
 * rule silently applying to every ledger and every source.
 *
 * `origin` excludes 'seed', which the store reserves for the shipped rule set.
 */
export interface NewTransactionRule {
  ledger: string;
  source: string;
  field: RuleField;
  match_value: string;
  action: RuleAction;
  match_kind?: RuleMatchKind;
  target?: string;
  priority?: number;
  enabled?: boolean;
  origin?: 'user' | 'migrated' | '';
  note?: string;
}

/** A rule that filled a slot. Never a rule that matched and was shadowed. */
export interface RuleHit {
  rule_id: number;
  action: RuleAction;
  target: string;
}

export interface RuleResolution {
  skip: boolean;
  /** null where no rule filled the slot; the import path supplies a fallback. */
  posting_account: string | null;
  contra_account: string | null;
  /** Rules evaluated, the terminating `skip` included. */
  considered: number;
  /**
   * The rules that filled a slot — and for a `skip` resolution, only the
   * skip: the engine replaces the list rather than appending to it, so a
   * mapping rule that fired earlier in the pass is not here. Read `trace`
   * for those; `superseded_by_skip` is what they carry.
   */
  hits: RuleHit[];
}

/**
 * What one rule did on a preview pass. One entry per *enabled* rule in
 * scope, which is the set an import is scored against and so is shorter than
 * the editor's list beside it.
 *
 * `shadowed` is the outcome the resolution alone cannot express — the rule
 * matched, but `shadowed_by` had already filled its slot — and it is what a
 * user editing priorities needs to see. `shadowed_by` is non-null exactly
 * there and null everywhere else. `superseded_by_skip` is the neighbouring
 * case: the rule did hold its slot, and a later `skip` then emptied it, so
 * nothing is posted and no rule beat it. `not_evaluated` means a `skip` ended
 * the pass before this rule was reached; `ignored` means an action this
 * release has no slot for.
 *
 * `field`, `match_kind` and `action` are the wire's strings rather than the
 * unions above, because `ignored` exists precisely for a row whose `action`
 * is outside `RuleAction` — a closed union would be a type that lies on the
 * one path that produces it. Compare against the unions; keep a default arm.
 */
export interface RuleTraceEntry {
  rule_id: number;
  priority: number;
  ledger: string;
  source: string;
  /** A `RuleField`, or anything a hand-edited row carries. */
  field: string;
  /** A `RuleMatchKind`, or anything a hand-edited row carries. */
  match_kind: string;
  match_value: string;
  /** A `RuleAction`, or anything a hand-edited row carries. */
  action: string;
  target: string;
  origin: string;
  outcome: 'applied' | 'shadowed' | 'superseded_by_skip' | 'no_match' | 'not_evaluated' | 'ignored';
  shadowed_by: number | null;
}

export interface RuleCoverageValue {
  /** A source category or source account as the import carried it. */
  value: string;
  count: number;
  last_seen: string | null;
  /** What the most recent row posted to, not what the rules say now. */
  posted_account: string | null;
}

export async function getTransactionRules(scope?: {
  ledger?: string;
  source?: string;
  includeDisabled?: boolean;
}): Promise<{ status: string; rules: TransactionRule[] }> {
  const params = new URLSearchParams();
  if (scope?.ledger !== undefined) params.set('ledger', scope.ledger);
  if (scope?.source !== undefined) params.set('source', scope.source);
  if (scope?.includeDisabled === false) params.set('include_disabled', 'false');
  const qs = params.toString();
  return apiFetch(`/config/transaction-rules${qs ? '?' + qs : ''}`);
}

export async function createTransactionRule(
  rule: NewTransactionRule,
): Promise<{ status: string; rule: TransactionRule }> {
  return apiFetch('/config/transaction-rules', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rule),
  });
}

/** An omitted key leaves that field alone; the whole merged row is validated. */
export async function updateTransactionRule(
  id: number,
  patch: Partial<NewTransactionRule>,
): Promise<{ status: string; rule: TransactionRule }> {
  return apiFetch(`/config/transaction-rules/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

/** `removed: false` for an id that was already gone — not an error. */
export async function deleteTransactionRule(
  id: number,
): Promise<{ status: string; removed: boolean }> {
  return apiFetch(`/config/transaction-rules/${id}`, { method: 'DELETE' });
}

/**
 * Resolve a made-up transaction against the stored rules.
 *
 * Evaluates enabled rules only, the same set an import runs against. A 409
 * means the deployment's one-time rule migration has not completed, so an
 * import still resolves from the legacy maps and there is nothing honest to
 * preview; read it off `ApiError.status`.
 */
export async function testTransactionRule(input: {
  ledger: string;
  source: string;
  category?: string;
  account?: string;
  payee?: string;
  notes?: string;
  tags?: string[];
}): Promise<{
  status: string;
  resolution: RuleResolution;
  trace: RuleTraceEntry[];
  /** Ids of rows the engine could not compile, so it dropped them. */
  dropped: number[];
}> {
  return apiFetch('/config/transaction-rules/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  });
}

/**
 * Distinct source values recent imports carried, and what they posted to.
 *
 * `untraced` comes back only for `field: 'category'` — it counts rows with no
 * stored source category, which is the set the category list excludes and
 * says nothing about the account column.
 *
 * `profile` scopes the read to one profile's rows; omitting it is every
 * profile, and `''` selects what a profile-less sync wrote.
 */
export async function getTransactionRuleCoverage(opts?: {
  field?: 'category' | 'account';
  limit?: number;
  profile?: string;
}): Promise<{
  status: string;
  field: string;
  values: RuleCoverageValue[];
  untraced?: number;
}> {
  const params = new URLSearchParams();
  if (opts?.field !== undefined) params.set('field', opts.field);
  if (opts?.limit !== undefined) params.set('limit', String(opts.limit));
  if (opts?.profile !== undefined) params.set('profile', opts.profile);
  const qs = params.toString();
  return apiFetch(`/config/transaction-rules/coverage${qs ? '?' + qs : ''}`);
}

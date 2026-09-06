# Money

Beancount-backed accounting with a web dashboard: ledger queries, transactions, invoicing, a work log, quarterly tax estimates, and investment portfolio tracking. The `money` package is vendored in-tree and runs in-process — no external service, no HTTP hop, no subprocess.

The `money` module is on by default. Opt out per user with `disabled_modules = ["money"]`.

## Ledgers

Istota auto-discovers `*.beancount` files at the top level of `{workspace}/ledgers/`. There is no per-resource path to declare.

```bash
istota-skill money list                      # available ledgers
istota-skill money check                     # bean-check a ledger
istota-skill money balances                  # account balances
istota-skill money query "<bql>"             # BQL query
istota-skill money report                    # financial report
istota-skill money add-transaction …
istota-skill money edit-transaction …        # by stable id
istota-skill money import-csv …
```

The same operations are reachable operator-side as `istota money <op> -u USER`. See the [CLI reference](../reference/cli.md#money).

## Transaction sync

Monarch Money sync uses a stored cookie pair (`session_id`, `csrftoken`) in the encrypted secrets table — both keys are required. `debug-monarch` is a whoami probe for checking the credentials are still live before blaming the sync.

```bash
istota secret ensure --user alice --service monarch --key session_id --value …
istota secret ensure --user alice --service monarch --key csrftoken  --value …
```

Imports are content-hash deduped, so re-running one is safe.

### Transaction rules

Which accounts an imported transaction posts to is decided by one ordered rule table, shared by every importer. A rule matches one field of the normalized transaction (`category`, `account`, `payee`, `notes` or `tag`) by `exact`, `iexact` (the default, case-insensitive equality) or `contains`, and fills one action slot: `posting_account`, `contra_account`, or `skip` to drop the transaction outright. Scope is a ledger and a source (`monarch-api`, a CSV import); `''` in either column means "any", which is why both have to be typed rather than defaulted into on a write.

`priority` alone decides order — lower first, 100 by default. A single pass runs over the rules in scope and each slot is filled by the first rule that matches it, so one transaction can take its posting account from one rule and its contra account from another; a matching `skip` ends the pass. Nothing is ranked implicitly: a `contains` rule does not lose to an `exact` one. An unfilled slot falls back to what the importer did before rules existed — `Expenses:Uncategorized:<slug>` for the posting account, the profile's default account (or the CSV import's `--account`) for the contra account.

The old Monarch category map, account map and exclude-tag filters are views over the same table, and keep working; `monarch-category-map` is still the shorter thing to type for a plain category-to-account mapping. Rules carry an `origin` of `user`, `migrated` or `seed` so a hand-written rule is distinguishable from a migrated map entry or the shipped default.

Edit them in the money settings page under **Transactions**, which also shows what recent imports matched and what fell through, or from the CLI:

```bash
istota money rules list   -u USER [--ledger NAME] [--source monarch-api] [--enabled-only]
istota money rules add    -u USER --ledger personal --source monarch-api \
                          --field category --match-value "Software" \
                          --action posting_account --target Expenses:Business:Software
istota money rules update -u USER --id 7 --target Expenses:Business:Tools
istota money rules remove -u USER --id 7
istota money rules test   -u USER --ledger personal --source monarch-api --category Software
```

`test` resolves a transaction you describe on the command line against the stored rules and reports what they would do with it, importing nothing. It refuses on a deployment where the one-time migration from the legacy maps has not completed, since a preview drawn from the table would describe behaviour the deployment does not have. Editing a rule does not move transactions already in a ledger — correct those with `edit-transaction`.

The agent-side surface is `istota-skill money transaction-rules list|set|test`; it has no delete verb, so removing a rule is an operator command.

### Settling invoices from the feed

A sync books incoming payments into the ledger. It also closes the invoice each one paid, where it can say which: a credit that fits exactly one open invoice — same amount, issued no later than the payment — marks that invoice paid. It records the payment directly rather than routing through `invoice paid`, because the sync has already written the income to the ledger and going through the command would post it twice.

Anything ambiguous is reported for you to settle rather than guessed at. Two open invoices at the same amount, two credits fitting one invoice, or a credit that fits to the cent but predates the invoice — all are named in the output and left alone. Three shapes of invoice are excluded from matching outright, because for each the total on hand is not the amount owed: partly paid ones, fully paid ones, and any invoice with a line whose service has left the config, which makes it look cheaper than it is.

`--tolerance` allows for a wire fee. `--no-match-invoices` turns matching off. `invoice unpaid` undoes a match that was wrong — `invoice void` is not the inverse, since it clears the invoice number and un-invoices the work.

Matching runs once across all sync profiles rather than once per profile. `sync_all_profiles` fetches from Monarch once and dedups per profile, so two profiles can each book a credit fitting the same invoice; a per-profile pass would let whichever ran first settle it and leave the other unreported, with profile order deciding the winner.

## Business

The web dashboard's Business section is **Work | Invoices | Clients**. Work is a full CRUD surface over the file-based work-entry store — entries addressed by stable id, with per-entry etags so a concurrent agent edit conflicts loudly instead of being silently reverted. Clients, together with the money settings page, is the CRUD surface over the invoicing config (clients, entities, services), so nothing about invoicing needs the CLI.

### Invoice dates

An invoice is a set of work entries sharing an `invoice` string. Entries carry an `invoice_date`, stamped inside the same work-lock acquisition as the number so the two cannot diverge, and cleared by `void_invoice`. Generation passes the date it already put on the PDF rather than taking a fresh `date.today()`, which would disagree with the document across midnight. One function, `invoice_issue_date`, is what the matcher and both invoice lists read, so they cannot drift apart.

Both invoice lists used to show the *earliest* work on an invoice under a column labelled its date. They show the issue date now.

Invoices raised before this have no date and nothing can reconstruct one, so they keep a fallback: the latest work billed on them, which is also what their listed date now shows. Hand-attaching a forgotten entry to an already-issued invoice inherits that invoice's date rather than stamping today — otherwise the whole invoice moves forward and the bound passes the payment that actually settled it. An entry joining a pre-field invoice stays unstamped instead of being handed a synthesized date.

## Taxes

A quarterly estimated-tax calculator at `/money/taxes` — the one page in the module that computes a number you send to a government, which shapes how it is built.

Rate data is versioned data, not constants: a bundled registry carries federal and per-state years, each naming the document it was transcribed from and the date it was last checked. Three signals fall out of that provenance and render on the page: attribution per jurisdiction, a **missing-year warning** when resolution fell back to a different year, and an **age warning** when the last check predates the tax year. A year the authority has not published yet resolves to the previous year's table *and says so*, rather than reporting last year's numbers as this year's.

State tax is a real dimension rather than a California special case: `state = ""` (the default) means no state tax, and an unsupported code returns an explicit reason (`no_income_tax`, `no_brackets`, `unknown_state`) so the page drops the state column entirely instead of rendering a misleading zero. Installment schedules are per-state.

Rates are deliberately not fetched from anywhere. The IRS publishes nothing machine-readable, and fetching would not have prevented the class of bug this design targets — a tax year whose *structure* changed returns current-looking thresholds from a bracket API and still produces a wrong answer. What is wanted is knowing when the numbers are stale, which needs no dependency.

A disclaimer naming what is not modelled — local taxes, credits, AMT, itemizing — renders persistently on both the estimate and its settings page.

## Portfolio

Point-in-time investment tracking. Fidelity Portfolio Positions CSV exports import as **snapshots** into the per-user money DB, content-hash deduped. Snapshots never touch the beancount ledgers.

The account registry and symbol classifications are per-user data, auto-populated on import and editable at `/money/settings/portfolio`. Classification resolves at read time, so an edit retroactively reclassifies history. New symbols auto-classify on import via ticker-metadata lookup then offline description heuristics; an automatic write can never replace an existing row, so a user edit always wins — including a deliberate `Unclassified`. `[money] autoclass_lookup` gates the third-party lookup.

```bash
istota-skill money portfolio <import|snapshots|summary|history|diff|accounts|classify>
```

The web tab is **Overview | History | Import**: allocation charts, holdings, value over time, and snapshot diffs.

## Experimental

Two operations are behind operator feature flags — `lots` (tax lots, `money_tax`) and `wash-sales` (`money_wash_sales`). See [experimental features](../EXPERIMENTAL.md).

## Configuration

| Setting | Default | Purpose |
|---|---|---|
| `[money] autoclass_lookup` | `true` | Allow portfolio auto-classification to look up unknown symbols |

Everything else — clients, entities, services, tax config, portfolio accounts — lives in the per-user money DB, not in `config.toml`.

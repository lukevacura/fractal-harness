# Benchmark tasks

## fx-export
Add multi-currency reporting. (1) A new module `ledger/fx.py` with a `Rates` type loaded
from a JSON file mapping currency codes to their value in USD (e.g. {"USD": 1, "EUR": 1.1}),
and `convert(amount, from_ccy, to_ccy, rates)` using Decimal, raising `UnknownCurrency` for
codes not in the table. (2) In `ledger/report.py`, `monthly_summary(txns, rates, target)`
returning {month: {category: total spending in target currency}}, spending as positive
numbers, rounded to 2 decimals. (3) A new module `ledger/export.py` with
`write_summary_csv(summary, out)` writing rows `month,category,total` sorted by month then
category, with a header. (4) A CLI subcommand `ledger export FILE --rates RATES --currency
CCY [--out PATH]` wiring it together (stdout if no --out).

## budgets
Add budget alerts. (1) A new module `ledger/budgets.py` with `load_budgets(path)` reading a
JSON object {category: monthly limit in a currency} plus a top-level "currency" key, and
`Budget` objects. (2) A new module `ledger/alerts.py` with `overspend(txns, budgets)`
returning a list of `Alert(month, category, spent, limit, over_by)` for every month and
category whose spending exceeds its limit (single currency: txns in another currency than
the budget raise `CurrencyMismatch`). (3) `format_alert(alert)` producing a one-line
human-readable message, e.g. "2026-01 food: spent 120.00 of 100.00 (over by 20.00)".
(4) A CLI subcommand `ledger alerts FILE --budgets PATH` printing one line per alert and
exiting 1 if there are any alerts, 0 otherwise.

## import-pipeline
Add a bank-statement import pipeline with categorization rules and recurring-payment detection.

(1) Importers in a new package `ledger/importers/`, one module per format, each exposing
`parse(text: str) -> list[Transaction]`. All importers set category "uncategorized";
malformed input raises `ledger.parse.ParseError`.
- `bankxml`: `<statement currency="EUR"><tx date="2026-01-05" amount="-12.50">Lunch</tx>...</statement>`;
  the description is the element's text, stripped.
- `jsonexport`: `{"currency": "USD", "transactions": [{"booked": "05/01/2026", "value": -12.5, "memo": "Lunch"}]}`;
  `booked` is DD/MM/YYYY; `value` is a JSON number, converted via its string form and
  quantized to 2 decimals.
- `semicolon`: a `;`-separated CSV with header `Date;Amount;Currency;Text`; dates are
  DD.MM.YYYY; amounts use `.` as the thousands separator and `,` as the decimal separator
  (e.g. `-1.234,50`).

(2) Deduplication in `ledger/dedupe.py`: two transactions are duplicates when date, amount,
currency and normalized description are equal. Normalized description: lowercase, surrounding
whitespace stripped, internal whitespace collapsed to single spaces.

(3) CLI `ledger import FILE --format {bankxml,jsonexport,semicolon} --out OUT [--existing LEDGER]`:
writes OUT as a ledger CSV (header `date,amount,currency,category,description`, amounts with
exactly 2 decimals) containing the existing ledger's rows in their original order, followed by
the imported rows that are not duplicates, sorted by date (stable). Duplicates within the
imported file itself are also skipped (keep the first). Prints `imported N, skipped M duplicates`.

(4) Categorization rules in `ledger/rules.py`. A rules file is a JSON list of objects
`{"category": str, "pattern": regex, "min_amount": number (optional), "max_amount": number
(optional), "priority": int (optional, default 0)}`. A rule matches a transaction when the
pattern matches its description (case-insensitive `re.search`) and its signed amount is within
[min_amount, max_amount] where given (inclusive). The highest-priority matching rule wins;
ties go to the rule listed first. CLI `ledger categorize LEDGER --rules RULES --out OUT [--force]`:
assigns categories to rows whose category is "uncategorized" (to every row with --force),
leaves other rows and rows no rule matches unchanged, writes OUT in the same ledger CSV
format, and prints `categorized N` (N = rows whose category was set by a rule).

(5) Recurring detection in `ledger/recurring.py`: group spending transactions (amount < 0) by
(normalized description, currency). A group is recurring when it has at least
`min_occurrences` (default 3) transactions, every absolute amount is within 5% of the group's
median absolute amount, and every gap between consecutive dates (sorted) is 26 to 35 days
inclusive. CLI `ledger recurring LEDGER [--min-occurrences N]` prints one line per recurring
group, sorted by normalized description:
`<normalized description>\t<currency>\t<median absolute amount, 2 decimals>\t<count>\t<next expected date YYYY-MM-DD>`,
where next expected date = last date + round(median gap in days). Prints nothing if none.

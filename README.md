# Trade Reconciliation Engine

[![tests](https://github.com/Saiharshaa/trade-reconciliation-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/Saiharshaa/trade-reconciliation-engine/actions/workflows/tests.yml)

A small Python/pandas tool that reconciles an internal trade ledger against an external bank or broker statement, the same-day (T0) trade-matching check a trading-operations desk runs before settlement. It normalises differently-named headers, matches trades on trade ID, gives every trade one label (MATCHED or a specific break type), and writes an exception report with signed quantity and price differences and the notional impact of each break, so Ops can see which breaks matter most and chase those first. Everything in `samples/` is synthetic.

## Break types

Each trade ID gets exactly one label. If a trade fails several of the side, quantity and price checks, the label is the highest one in this table and the others are listed in the `Notes` column. A symbol mismatch is not compared further, because the two legs are different instruments.

| Label | Meaning |
|---|---|
| `MISSING_EXTERNAL` | Booked internally, not on the statement |
| `MISSING_INTERNAL` | On the statement, not booked internally |
| `DUPLICATE_ID` | Trade ID appears more than once on either side (reported once, not joined) |
| `SYMBOL_MISMATCH` | Same trade ID, different instrument |
| `SIDE_MISMATCH` | Internal BUY vs external SELL, or the reverse |
| `QTY_BREAK` | Quantities differ |
| `PRICE_BREAK` | Price difference beyond tolerance (`--price-tol-bps`, default 5; optional `--price-tol-abs`; a break must exceed both) |
| `MATCHED` | Everything agrees (price within tolerance) |

Exception report columns worth knowing:

* `QtyDiff`, `PriceDiff`, `NotionalImpact` are **internal minus external**. Quantities are signed by side (BUY +, SELL -), so `QtyDiff` and `NotionalImpact` read as the position difference. A missing side counts as zero, a side mismatch shows twice the trade size, and a symbol mismatch shows the internal leg (the external leg is a different instrument).
* `PriceDiffBps` is the price difference in basis points of the external price.
* Notional at risk in the summary is the sum of `|NotionalImpact|` per currency. There is no FX conversion.

## Quick start

```bash
pip install -r requirements.txt

# Demo run on the sample files
python trade_reconciliation_engine.py

# Your own files
python trade_reconciliation_engine.py --internal samples/internal_ledger.csv \
    --external samples/broker_statement.csv --price-tol-bps 5 --out reconciliation_report.csv
```

Required columns: trade ID, symbol, side, quantity, price, currency. Trade date and counterparty are carried into the report if present, and other columns (fees, status, ...) are ignored. Headers are matched case-insensitively with spaces and punctuation ignored, and common aliases are accepted, for example `RefID` / `Trade Ref` -> `TradeID`, `Ticker` -> `Symbol`, `B/S` -> `Side`, `Qty` -> `Quantity`, `Exec Price` -> `Price`, `CCY` -> `Currency` (see `COLUMN_ALIASES` in the script). The sample broker statement uses these alternative headers on purpose.

## Example output

Output of `python trade_reconciliation_engine.py --internal samples/internal_ledger.csv --external samples/broker_statement.csv --price-tol-bps 5 --out samples/reconciliation_report.csv`:

```text
Reconciliation summary
  Internal rows: 31   External rows: 30   Price tolerance: 5 bps

  MATCHED              21
  MISSING_EXTERNAL      2
  MISSING_INTERNAL      2
  DUPLICATE_ID          1
  SYMBOL_MISMATCH       1
  SIDE_MISMATCH         1
  QTY_BREAK             2
  PRICE_BREAK           2
  Total breaks         11

  Notional at risk (sum of |NotionalImpact| per currency, no FX conversion):
    HKD         387,650.00
    JPY         855,000.00
    SGD          27,590.00
    USD         353,318.00

Exception report: samples/reconciliation_report.csv (11 rows)
```

The resulting exception report is [`samples/reconciliation_report.csv`](samples/reconciliation_report.csv).

## Running the tests

```bash
pip install -r requirements.txt
pytest -q
```

The tests in `tests/` cover each break type, the tolerance boundary (exactly 5 bps matches, 5.5 bps breaks), absolute tolerance, duplicate IDs on either side, header normalisation, bad input and the CLI. GitHub Actions runs them on every push and pull request (Python 3.11).

## Project layout

```text
trade_reconciliation_engine.py   matching, break classification, summary and CLI
samples/
  internal_ledger.csv            synthetic internal ledger (31 rows, USD/SGD/HKD/JPY equities)
  broker_statement.csv           synthetic broker statement with different headers (30 rows)
  reconciliation_report.csv      exception report produced from the two files above
tests/test_reconciliation.py     pytest suite
requirements.txt                 pinned pandas and pytest
pytest.ini                       test path config
.github/workflows/tests.yml      CI
```

## Limitations

* Matches on trade ID only. No fuzzy matching on symbol, quantity or time when IDs are missing or different.
* Trade date, settlement date and fees are not compared.
* Notional at risk is reported per currency. Nothing is converted to a base currency.

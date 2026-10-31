"""Trade reconciliation engine.

Compares an internal trade ledger with an external bank/broker statement,
labels every trade as MATCHED or a specific break type, and writes an
exception report (breaks only) for Ops review.

Usage:
    python trade_reconciliation_engine.py              # demo run on samples/
    python trade_reconciliation_engine.py --internal samples/internal_ledger.csv \
        --external samples/broker_statement.csv --price-tol-bps 5 --out reconciliation_report.csv
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

SAMPLES_DIR = Path(__file__).resolve().parent / "samples"
DEFAULT_PRICE_TOL_BPS = 5.0

# Break labels. Keep these strings stable - reports and tests rely on them.
MATCHED = "MATCHED"
MISSING_EXTERNAL = "MISSING_EXTERNAL"  # booked internally, not on the statement
MISSING_INTERNAL = "MISSING_INTERNAL"  # on the statement, not booked internally
DUPLICATE_ID = "DUPLICATE_ID"          # trade ID appears more than once on a side
SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
SIDE_MISMATCH = "SIDE_MISMATCH"
QTY_BREAK = "QTY_BREAK"
PRICE_BREAK = "PRICE_BREAK"

# Report order: breaks first, MATCHED last
LABELS = [MISSING_EXTERNAL, MISSING_INTERNAL, DUPLICATE_ID, SYMBOL_MISMATCH,
          SIDE_MISMATCH, QTY_BREAK, PRICE_BREAK, MATCHED]

# Header aliases seen on different statements -> standard column names.
# Keys are lower-case with spaces and punctuation removed (see header_key).
COLUMN_ALIASES = {
    "tradeid": "TradeID", "refid": "TradeID", "traderef": "TradeID",
    "tradereference": "TradeID", "reference": "TradeID",
    "symbol": "Symbol", "ticker": "Symbol", "instrument": "Symbol", "security": "Symbol",
    "side": "Side", "buysell": "Side", "bs": "Side", "direction": "Side",
    "quantity": "Quantity", "qty": "Quantity", "shares": "Quantity", "units": "Quantity",
    "price": "Price", "execprice": "Price", "executionprice": "Price",
    "tradeprice": "Price", "px": "Price",
    "currency": "Currency", "ccy": "Currency", "tradecurrency": "Currency",
    "tradedate": "TradeDate", "date": "TradeDate",
    "counterparty": "Counterparty", "cpty": "Counterparty", "broker": "Counterparty",
}
REQUIRED_COLUMNS = ["TradeID", "Symbol", "Side", "Quantity", "Price", "Currency"]
OPTIONAL_COLUMNS = ["TradeDate", "Counterparty"]  # carried into the report if present

SIDE_ALIASES = {"B": "BUY", "BUY": "BUY", "BOT": "BUY",
                "S": "SELL", "SELL": "SELL", "SLD": "SELL"}

REPORT_COLUMNS = [
    "TradeID", "BreakType", "TradeDate", "Symbol", "Currency", "Counterparty",
    "Side_Internal", "Side_External", "Quantity_Internal", "Quantity_External",
    "Price_Internal", "Price_External", "QtyDiff", "PriceDiff", "PriceDiffBps",
    "NotionalImpact", "Notes",
]


def header_key(name):
    """'Trade Ref' / 'trade_ref' / 'TRADE-REF' -> 'traderef'."""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def normalise_columns(df):
    """Rename headers to the standard names and clean up the values."""
    df = df.rename(columns=lambda c: COLUMN_ALIASES.get(header_key(c), str(c).strip()))

    dupes = sorted(set(df.columns[df.columns.duplicated()]))
    if dupes:
        raise ValueError(f"More than one column maps to: {', '.join(dupes)}")
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(missing)}")

    df = df.copy()
    for col in OPTIONAL_COLUMNS:
        if col not in df.columns:
            df[col] = ""
    df = df[REQUIRED_COLUMNS + OPTIONAL_COLUMNS]

    for col in ["TradeID", "Symbol", "Currency", "TradeDate", "Counterparty"]:
        df[col] = df[col].fillna("").astype(str).str.strip()
    df["Symbol"] = df["Symbol"].str.upper()
    df["Currency"] = df["Currency"].str.upper()

    # B / S / Buy / Sell -> BUY / SELL
    side = df["Side"].fillna("").astype(str).str.strip().str.upper()
    unknown = sorted(set(side[~side.isin(list(SIDE_ALIASES))]))
    if unknown:
        raise ValueError(f"Unrecognised side value(s): {', '.join(unknown)}")
    df["Side"] = side.map(SIDE_ALIASES)

    # "1,000" -> 1000
    for col in ["Quantity", "Price"]:
        df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False))
    return df


def load_trades(path):
    """Read a CSV of trades and normalise its columns."""
    return normalise_columns(pd.read_csv(path, dtype=str, skipinitialspace=True))


def signed_qty(side, qty):
    """Position view: BUY is +qty, SELL is -qty."""
    return qty.where(side == "BUY", -qty)


def price_beyond_tolerance(price_diff, price_diff_bps, tol_bps, tol_abs):
    """True where a price difference is outside both the bps and the absolute tolerance."""
    eps = 1e-9  # ignore floating-point noise exactly at the boundary
    return (price_diff.abs() > tol_abs + eps) & (price_diff_bps.abs() > tol_bps + eps)


def duplicate_report(internal, external, dup_ids):
    """One DUPLICATE_ID row per repeated trade ID, with the net position difference."""
    rows = []
    for trade_id in sorted(dup_ids):
        ins = internal[internal["TradeID"] == trade_id]
        ext = external[external["TradeID"] == trade_id]
        first = pd.concat([ins, ext]).iloc[0]
        qty_in = signed_qty(ins["Side"], ins["Quantity"])
        qty_ext = signed_qty(ext["Side"], ext["Quantity"])
        rows.append({
            "TradeID": trade_id,
            "BreakType": DUPLICATE_ID,
            "TradeDate": first["TradeDate"],
            "Symbol": first["Symbol"],
            "Currency": first["Currency"],
            "Counterparty": first["Counterparty"],
            "QtyDiff": qty_in.sum() - qty_ext.sum(),
            "NotionalImpact": (qty_in * ins["Price"]).sum() - (qty_ext * ext["Price"]).sum(),
            "Notes": f"{len(ins)} internal row(s), {len(ext)} external row(s)",
        })
    return pd.DataFrame(rows, columns=REPORT_COLUMNS)


def reconcile(internal, external, price_tol_bps=DEFAULT_PRICE_TOL_BPS, price_tol_abs=0.0):
    """Match trades on TradeID and label each one. Returns one row per trade ID.

    Differences are internal minus external. QtyDiff and NotionalImpact use
    signed quantities (BUY +, SELL -), so they read as the position difference.
    """
    internal = normalise_columns(internal)
    external = normalise_columns(external)

    # 1. Pull out IDs repeated on either side - joining on them would multiply rows
    dup_ids = set(internal.loc[internal["TradeID"].duplicated(), "TradeID"]) | \
        set(external.loc[external["TradeID"].duplicated(), "TradeID"])
    dups = duplicate_report(internal, external, dup_ids)
    internal = internal[~internal["TradeID"].isin(dup_ids)]
    external = external[~external["TradeID"].isin(dup_ids)]

    # 2. Outer join so trades missing on either side still show up
    m = pd.merge(internal, external, on="TradeID", how="outer",
                 suffixes=("_Internal", "_External"), indicator=True)
    both = m["_merge"] == "both"
    symbol_break = both & (m["Symbol_Internal"] != m["Symbol_External"])
    paired = both & ~symbol_break  # same trade ID and same instrument on both sides

    # 3. Signed differences. A missing side counts as zero, and so does the
    #    external leg of a symbol mismatch (it is a different instrument).
    qty_in = signed_qty(m["Side_Internal"], m["Quantity_Internal"]).fillna(0)
    qty_ext = signed_qty(m["Side_External"], m["Quantity_External"]).fillna(0)
    qty_ext = qty_ext.where(~symbol_break, 0)
    m["QtyDiff"] = qty_in - qty_ext
    m["PriceDiff"] = (m["Price_Internal"] - m["Price_External"]).where(paired)
    m["PriceDiffBps"] = m["PriceDiff"] / m["Price_External"] * 10_000
    m["NotionalImpact"] = (qty_in * m["Price_Internal"].fillna(0)
                           - qty_ext * m["Price_External"].fillna(0))

    # 4. Checks in priority order - if a trade fails several, the first one is its label
    checks = [
        (MISSING_EXTERNAL, m["_merge"] == "left_only"),
        (MISSING_INTERNAL, m["_merge"] == "right_only"),
        (SYMBOL_MISMATCH, symbol_break),
        (SIDE_MISMATCH, paired & (m["Side_Internal"] != m["Side_External"])),
        (QTY_BREAK, paired & (m["Quantity_Internal"] != m["Quantity_External"])),
        (PRICE_BREAK, paired & price_beyond_tolerance(
            m["PriceDiff"], m["PriceDiffBps"], price_tol_bps, price_tol_abs)),
    ]
    m["BreakType"] = MATCHED
    for label, failed in checks:
        m.loc[failed & (m["BreakType"] == MATCHED), "BreakType"] = label

    # 5. Notes: extra breaks on the same trade, the other symbol, small price diffs that passed
    notes = pd.Series("", index=m.index)
    for label, failed in checks[3:]:
        extra = failed & (m["BreakType"] != label)
        notes[extra] = notes[extra] + "also " + label + "; "
    notes[symbol_break] = "external symbol " + m["Symbol_External"] + "; "
    in_tol = (m["BreakType"] == MATCHED) & (m["PriceDiff"].abs() > 1e-9)
    notes[in_tol] = notes[in_tol] + "price diff " + \
        m["PriceDiffBps"].round(2).astype(str) + " bps within tolerance; "
    m["Notes"] = notes.str.rstrip("; ")

    # 6. One value per descriptive column (internal first, external as fallback)
    for col in ["Symbol", "Currency", "TradeDate", "Counterparty"]:
        m[col] = m[col + "_Internal"].combine_first(m[col + "_External"])

    results = m[REPORT_COLUMNS] if dups.empty else pd.concat([m[REPORT_COLUMNS], dups])
    return tidy(results)


def tidy(results):
    """Sort by break type then trade ID, round numbers, show whole quantities as ints."""
    results = results.copy()
    results["BreakType"] = pd.Categorical(results["BreakType"], categories=LABELS, ordered=True)
    results = results.sort_values(["BreakType", "TradeID"]).reset_index(drop=True)
    results["BreakType"] = results["BreakType"].astype(str)

    for col in ["Quantity_Internal", "Quantity_External", "QtyDiff"]:
        values = pd.to_numeric(results[col])
        whole = (values.dropna() % 1 == 0).all()
        results[col] = values.round().astype("Int64") if whole else values
    for col, places in [("Price_Internal", 6), ("Price_External", 6), ("PriceDiff", 6),
                        ("PriceDiffBps", 2), ("NotionalImpact", 2)]:
        results[col] = pd.to_numeric(results[col]).round(places)
    return results


def exceptions(results):
    """Only the breaks - this is what goes to Ops."""
    return results[results["BreakType"] != MATCHED].reset_index(drop=True)


def summarise(results):
    """Counts per label and notional at risk (sum of |NotionalImpact|) per currency."""
    counts = results["BreakType"].value_counts().reindex(LABELS, fill_value=0)
    breaks = exceptions(results)
    at_risk = breaks["NotionalImpact"].abs().groupby(breaks["Currency"]).sum()
    return counts, at_risk


def format_summary(results, price_tol_bps, price_tol_abs, n_internal, n_external):
    counts, at_risk = summarise(results)
    tol = f"{price_tol_bps:g} bps"
    if price_tol_abs:
        tol += f" / {price_tol_abs:g} abs (break only if beyond both)"
    lines = [
        "Reconciliation summary",
        f"  Internal rows: {n_internal}   External rows: {n_external}   Price tolerance: {tol}",
        "",
    ]
    for label in [MATCHED] + LABELS[:-1]:
        lines.append(f"  {label:<18}{counts[label]:>5}")
    lines.append(f"  {'Total breaks':<18}{counts.drop(MATCHED).sum():>5}")
    lines.append("")
    lines.append("  Notional at risk (sum of |NotionalImpact| per currency, no FX conversion):")
    if at_risk.empty:
        lines.append("    none")
    for ccy, amount in at_risk.items():
        lines.append(f"    {ccy:<4}{amount:>18,.2f}")
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        description="Reconcile an internal trade ledger against a bank/broker statement.")
    parser.add_argument("--internal", default=SAMPLES_DIR / "internal_ledger.csv",
                        help="internal ledger CSV (default: samples/internal_ledger.csv)")
    parser.add_argument("--external", default=SAMPLES_DIR / "broker_statement.csv",
                        help="bank/broker statement CSV (default: samples/broker_statement.csv)")
    parser.add_argument("--price-tol-bps", type=float, default=DEFAULT_PRICE_TOL_BPS,
                        help=f"price tolerance in basis points (default: {DEFAULT_PRICE_TOL_BPS:g})")
    parser.add_argument("--price-tol-abs", type=float, default=0.0,
                        help="absolute price tolerance; a break must exceed both (default: 0)")
    parser.add_argument("--out", default="reconciliation_report.csv",
                        help="where to write the exception report (default: reconciliation_report.csv)")
    args = parser.parse_args(argv)

    if not argv:
        print("No input files given - demo run on the sample files in samples/\n")

    try:
        internal = load_trades(args.internal)
        external = load_trades(args.external)
        results = reconcile(internal, external, args.price_tol_bps, args.price_tol_abs)
    except (OSError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1

    report = exceptions(results)
    report.to_csv(args.out, index=False)
    print(format_summary(results, args.price_tol_bps, args.price_tol_abs,
                         len(internal), len(external)))
    print(f"\nException report: {args.out} ({len(report)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

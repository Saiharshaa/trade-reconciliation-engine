from pathlib import Path

import pandas as pd
import pytest

import trade_reconciliation_engine as tre

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def trades(*rows):
    """Build a small trade table: (TradeID, Symbol, Side, Quantity, Price[, Currency])."""
    cols = ["TradeID", "Symbol", "Side", "Quantity", "Price", "Currency"]
    return pd.DataFrame([row if len(row) == 6 else row + ("USD",) for row in rows], columns=cols)


def row_for(results, trade_id):
    match = results[results["TradeID"] == trade_id]
    assert len(match) == 1, f"expected one row for {trade_id}, got {len(match)}"
    return match.iloc[0]


# --- one test per break type ---------------------------------------------------

def test_identical_trade_is_matched():
    results = tre.reconcile(trades(("1", "AAPL", "BUY", 100, 150.0)),
                            trades(("1", "AAPL", "BUY", 100, 150.0)))
    r = row_for(results, "1")
    assert r["BreakType"] == tre.MATCHED
    assert r["QtyDiff"] == 0 and r["NotionalImpact"] == 0
    assert tre.exceptions(results).empty


def test_qty_break_has_signed_diff_and_notional():
    results = tre.reconcile(trades(("1", "TSLA", "BUY", 50, 200.0)),
                            trades(("1", "TSLA", "BUY", 40, 200.0)))
    r = row_for(results, "1")
    assert r["BreakType"] == tre.QTY_BREAK
    assert r["QtyDiff"] == 10
    assert r["NotionalImpact"] == pytest.approx(2_000.0)


def test_qty_break_on_a_sell_is_negative_position_diff():
    # Internal sold 50, statement says 40 sold -> internal is 10 shares shorter
    results = tre.reconcile(trades(("1", "TSLA", "SELL", 50, 200.0)),
                            trades(("1", "TSLA", "SELL", 40, 200.0)))
    r = row_for(results, "1")
    assert r["BreakType"] == tre.QTY_BREAK
    assert r["QtyDiff"] == -10
    assert r["NotionalImpact"] == pytest.approx(-2_000.0)


def test_price_break_beyond_tolerance():
    results = tre.reconcile(trades(("1", "MSFT", "BUY", 100, 301.0)),
                            trades(("1", "MSFT", "BUY", 100, 300.0)), price_tol_bps=5)
    r = row_for(results, "1")
    assert r["BreakType"] == tre.PRICE_BREAK
    assert r["PriceDiff"] == pytest.approx(1.0)
    assert r["PriceDiffBps"] == pytest.approx(33.33, abs=0.01)
    assert r["NotionalImpact"] == pytest.approx(100.0)


def test_missing_external():
    results = tre.reconcile(trades(("1", "AAPL", "BUY", 100, 150.0), ("2", "GOOG", "SELL", 10, 2800.0)),
                            trades(("1", "AAPL", "BUY", 100, 150.0)))
    r = row_for(results, "2")
    assert r["BreakType"] == tre.MISSING_EXTERNAL
    assert r["QtyDiff"] == -10
    assert r["NotionalImpact"] == pytest.approx(-28_000.0)


def test_missing_internal():
    results = tre.reconcile(trades(("1", "AAPL", "BUY", 100, 150.0)),
                            trades(("1", "AAPL", "BUY", 100, 150.0), ("4", "MSFT", "BUY", 20, 300.0)))
    r = row_for(results, "4")
    assert r["BreakType"] == tre.MISSING_INTERNAL
    assert r["Symbol"] == "MSFT"  # descriptive columns fall back to the external side
    assert r["QtyDiff"] == -20
    assert r["NotionalImpact"] == pytest.approx(-6_000.0)


def test_side_mismatch_counts_both_legs():
    results = tre.reconcile(trades(("1", "NVDA", "SELL", 100, 10.0)),
                            trades(("1", "NVDA", "BUY", 100, 10.0)))
    r = row_for(results, "1")
    assert r["BreakType"] == tre.SIDE_MISMATCH
    assert r["QtyDiff"] == -200
    assert r["NotionalImpact"] == pytest.approx(-2_000.0)


def test_symbol_mismatch():
    results = tre.reconcile(trades(("1", "0941.HK", "BUY", 1500, 72.4, "HKD")),
                            trades(("1", "0914.HK", "BUY", 1500, 72.4, "HKD")))
    r = row_for(results, "1")
    assert r["BreakType"] == tre.SYMBOL_MISMATCH
    assert "0914.HK" in r["Notes"]
    assert r["NotionalImpact"] == pytest.approx(1500 * 72.4)


def test_several_breaks_take_the_highest_priority_label():
    results = tre.reconcile(trades(("1", "AAPL", "BUY", 100, 150.0)),
                            trades(("1", "AAPL", "BUY", 90, 155.0)))
    r = row_for(results, "1")
    assert r["BreakType"] == tre.QTY_BREAK
    assert "also PRICE_BREAK" in r["Notes"]


# --- price tolerance -----------------------------------------------------------

@pytest.mark.parametrize("internal_price, expected", [
    (200.10, tre.MATCHED),      # exactly 5 bps -> within tolerance
    (200.11, tre.PRICE_BREAK),  # 5.5 bps -> break
    (199.90, tre.MATCHED),      # -5 bps -> within tolerance
    (199.89, tre.PRICE_BREAK),
])
def test_bps_tolerance_boundary(internal_price, expected):
    results = tre.reconcile(trades(("1", "AAPL", "BUY", 100, internal_price)),
                            trades(("1", "AAPL", "BUY", 100, 200.00)), price_tol_bps=5)
    assert row_for(results, "1")["BreakType"] == expected


def test_zero_tolerance_flags_any_price_difference():
    results = tre.reconcile(trades(("1", "AAPL", "BUY", 100, 200.01)),
                            trades(("1", "AAPL", "BUY", 100, 200.00)), price_tol_bps=0)
    assert row_for(results, "1")["BreakType"] == tre.PRICE_BREAK


def test_within_tolerance_is_noted_on_matched_trade():
    results = tre.reconcile(trades(("1", "AAPL", "BUY", 100, 200.02)),
                            trades(("1", "AAPL", "BUY", 100, 200.00)), price_tol_bps=5)
    r = row_for(results, "1")
    assert r["BreakType"] == tre.MATCHED
    assert "within tolerance" in r["Notes"]


def test_absolute_tolerance_must_also_be_exceeded():
    # 0.05 on a 10.00 price is 50 bps, but within an absolute tolerance of 0.05
    internal = trades(("1", "Z74.SI", "BUY", 1000, 10.05, "SGD"))
    external = trades(("1", "Z74.SI", "BUY", 1000, 10.00, "SGD"))
    assert row_for(tre.reconcile(internal, external, price_tol_bps=5, price_tol_abs=0.05),
                   "1")["BreakType"] == tre.MATCHED
    assert row_for(tre.reconcile(internal, external, price_tol_bps=5, price_tol_abs=0.04),
                   "1")["BreakType"] == tre.PRICE_BREAK


# --- duplicate IDs -------------------------------------------------------------

def test_duplicate_id_on_internal_side():
    internal = trades(("7", "0700.HK", "SELL", 500, 413.0, "HKD"),
                      ("7", "0700.HK", "SELL", 500, 413.0, "HKD"),
                      ("8", "AAPL", "BUY", 100, 150.0))
    external = trades(("7", "0700.HK", "SELL", 500, 413.0, "HKD"),
                      ("8", "AAPL", "BUY", 100, 150.0))
    results = tre.reconcile(internal, external)
    r = row_for(results, "7")  # one row, not a 2x1 join explosion
    assert r["BreakType"] == tre.DUPLICATE_ID
    assert r["Notes"] == "2 internal row(s), 1 external row(s)"
    assert r["QtyDiff"] == -500
    assert r["NotionalImpact"] == pytest.approx(-206_500.0)
    assert row_for(results, "8")["BreakType"] == tre.MATCHED
    assert len(results) == 2


def test_duplicate_id_on_external_side():
    internal = trades(("9", "AAPL", "BUY", 100, 150.0))
    external = trades(("9", "AAPL", "BUY", 100, 150.0), ("9", "AAPL", "BUY", 100, 150.0))
    r = row_for(tre.reconcile(internal, external), "9")
    assert r["BreakType"] == tre.DUPLICATE_ID
    assert r["QtyDiff"] == -100


# --- column normalisation ------------------------------------------------------

def test_differently_named_headers_are_normalised():
    statement = pd.DataFrame({
        " Trade Ref ": ["1", "2"],
        "TICKER": ["aapl", "msft"],
        "B/S": ["b", "S"],
        "qty": ["1,000", "20"],
        "Exec_Price": ["150.00", "300.00"],
        "ccy": ["usd", "usd"],
        "Fee": [5.0, 2.0],  # extra columns are ignored
    })
    df = tre.normalise_columns(statement)
    assert list(df.columns) == tre.REQUIRED_COLUMNS + tre.OPTIONAL_COLUMNS
    assert df["Symbol"].tolist() == ["AAPL", "MSFT"]
    assert df["Side"].tolist() == ["BUY", "SELL"]
    assert df["Quantity"].tolist() == [1000, 20]
    assert df["Currency"].tolist() == ["USD", "USD"]

    ledger = trades(("1", "AAPL", "BUY", 1000, 150.0), ("2", "MSFT", "SELL", 20, 300.0))
    results = tre.reconcile(ledger, statement)
    assert (results["BreakType"] == tre.MATCHED).all()


def test_numeric_and_text_trade_ids_match():
    internal = trades((101, "AAPL", "BUY", 100, 150.0))
    external = trades(("101", "AAPL", "BUY", 100, 150.0))
    assert row_for(tre.reconcile(internal, external), "101")["BreakType"] == tre.MATCHED


def test_missing_required_column_raises():
    with pytest.raises(ValueError, match="Missing required column"):
        tre.normalise_columns(pd.DataFrame({"TradeID": [1], "Symbol": ["AAPL"]}))


def test_unknown_side_raises():
    with pytest.raises(ValueError, match="Unrecognised side"):
        tre.normalise_columns(trades(("1", "AAPL", "HOLD", 100, 150.0)))


# --- sample files and CLI ------------------------------------------------------

def test_sample_files_exercise_every_break_type():
    results = tre.reconcile(tre.load_trades(SAMPLES / "internal_ledger.csv"),
                            tre.load_trades(SAMPLES / "broker_statement.csv"))
    assert set(results["BreakType"]) == set(tre.LABELS)
    assert results["TradeID"].is_unique


def test_cli_writes_exception_report_and_summary(tmp_path, capsys):
    out = tmp_path / "report.csv"
    exit_code = tre.main(["--internal", str(SAMPLES / "internal_ledger.csv"),
                          "--external", str(SAMPLES / "broker_statement.csv"),
                          "--price-tol-bps", "5", "--out", str(out)])
    assert exit_code == 0

    report = pd.read_csv(out)
    assert list(report.columns) == tre.REPORT_COLUMNS
    assert tre.MATCHED not in set(report["BreakType"])

    summary = capsys.readouterr().out
    assert "Total breaks" in summary
    assert f"({len(report)} rows)" in summary


def test_cli_reports_bad_input(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("TradeID,Symbol\n1,AAPL\n")
    exit_code = tre.main(["--internal", str(bad), "--external", str(bad),
                          "--out", str(tmp_path / "r.csv")])
    assert exit_code == 1
    assert "Missing required column" in capsys.readouterr().err

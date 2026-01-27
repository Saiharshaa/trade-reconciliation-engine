# Automated Trade Reconciliation Engine

## Overview
A Python-based reconciliation tool designed to automate the comparison between internal trading ledgers (e.g., Citadel internal) and external bank statements. This tool identifies breaks in trade quantity, price, and missing bookings, reducing manual operational risk.

## Features
* **Data Normalization:** Standardizes column headers from disparate sources.
* **Automated Matching:** Uses outer joins to identify unmatched trades on either side.
* **Break Detection:** Flags discrepancies in:
    * Quantity (Unit mismatches)
    * Execution Price
    * Missing/Unbooked trades
* **Reporting:** Exports a CSV exception report (`reconciliation_report.csv`) for Ops review.

## Technology Stack
* **Python:** Core logic
* **Pandas:** Data manipulation and vectorised comparisons

## How to Run
1. Ensure you have pandas installed:
   ```bash
   pip install pandas

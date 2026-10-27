# NorthBay Claim Splitter

A Windows desktop tool for NorthBay billing. Drop a RescueNet CMS-1500 batch PDF on the window and it:

1. Refreshes the invoice data in Power BI.
2. Sorts the 1500 pages by the run number in box 26 into NBC, TX and 911 groups.
3. Exports the matching NorthBay invoices.
4. Writes `<name>-NBC.pdf`, `<name>-TX.pdf` and `<name>-911.pdf` next to the input, each with its invoice on top and the 1500s in invoice line order.
5. Writes a matching `<name>-NBC.xlsx`, `<name>-TX.xlsx` and `<name>-911.xlsx` claims summary for each invoice: date, run, name, charge, balance, and the 30-day prompt-pay amount (65% of the balance).

Signing in uses your own Microsoft work account. The tool sends nothing anywhere except to Power BI and, for updates, to GitHub.

## Install

1. Open the [latest Release](../../releases/latest) and download the `.exe`.
2. Save it in a folder you can write to, such as your Desktop. Do not put it in Program Files, because the tool replaces its own file when it updates.
3. Double-click it to run. The first time, Windows may ask you to sign in to your Microsoft account.

## Updates

The tool checks GitHub for a newer release when it starts and every 10 minutes after that. When one is available, it downloads it in the background and restarts on the new version once it is idle. Help > About shows the version you have.

## Releasing (maintainers)

1. Bump `__version__` in `splitter_engine.py`, for example to `2.0.1`.
2. Push a commit to `main` whose message starts with `release v2.0.1` (the same version).

GitHub Actions builds the exe and publishes it as Release `v2.0.1`. If that tag already exists, nothing is published.

The build needs these repository secrets:

- `NBS_TENANT_ID`
- `NBS_CLIENT_ID`
- `NBS_WORKSPACE_ID`
- `NBS_DATAFLOW_ID`
- `NBS_DATASET_ID`
- `NBS_BATCH_REPORT_ID`

For a local build, set those as environment variables, run `python scripts/generate_config.py`, then `pyinstaller --noconfirm --clean build_splitter.spec`.

Never commit PDFs, logs or any patient data. `.gitignore` excludes them.

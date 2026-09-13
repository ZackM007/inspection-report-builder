# Quickstart

*For anyone who needs to turn an inspection export into the finished owner report, without touching the code.*

## One-time setup

**Windows**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
.\.venv\Scripts\python.exe -m playwright install chromium
```

**macOS, Linux, or a Cowork container**

```bash
bash scripts/setup.sh
.venv/bin/python -m playwright install chromium
```

That creates the virtual environment, installs the package, and downloads the headless browser used to print the PDF.

## The easy way: say "build the report"

Open this folder in Claude Code or Cowork and say:

> build the report

and point at the inspection file. The bundled `build-inspection-report` skill drives everything: parsing, photos, charts, the owner-facing writing, and the QA gates. You review the finished PDF.

No API key is needed. The narratives are written in-session.

## The manual way

```bash
.venv/bin/inspection-report build "sample-data/Prop 4820 - Inspection Report.xlsx"
```

The PDF lands in `outputs/`, named after the property. Scratch files go in `outputs/.work/` and are safe to delete.

To run the quality checks against a finished PDF:

```bash
.venv/bin/inspection-report check "outputs/<file>.pdf"
```

## What you get

Cover with the key numbers and the inspector's certification, then a property summary with charts and the high-priority items, then one page per unit with findings, photos and plain-language notes, then a repairs rollup, unit status, next steps with the inspection disclaimer, and a certification appendix.

Units that could not be entered appear under **Not Inspected** with the reason given. They are never counted as passing.

## When something goes wrong

**`inspection-report` not found.** The virtual environment is not active. Use the full path shown above.

**"Executable doesn't exist."** Chromium did not install. Run the playwright install line from setup again.

**`PermissionError` writing the PDF.** The previous version is open in a viewer. Close it, or build with `--out` and a different name.

**0 units parsed.** The export format changed. Run `inspection-report discover <file>` and look at the output before guessing.

**The report is over 15 MB.** Rebuild with `--quality 65` to recompress the photos.

**An inspector's certification is missing from the cover.** Add a row to `assets/inspectors.csv` and drop the certificate image in `assets/inspector-certs/`. Name matching tolerates spelling differences, but a brand-new inspector needs a row.

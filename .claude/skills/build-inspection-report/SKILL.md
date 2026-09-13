---
name: build-inspection-report
description: Build a Fernwater owner-ready inspection report PDF from a Yardi export (xlsx Analytics or PDF), with Claude writing the Steady Hand narratives in-session — no API key required. Use when the user says "build the report", "build the inspection report", "run the report for <property>", "make the owner report", or drops a Yardi inspection file and asks to process it.
---

# Build a Fernwater Inspection Report

Turn a raw Yardi inspection export into the polished, owner-ready PDF. You (Claude) write every owner-facing sentence yourself during this session — the CLI handles parsing, photos, charts, and layout. **No API key is ever required**; that is a deliberate owner constraint, not a gap.

## Step 0 — Bootstrap (only when `.venv` is missing, e.g. a fresh Cowork worktree)

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```
```bash
# macOS / Linux / Cowork containers
bash scripts/setup.sh
```

Both create `.venv`, install the package, and download Playwright's Chromium. Then use the platform's venv binary for every command below — no activation needed:

- Windows: `.\.venv\Scripts\inspection-report.exe <command> ...`
- POSIX: `.venv/bin/inspection-report <command> ...`

(The remaining examples show the Windows form — substitute the POSIX path as needed.)

## Locate the input

The Yardi file is usually in `sample-data/` or given by the user. Accepts `.xlsx` (Inspection Analytics export — the standard) or `.pdf`. If multiple candidates exist, ask which one.

## Step 1 — Extract findings

```powershell
.\.venv\Scripts\inspection-report.exe extract "<path-to-yardi-file>"
```

This writes `outputs\.work\<name>\findings.json`. Read that file.

## Step 2 — Write the narratives (your job)

For every finding **that has an `action`** (Repair / Replace / Inspect / Monitor / Clean), write a `narrative` and a `priority`. For every unit with at least one actionable finding, write a `bottom_line`. Findings with `action: null` are passed items — skip them.

Save your work as `outputs\.work\<name>\narratives.json`:

```json
{
  "units": {
    "540-05": {
      "bottom_line": "...",
      "findings": [
        {"index": 0, "priority": "Medium", "narrative": "..."}
      ]
    }
  }
}
```

Use each finding's `index` exactly as it appears in findings.json.

### Voice — facts only

Fernwater's report states **what the inspection found** — it does not suggest, recommend, schedule, or prescribe repairs. The owner decides what to do with the facts. Your job is to turn each technician note into one clean, factual sentence.

- One short sentence per finding, stating only the observed condition, grounded in the technician's note (your source of truth). Example: *"The water heater is past its service life and shows corrosion at the base."*
- **NEVER** use recommending or prescriptive language: no "should", "recommend", "needs to be", "we advise", "warrants", "must", and never say what "will" fix it or that a repair "is recommended / scheduled / underway".
- The Yardi action (Repair / Replace / Clean) already shows as a label beside the finding — describe the **problem**, not the fix. Don't restate the action.
- The only past action you may state is something the technician genuinely completed **on-site during the inspection** ("the technician replaced the battery on site").
- No filler, no alarm, no casual phrasing ("looks pretty bad"), no checklist tone ("Item flagged. Repair required.").

`inspection-report check` enforces this: a build whose text contains recommendation phrasing (should / recommend / needs to be / advised) or started-work phrasing **fails** the QA gates.

To omit a finding entirely (e.g. a duplicate or one with no real detail), add `"omit": true` to its finding entry instead of a narrative.

### Priority rubric

- **High** — life-safety or active damage: missing/non-working smoke or CO alarms, active leaks, mold, exposed electrical, gas, structural, broken exterior locks, anything posing shock/fall/fire risk, hoarding that blocks exits.
- **Medium** — schedule this quarter: end-of-life appliances/fixtures, deterioration that escalates if ignored, broken-but-not-urgent items. Default for action "Replace" unless obviously cosmetic.
- **Low** — cosmetic, low-cost preventive, or already fixed on site.

## Step 3 — Build

```powershell
.\.venv\Scripts\inspection-report.exe build "<path-to-yardi-file>" --narratives "outputs\.work\<name>\narratives.json"
```

Must report `Done ... OK` and under 15 MB. If over 15 MB, rebuild with `--quality 65`.

## Step 4 — Verify and deliver

1. `.\.venv\Scripts\inspection-report.exe check "outputs\<output>.pdf"`
2. Spot-check by rendering 2–3 pages to PNG (PyMuPDF, the venv has it) and confirm: cover KPIs + Manager/RPS filled, unit pages show findings + photos, narratives read as plain facts (no recommendations), disclaimer present on the final page.
3. Open the PDF for the user (`Invoke-Item`) and summarize: units, findings, not-inspected units with reasons, file size.

## Troubleshooting

- **"Executable doesn't exist" (Playwright/Chromium)**: run `.\.venv\Scripts\python.exe -m playwright install chromium`, then rebuild.
- **0 units parsed**: the export format changed — run `inspection-report discover` and investigate before guessing.
- **Inspector cert missing on cover**: check `assets\inspectors.csv` — lookup is fuzzy but a brand-new inspector needs a row + cert image.

"""Fernwater Inspection Report Builder — CLI entry point.

Usage:
    inspection-report build "..\\Prop 4820 - Inspection Analytics Report.xlsx"
    inspection-report extract "..\\Prop 4820 - Inspection Analytics Report.xlsx"
    inspection-report build "...xlsx" --narratives narratives.json
    inspection-report check outputs/Prop_4820.pdf
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

load_dotenv()

app = typer.Typer(
    help="Turns a Yardi annual inspection file into a Fernwater-branded owner-ready PDF.",
    no_args_is_help=True,
)
console = Console()

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, show_time=False, show_path=False)],
)
log = logging.getLogger("inspection_report")


def _parser_for(input: Path):
    ext = input.suffix.lower()
    if ext == ".pdf":
        from .parsers.pdf_parser import parse_pdf as parse_fn
    elif ext == ".xlsx":
        from .parsers.xlsx_parser import parse_xlsx as parse_fn
    else:
        console.print(f"[red]Unsupported file type: {ext}[/]")
        raise typer.Exit(code=2)
    return parse_fn


@app.command()
def build(
    input: Path = typer.Argument(..., exists=True, readable=True, help="Path to a Yardi xlsx (Analytics export) or PDF"),
    out: Path = typer.Option(Path("outputs"), "--out", "-o", help="Output directory for the PDF"),
    assets: Path = typer.Option(Path("assets"), "--assets", "-a", help="Folder containing fonts/ and logos/"),
    work: Path = typer.Option(None, "--work", "-w", help="Scratch directory (default: outputs/.work/<name>)"),
    quality: int = typer.Option(80, "--quality", "-q", min=40, max=95, help="JPEG quality for photos"),
    narratives: Path = typer.Option(None, "--narratives", "-n", exists=True, readable=True,
                                    help="Pre-written narratives JSON (from the Claude Code skill) — no API key needed"),
    manager: str = typer.Option("", "--manager", help="Property Manager name for the cover footer"),
    rps: str = typer.Option("", "--rps", help="Regional Property Supervisor name for the cover footer"),
) -> None:
    """Build a polished, owner-ready PDF from a Yardi inspection report."""
    from .pipeline import llm_narrative
    from .pipeline.narrative import apply_provided, populate
    from .render.builder import build_pdf

    parse_fn = _parser_for(input)

    work_dir = work or (out / ".work" / input.stem)
    work_dir.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)

    # Auto-recompress: rebuild at descending JPEG quality until the PDF fits
    # the 15 MB email limit. Each attempt re-parses, because build_pdf rewrites
    # photo paths on the parsed property.
    qualities = [quality, max(45, quality - 12), max(45, quality - 24)]
    out_pdf: Path | None = None
    size_mb = 0.0
    for attempt, q in enumerate(qualities, start=1):
        if attempt > 1:
            console.print(f"[yellow]Attempt {attempt}[/] — recompressing at quality {q}")
        console.print(f"[bold blue]Parsing[/] {input.name}")
        prop = parse_fn(input, media_out_dir=work_dir / "raw_photos")
        prop.manager = manager
        prop.rps = rps
        n_findings = sum(len(u.findings) for u in prop.units)
        n_photos = sum(len(u.photos) for u in prop.units)
        console.print(f"  {len(prop.units)} unit(s), {n_findings} finding(s), {n_photos} photo(s)")

        if narratives:
            n_applied = apply_provided(prop, narratives)
            console.print(f"[bold blue]Narratives[/] applied {n_applied} pre-written narrative(s) from {narratives.name}")
        if llm_narrative.is_available():
            console.print(f"[bold blue]Generating narrative[/] via Claude ({llm_narrative.MODEL}) for any remaining findings")
        elif not narratives:
            console.print("[bold blue]Generating narrative[/] [dim](stub mode — use the Claude Code skill or set ANTHROPIC_API_KEY for Steady Hand voice)[/]")
        for unit in prop.units:
            populate(unit)

        out_pdf = out / f"{_safe_filename(prop.name)}.pdf"
        console.print(f"[bold blue]Rendering[/] {out_pdf}")
        build_pdf(prop, work_dir=work_dir, out_path=out_pdf, assets_dir=assets, jpeg_quality=q)

        size_mb = out_pdf.stat().st_size / (1024 * 1024)
        if size_mb < 15:
            break
        console.print(f"[yellow]{size_mb:.1f} MB exceeds the 15 MB email limit[/]")

    status = "[green]OK[/]" if size_mb < 15 else "[red]still over 15 MB after recompression — review photo volume[/]"
    console.print(f"[bold green]Done[/] — {out_pdf} ({size_mb:.1f} MB) {status}")


@app.command()
def extract(
    input: Path = typer.Argument(..., exists=True, readable=True, help="Path to a Yardi xlsx (Analytics export) or PDF"),
    out: Path = typer.Option(None, "--out", "-o", help="Where to write the findings JSON (default: outputs/.work/<name>/findings.json)"),
) -> None:
    """Parse a Yardi file and write a findings JSON for narrative writing.

    This is step 1 of the no-API-key flow: Claude Code reads this JSON, writes
    Steady Hand narratives, and feeds them back via `build --narratives`.
    """
    parse_fn = _parser_for(input)

    work = Path("outputs/.work") / input.stem
    work.mkdir(parents=True, exist_ok=True)
    out_path = out or (work / "findings.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    prop = parse_fn(input, media_out_dir=work / "raw_photos")

    payload = {
        "property": prop.name,
        "address": prop.address,
        "units": {
            u.number: {
                "status": u.status,
                "overall_note": u.overall_note,
                "findings": [
                    {
                        "index": i,
                        "category": f.category,
                        "observation": f.observation,
                        "action": f.action,
                        "tech_note": f.tech_note,
                        "photos": len(f.photos),
                    }
                    for i, f in enumerate(u.findings)
                ],
            }
            for u in prop.units
        },
    }
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    n_findings = sum(len(u["findings"]) for u in payload["units"].values())
    console.print(f"[bold green]Wrote[/] {out_path} — {len(payload['units'])} unit(s), {n_findings} finding(s)")


@app.command()
def check(
    pdf: Path = typer.Argument(..., exists=True, readable=True, help="Built report PDF to verify"),
    source: Path = typer.Option(None, "--source", "-s", exists=True, readable=True,
                                help="Original Yardi file — enables unit-coverage cross-check"),
) -> None:
    """Run QA gates on a built PDF. Exits non-zero if any hard gate fails."""
    import fitz
    import re as _re

    failures: list[str] = []
    warnings: list[str] = []

    # Phrases that falsely imply repair work has been scheduled, started, or
    # finished. The report reaches the owner days after inspection, before any
    # work is planned — so these must never appear. Word-boundary anchored to
    # avoid false positives on legitimate recommending language.
    BANNED_WORK_STARTED = [
        r"\bis being (?:installed|replaced|repaired|handled|addressed|scheduled|done)\b",
        r"\bare being (?:installed|replaced|repaired|handled|addressed|scheduled|done)\b",
        r"\bis scheduled\b", r"\bare scheduled\b", r"\bbeing scheduled\b",
        r"\bin progress\b", r"\bunderway\b",
        r"\bwe are (?:installing|replacing|repairing|handling|scheduling)\b",
        r"\b(?:has|have) been (?:scheduled|installed|replaced|handled|addressed)\b",
        r"\b(?:has|have) scheduled\b",
        r"\bscheduled (?:first|this week|immediately|right away)\b",
    ]

    # Gate 1 — emailable size
    size_mb = pdf.stat().st_size / (1024 * 1024)
    if size_mb < 15:
        console.print(f"[green]PASS[/] size: {size_mb:.2f} MB (< 15 MB)")
    else:
        failures.append(f"size: {size_mb:.2f} MB exceeds the 15 MB email limit — rebuild with --quality 65")

    # Gate 2 — readable PDF with content
    doc = fitz.open(pdf)
    text = "".join(page.get_text() for page in doc)
    flat = " ".join(text.upper().split())
    if len(doc) >= 3:
        console.print(f"[green]PASS[/] structure: {len(doc)} pages")
    else:
        failures.append(f"structure: only {len(doc)} page(s) — render likely failed")

    # Gate 3 — disclaimer present
    if "PROPERTY INSPECTION DISCLAIMER" in flat:
        console.print("[green]PASS[/] disclaimer present")
    else:
        failures.append("disclaimer: 'Property Inspection Disclaimer' not found in the PDF")

    # Gate 4 — every unit in the source appears in the report
    if source is not None:
        parse_fn = _parser_for(source)
        work = Path("outputs/.work") / f"check-{source.stem}"
        prop = parse_fn(source, media_out_dir=work / "raw_photos")
        missing = [u.number for u in prop.units if f"UNIT {u.number.upper()}" not in flat]
        if missing:
            failures.append(f"unit coverage: {len(missing)} unit(s) missing from the PDF: {', '.join(missing)}")
        else:
            console.print(f"[green]PASS[/] unit coverage: all {len(prop.units)} unit(s) present")

    # Gate 5 — truthfulness: no language implying work has started
    lowered = " ".join(text.lower().split())
    hits: list[str] = []
    for pat in BANNED_WORK_STARTED:
        for m in _re.finditer(pat, lowered):
            hits.append(m.group(0))
    if hits:
        uniq = ", ".join(sorted(set(hits)))
        failures.append(
            f"truthfulness: {len(hits)} phrase(s) imply work has started/is scheduled "
            f"(report goes out before any work begins): {uniq}"
        )
    else:
        console.print("[green]PASS[/] truthfulness: no 'work started/scheduled' language")

    # Gate 6 — facts only: Fernwater states what the inspection found, never a
    # recommended fix (the owner decides). The Property Inspection Disclaimer
    # legitimately reads "...should be addressed promptly", so exclude it.
    facts_scan = lowered.replace("should be addressed promptly", "")
    BANNED_RECOMMENDATION = [
        r"\bshould\b",
        r"\brecommend(?:s|ed|ing)?\b",
        r"\bneeds to be\b",
        r"\b(?:we advise|is advised|are advised)\b",
    ]
    rec_hits: list[str] = []
    for pat in BANNED_RECOMMENDATION:
        for m in _re.finditer(pat, facts_scan):
            rec_hits.append(m.group(0))
    if rec_hits:
        uniq = ", ".join(sorted(set(rec_hits)))
        failures.append(
            f"facts-only: {len(rec_hits)} recommendation phrase(s) found "
            f"(Fernwater states the facts, not the fix): {uniq}"
        )
    else:
        console.print("[green]PASS[/] facts-only: no recommended-repair language")

    # Warnings (don't fail the run)
    n_stub = text.count("flagged during inspection.")
    if n_stub:
        warnings.append(f"{n_stub} finding(s) carry stub narrative text — run the Claude Code skill for Steady Hand voice")
    n_placeholder = text.count("Photo not available")
    if n_placeholder:
        warnings.append(f"{n_placeholder} finding(s) have no photo")

    for w in warnings:
        console.print(f"[yellow]WARN[/] {w}")
    if failures:
        for f in failures:
            console.print(f"[red]FAIL[/] {f}")
        raise typer.Exit(code=1)
    console.print("[bold green]All gates passed[/]")


@app.command()
def discover(input: Path = typer.Argument(..., exists=True, readable=True)) -> None:
    """Parse a Yardi file and print what was found, without rendering. Useful for debugging."""
    parse_fn = _parser_for(input)

    work = Path("outputs/.work") / input.stem
    work.mkdir(parents=True, exist_ok=True)
    prop = parse_fn(input, media_out_dir=work / "raw_photos")

    console.print(f"\n[bold]{prop.name}[/]")
    console.print(f"Address:   {prop.address}")
    console.print(f"Inspector: {prop.inspector}")
    console.print(f"Date:      {prop.report_date}")
    console.print(f"KPIs:      {prop.kpis}")
    console.print(f"Top issues: {prop.common_issues}")
    console.print(f"\nUnits ({len(prop.units)}):")
    for u in prop.units[:5]:
        console.print(f"  Unit {u.number} — {u.status} — {len(u.findings)} finding(s), {len(u.photos)} photo(s)")
        for f in u.findings:
            console.print(f"    · {f.category} [{f.action}] — {f.tech_note}")
    if len(prop.units) > 5:
        console.print(f"  … and {len(prop.units) - 5} more")


def _safe_filename(name: str) -> str:
    keep = "-_."
    return "".join(c if c.isalnum() or c in keep else "_" for c in name).strip("_")


if __name__ == "__main__":
    app()

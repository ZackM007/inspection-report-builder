"""Render a Property into a polished, branded PDF via Jinja2 + Playwright/Chromium."""
from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from PIL import Image

from ..inspectors import Inspector, find, load
from ..models import Finding, Property, Unit
from ..palette import THEME_RANK, color_for_category, theme_for
from ..pipeline import photos as photo_pipeline

log = logging.getLogger(__name__)

PKG_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PKG_DIR / "templates"
STATIC_DIR = PKG_DIR / "static"

PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
CERT_MAX_WIDTH = 1800  # safely under Claude Code's 2000px image cap; keeps PDF size sane


@dataclass
class CertCardView:
    display_name: str
    certification_number: str
    cert_image_rel: str | None  # relative path inside work_dir, or None


def build_pdf(
    prop: Property,
    *,
    work_dir: Path,
    out_path: Path,
    assets_dir: Path,
    jpeg_quality: int = 82,
) -> Path:
    """Render the property to a PDF at out_path."""
    work_dir.mkdir(parents=True, exist_ok=True)
    _stage_static_assets(work_dir, assets_dir)

    # Load inspector master data and fill cert # on the property
    inspectors_dir = load(assets_dir / "inspectors.csv")
    property_inspector = find(inspectors_dir, prop.inspector)
    if property_inspector:
        prop.cert_id = property_inspector.certification_number

    # Process per-finding photos AND supplemental photos through the pipeline
    photo_out = work_dir / "photos"
    _process_unit_photos(prop, photo_out, work_dir, jpeg_quality)

    # Filter units: render full pages only for units with action or photos
    actionable_units, no_action_unit_numbers, not_inspected_units = _split_units(prop.units)

    # Build view-models. The summary's "Most common issues" / "Unit status"
    # visuals are HTML/CSS (see summary.html), so no matplotlib charts.
    attention_items = _build_attention_items(prop)
    chart_issues = _chart_issues(prop)
    common_issues_max = max((n for _, n in chart_issues), default=1)
    repairs = _build_repairs_list(prop)
    cert_inspectors = _stage_cert_images(prop.units, inspectors_dir, assets_dir, work_dir)

    logo_rel = _stage_logo(work_dir, assets_dir)

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["category_color"] = color_for_category

    html_str = env.get_template("report.html").render(
        property=prop,
        actionable_units=actionable_units,
        no_action_unit_numbers=no_action_unit_numbers,
        not_inspected_units=not_inspected_units,
        attention_items=attention_items,
        chart_issues=chart_issues,
        common_issues_max=common_issues_max,
        repairs=repairs,
        logo=logo_rel,
        cert_inspectors=cert_inspectors,
    )

    html_path = work_dir / "report.html"
    html_path.write_text(html_str, encoding="utf-8")

    _render_pdf(html_path, out_path, work_dir)
    _stamp_page_numbers(out_path, assets_dir / "fonts" / "Ubuntu-Regular.ttf")

    log.info("Wrote %s (%d bytes)", out_path, out_path.stat().st_size)
    return out_path


# ---------- staging ----------------------------------------------------------

def _stage_static_assets(work_dir: Path, assets_dir: Path) -> None:
    shutil.copy(STATIC_DIR / "report.css", work_dir / "report.css")

    fonts_src = assets_dir / "fonts"
    fonts_dst = work_dir / "fonts"
    fonts_dst.mkdir(exist_ok=True)
    if fonts_src.exists():
        for f in fonts_src.iterdir():
            if f.suffix.lower() in (".ttf", ".otf"):
                shutil.copy(f, fonts_dst / f.name)
    else:
        log.warning("No assets/fonts/ found at %s — text will use fallback fonts", fonts_src)


def _stage_logo(work_dir: Path, assets_dir: Path) -> str | None:
    logos_src = assets_dir / "logos"
    if not logos_src.exists():
        return None
    candidates = [logos_src / "lockup.png", logos_src / "logo.png"]
    candidates += sorted(logos_src.glob("*.png")) + sorted(logos_src.glob("*.svg"))
    for c in candidates:
        if c.exists():
            ext = c.suffix.lower()
            dst = work_dir / f"logo{ext}"
            shutil.copy(c, dst)
            return dst.name
    return None


def _stage_cert_images(
    units: list[Unit],
    inspectors_dir: dict[str, Inspector],
    assets_dir: Path,
    work_dir: Path,
) -> list[CertCardView]:
    """Find the inspectors referenced in this report and stage their cert images."""
    seen: dict[str, Inspector] = {}
    for u in units:
        insp = find(inspectors_dir, u.inspector)
        if insp and insp.certification_number not in seen:
            seen[insp.certification_number] = insp

    out: list[CertCardView] = []
    cert_dst_dir = work_dir / "inspector-certs"
    cert_dst_dir.mkdir(exist_ok=True)
    for insp in seen.values():
        rel = None
        if insp.cert_image:
            src = assets_dir / insp.cert_image
            if src.exists():
                dst_name = _stage_cert_image(src, cert_dst_dir)
                rel = f"inspector-certs/{dst_name}"
            else:
                log.warning("Cert image not found at %s", src)
        out.append(CertCardView(
            display_name=insp.display_name,
            certification_number=insp.certification_number,
            cert_image_rel=rel,
        ))
    return out


def _stage_cert_image(src: Path, dst_dir: Path) -> str:
    """Stage a cert image into dst_dir as a JPEG, downscaling oversized sources.

    Sources ship as PNGs at 2200x1700 which exceeds Claude Code's 2000px image
    cap (blocks dev sessions) and bloats the final PDF. Certs are credential
    photos — JPEG at quality 88 is visually indistinguishable and much smaller.
    Returns the destination filename.
    """
    dst = dst_dir / (src.stem + ".jpg")
    if dst.exists():
        return dst.name
    with Image.open(src) as img:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        if max(img.size) > CERT_MAX_WIDTH:
            scale = CERT_MAX_WIDTH / max(img.size)
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
        img.save(dst, "JPEG", quality=88, optimize=True)
    return dst.name


# ---------- photo processing -------------------------------------------------

def _process_unit_photos(prop: Property, photo_out: Path, work_dir: Path, quality: int) -> None:
    """Run finding photos AND supplemental photos through the resize/compress pipeline,
    then rebase paths to be relative to work_dir."""
    for unit in prop.units:
        for f in unit.findings:
            f.photos = _process_and_rebase(f.photos, photo_out, work_dir, quality)
        unit.photos = _process_and_rebase(unit.photos, photo_out, work_dir, quality)


def _process_and_rebase(photos, photo_out: Path, work_dir: Path, quality: int):
    processed = photo_pipeline.process_all(photos, photo_out, quality=quality)
    for p in processed:
        try:
            p.path = p.path.relative_to(work_dir)
        except ValueError:
            pass
    return processed


# ---------- view-model builders ----------------------------------------------

def _split_units(units: list[Unit]) -> tuple[list[Unit], list[str], list[Unit]]:
    """Render full pages for units with action OR photos; no-action units get
    listed by number; not-inspected units (vacant/cancelled) get listed with
    their reason and never receive a full page."""
    actionable: list[Unit] = []
    no_action: list[str] = []
    not_inspected: list[Unit] = []
    for u in units:
        if u.status == "Not Inspected":
            not_inspected.append(u)
            continue
        non_sig_photos = [p for p in u.photos if not p.is_signature]
        finding_photos = sum(len(f.photos) for f in u.findings)
        if u.has_action or non_sig_photos or finding_photos:
            actionable.append(u)
        else:
            no_action.append(u.number)
    return actionable, no_action, not_inspected


ATTENTION_MAX_ROWS = 6        # keep the summary on one page
ATTENTION_MAX_UNITS = 6       # units shown per row before "+N more"


def _build_attention_items(prop: Property) -> list[dict]:
    """One row per category that has an actionable High-priority finding
    (Smoke/CO always counts when flagged). Each row carries the unit count, a
    compact unit list, and a deterministic risk theme. Sorted most-serious
    theme first, then by unit count, so life-safety leads."""
    by_category: dict[str, list[str]] = {}
    for u in prop.units:
        for f in u.findings:
            if not f.action:
                continue  # passed items must never reach the attention list
            if f.priority == "High" or f.category == "Smoke / CO Alarms":
                units = by_category.setdefault(f.category, [])
                if u.number not in units:
                    units.append(u.number)

    items: list[dict] = []
    for category, units in by_category.items():
        ordered = sorted(units, key=lambda s: (len(s), s))
        unit_label = "Unit " + ", ".join(ordered[:ATTENTION_MAX_UNITS])
        if len(ordered) > ATTENTION_MAX_UNITS:
            unit_label += f" +{len(ordered) - ATTENTION_MAX_UNITS} more"
        verb = "needs" if not category.endswith("s") else "need"
        items.append({
            "category": category,
            "label": f"{category} {verb} attention",
            "count": len(units),
            "unit_label": unit_label,
            "theme": theme_for(category),
            "color": color_for_category(category),
        })

    items.sort(key=lambda it: (THEME_RANK.get(it["theme"], 9), -it["count"]))
    if len(items) > ATTENTION_MAX_ROWS:
        hidden = len(items) - ATTENTION_MAX_ROWS
        items = items[:ATTENTION_MAX_ROWS]
        items[-1]["overflow_note"] = f"+{hidden} more priority categor{'y' if hidden == 1 else 'ies'}"
    return items


def _chart_issues(prop: Property) -> list[tuple[str, int]]:
    """Chart-only view of the 'Most common issues' bars: count actionable
    findings by category but fold 'Bathroom' into 'Plumbing' (per operations
    markup), then take the top 7. The underlying category system is unchanged —
    this affects the bar chart only."""
    counts: dict[str, int] = {}
    for u in prop.units:
        for f in u.findings:
            if f.action:
                key = "Plumbing" if f.category == "Bathroom" else f.category
                counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:7]


def _build_repairs_list(prop: Property) -> list[tuple[Unit, Finding]]:
    pairs: list[tuple[Unit, Finding]] = []
    for u in prop.units:
        for f in u.findings:
            if f.action:
                pairs.append((u, f))
    pairs.sort(key=lambda uf: (PRIORITY_ORDER.get(uf[1].priority, 9), uf[0].number))
    return pairs


# ---------- PDF rendering -----------------------------------------------------

PAGE_NUMBER_GRAY = (0.6, 0.6, 0.604)  # Fernwater Gray #99999A


def _stamp_page_numbers(pdf_path: Path, font_path: Path) -> None:
    """Stamp brand-styled page numbers (Ubuntu, gray, bottom-right) onto every
    page except the cover. Chromium's page.pdf() ignores CSS paged-media margin
    boxes, so we post-process with PyMuPDF for full control over placement and
    font. Numbers reflect true page position ("Page 2 of 75")."""
    import fitz

    doc = fitz.open(pdf_path)
    total = doc.page_count
    have_font = font_path.exists()
    fontfile = str(font_path) if have_font else None
    fontname = "Ubuntu" if have_font else "helv"
    for i, page in enumerate(doc):
        if i == 0:
            continue  # cover stays clean — it already carries the footer band
        w, h = page.rect.width, page.rect.height
        # right edge aligns with the 0.6in content margin; sits ~0.3in up from the bottom
        rect = fitz.Rect(w - 220, h - 34, w - 43.2, h - 16)
        page.insert_textbox(
            rect,
            f"Page {i + 1} of {total}",
            fontsize=8,
            fontname=fontname,
            fontfile=fontfile,
            color=PAGE_NUMBER_GRAY,
            align=fitz.TEXT_ALIGN_RIGHT,
        )
    if not have_font:
        log.warning("Ubuntu font missing at %s — page numbers use a fallback face", font_path)

    # Subset the font we just embedded so the PDF carries only the glyphs it
    # uses ("Page 0-9 of"). insert_textbox embeds the FULL Ubuntu face, which
    # bloats the file and trips fussy font-extraction tools. A full rewrite
    # (garbage collect + deflate) is needed because subsetting rewrites the
    # font streams — incremental save can't express that.
    try:
        doc.subset_fonts()
    except Exception as exc:  # fontTools missing or an unsupported face
        log.warning("Font subsetting skipped (%s) — page-number font stays full", exc)
    tmp_path = pdf_path.with_suffix(".tmp.pdf")
    doc.save(str(tmp_path), garbage=4, deflate=True)
    doc.close()
    tmp_path.replace(pdf_path)


def _render_pdf(html_path: Path, out_path: Path, base_dir: Path) -> None:
    """Render HTML to PDF using Playwright/Chromium."""
    from playwright.sync_api import sync_playwright

    out_path.parent.mkdir(parents=True, exist_ok=True)
    file_url = "file:///" + html_path.resolve().as_posix()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(file_url, wait_until="networkidle")
            # Chromium's load event fires before @font-face downloads finish.
            # If we print too early, the bottom-line's Merriweather Italic isn't
            # ready and the text renders as nothing (only the styled box survives).
            page.evaluate("async () => { await document.fonts.ready; }")
            page.pdf(
                path=str(out_path),
                format="Letter",
                print_background=True,
                prefer_css_page_size=True,
                margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
            )
        finally:
            browser.close()

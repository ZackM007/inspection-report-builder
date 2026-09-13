"""Resize, upscale-with-sharpen, compress, and signature-filter inspection photos."""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

from ..models import Photo

log = logging.getLogger(__name__)

MAX_WIDTH = 1800            # cap for the final output (prevents bloat)
TARGET_DISPLAY_WIDTH = 700  # rough px we want for a 2-2.5" photo slot at ~300 DPI
MAX_UPSCALE_FACTOR = 2.5    # beyond ~2.5x, LANCZOS manufactures mush, not detail —
                            # matters for the Analytics export's 75-134 px thumbnails
DEFAULT_JPEG_QUALITY = 88   # higher quality post-upscale (less re-compression damage)
SIGNATURE_ASPECT_RATIO = 4.0


def process_photo(photo: Photo, out_dir: Path, quality: int = DEFAULT_JPEG_QUALITY) -> Photo:
    """Smart-resample to TARGET_DISPLAY_WIDTH when source is smaller (Yardi embeds
    100-305 px thumbnails). LANCZOS upscale + light sharpen masks the pixelated
    look without manufacturing fake detail. Downscales when source is huge."""
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        img = Image.open(photo.path)
    except Exception as exc:
        log.warning("Failed to open %s: %s", photo.path, exc)
        return photo

    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    img = _smart_resample(img)

    is_sig = photo.is_signature or _looks_like_signature(img)
    out_path = out_dir / f"{photo.path.stem}.jpg"
    img.save(out_path, "JPEG", quality=quality, optimize=True)
    return Photo(path=out_path, caption=photo.caption, is_signature=is_sig)


def _smart_resample(img: Image.Image) -> Image.Image:
    """Bring image into a target display-resolution band.

    - If width < TARGET_DISPLAY_WIDTH: LANCZOS upscale, then sharpen + subtle smooth.
    - If width > MAX_WIDTH: LANCZOS downscale.
    - Otherwise: pass through.
    """
    if img.width >= TARGET_DISPLAY_WIDTH and img.width <= MAX_WIDTH:
        return img

    if img.width > MAX_WIDTH:
        new_h = int(img.height * (MAX_WIDTH / img.width))
        return img.resize((MAX_WIDTH, new_h), Image.LANCZOS)

    # Upscale path, capped: tiny sources gain nothing past ~2.5x except file size.
    target = min(TARGET_DISPLAY_WIDTH, int(img.width * MAX_UPSCALE_FACTOR))
    if target <= img.width:
        return img
    new_h = int(img.height * (target / img.width))
    upscaled = img.resize((target, new_h), Image.LANCZOS)

    # Light unsharp-mask sharpening (radius 1, threshold low) to recover edges
    # blurred by the upscale. Then a tiny smooth to mask any residual JPEG blocks.
    sharpened = upscaled.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=3))
    # Subtle contrast boost — Yardi thumbnails are often slightly washed out.
    sharpened = ImageEnhance.Contrast(sharpened).enhance(1.05)
    return sharpened


def _looks_like_signature(img: Image.Image) -> bool:
    if img.height == 0:
        return False
    ratio = img.width / img.height
    return ratio > SIGNATURE_ASPECT_RATIO


def process_all(photos: list[Photo], out_dir: Path, quality: int = DEFAULT_JPEG_QUALITY) -> list[Photo]:
    return [process_photo(p, out_dir, quality) for p in photos]

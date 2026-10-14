"""Evidence capture.

Captured when a violation OPENS, not on every price check. That single decision
is what keeps screenshot cost negligible -- violations are rare relative to
checks, so this is dozens of vendor requests rather than thousands.

Artifacts: gzipped HTML, an optional full-page screenshot, a SHA-256 over each
(tamper-evident), and a row recording URL, price, MAP, and timestamp. That is
what makes a dealer notice defensible rather than an assertion.

The screenshot's image format is the vendor's choice, not ours -- ScrapingBee
returns PNG, Zyte returns JPEG -- so it is stored under whatever extension its
bytes actually warrant. See _image_suffix.
"""
from __future__ import annotations

import gzip
import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from app import db
from app.config import settings

log = logging.getLogger(__name__)


def _paths(listing_id: int, when: datetime) -> tuple[Path, str, str]:
    """Shard by year/month so the directory stays listable after a few years.

    Returns (abs_dir, relative html path, relative image path WITHOUT extension)
    -- the image extension is not knowable until the bytes are in hand; see
    _image_suffix.
    """
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    rel_dir = Path(when.strftime("%Y/%m"))
    abs_dir = settings.evidence_dir / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    base = f"listing{listing_id}_{stamp}"
    return abs_dir, f"{rel_dir}/{base}.html.gz", f"{rel_dir}/{base}"


def _image_suffix(blob: bytes) -> str:
    """Extension for a screenshot, decided by content rather than by vendor.

    This used to be hardcoded ".png", which was wrong for Zyte: it returns JPEG
    for full-page captures and has no PNG option for them. A file whose
    extension contradicts its bytes is a liability in evidence meant to support
    a dealer notice -- it is exactly the discrepancy that invites the question
    of whether the artifact was altered. Sniffing keeps ScrapingBee (PNG) and
    Zyte (JPEG) both honestly labelled.
    """
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if blob[:2] == b"\xff\xd8":
        return ".jpg"
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ".webp"
    log.warning("screenshot has unrecognised magic bytes %s; storing as .bin",
                blob[:8].hex())
    return ".bin"


async def capture(
    listing_id: int,
    url: str,
    html: Optional[str],
    observed_price: Optional[Decimal],
    map_price: Optional[Decimal],
    fetcher=None,
    violation_id: Optional[int] = None,
    source_tier: Optional[str] = None,
) -> Optional[int]:
    """Persist evidence for one observation. Returns the evidence row id."""
    when = datetime.now(timezone.utc)
    abs_dir, rel_html, rel_img_base = _paths(listing_id, when)

    html_path: Optional[str] = None
    sha: Optional[str] = None
    if html:
        raw = html.encode("utf-8", errors="replace")
        sha = hashlib.sha256(raw).hexdigest()
        try:
            (settings.evidence_dir / rel_html).write_bytes(gzip.compress(raw, 6))
            html_path = rel_html
        except OSError as exc:
            log.error("could not write evidence html for listing %s: %s", listing_id, exc)

    png_path: Optional[str] = None
    shot_sha: Optional[str] = None
    if fetcher is not None:
        shot = await fetcher.screenshot(url)
        if shot:
            rel_img = rel_img_base + _image_suffix(shot)
            # Hashed for the same reason the HTML is: the image is half the
            # evidence, and it should not be the half nobody can verify.
            shot_sha = hashlib.sha256(shot).hexdigest()
            try:
                (settings.evidence_dir / rel_img).write_bytes(shot)
                png_path = rel_img
            except OSError as exc:
                log.error("could not write screenshot for listing %s: %s", listing_id, exc)
                shot_sha = None
        elif not getattr(fetcher, "vendor_enabled", False):
            # Expected when running tier-1-only: no browser exists on this box.
            log.info("screenshot skipped for listing %s (no fetch vendor configured)",
                     listing_id)

    return await db.fetchval(
        """
        INSERT INTO evidence (violation_id, listing_id, url, observed_price,
                              map_price, screenshot_path, html_path, sha256_html,
                              sha256_screenshot, source_tier, captured_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        RETURNING id
        """,
        violation_id, listing_id, url, observed_price, map_price,
        png_path, html_path, sha, shot_sha, source_tier, when,
    )


def read_html(rel_path: str) -> Optional[str]:
    """Decompress an archived snapshot for display in the dashboard."""
    target = (settings.evidence_dir / rel_path).resolve()
    # Confine reads to the evidence root -- rel_path comes from the DB, but the
    # cost of getting this wrong is arbitrary file disclosure.
    if not str(target).startswith(str(settings.evidence_dir.resolve())):
        log.warning("refusing evidence read outside root: %s", rel_path)
        return None
    if not target.exists():
        return None
    try:
        return gzip.decompress(target.read_bytes()).decode("utf-8", errors="replace")
    except OSError as exc:
        log.error("evidence read failed for %s: %s", rel_path, exc)
        return None

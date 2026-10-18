"""Representative figure: list 'Figure N' captions in a PDF and crop the chosen figure to PNG.

Conference PDFs mostly use vector drawings, so instead of extracting embedded images the figure is
located from its caption: the region between the caption and the nearest paragraph above it (same
column), tightened to the drawings/images inside, is rendered with PyMuPDF.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

CAPTION_RE = re.compile(r"^\s*(?:Figure|Fig\.?)\s*(\d+)\s*[.:\-–—]?\s*(.*)", re.I | re.S)
MIN_HEIGHT = 50.0   # pt; smaller regions are probably not a figure
ZOOM = 2.0


@dataclass
class Caption:
    number: int
    text: str
    page: int
    bbox: pymupdf.Rect


def _caption_lines(block: dict) -> tuple[str, pymupdf.Rect] | None:
    """Caption text/rect from a text block, stopping where the caption sentence ends
    (PyMuPDF often merges the caption with the paragraph that follows it)."""
    lines = []
    for line in block.get("lines", []):
        text = "".join(span["text"] for span in line["spans"]).strip()
        if text:
            lines.append((text, pymupdf.Rect(line["bbox"])))
    if not lines or not CAPTION_RE.match(lines[0][0]):
        return None
    keep = [lines[0]]
    for text, rect in lines[1:3]:
        if keep[-1][0].endswith((".", ":")) and len(keep[-1][0]) > 12:
            break
        if rect.y0 - keep[-1][1].y1 > 6:
            break
        keep.append((text, rect))
    rect = pymupdf.Rect(keep[0][1])
    for _, r in keep[1:]:
        rect |= r
    return " ".join(t for t, _ in keep), rect


def captions(pdf: Path) -> list[Caption]:
    """First caption for each figure number, in document order."""
    out: dict[int, Caption] = {}
    with pymupdf.open(pdf) as doc:
        for pno, page in enumerate(doc):
            for block in page.get_text("dict")["blocks"]:
                got = _caption_lines(block)
                if got is None:
                    continue
                text, rect = got
                n = int(CAPTION_RE.match(text).group(1))
                if n not in out:
                    out[n] = Caption(n, " ".join(text.split()), pno, rect)
    return [out[k] for k in sorted(out)]


def _graphics(page: pymupdf.Page) -> list[pymupdf.Rect]:
    """Drawings and images, without the running header/footer (conference logos, rules)."""
    band = page.rect.height * 0.11
    rects = [pymupdf.Rect(d["rect"]) for d in page.get_drawings()]
    rects += [pymupdf.Rect(i["bbox"]) for i in page.get_image_info()]
    return [r for r in rects if (r.width > 2 or r.height > 2)
            and r.y0 > page.rect.y0 + band and r.y1 < page.rect.y1 - band * 0.6]


def crop(pdf: Path, cap: Caption, out: Path) -> bool:
    """Render the figure above `cap` into `out` (PNG). Returns False if no figure region is found."""
    with pymupdf.open(pdf) as doc:
        page = doc[cap.page]
        pw = page.rect.width
        # column: full width if the caption is centred across the middle, else the caption's half
        if cap.bbox.x0 < pw / 2 < cap.bbox.x1 or cap.bbox.width > pw * 0.55:
            col = pymupdf.Rect(page.rect.x0 + 20, 0, page.rect.x1 - 20, 0)
        elif cap.bbox.x1 <= pw / 2 + 10:
            col = pymupdf.Rect(page.rect.x0 + 20, 0, pw / 2, 0)
        else:
            col = pymupdf.Rect(pw / 2, 0, page.rect.x1 - 20, 0)
        top = page.rect.y0 + page.rect.height * 0.11  # below the running header
        graphics = _graphics(page)
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks"):
            tb = pymupdf.Rect(x0, y0, x1, y1)
            # a body-text block: long enough, wide relative to the column, not drawn inside a figure
            body = (len(text) > 60 and tb.width > (col.x1 - col.x0) * 0.6
                    and not any(g.contains(tb) for g in graphics))
            if (body or CAPTION_RE.match(text)) and y1 <= cap.bbox.y0 - 2 and x1 > col.x0 and x0 < col.x1:
                top = max(top, y1)
        region = pymupdf.Rect(col.x0, top, col.x1, cap.bbox.y0)
        inside = [r for r in graphics if r.intersects(region) and (r & region).height > 1]
        if inside:
            g = pymupdf.Rect(inside[0])
            for r in inside[1:]:
                g |= r
            region = (g & region) + (-6, -6, 6, 4)
        if region.height < MIN_HEIGHT or region.width < 80:
            return False
        region |= pymupdf.Rect(region.x0, region.y0, region.x1, cap.bbox.y1)  # include the caption
        out.parent.mkdir(parents=True, exist_ok=True)
        page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), clip=region & page.rect).save(out)
    return True


def representative(pdf: Path, number: int | None, out: Path) -> Caption | None:
    """Crop figure `number` (or the first croppable figure) into `out`."""
    caps = captions(pdf)
    order = [c for c in caps if c.number == number] + [c for c in caps if c.number != number]
    for cap in order:
        try:
            if crop(pdf, cap, out):
                return cap
        except (RuntimeError, ValueError) as e:  # broken PDF objects
            log.warning("그림 추출 실패 (Figure %d): %s", cap.number, e)
    return None

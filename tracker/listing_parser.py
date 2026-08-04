"""Parse a profession.hu /allasok listing page into per-card dicts.

Card markup reference (as of 2026-08-04): each posting is an
<li class="advertisement-result-list-item" data-prof-id="..." ...> carrying
most fields as data-* attributes directly on the <li>, plus a handful of
<span id="detailed-job-card-{id}-details-{field}"> elements for the
icon-labelled bullet lists (benefits, employment type, tech stack, etc).
Selecting by the id suffix (ends-with) avoids needing the numeric id
up front and is robust to the id's numeric prefix changing.
"""

import hashlib
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag


@dataclass
class Card:
    posting_id: int
    url: str
    title: str
    company: str | None
    location: str | None
    experience_level: str | None
    language_req: str | None
    benefits: str | None
    tech_stack: str | None
    task_summary: str | None
    raw_card_html: str
    content_hash: str
    work_arrangement: str | None = None


def _text(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    text = tag.get_text(separator=" ", strip=True)
    return text or None


def _detail_field(li: Tag, field: str) -> str | None:
    span = li.select_one(f'[id$="-details-{field}"]')
    return _text(span)


def _requirements_split(li: Tag) -> tuple[str | None, str | None]:
    """The card's 'requirements' bullet mixes experience and language,
    e.g. "1-3 év tapasztalat • Angol alapfok". experience_level is taken
    from the more reliable data-category4 attribute instead; this helper
    only pulls out the language portion when present.
    """
    raw = _detail_field(li, "requirements")
    if not raw:
        return None, None
    parts = [p.strip() for p in raw.split("•")]
    lang_parts = [p for p in parts if p and "tapasztalat" not in p.lower()]
    return raw, (" • ".join(lang_parts) or None)


def parse_listing_page(html: str) -> list[Card]:
    soup = BeautifulSoup(html, "lxml")
    cards = []

    for li in soup.select("li.advertisement-result-list-item[data-prof-id]"):
        posting_id = int(li["data-prof-id"])
        url = li.get("data-link") or ""
        title = li.get("data-item-name") or _text(li.select_one("h2 a")) or ""
        company = li.get("data-item-brand")
        location = _text(li.select_one('[id$="-details-location"]')) or li.get("data-category6")
        experience_level = li.get("data-category4")
        benefits = _detail_field(li, "salaryBonus")
        tech_stack = _detail_field(li, "techStacks")

        task_items = li.select('[id$="-details-task-list"] li')
        task_texts = [t for t in (_text(item) for item in task_items) if t]
        task_summary = " | ".join(task_texts) or None

        _, language_req = _requirements_split(li)

        raw_card_html = str(li)
        content_hash = hashlib.sha256(raw_card_html.encode("utf-8")).hexdigest()

        cards.append(
            Card(
                posting_id=posting_id,
                url=url,
                title=title,
                company=company,
                location=location,
                experience_level=experience_level,
                language_req=language_req,
                benefits=benefits,
                tech_stack=tech_stack,
                task_summary=task_summary,
                raw_card_html=raw_card_html,
                content_hash=content_hash,
            )
        )

    return cards


def result_count(html: str) -> int | None:
    """Total posting count for the active filter, parsed from the page's
    own '<meta name="description" content="<N> db friss ...">' tag. Used
    to compute how many pages to walk.
    """
    soup = BeautifulSoup(html, "lxml")
    meta = soup.select_one('meta[name="description"]')
    if meta is None:
        return None
    content = meta.get("content", "")
    digits = "".join(ch for ch in content.split(" ")[0] if ch.isdigit())
    return int(digits) if digits else None

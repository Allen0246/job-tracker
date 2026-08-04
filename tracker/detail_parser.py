"""Parse a profession.hu /allas/<slug>-<id> detail page.

Rather than scraping the visual div structure (which the listing card
markup shows is quite deep and redesign-prone), this reads the
schema.org JobPosting object the site already embeds as JSON-LD for
search engines: <script type="application/ld+json"> containing an
"@graph" array with one "@type": "JobPosting" entry. That's the stable
contract a job board has the most incentive to keep working, since
breaking it costs them Google visibility.
"""

import json
from dataclasses import dataclass

from bs4 import BeautifulSoup


@dataclass
class DetailData:
    description: str | None
    responsibilities: str | None
    education_requirements: str | None
    employment_type: str | None
    experience_requirements: str | None
    occupational_category: str | None
    skills: str | None
    job_benefits: str | None
    job_location_type: str | None  # "TELECOMMUTE" when remote
    date_posted: str | None
    valid_through: str | None
    hiring_organization: str | None
    raw_detail_html: str


def _find_job_posting(html: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue

        graph = data.get("@graph") if isinstance(data, dict) else None
        candidates = graph if graph is not None else [data]
        for entry in candidates:
            if isinstance(entry, dict) and entry.get("@type") == "JobPosting":
                return entry
    return None


def parse_detail_page(html: str) -> DetailData | None:
    posting = _find_job_posting(html)
    if posting is None:
        return None

    hiring_org = posting.get("hiringOrganization")
    org_name = hiring_org.get("name") if isinstance(hiring_org, dict) else None

    return DetailData(
        description=posting.get("description"),
        responsibilities=posting.get("responsibilities"),
        education_requirements=posting.get("educationRequirements"),
        employment_type=posting.get("employmentType"),
        experience_requirements=posting.get("experienceRequirements"),
        occupational_category=posting.get("occupationalCategory"),
        skills=posting.get("skills"),
        job_benefits=posting.get("jobBenefits"),
        job_location_type=posting.get("jobLocationType"),
        date_posted=posting.get("datePosted"),
        valid_through=posting.get("validThrough"),
        hiring_organization=org_name,
        raw_detail_html=html,
    )

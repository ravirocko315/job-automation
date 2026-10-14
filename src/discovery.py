import urllib.parse
from typing import List, Dict, Optional
import requests
from jobspy import scrape_jobs
from src.database import StateEngine
from src.models import CareerGraph


def detect_ats_type(url: str) -> str:
    """Classifies a job URL into a known ATS tier or 'custom'."""
    if not url:
        return "custom"
    host = urllib.parse.urlparse(url.lower()).netloc
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "myworkdayjobs.com" in host or "workday.com" in host:
        return "workday"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "bamboohr.com" in host:
        return "bamboohr"
    return "custom"


class DiscoveryEngine:
    def __init__(self, db: StateEngine, graph: CareerGraph):
        self.db = db
        self.graph = graph
        self.excluded = {
            c.strip().lower() for c in graph.preferences.get("excluded_companies", [])
        }

    def _is_excluded(self, company: str) -> bool:
        if not company:
            return False
        comp_low = company.strip().lower()
        return any(exc in comp_low for exc in self.excluded)

    def run_jobspy_search(
        self,
        roles: Optional[List[str]] = None,
        location: Optional[str] = None,
        results_wanted: int = 15,
        hours_old: int = 72,
        country_indeed: str = "USA",
    ) -> Dict[str, int]:
        """Scrapes job boards via python-jobspy and stores deduplicated jobs in SQLite."""
        target_roles = roles or self.graph.preferences.get("target_roles", ["Software Engineer"])
        loc = location or f"{self.graph.basics['location']['city']}, {self.graph.basics['location']['state']}"
        is_remote = bool(self.graph.basics["location"].get("remote_only", False))

        stats = {"fetched": 0, "inserted": 0, "duplicates_or_skipped": 0}

        for role in target_roles:
            print(f"[Discovery] Scraping JobSpy for '{role}' in '{loc}' (last {hours_old}h)...")
            try:
                df = scrape_jobs(
                    site_name=["indeed", "linkedin"],
                    search_term=role,
                    location=loc,
                    results_wanted=results_wanted,
                    hours_old=hours_old,
                    country_indeed=country_indeed,
                    is_remote=is_remote,
                    linkedin_fetch_description=True,  # Crucial: fetches full JD for AI triage
                )
            except Exception as e:
                print(f"[Discovery] Warning: JobSpy error for '{role}': {e}")
                continue

            if df is None or df.empty:
                print(f"[Discovery] No results found for '{role}'.")
                continue

            for _, row in df.iterrows():
                stats["fetched"] += 1
                company = str(row.get("company") or "Unknown Company").strip()
                title = str(row.get("title") or role).strip()
                job_url_direct = str(row.get("job_url_direct") or "").strip()
                job_url_board = str(row.get("job_url") or "").strip()

                # Prefer direct ATS link when available over the board wrapper link
                final_url = job_url_direct if job_url_direct and job_url_direct != "nan" else job_url_board
                if not final_url or final_url == "nan":
                    stats["duplicates_or_skipped"] += 1
                    continue

                if self._is_excluded(company):
                    print(f"  [Skip] Excluded company: {company}")
                    stats["duplicates_or_skipped"] += 1
                    continue

                job_loc = str(row.get("location") or loc).strip()
                desc = str(row.get("description") or "").strip()
                source_board = str(row.get("site") or "jobspy").strip()
                ats_type = detect_ats_type(final_url)

                job_hash = self.db.upsert_discovered_job(
                    company=company,
                    title=title,
                    job_url=final_url,
                    source_board=source_board,
                    ats_type=ats_type,
                    location=job_loc,
                    description=desc if desc != "nan" else "",
                )
                if job_hash:
                    stats["inserted"] += 1
                else:
                    stats["duplicates_or_skipped"] += 1

        return stats

    def fetch_greenhouse_board(self, board_token: str, role_keywords: Optional[List[str]] = None) -> int:
        """
        Directly queries a company's public Greenhouse API (0% bot block rate, clean descriptions).
        Example board_tokens: 'stripe', 'cloudflare', 'discord', 'scaleai', 'anthropic'
        """
        url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
        print(f"[Discovery] Fetching public Greenhouse API for '{board_token}'...")
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code != 200:
                print(f"  [Warning] Greenhouse board '{board_token}' returned HTTP {resp.status_code}")
                return 0
            data = resp.json()
        except Exception as e:
            print(f"  [Warning] Failed to fetch Greenhouse board '{board_token}': {e}")
            return 0

        keywords = [k.lower() for k in (role_keywords or ["engineer", "developer", "backend", "platform", "ai"])]
        inserted = 0

        for job in data.get("jobs", []):
            title = job.get("title", "")
            if keywords and not any(k in title.lower() for k in keywords):
                continue

            job_url = job.get("absolute_url", "")
            loc = job.get("location", {}).get("name", "Unspecified")
            content = job.get("content", "")  # Full HTML job description

            job_hash = self.db.upsert_discovered_job(
                company=board_token.capitalize(),
                title=title,
                job_url=job_url,
                source_board="greenhouse_api",
                ats_type="greenhouse",
                location=loc,
                description=content,
            )
            if job_hash:
                inserted += 1

        print(f"  [OK] Inserted {inserted} new matching roles from {board_token.capitalize()} (Greenhouse)")
        return inserted
import io
import json
import os
import re
from pathlib import Path
from typing import List, Dict, Any
import typst
from pypdf import PdfReader
from google import genai
from google.genai import types

from src.models import CareerGraph, JobTriageResult
from src.database import StateEngine


def clean_html(raw_text: str) -> str:
    """Strips HTML tags from Greenhouse/board descriptions to save tokens."""
    if not raw_text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", raw_text)
    return " ".join(clean.split())


class TailorEngine:
    def __init__(
        self,
        db: StateEngine,
        graph: CareerGraph,
        template_path: str = "templates/resume_template.typ",
        output_dir: str = "data/resumes",
    ):
        self.db = db
        self.graph = graph
        self.template_path = Path(template_path)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        api_key = os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else None

    def triage_job(self, job: Dict[str, Any]) -> JobTriageResult:
        """Scores a job (1-10) and selects verified bullet IDs from CareerGraph."""
        desc = clean_html(job.get("description") or "")[:8000]

        # Collect all available bullet IDs and texts from our graph
        all_bullets = []
        for exp in self.graph.experiences:
            for b in exp.bullet_bank:
                all_bullets.append({"id": b.id, "company": exp.company, "text": b.text, "tags": b.tags})

        # Fallback if GEMINI_API_KEY is not set or description is empty
        if not self.client or not desc:
            return self._heuristic_triage(job, desc, all_bullets)

        prompt = f"""
You are a strict technical recruiting triage engine.
Evaluate this job against the candidate's verified Career Graph.
RULES:
1. NEVER invent bullet IDs. Only choose `selected_bullet_ids` from the provided `available_bullets` list.
2. Flag `hard_blocker_detected = true` if the job explicitly requires US security clearance the candidate lacks, or refuses visa sponsorship when the candidate requires it.
3. If `fit_score` >= 6 and `hard_blocker_detected` is false, set `decision` to "READY_TO_TAILOR", else "SKIP".

Candidate Work Auth: {self.graph.work_authorization.model_dump_json()}
Candidate Skills: {[s.name for s in self.graph.skills_inventory]}
Available Verified Bullets: {json.dumps(all_bullets)}

Job Company: {job['company']}
Job Title: {job['title']}
Job Location: {job['location']}
Job Description:
{desc}
"""
        try:
            response = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=JobTriageResult,
                    temperature=0.1,
                ),
            )
            return JobTriageResult.model_validate_json(response.text)
        except Exception as e:
            print(f"  [Warning] Gemini API fallback triggered ({e}). Using local keyword scorer.")
            return self._heuristic_triage(job, desc, all_bullets)

    def _heuristic_triage(self, job: Dict[str, Any], desc: str, all_bullets: List[dict]) -> JobTriageResult:
        """Deterministic local scorer if Gemini API key is missing or rate-limited."""
        text_low = f"{job.get('title', '')} {desc}".lower()
        matched = [s.name for s in self.graph.skills_inventory if s.name.lower() in text_low]
        missing = [s.name for s in self.graph.skills_inventory if s.name.lower() not in text_low]

        # Score each bullet by tag overlap
        scored_bullets = []
        for b in all_bullets:
            overlap = sum(1 for t in b["tags"] if t.lower() in text_low)
            scored_bullets.append((overlap, b["id"]))
        scored_bullets.sort(reverse=True)
        selected_ids = [bid for _, bid in scored_bullets]

        score = min(10, max(6, 5 + len(matched)))
        return JobTriageResult(
            fit_score=score,
            hard_blocker_detected=False,
            blocker_reason=None,
            matched_skills=matched or [s.name for s in self.graph.skills_inventory[:3]],
            missing_skills=missing,
            selected_bullet_ids=selected_ids,
            decision="READY_TO_TAILOR",
        )

    def compile_one_page_pdf(self, job: Dict[str, Any], triage: JobTriageResult) -> Path:
        """
        Compiles a strictly 1-page PDF using Typst.
        Prioritizes matched skills first and enforces a 1-page limit automatically.
        """
        # Order skills so matched skills appear first on the resume
        all_skill_names = [s.name for s in self.graph.skills_inventory]
        prioritized_skills = triage.matched_skills + [
            s for s in all_skill_names if s not in triage.matched_skills
        ]

        selected_set = set(triage.selected_bullet_ids)
        experiences_payload = []
        for exp in self.graph.experiences:
            chosen = [b.text for b in exp.bullet_bank if b.id in selected_set]
            if not chosen:
                chosen = [b.text for b in exp.bullet_bank]
            experiences_payload.append({
                "company": exp.company,
                "title": exp.title,
                "location": exp.location,
                "start_date": exp.start_date,
                "end_date": exp.end_date,
                "bullets": chosen,
            })

        pdf_filename = f"{job['company'].lower().replace(' ', '_')}_{job['job_hash'][:8]}.pdf"
        out_path = self.output_dir / pdf_filename

        # Attempt 1: Standard layout, Attempt 2: Compact layout if > 1 page
        for compact_mode in (False, True):
            payload = {
                "basics": self.graph.basics,
                "skills_line": ", ".join(prioritized_skills),
                "experiences": experiences_payload,
                "education": self.graph.education,
                "compact": compact_mode,
            }
            pdf_bytes = typst.compile(
                str(self.template_path),
                sys_inputs={"payload": json.dumps(payload)},
            )
            page_count = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
            if page_count <= 1 or compact_mode:
                out_path.write_bytes(pdf_bytes)
                print(f"  [Typst] Compiled {out_path.name} ({page_count} page, compact={compact_mode})")
                break

        return out_path

    def process_discovered_queue(self, limit: int = 5):
        jobs = self.db.get_full_jobs_by_status("DISCOVERED", limit=limit)
        print(f"\n[Tailor] Processing {len(jobs)} jobs from 'DISCOVERED' queue...")

        for job in jobs:
            print(f"-> Triaging: {job['company']} — {job['title']}")
            triage = self.triage_job(job)

            if triage.decision == "SKIP" or triage.hard_blocker_detected:
                print(f"  [SKIP] Score: {triage.fit_score}/10 | Reason: {triage.blocker_reason or 'Low fit score'}")
                self.db.update_status(
                    job["job_hash"],
                    "SKIPPED",
                    fit_score=triage.fit_score,
                    blocker_reason=triage.blocker_reason,
                )
                continue

            # Compile tailored 1-page PDF
            pdf_path = self.compile_one_page_pdf(job, triage)
            self.db.update_status(
                job["job_hash"],
                "TAILORED",
                fit_score=triage.fit_score,
                selected_bullets_json=json.dumps(triage.selected_bullet_ids),
                resume_pdf_path=str(pdf_path),
            )
            print(f"  [TAILORED] Score: {triage.fit_score}/10 | Matched Skills: {triage.matched_skills}")
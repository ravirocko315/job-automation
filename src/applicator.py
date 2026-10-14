import json
import os
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from playwright.sync_api import sync_playwright, Page, Locator
from google import genai
from google.genai import types

from src.database import StateEngine
from src.models import CareerGraph, FieldAnswerResult


class CentaurApplicator:
    def __init__(
        self,
        db: StateEngine,
        graph: CareerGraph,
        headless: bool = False,
        confidence_threshold: float = 0.85,
    ):
        self.db = db
        self.graph = graph
        self.headless = headless
        self.confidence_threshold = confidence_threshold

        api_key = os.environ.get("GEMINI_API_KEY")
        self.client = genai.Client(api_key=api_key) if api_key else None
        self._seed_standard_qa_memory()

    def _seed_standard_qa_memory(self):
        """Pre-populates qa_memory with deterministic facts from CareerGraph."""
        auth = self.graph.work_authorization
        basics = self.graph.basics
        loc = basics.get("location", {})

        seeds = [
            ("are you legally authorized to work in the united states?", "select", "Yes" if auth.us_authorized else "No"),
            ("are you legally authorized to work in the country where this role is located?", "select", "Yes" if auth.us_authorized else "No"),
            ("will you now or in the future require sponsorship for employment visa status?", "select", "Yes" if (auth.requires_sponsorship_now or auth.requires_sponsorship_future) else "No"),
            ("do you require visa sponsorship?", "select", "Yes" if auth.requires_sponsorship_now else "No"),
            ("linkedin profile", "text", basics.get("linkedin", "")),
            ("linkedin url", "text", basics.get("linkedin", "")),
            ("github profile", "text", basics.get("github", "")),
            ("github url", "text", basics.get("github", "")),
            ("website", "text", basics.get("portfolio", basics.get("github", ""))),
            ("portfolio", "text", basics.get("portfolio", basics.get("github", ""))),
            ("current city", "text", f"{loc.get('city', '')}, {loc.get('state', '')}".strip(", ")),
            ("location", "text", f"{loc.get('city', '')}, {loc.get('state', '')}".strip(", ")),
        ]
        for q, ftype, val in seeds:
            if val and not self.db.lookup_exact_qa(q):
                self.db.save_qa_answer(q, ftype, str(val), source="career_graph")

    def resolve_question(
        self,
        job: Dict[str, Any],
        question_text: str,
        field_type: str = "text",
        options: Optional[List[str]] = None,
    ) -> Tuple[str, str]:
        """
        Resolves an application form question using:
        1. Exact / Fuzzy match in SQLite `qa_memory`
        2. Gemini AI reasoning over `CareerGraph`
        3. Interactive HITL prompt if low confidence
        Returns (answer_value, resolution_source).
        """
        clean_q = " ".join(question_text.replace("*", "").strip().split())

        # Tier 1: Exact SQLite QA Memory Lookup
        cached = self.db.lookup_exact_qa(clean_q)
        if cached:
            return cached["answer_value"], f"qa_memory ({cached['source']})"

        # Tier 1b: Common keyword heuristics from CareerGraph
        q_low = clean_q.lower()
        basics = self.graph.basics
        auth = self.graph.work_authorization

        if "linkedin" in q_low and basics.get("linkedin"):
            return basics["linkedin"], "career_graph_rule"
        if "github" in q_low and basics.get("github"):
            return basics["github"], "career_graph_rule"
        if ("website" in q_low or "portfolio" in q_low) and (basics.get("portfolio") or basics.get("github")):
            return basics.get("portfolio") or basics.get("github"), "career_graph_rule"
        if "authorized to work" in q_low:
            ans = "Yes" if auth.us_authorized else "No"
            return self._match_option(ans, options), "career_graph_rule"
        if "sponsorship" in q_low or "visa" in q_low:
            needs = auth.requires_sponsorship_now or auth.requires_sponsorship_future
            ans = "Yes" if needs else "No"
            return self._match_option(ans, options), "career_graph_rule"

        # Tier 2: Gemini AI Evaluation
        ai_result = self._ask_gemini_for_field(job, clean_q, field_type, options)
        if (
            ai_result
            and not ai_result.needs_human_review
            and ai_result.confidence >= self.confidence_threshold
        ):
            final_ans = self._match_option(ai_result.answer, options)
            self.db.save_qa_answer(clean_q, field_type, final_ans, source="career_graph")
            return final_ans, f"gemini_auto (conf={ai_result.confidence:.2f})"

        # Tier 3: Human-in-the-Loop (HITL) Escalation
        suggested = ai_result.answer if ai_result else ""
        conf = ai_result.confidence if ai_result else 0.0
        hitl_id = self.db.enqueue_hitl_question(
            job_hash=job["job_hash"],
            question_text=clean_q,
            field_type=field_type,
            options=options,
            suggested_answer=suggested,
            confidence=conf,
        )
        human_ans = self._prompt_human_cli(job, hitl_id, clean_q, field_type, options, suggested, conf)
        return human_ans, "human_hitl"

    def _match_option(self, target: str, options: Optional[List[str]]) -> str:
        """If dropdown options exist, picks the closest matching option string."""
        if not options:
            return target
        target_low = target.strip().lower()
        for opt in options:
            if opt.strip().lower() == target_low:
                return opt
        for opt in options:
            if target_low in opt.strip().lower() or opt.strip().lower() in target_low:
                return opt
        return target

    def _ask_gemini_for_field(
        self,
        job: Dict[str, Any],
        question: str,
        field_type: str,
        options: Optional[List[str]],
    ) -> Optional[FieldAnswerResult]:
        if not self.client:
            return None

        prompt = f"""
You are filling out a job application on behalf of the candidate.
Answer the following form field strictly using the candidate's verified Career Graph.
RULES:
1. NEVER fabricate experience, degrees, or clearances not in the Career Graph.
2. If options are provided, `answer` MUST exactly match one of the provided options.
3. If the question asks for legally binding disclosures not in standard_disclosures, or you are unsure, set `needs_human_review = true` and `confidence < 0.7`.

Candidate Career Graph:
{self.graph.model_dump_json(indent=2)}

Target Job: {job['title']} at {job['company']}
Question Label: "{question}"
Field Type: {field_type}
Available Options: {json.dumps(options) if options else "Free-form text"}
"""
        try:
            resp = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=FieldAnswerResult,
                    temperature=0.1,
                ),
            )
            return FieldAnswerResult.model_validate_json(resp.text)
        except Exception as e:
            print(f"    [AI Q&A Warning] Gemini call failed: {e}")
            return None

    def _prompt_human_cli(
        self,
        job: Dict[str, Any],
        hitl_id: int,
        question: str,
        field_type: str,
        options: Optional[List[str]],
        suggested: str,
        confidence: float,
    ) -> str:
        """Interactive Centaur CLI pause when the bot encounters an unfamiliar question."""
        print("\n" + "=" * 65)
        print(f" [CENTAUR HITL INTERVENTION] {job['company']} — {job['title']}")
        print("=" * 65)
        print(f" Question   : {question}")
        print(f" Field Type : {field_type}")
        if options:
            print(" Options    :")
            for idx, opt in enumerate(options, 1):
                print(f"   [{idx}] {opt}")
        if suggested:
            print(f" AI Suggests: '{suggested}' (Confidence: {confidence:.0%})")
        print("-" * 65)

        while True:
            user_in = input(" Enter answer (or press Enter to accept AI suggestion): ").strip()
            if not user_in and suggested:
                final_answer = suggested
                break
            elif options and user_in.isdigit():
                choice_idx = int(user_in) - 1
                if 0 <= choice_idx < len(options):
                    final_answer = options[choice_idx]
                    break
            elif user_in:
                final_answer = user_in
                break
            print(" [!] Please provide a valid answer.")

        self.db.resolve_hitl_question(hitl_id, final_answer, save_to_memory=True)
        print(f" [Saved to QA Memory] '{question}' -> '{final_answer}'\n")
        return final_answer

    # -------------------------------------------------------------------------
    # Playwright Browser Automation (Greenhouse & Generic ATS)
    # -------------------------------------------------------------------------
    def apply_to_tailored_queue(self, limit: int = 3, dry_run: bool = True):
        """Processes jobs in 'TAILORED' status using Playwright."""
        jobs = self.db.get_full_jobs_by_status("TAILORED", limit=limit)
        if not jobs:
            print("\n[Applicator] No jobs in 'TAILORED' queue ready for application.")
            return

        print(f"\n[Applicator] Launching Centaur Browser for {len(jobs)} tailored role(s)...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless, slow_mo=150)
            context = browser.new_context(viewport={"width": 1280, "height": 900})

            for job in jobs:
                print(f"\n>>> Starting Application: {job['company']} | {job['title']} (Fit: {job['fit_score']}/10)")
                print(f"    URL: {job['job_url']}")
                page = context.new_page()
                try:
                    if job["ats_type"] == "greenhouse" or "greenhouse.io" in job["job_url"]:
                        success = self._fill_greenhouse_application(page, job, dry_run=dry_run)
                    else:
                        success = self._fill_generic_application(page, job, dry_run=dry_run)

                    if success:
                        new_status = "PAUSED_FOR_HUMAN" if dry_run else "SUBMITTED"
                        self.db.update_status(job["job_hash"], new_status)
                        print(f"  [OK] Job status updated to '{new_status}'.")
                except Exception as e:
                    print(f"  [ERROR] Failed while applying to {job['company']}: {e}")
                    self.db.update_status(job["job_hash"], "FAILED", error_log=str(e))
                finally:
                    page.close()

            browser.close()

    def _fill_greenhouse_application(self, page: Page, job: Dict[str, Any], dry_run: bool = True) -> bool:
        page.goto(job["job_url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # Click "Apply" button if Greenhouse uses a tabbed/modal layout
        apply_btn = page.locator("a:has-text('Apply for this job'), button:has-text('Apply for this job')").first
        if apply_btn.count() > 0 and apply_btn.is_visible():
            apply_btn.click()
            page.wait_for_timeout(800)

        basics = self.graph.basics
        full_name = basics.get("name", "Alex Rivera").split(" ", 1)
        first_name = basics.get("first_name") or full_name[0]
        last_name = basics.get("last_name") or (full_name[1] if len(full_name) > 1 else "")
        email = basics.get("email", "")
        phone = basics.get("phone", "")

        # 1. Fill Core Identity Fields (supports both classic #application_form and modern Remix/React Greenhouse)
        self._safe_fill(page, ["input#first_name", "input[name='job_application[first_name]']", "input[autocomplete='given-name']"], first_name, "First Name")
        self._safe_fill(page, ["input#last_name", "input[name='job_application[last_name]']", "input[autocomplete='family-name']"], last_name, "Last Name")
        self._safe_fill(page, ["input#email", "input[name='job_application[email]']", "input[type='email']"], email, "Email")
        self._safe_fill(page, ["input#phone", "input[name='job_application[phone]']", "input[type='tel']"], phone, "Phone")

        # 2. Upload Tailored 1-Page Typst PDF Resume
        pdf_path = Path(job.get("resume_pdf_path") or "")
        if pdf_path.exists():
            file_inputs = page.locator("input[type='file']")
            if file_inputs.count() > 0:
                file_inputs.first.set_input_files(str(pdf_path.resolve()))
                print(f"    [Upload] Attached tailored PDF: {pdf_path.name}")
                page.wait_for_timeout(1000)
        else:
            print(f"    [Warning] Tailored PDF not found at '{pdf_path}'")

        # 3. Scan & Fill Custom Questions
        self._process_custom_form_fields(page, job)

        # 4. Centaur Review Gate
        return self._centaur_review_gate(page, job, dry_run=dry_run)

    def _fill_generic_application(self, page: Page, job: Dict[str, Any], dry_run: bool = True) -> bool:
        page.goto(job["job_url"], wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        self._process_custom_form_fields(page, job)
        return self._centaur_review_gate(page, job, dry_run=dry_run)

    def _safe_fill(self, page: Page, selectors: List[str], value: str, label_name: str) -> bool:
        if not value:
            return False
        for sel in selectors:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.fill(value)
                print(f"    [Fill] {label_name}: {value}")
                return True
        return False

    def _process_custom_form_fields(self, page: Page, job: Dict[str, Any]):
        """Finds visible unfilled text inputs, textareas, and selects, and resolves them via 3-tier Q&A."""
        already_handled = {"first_name", "last_name", "email", "phone", "resume", "cover_letter"}

        # A. Process standard <select> dropdowns
        selects = page.locator("form select:visible").all()
        for sel in selects:
            label = self._extract_label_for_element(page, sel)
            if not label:
                continue
            options = [
                opt.inner_text().strip()
                for opt in sel.locator("option").all()
                if opt.get_attribute("value") and opt.inner_text().strip().lower() not in ("", "select...", "please select")
            ]
            if not options:
                continue
            answer, source = self.resolve_question(job, label, field_type="select", options=options)
            try:
                sel.select_option(label=answer)
                print(f"    [Select ({source})] {label[:45]} -> {answer}")
            except Exception:
                pass

        # B. Process visible text inputs & textareas inside the form
        inputs = page.locator("form input[type='text']:visible, form input[type='number']:visible, form input[type='url']:visible, form textarea:visible").all()
        for inp in inputs:
            el_id = (inp.get_attribute("id") or "").lower()
            el_name = (inp.get_attribute("name") or "").lower()
            if any(k in el_id or k in el_name for k in already_handled):
                continue
            if inp.input_value().strip():
                continue  # Already filled

            label = self._extract_label_for_element(page, inp)
            if not label or len(label) < 2:
                continue

            # Skip internal combobox search inputs if React-Select is used
            role_attr = (inp.get_attribute("role") or "").lower()
            if role_attr == "combobox" and "location" not in label.lower():
                self._handle_react_combobox(page, inp, label, job)
                continue

            answer, source = self.resolve_question(job, label, field_type="text")
            if answer:
                inp.fill(answer)
                print(f"    [Input ({source})] {label[:45]} -> {answer}")

    def _handle_react_combobox(self, page: Page, inp: Locator, label: str, job: Dict[str, Any]):
        """Handles modern Greenhouse React-Select dropdowns (role='combobox')."""
        try:
            inp.click()
            page.wait_for_timeout(400)
            option_locs = page.locator("[role='option']:visible").all()
            options = [o.inner_text().strip() for o in option_locs if o.inner_text().strip()]
            if not options:
                page.keyboard.press("Escape")
                return

            answer, source = self.resolve_question(job, label, field_type="select", options=options)
            for o in option_locs:
                if o.inner_text().strip().lower() == answer.strip().lower():
                    o.click()
                    print(f"    [Combobox ({source})] {label[:45]} -> {answer}")
                    return
            # Fallback: type the answer and press Enter
            inp.fill(answer)
            page.wait_for_timeout(300)
            page.keyboard.press("Enter")
            print(f"    [Combobox-Typed ({source})] {label[:45]} -> {answer}")
        except Exception:
            pass

    def _extract_label_for_element(self, page: Page, element: Locator) -> str:
        """Extracts human-readable question text associated with an input/select element."""
        try:
            el_id = element.get_attribute("id")
            if el_id:
                lbl = page.locator(f"label[for='{el_id}']").first
                if lbl.count() > 0:
                    return " ".join(lbl.inner_text().split())

            aria_label = element.get_attribute("aria-label")
            if aria_label:
                return " ".join(aria_label.split())

            # Traverse up to parent field container
            parent_label = element.locator("xpath=ancestor::div[contains(@class, 'field') or contains(@class, 'question')][1]//label").first
            if parent_label.count() > 0:
                return " ".join(parent_label.inner_text().split())
        except Exception:
            pass
        return ""

    def _centaur_review_gate(self, page: Page, job: Dict[str, Any], dry_run: bool = True) -> bool:
        """Pauses at the end of form filling so the human can verify or submit."""
        print("\n  " + "-" * 60)
        print(f"  [CENTAUR REVIEW GATE] Form filled for {job['company']} — {job['title']}")
        print(f"  Tailored PDF : {job.get('resume_pdf_path')}")
        print("  " + "-" * 60)

        if not self.headless:
            choice = input(
                "  Inspect the browser window.\n"
                "  Press [Enter] once you have submitted (or to save state), or type 's' to skip: "
            ).strip().lower()
            if choice == "s":
                return False
            return True

        if not dry_run:
            submit_btn = page.locator("input[type='submit'], button[type='submit'], button:has-text('Submit Application')").first
            if submit_btn.count() > 0 and submit_btn.is_visible():
                submit_btn.click()
                page.wait_for_timeout(3000)
                return True
        return True
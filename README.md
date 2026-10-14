# Centaur ATS Engine

An end-to-end **Human-in-the-Loop ("Centaur") Job Discovery, Zero-Hallucination Typst Resume Tailoring, and Playwright Autofill Engine** powered by Gemini 2.5 Flash and SQLite.

## Why Centaur?
Most "AI job bots" hallucinate fake skills onto resumes and blindly submit broken forms. **Centaur** enforces strict engineering guardrails:
1. **Zero-Hallucination Bullet Bank (`CareerGraph`)**: The LLM never writes unverified prose. It scores job descriptions against a structured `career_graph.json` and selects verified bullet IDs from your experience bank.
2. **Guaranteed 1-Page Typst Resume Compilation**: Dynamically orders matched skills first and compiles a razor-sharp PDF via **Typst**. Automatically inspects page count with `pypdf` and re-compiles in compact mode if the layout overflows 1 page.
3. **3-Tier Self-Learning Q&A Memory (`qa_memory` + `hitl_queue`)**:
   - **Tier 1**: Deterministic lookup from `CareerGraph` & cached `qa_memory` in SQLite.
   - **Tier 2**: Gemini 2.5 Flash evaluates custom ATS questions with a strict confidence score (`0.0 - 1.0`).
   - **Tier 3**: Questions scoring `< 0.85` confidence pause execution in the `hitl_queue`, prompt you in the CLI, and permanently cache your answer for all future applications.
4. **Centaur Browser Verification Gate**: Playwright uploads your tailored 1-page PDF, fills standard and React-Select combobox fields, and pauses at the final Submit button for human review.

## Architecture
```text
├── main.py                        # CLI Orchestrator (Phases 2–4)
├── data/
│   ├── career_graph.json          # Single source of truth for skills, bullets & work auth
│   ├── centaur.db                 # SQLite state engine (jobs, qa_memory, hitl_queue)
│   └── resumes/                   # Auto-compiled 1-page Typst PDFs per job
├── templates/
│   └── resume_template.typ        # Dynamic 1-page Typst template with compact fallback
└── src/
    ├── models.py                  # Pydantic v2 schemas & Gemini structured output contracts
    ├── database.py                # SQLite StateEngine & semantic job hash deduplication
    ├── discovery.py               # Public Greenhouse API + JobSpy multi-board scraper
    ├── tailor.py                  # Gemini AI triage + Typst 1-page PDF compiler
    └── applicator.py              # Playwright autofill + 3-tier Q&A / HITL engine
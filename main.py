import argparse
from src.models import CareerGraph
from src.database import StateEngine
from src.discovery import DiscoveryEngine
from src.tailor import TailorEngine
from src.applicator import CentaurApplicator


def main():
    parser = argparse.ArgumentParser(
        description="Centaur: Zero-Hallucination AI Job Triage, 1-Page Typst Resume Tailor & HITL Autofill Engine"
    )
    parser.add_argument("--board", type=str, default="stripe", help="Greenhouse company board token (e.g., stripe, cloudflare, anthropic)")
    parser.add_argument("--jobspy", action="store_true", help="Run multi-board scrape via JobSpy (LinkedIn/Indeed)")
    parser.add_argument("--triage-limit", type=int, default=3, help="Max discovered jobs to triage and compile PDFs for")
    parser.add_argument("--apply-limit", type=int, default=1, help="Max tailored jobs to launch in Centaur browser mode")
    parser.add_argument("--live-submit", action="store_true", help="Mark applications as SUBMITTED instead of dry-run PAUSED_FOR_HUMAN")
    args = parser.parse_args()

    graph = CareerGraph.load("data/career_graph.json")
    db = StateEngine()
    discovery = DiscoveryEngine(db, graph)

    # Phase 2: Discovery
    if args.jobspy:
        stats = discovery.run_jobspy_search()
        print(f"[Discovery] JobSpy Summary: {stats}")
    elif not db.get_jobs_by_status("DISCOVERED", limit=1) and not db.get_jobs_by_status("TAILORED", limit=1):
        discovery.fetch_greenhouse_board(args.board, role_keywords=["backend", "platform", "ai", "engineer"])

    # Phase 3: AI Triage + 1-Page Typst PDF Compilation
    if db.get_jobs_by_status("DISCOVERED", limit=1):
        tailor = TailorEngine(db, graph)
        tailor.process_discovered_queue(limit=args.triage_limit)

    # Summary of Tailored Queue
    tailored = db.get_full_jobs_by_status("TAILORED", limit=10)
    print(f"\n=== {len(tailored)} Jobs Ready in 'TAILORED' Queue ===")
    for j in tailored:
        print(f"- {j['company']} | {j['title']} | Fit: {j['fit_score']}/10 | PDF: {j['resume_pdf_path']}")

    # Phase 4: Centaur HITL & Browser Autofill Execution
    if tailored:
        run_apply = input("\nLaunch Phase 4 Centaur Browser Autofill for tailored jobs? [Y/n]: ").strip().lower()
        if run_apply in ("", "y", "yes"):
            applicator = CentaurApplicator(db, graph, headless=False)
            applicator.apply_to_tailored_queue(limit=args.apply_limit, dry_run=not args.live_submit)


if __name__ == "__main__":
    main()
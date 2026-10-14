import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any

DB_PATH = Path("data/centaur.db")

def compute_job_hash(company: str, title: str, location: str = "") -> str:
    """Deterministic semantic hash so we never apply to the same role twice across job boards."""
    normalized = f"{company.strip().lower()}::{title.strip().lower()}::{location.strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]

class StateEngine:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def get_jobs_by_status(self, status: str = "DISCOVERED", limit: int = 20):
        rows = self.conn.execute(
            "SELECT job_hash, company, title, ats_type, source_board, location, job_url "
            "FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_full_jobs_by_status(self, status: str = "DISCOVERED", limit: int = 10):
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (status, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def _init_schema(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_hash TEXT PRIMARY KEY,
                    source_board TEXT NOT NULL,
                    ats_type TEXT NOT NULL,          -- 'greenhouse', 'lever', 'ashby', 'workday', 'custom'
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    location TEXT,
                    job_url TEXT UNIQUE NOT NULL,
                    description TEXT,
                    fit_score INTEGER,
                    blocker_reason TEXT,
                    selected_bullets_json TEXT,
                    resume_pdf_path TEXT,
                    status TEXT NOT NULL DEFAULT 'DISCOVERED',
                    -- Valid statuses: DISCOVERED, SKIPPED, READY_TO_TAILOR, TAILORED, PAUSED_FOR_HUMAN, SUBMITTED, FAILED
                    error_log TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS qa_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    normalized_question TEXT UNIQUE NOT NULL,
                    field_type TEXT NOT NULL,        -- 'text', 'number', 'select', 'radio', 'checkbox'
                    answer_value TEXT NOT NULL,
                    source TEXT NOT NULL,            -- 'career_graph' or 'human_hitl'
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hitl_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_hash TEXT NOT NULL,
                    question_text TEXT NOT NULL,
                    field_type TEXT NOT NULL,
                    options_json TEXT,               -- Dropdown/Radio choices if applicable
                    suggested_answer TEXT,
                    confidence REAL,
                    resolved INTEGER DEFAULT 0,
                    human_answer TEXT,
                    FOREIGN KEY(job_hash) REFERENCES jobs(job_hash)
                );
            """)

    def upsert_discovered_job(
        self,
        company: str,
        title: str,
        job_url: str,
        source_board: str,
        ats_type: str,
        location: str = "",
        description: str = ""
    ) -> Optional[str]:
        """Inserts a newly scraped job if we haven't seen this company+title or URL before."""
        job_hash = compute_job_hash(company, title, location)
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self.conn:
                self.conn.execute("""
                    INSERT INTO jobs (
                        job_hash, source_board, ats_type, company, title,
                        location, job_url, description, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', ?, ?)
                """, (job_hash, source_board, ats_type, company, title, location, job_url, description, now, now))
            return job_hash
        except sqlite3.IntegrityError:
            # Duplicate job_hash or job_url — safely ignore
            return None

    def update_status(self, job_hash: str, status: str, **extra_fields: Any):
        now = datetime.now(timezone.utc).isoformat()
        fields = ["status = ?", "updated_at = ?"]
        values = [status, now]
        for k, v in extra_fields.items():
            fields.append(f"{k} = ?")
            values.append(v)
        values.append(job_hash)
        query = f"UPDATE jobs SET {', '.join(fields)} WHERE job_hash = ?"
        with self.conn:
            self.conn.execute(query, values)

    def save_qa_answer(self, question: str, field_type: str, answer: str, source: str = "human_hitl"):
        norm_q = " ".join(question.strip().lower().split())
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute("""
                INSERT INTO qa_memory (normalized_question, field_type, answer_value, source, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(normalized_question) DO UPDATE SET
                    answer_value = excluded.answer_value,
                    source = excluded.source,
                    updated_at = excluded.updated_at
            """, (norm_q, field_type, answer, source, now))

    def lookup_exact_qa(self, question: str) -> Optional[Dict[str, Any]]:
        norm_q = " ".join(question.strip().lower().split())
        row = self.conn.execute(
            "SELECT * FROM qa_memory WHERE normalized_question = ?", (norm_q,)
        ).fetchone()
        return dict(row) if row else None
    # --- Phase 4: HITL Queue Methods ---
    def enqueue_hitl_question(
        self,
        job_hash: str,
        question_text: str,
        field_type: str,
        options: Optional[list] = None,
        suggested_answer: str = "",
        confidence: float = 0.0,
    ) -> int:
        import json
        options_str = json.dumps(options) if options else None
        with self.conn:
            cur = self.conn.execute("""
                INSERT INTO hitl_queue (
                    job_hash, question_text, field_type, options_json, suggested_answer, confidence, resolved
                ) VALUES (?, ?, ?, ?, ?, ?, 0)
            """, (job_hash, question_text, field_type, options_str, suggested_answer, confidence))
            return cur.lastrowid

    def get_pending_hitl(self, job_hash: Optional[str] = None):
        if job_hash:
            rows = self.conn.execute(
                "SELECT h.*, j.company, j.title, j.job_url FROM hitl_queue h "
                "JOIN jobs j ON h.job_hash = j.job_hash "
                "WHERE h.resolved = 0 AND h.job_hash = ? ORDER BY h.id ASC",
                (job_hash,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT h.*, j.company, j.title, j.job_url FROM hitl_queue h "
                "JOIN jobs j ON h.job_hash = j.job_hash "
                "WHERE h.resolved = 0 ORDER BY h.id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_hitl_question(self, hitl_id: int, human_answer: str, save_to_memory: bool = True):
        row = self.conn.execute("SELECT * FROM hitl_queue WHERE id = ?", (hitl_id,)).fetchone()
        if not row:
            return
        with self.conn:
            self.conn.execute(
                "UPDATE hitl_queue SET resolved = 1, human_answer = ? WHERE id = ?",
                (human_answer, hitl_id)
            )
        if save_to_memory:
            self.save_qa_answer(
                question=row["question_text"],
                field_type=row["field_type"],
                answer=human_answer,
                source="human_hitl"
            )
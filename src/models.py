from pathlib import Path
import json
from typing import List, Optional, Literal
from pydantic import BaseModel, Field

class Location(BaseModel):
    city: str
    state: str
    country: str
    remote_only: bool = False
    willing_to_relocate: bool = True

class WorkAuth(BaseModel):
    us_authorized: bool
    requires_sponsorship_now: bool
    requires_sponsorship_future: bool
    security_clearance: str = "None"

class SkillItem(BaseModel):
    name: str
    years: float
    Category: str

class BulletVariant(BaseModel):
    id: str
    text: str
    tags: List[str]

class Experience(BaseModel):
    company: str
    title: str
    location: str
    start_date: str
    end_date: str
    bullet_bank: List[BulletVariant]

class CareerGraph(BaseModel):
    basics: dict
    work_authorization: WorkAuth
    preferences: dict
    skills_inventory: List[SkillItem]
    experiences: List[Experience]
    education: List[dict]
    standard_disclosures: dict

    @classmethod
    def load(cls, path: str = "data/career_graph.json") -> "CareerGraph":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(raw)

class JobTriageResult(BaseModel):
    """Structured output schema for Stage 2 AI Triage."""
    fit_score: int = Field(ge=1, le=10, description="1-10 overall fit score")
    hard_blocker_detected: bool = Field(description="True if visa, clearance, or location disqualifies candidate")
    blocker_reason: Optional[str] = None
    matched_skills: List[str]
    missing_skills: List[str]
    selected_bullet_ids: List[str] = Field(description="Exact bullet IDs from career_graph.json best matching this JD")
    decision: Literal["SKIP", "READY_TO_TAILOR"]
    
class FieldAnswerResult(BaseModel):
    """Structured output schema for Phase 4 custom ATS form questions."""
    answer: str = Field(description="The exact answer or option text to fill into the form field")
    confidence: float = Field(ge=0.0, le=1.0, description="0.0-1.0 confidence that this answer is factual based on CareerGraph")
    needs_human_review: bool = Field(description="True if question requires subjective personal judgment, salary commitment, or unknown facts")
    reasoning: str = Field(description="Brief explanation of how the answer was derived")
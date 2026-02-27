import base64
import copy
import json
import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request, Body
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google import genai


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3-pro-preview")
PATIENT_NAME = os.getenv("PATIENT_NAME", "Sophie Laurent")
DEFAULT_DOCTOR = os.getenv("DEFAULT_DOCTOR", "Dr. Martin (Généraliste)")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is not set. Please update dev/.env.")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
logger = logging.getLogger("medvisit")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

DATA_DIR = BASE_DIR / "DATA"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or "patient"


PATIENT_SLUG = f"{slugify(PATIENT_NAME)}-{uuid.uuid5(uuid.NAMESPACE_URL, PATIENT_NAME).hex[:6]}"
DEFAULT_PATIENT_ID = PATIENT_SLUG
DEFAULT_PATIENT_NAME = PATIENT_NAME

YES_RESPONSES = {"yes", "oui", "taken", "did", "y"}
NO_RESPONSES = {"no", "non", "missed", "skipped", "n"}


def guess_extension(filename: str | None, mime: str | None, default: str) -> str:
    if filename and "." in filename:
        ext = Path(filename).suffix
        if ext:
            return ext
    mime_map = {
        "audio/webm": ".webm",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/mp4": ".m4a",
        "audio/ogg": ".ogg",
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
    }
    if mime and mime in mime_map:
        return mime_map[mime]
    return default


def _resolve_patient_id(patient_id: str | None) -> str:
    if patient_id:
        return slugify(patient_id)
    return DEFAULT_PATIENT_ID


def _patient_root(patient_id: str | None) -> Path:
    slug = _resolve_patient_id(patient_id)
    root = DATA_DIR / slug
    root.mkdir(parents=True, exist_ok=True)
    return root


def _visits_dir(patient_id: str | None) -> Path:
    visits = _patient_root(patient_id) / "visits"
    visits.mkdir(parents=True, exist_ok=True)
    return visits


def _visit_directory(patient_id: str | None, visit_id: str) -> Path:
    return _visits_dir(patient_id) / visit_id


def _analysis_path(patient_id: str | None, visit_id: str) -> Path:
    return _visit_directory(patient_id, visit_id) / "analysis.json"


def _state_path(patient_id: str | None, visit_id: str) -> Path:
    return _visit_directory(patient_id, visit_id) / "state.json"


def _ensure_visit_dir(patient_id: str | None, visit_id: str) -> Path:
    visit_directory = _visit_directory(patient_id, visit_id)
    visit_directory.mkdir(parents=True, exist_ok=True)
    return visit_directory


def _migrate_legacy_analysis(patient_id: str | None, file_path: Path) -> None:
    patient_root = _patient_root(patient_id)
    try:
        with file_path.open(encoding="utf-8") as source:
            record = json.load(source)
    except json.JSONDecodeError:
        logger.warning("Skipping malformed legacy analysis file: %s", file_path)
        return
    visit_id = record.get("id") or file_path.stem
    dest_dir = _ensure_visit_dir(patient_id, visit_id)
    dest_analysis_path = dest_dir / "analysis.json"
    if dest_analysis_path.exists():
        file_path.unlink(missing_ok=True)
        return
    dest_analysis_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    audio_info = (record.get("audio") or {}).get("filename")
    if audio_info:
        source_audio = patient_root / audio_info
        if source_audio.exists():
            source_audio.rename(dest_dir / audio_info)
            (record.setdefault("audio", {}))["url"] = f"/data/{patient_root.name}/visits/{visit_id}/{audio_info}"
    prescription = record.get("prescription") or {}
    prescription_file = prescription.get("filename")
    if prescription_file:
        source_image = patient_root / prescription_file
        if source_image.exists():
            source_image.rename(dest_dir / prescription_file)
            prescription["url"] = f"/data/{patient_root.name}/visits/{visit_id}/{prescription_file}"
    dest_analysis_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    file_path.unlink(missing_ok=True)


def _load_visit_state(patient_id: str | None, visit_id: str) -> Dict[str, Any] | None:
    path = _state_path(patient_id, visit_id)
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except json.JSONDecodeError:
        logger.warning("State file corrupted for visit %s", visit_id)
        return None


def _save_visit_state(patient_id: str | None, visit_id: str, state: Dict[str, Any]) -> None:
    path = _state_path(patient_id, visit_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(state, file_obj, ensure_ascii=False, indent=2)


app = FastAPI(title="MedVisit Dev Server")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

static_path = BASE_DIR / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")


@app.get("/", response_class=HTMLResponse)
async def medvisit_app(request: Request):
    return templates.TemplateResponse(
        "medvisit.html",
        {
            "request": request,
            "default_patient_id": DEFAULT_PATIENT_ID,
        },
    )


def _safe_json_loads(raw: str) -> Dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned[3:-3].strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        logger.error("Gemini JSON parse failure. Raw output: %s", raw)
        raise HTTPException(
            status_code=502,
            detail="Gemini response was not valid JSON. Please retry the analysis.",
        )


def _build_prompt(notes: str) -> str:
    return f"""
You are MedVisit's AI medical assistant. Analyse the consultation audio (and optional prescription photo)
to create a concise patient-friendly report.

Return STRICT JSON with this schema:
{{
  "transcription": "<full transcript in English or the language of the visit>",
  "summary": "<2-3 sentence overview of diagnosis, treatment and safety>",
  "sections": [
    {{
      "title": "Red Flags & Safety",
      "icon": "shield-alert",
      "items": ["bullet insights ..."]
    }},
    {{
      "title": "Doctor Recommendations",
      "icon": "stethoscope",
      "items": []
    }},
    {{
      "title": "Medication & Dosing",
      "icon": "pill",
      "items": []
    }},
    {{
      "title": "Follow-up & Lifestyle",
      "icon": "calendar-days",
      "items": []
    }},
    {{
      "title": "Prescription QA",
      "icon": "file-text",
      "items": []
    }},
    {{
      "title": "Interaction Watch",
      "icon": "alert-triangle",
      "items": []
    }}
  ]
}}

Rules:
- Keep bullet items concise (max 25 words each).
- If information is missing, use "No data provided." as the only item for that section.
- Mention any critical interactions or missing prescription info in the relevant sections.
- Patient notes from the UI (if any): "{notes.strip() or 'None provided'}".
"""


def _call_gemini(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                {
                    "role": "user",
                    "parts": parts,
                }
            ],
        )
    except Exception as exc:  # pylint: disable=broad-except
        raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}") from exc

    text_payload = response.text
    if not text_payload:
        raise HTTPException(status_code=502, detail="Gemini response did not include text output.")
    logger.info("Gemini response text: %s", text_payload)
    return _safe_json_loads(text_payload)


def _format_sections_text(sections: List[Dict[str, Any]] | None) -> str:
    if not sections:
        return ""
    lines: List[str] = []
    for section in sections:
        title = section.get("title", "Section")
        lines.append(f"{title}:")
        for item in section.get("items", []) or []:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _ensure_medication_ids(plan: List[Dict[str, Any]] | None) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    if not plan:
        return prepared
    for day in plan:
        meds = day.get("medications") or []
        for med in meds:
            med_name = med.get("name", "medication")
            base_slug = slugify(f"{med_name}-{med.get('schedule', '')}")
            if not med.get("id"):
                med["id"] = base_slug or uuid.uuid4().hex[:8]
            status = (med.get("status") or "pending").lower()
            if status not in {"taken", "missed", "pending"}:
                med["status"] = "pending"
            else:
                med["status"] = status
            if med.get("status_detail"):
                med["status_detail"] = med["status_detail"][:120]
        day["medications"] = meds
        prepared.append(day)
    return prepared


def _default_symptom_questions() -> List[Dict[str, Any]]:
    return [
        {
            "id": "symptom-energy",
            "prompt": "How is your energy compared to yesterday?",
            "options": ["Much better", "Slightly better", "Same", "Worse"],
            "layout": "grid",
        },
        {
            "id": "symptom-red-flags",
            "prompt": "Any new warning signs today?",
            "options": ["Shortness of breath", "Chest discomfort", "Fever spike", "No change"],
            "layout": "chips",
        },
        {
            "id": "symptom-side-effects",
            "prompt": "Side effects experienced right now?",
            "options": ["Drowsiness", "Nausea", "Dizziness", "None"],
            "layout": "grid",
        },
    ]


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def _normalize_current_day_index(state: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not state:
        return state
    plan = state.get("treatment_plan") or []
    if not plan:
        state["current_day_index"] = 0
        return state
    idx = state.get("current_day_index")
    if not isinstance(idx, int) or idx < 0 or idx >= len(plan):
        idx = 0
    state["current_day_index"] = idx
    return state


def _extract_status_map(state: Dict[str, Any] | None) -> Dict[tuple, Dict[str, Any]]:
    mapping: Dict[tuple, Dict[str, Any]] = {}
    if not state:
        return mapping
    for idx, day in enumerate(state.get("treatment_plan") or []):
        for med in day.get("medications") or []:
            med_id = med.get("id")
            if not med_id:
                continue
            mapping[(idx, med_id)] = {
                "status": med.get("status", "pending"),
                "status_detail": med.get("status_detail"),
                "notes": med.get("notes"),
            }
    return mapping


def _course_slug(course: Dict[str, Any]) -> str:
    base = f"{course.get('name', 'medication')}-{course.get('schedule', '')}"
    return slugify(base) or uuid.uuid4().hex[:8]


def _build_plan_from_courses(
    courses: List[Dict[str, Any]],
    start_iso: str | None,
    prev_state: Dict[str, Any] | None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    start_dt = _parse_iso_datetime(start_iso)
    if not start_dt:
        start_dt = datetime.utcnow()
    prev_status = _extract_status_map(prev_state)

    normalized_courses: List[Dict[str, Any]] = []
    total_days = 0
    for course in courses:
        course = course or {}
        course = course.copy()
        duration = course.get("duration_days", 1)
        start_day = course.get("start_day", 1)
        try:
            duration = max(1, int(duration))
        except (TypeError, ValueError):
            duration = 1
        try:
            start_day = max(1, int(start_day))
        except (TypeError, ValueError):
            start_day = 1
        course["duration_days"] = duration
        course["start_day"] = start_day
        if not course.get("id"):
            course["id"] = _course_slug(course)
        normalized_courses.append(course)
        total_days = max(total_days, start_day + duration - 1)

    plan: List[Dict[str, Any]] = []
    for day_idx in range(total_days):
        day_date = (start_dt + timedelta(days=day_idx)).date().isoformat()
        day_entry = {
            "day_label": f"Day {day_idx + 1}",
            "day_subtitle": f"Day {day_idx + 1}",
            "day_number": day_idx + 1,
            "day_date": day_date,
            "medications": [],
        }
        for course in normalized_courses:
            start_day = course["start_day"] - 1
            end_day = start_day + course["duration_days"]
            if start_day <= day_idx < end_day:
                med = {
                    "id": course["id"],
                    "name": course.get("name", "Medication"),
                    "type": course.get("type", "Prescription"),
                    "schedule": course.get("schedule", "Daily"),
                    "dosage": course.get("dosage", "As prescribed"),
                    "instructions": course.get("instructions", "Follow medical advice."),
                    "purpose": course.get("purpose", "Supports your treatment"),
                    "notes": course.get("notes"),
                    "status": "pending",
                }
                prev = prev_status.get((day_idx, med["id"]))
                if prev:
                    med["status"] = prev.get("status", "pending")
                    if prev.get("status_detail"):
                        med["status_detail"] = prev["status_detail"]
                    if prev.get("notes"):
                        med["notes"] = prev["notes"]
                day_entry["medications"].append(med)
        plan.append(day_entry)

    return plan, normalized_courses


def _compute_current_day_index(
    prev_state: Dict[str, Any] | None,
    plan: List[Dict[str, Any]],
    start_iso: str | None,
) -> int:
    if not plan:
        return 0
    start_dt = _parse_iso_datetime(start_iso)
    if not start_dt:
        start_dt = datetime.utcnow()
    today = datetime.utcnow().date()
    days_since = max(0, (today - start_dt.date()).days)
    max_idx = len(plan) - 1
    computed_idx = min(days_since, max_idx)
    prev_idx = None
    if prev_state and isinstance(prev_state.get("current_day_index"), int):
        prev_idx = prev_state.get("current_day_index")
    if isinstance(prev_idx, int):
        return min(max(prev_idx, computed_idx), max_idx)
    return computed_idx


def _aggregate_plan(visits: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    plan_map: Dict[str, Dict[str, Any]] = {}
    today_iso = datetime.utcnow().date().isoformat()
    for visit in visits:
        record = visit["record"]
        state = visit["state"]
        visit_id = record["id"]
        for idx, day in enumerate(state.get("treatment_plan") or []):
            day_date = day.get("day_date")
            if not day_date:
                continue
            entry = plan_map.setdefault(
                day_date,
                {
                    "day_date": day_date,
                    "day_label": day.get("day_label") or day_date,
                    "day_subtitle": day.get("day_subtitle") or day_date,
                    "medications": [],
                },
            )
            for med in day.get("medications") or []:
                med_entry = med.copy()
                med_entry["visit_id"] = visit_id
                med_entry["visit_title"] = record.get("title")
                med_entry["visit_day_index"] = idx
                entry["medications"].append(med_entry)
    sorted_dates = sorted(plan_map.keys())
    aggregated_plan: List[Dict[str, Any]] = []
    current_index = 0
    for i, date_key in enumerate(sorted_dates):
        item = plan_map[date_key]
        dt_obj = _parse_iso_datetime(date_key)
        if dt_obj:
            item["day_label"] = dt_obj.strftime("%A")
            item["day_subtitle"] = dt_obj.strftime("%b %d")
        aggregated_plan.append(item)
        if date_key <= today_iso:
            current_index = i
    if not aggregated_plan:
        return [], 0
    current_index = min(current_index, len(aggregated_plan) - 1)
    return aggregated_plan, current_index


def _combine_alerts(visits: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    for visit in visits:
        alerts.extend(visit["state"].get("safety_alerts") or [])
        if len(alerts) >= limit:
            break
    return alerts[:limit]


def _visit_status(state: Dict[str, Any]) -> str:
    today = datetime.utcnow().date()
    for day in state.get("treatment_plan") or []:
        day_date = _parse_iso_datetime(day.get("day_date"))
        if day_date and day_date.date() < today:
            continue
        for med in day.get("medications") or []:
            if med.get("status", "pending") == "pending":
                return "in_progress"
    return "completed"


def _build_checkin_questions_multi(
    aggregated_plan: List[Dict[str, Any]],
    current_index: int,
    visits: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    questions: List[Dict[str, Any]] = []
    if aggregated_plan and 0 <= current_index < len(aggregated_plan):
        today_plan = aggregated_plan[current_index]
        for med in today_plan.get("medications") or []:
            if med.get("status", "pending") != "pending":
                continue
            med_name = med.get("name", "this medication")
            schedule = med.get("schedule")
            schedule_text = f" ({schedule})" if schedule else ""
            questions.append(
                {
                    "id": f"med-{med.get('visit_id')}-{med.get('id')}",
                    "type": "medication",
                    "visit_id": med.get("visit_id"),
                    "medication_id": med.get("id"),
                    "day_index": med.get("visit_day_index"),
                    "prompt": f"Did you take {med_name}{schedule_text}?",
                    "options": ["Yes", "No"],
                    "layout": "two",
                }
            )
    # Add symptom questions from active visits
    for visit in visits:
        visit_id = visit["record"].get("id")
        for question in visit["state"].get("symptom_questions") or []:
            questions.append(
                {
                    "id": question.get("id") or f"symptom-{len(questions)}",
                    "type": "symptom",
                    "visit_id": visit_id,
                    "prompt": question.get("prompt", "How are you feeling?"),
                    "options": question.get("options") or ["Better", "Same", "Worse"],
                    "layout": question.get("layout") or "grid",
                }
            )
            if len(questions) >= 6:
                break
        if len(questions) >= 6:
            break
    return questions


def _load_visit_bundles(patient_id: str | None) -> List[Dict[str, Any]]:
    bundles: List[Dict[str, Any]] = []
    records = _load_analysis_records(patient_id)
    for record in records:
        visit_id = record.get("id")
        if not visit_id:
            continue
        state = _load_visit_state(patient_id, visit_id)
        if not state:
            state = _generate_overview_state(record)
            _save_visit_state(patient_id, visit_id, state)
        bundles.append({"record": record, "state": state})
    return bundles


def _build_overview_payload(patient_id: str | None) -> Dict[str, Any]:
    bundles = _load_visit_bundles(patient_id)
    aggregated_plan, current_index = _aggregate_plan(bundles)
    safety_alerts = _combine_alerts(bundles)
    questions = _build_checkin_questions_multi(aggregated_plan, current_index, bundles)
    visit_summaries = []
    for bundle in bundles:
        record = bundle["record"]
        visit_summaries.append(
            {
                "id": record.get("id"),
                "title": record.get("title"),
                "doctor_name": record.get("doctor_name"),
                "created_at": record.get("created_at"),
                "status": _visit_status(bundle["state"]),
            }
        )
    latest_record = bundles[0]["record"] if bundles else None
    identifier = (
        (latest_record.get("patient_id") if latest_record else None)
        or patient_id
        or DEFAULT_PATIENT_ID
    )
    return {
        "patient_name": latest_record.get("patient_name") if latest_record else DEFAULT_PATIENT_NAME,
        "patient_id": identifier,
        "latest_analysis": latest_record,
        "overview": {
            "plan": aggregated_plan,
            "current_day_index": current_index,
            "safety_alerts": safety_alerts,
        },
        "checkin_questions": questions,
        "visits": visit_summaries,
    }


def _collect_prescription_images(patient_id: str | None, limit: int = 4) -> List[Dict[str, Any]]:
    images: List[Dict[str, Any]] = []
    records = _load_analysis_records(patient_id)
    for record in records:
        visit_id = record.get("id")
        if not visit_id:
            continue
        prescription = record.get("prescription") or {}
        filename = prescription.get("filename")
        if not filename:
            continue
        path = _visit_directory(patient_id, visit_id) / filename
        if not path.exists():
            continue
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        images.append(
            {
                "visit_id": visit_id,
                "title": record.get("title"),
                "doctor": record.get("doctor_name"),
                "summary": record.get("summary"),
                "created_at": record.get("created_at"),
                "path": path,
                "mime_type": mime_type,
            }
        )
        if len(images) >= limit:
            break
    return images


def _build_medication_verification_prompt(references: List[Dict[str, Any]]) -> str:
    lines = [
        "You are a medication safety assistant. Determine whether the uploaded photo of a medication box matches any of the active prescriptions listed below.",
        "Consider name, dosage, brand, visual cues, color, layout, and warnings on the packaging.",
        "If it does not match any reference, explain why.",
        "Return STRICT JSON with this schema:",
        '{',
        '  "match": true|false,',
        '  "matched_medication": "<name or None>",',
        '  "confidence": 0-100,',
        '  "message": "<concise user-facing summary>",',
        '  "recommendation": "<next step for the patient>"',
        '}',
        "",
        "Active prescriptions:",
    ]
    for ref in references:
        summary = ref.get("summary") or "No summary provided."
        lines.append(
            f"- Visit: {ref.get('title') or 'Consultation'} ({ref.get('doctor') or 'Doctor'}) on {ref.get('created_at') or 'unknown date'} — Summary: {summary}"
        )
    return "\n".join(lines)

def _apply_medication_answers(state: Dict[str, Any] | None, answers: List[Dict[str, Any]] | None) -> None:
    if not state or not answers:
        return
    update_map: Dict[tuple, Dict[str, str]] = {}
    for answer in answers:
        if answer.get("type") != "medication":
            continue
        med_id = answer.get("medication_id")
        if not med_id:
            continue
        target_day = answer.get("day_index")
        try:
            target_day = int(target_day)
        except (TypeError, ValueError):
            target_day = None
        raw_response = (answer.get("answer") or "").strip().lower()
        if raw_response in YES_RESPONSES:
            update_map[(target_day, med_id)] = {
                "status": "taken",
                "status_detail": "Patient confirmed dose.",
            }
        elif raw_response in NO_RESPONSES:
            update_map[(target_day, med_id)] = {
                "status": "missed",
                "status_detail": "Patient reported the dose was skipped.",
            }
    if not update_map:
        return
    for idx, day in enumerate(state.get("treatment_plan") or []):
        for med in day.get("medications") or []:
            med_id = med.get("id")
            if not med_id:
                continue
            for (target_day, target_id), payload in update_map.items():
                if target_id != med_id:
                    continue
                if target_day is not None and target_day != idx:
                    continue
                med.update(payload)


def _build_overview_prompt(
    analysis_record: Dict[str, Any],
    prev_state: Dict[str, Any] | None = None,
    checkin_answers: List[Dict[str, Any]] | None = None,
) -> str:
    summary = analysis_record.get("summary", "")
    transcription = (analysis_record.get("transcription") or "")[:5000]
    sections_text = _format_sections_text(analysis_record.get("sections"))
    prev_json = json.dumps(prev_state or None, ensure_ascii=False, indent=2)
    answers_json = json.dumps(checkin_answers or [], ensure_ascii=False, indent=2)
    notes = analysis_record.get("notes", "")
    consultation_date = analysis_record.get("created_at", datetime.utcnow().isoformat())

    return f"""
You are MedVisit's follow-up AI. Maintain an up-to-date treatment plan and patient-facing safety alerts using the consultation context and (optionally) the latest check-in answers.

Consultation summary:
{summary or 'No summary provided.'}

Patient notes:
{notes or 'No extra notes.'}

Transcript excerpt (trimmed):
{transcription or 'No transcript provided.'}

Structured highlights:
{sections_text or 'No structured sections provided.'}

Previous overview state JSON:
{prev_json}

Latest check-in answers JSON:
{answers_json}

Consultation date (UTC ISO):
{consultation_date}

Return STRICT JSON with the following structure (do not include prose outside JSON):
{{
  "medication_courses": [
    {{
      "name": "Cetirizine 10mg",
      "type": "Antihistamine",
      "dosage": "1 tablet (10mg) every evening",
      "instructions": "Take with water before bedtime.",
      "purpose": "Controls allergic rhinitis symptoms.",
      "schedule": "Evening",
      "start_day": 1,
      "duration_days": 10,
      "daily_frequency": "1x evening",
      "notes": "Monitor drowsiness"
    }}
  ],
  "safety_alerts": [
    {{
      "category": "emergency|doctor|tip",
      "title": "Emergency watch",
      "description": "<=30 word alert",
      "icon": "alert-triangle|stethoscope|info|shield-alert",
      "tone": "critical|warning|info"
    }}
  ],
  "symptom_questions": [
    {{
      "id": "symptom-energy",
      "prompt": "question text",
      "options": ["..."],
      "layout": "two|grid|chips"
    }}
  ],
  "checkin_summary": "<=30 word recap of notable findings or empty string"
}}

Rules:
- Enumerate every medication course with accurate start_day and duration_days based on the prescription; include ALL remaining days (e.g., a 10-day antihistamine must report duration_days = 10).
- Preserve previously generated days and do not advance the focus day automatically unless the patient clearly completed that day.
- Provide precise dosage/instruction/purpose details for each course so we can build the per-day timeline.
- Provide between 2 and 3 safety alerts (emergency, doctor recommendation, helpful tip). Keep them patient-friendly.
- Provide 2 to 3 symptom questions tailored to this case (after medication questions in the UI).
- Keep every entry concise (<=30 words).
- Output only JSON.
"""


def _generate_overview_state(
    analysis_record: Dict[str, Any],
    prev_state: Dict[str, Any] | None = None,
    checkin_answers: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    prompt = _build_overview_prompt(analysis_record, prev_state, checkin_answers)
    state = _call_gemini([{"text": prompt}])
    courses = state.get("medication_courses") or []
    plan, normalized_courses = _build_plan_from_courses(courses, analysis_record.get("created_at"), prev_state)
    state_out = {
        "medication_courses": normalized_courses,
        "treatment_plan": plan,
        "safety_alerts": state.get("safety_alerts") or [],
        "symptom_questions": state.get("symptom_questions") or _default_symptom_questions(),
        "checkin_summary": state.get("checkin_summary", ""),
    }
    state_out["start_date"] = (_parse_iso_datetime(analysis_record.get("created_at")) or datetime.utcnow()).date().isoformat()
    state_out["current_day_index"] = _compute_current_day_index(prev_state, plan, analysis_record.get("created_at"))
    _apply_medication_answers(state_out, checkin_answers)
    state_out["latest_analysis_id"] = analysis_record.get("id")
    state_out["generated_at"] = datetime.utcnow().isoformat()
    if checkin_answers:
        state_out["last_checkin_answers"] = checkin_answers
    return state_out


@app.post("/analyze")
async def analyze_visit(
    audio_file: UploadFile = File(...),
    prescription_photo: UploadFile | None = File(None),
    notes: str = Form(""),
    patient_id: str | None = Form(None),
    patient_name: str | None = Form(None),
    doctor_name: str | None = Form(None),
):
    if audio_file is None:
        raise HTTPException(status_code=400, detail="Audio file is required.")

    audio_bytes = await audio_file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio file is empty.")
    audio_part = {
        "inline_data": {
            "mime_type": audio_file.content_type or "audio/webm",
            "data": base64.b64encode(audio_bytes).decode("utf-8"),
        }
    }

    text_part = {"text": _build_prompt(notes)}
    parts: List[Dict[str, Any]] = [text_part, audio_part]

    prescription_bytes = None
    if prescription_photo:
        prescription_bytes = await prescription_photo.read()
        if prescription_bytes:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": prescription_photo.content_type
                        or "image/jpeg",
                        "data": base64.b64encode(prescription_bytes).decode("utf-8"),
                    }
                }
            )

    display_patient_id = patient_id or DEFAULT_PATIENT_ID
    patient_slug = _resolve_patient_id(display_patient_id)
    analysis = _call_gemini(parts)

    analysis_id = uuid.uuid4().hex
    timestamp = datetime.utcnow()
    visit_dir = _ensure_visit_dir(display_patient_id, analysis_id)

    audio_ext = guess_extension(audio_file.filename, audio_file.content_type, ".webm")
    audio_filename = f"{analysis_id}{audio_ext}"
    audio_disk_path = visit_dir / audio_filename
    with audio_disk_path.open("wb") as f:
        f.write(audio_bytes)
    audio_url = f"/data/{patient_slug}/visits/{analysis_id}/{audio_filename}"

    prescription_info = None
    if prescription_photo and prescription_bytes:
        image_ext = guess_extension(
            prescription_photo.filename, prescription_photo.content_type, ".jpg"
        )
        image_filename = f"{analysis_id}{image_ext}"
        image_disk_path = visit_dir / image_filename
        with image_disk_path.open("wb") as f:
            f.write(prescription_bytes)
        prescription_info = {
            "filename": image_filename,
            "mime_type": prescription_photo.content_type,
            "url": f"/data/{patient_slug}/visits/{analysis_id}/{image_filename}",
        }

    doctor_value = (doctor_name or "").strip()
    doctor_for_record = doctor_value or DEFAULT_DOCTOR

    analysis_record = {
        "id": analysis_id,
        "title": f"Consultation • {timestamp.strftime('%d %b %Y')}",
        "patient_name": patient_name or DEFAULT_PATIENT_NAME,
        "patient_id": display_patient_id,
        "doctor_name": doctor_for_record,
        "created_at": timestamp.isoformat(),
        "model": GEMINI_MODEL,
        "notes": notes.strip(),
        "audio": {
            "filename": audio_filename,
            "mime_type": audio_file.content_type,
            "url": audio_url,
        },
        "prescription": prescription_info,
        "transcription": analysis.get("transcription", ""),
        "summary": analysis.get("summary", ""),
        "sections": analysis.get("sections") or [],
    }

    analysis_path = _analysis_path(display_patient_id, analysis_id)
    with analysis_path.open("w", encoding="utf-8") as f:
        json.dump(analysis_record, f, ensure_ascii=False, indent=2)

    visit_state = _generate_overview_state(analysis_record, prev_state=None)
    _save_visit_state(display_patient_id, analysis_id, visit_state)

    overview_payload = _build_overview_payload(display_patient_id)

    return JSONResponse({"analysis": analysis, "record": analysis_record, "overview": overview_payload.get("overview")})


def _load_analysis_records(patient_id: str | None) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    patient_root = _patient_root(patient_id)
    visits_dir = _visits_dir(patient_id)
    for legacy_path in patient_root.glob("*.json"):
        name = legacy_path.name
        if name.startswith("checkin-"):
            continue
        _migrate_legacy_analysis(patient_id, legacy_path)
    for analysis_file in visits_dir.glob("*/analysis.json"):
        visit_name = analysis_file.parent.name
        if visit_name == "overview_state":
            continue
        try:
            with analysis_file.open(encoding="utf-8") as f:
                record = json.load(f)
                record.setdefault("id", visit_name)
                records.append(record)
        except json.JSONDecodeError:
            logger.warning("Skipped malformed analysis file: %s", analysis_file)
    records.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    return records


@app.get("/analyses")
async def list_analyses(patient_id: str | None = None, search: str | None = None):
    records = _load_analysis_records(patient_id)
    for record in records:
        visit_id = record.get("id")
        if not visit_id:
            continue
        state = _load_visit_state(patient_id, visit_id)
        if not state:
            state = _generate_overview_state(record)
            _save_visit_state(patient_id, visit_id, state)
        record["status"] = _visit_status(state)
    if search:
        q = search.lower()
        filtered: List[Dict[str, Any]] = []
        for record in records:
            blob = " ".join(
                [
                    record.get("summary", ""),
                    record.get("transcription", ""),
                    record.get("notes", ""),
                    " ".join(
                        " ".join(section.get("items", []))
                        for section in record.get("sections", [])
                    ),
                ]
            ).lower()
            if q in blob:
                filtered.append(record)
        records = filtered
    return {"analyses": records}


@app.get("/overview")
async def get_overview(patient_id: str | None = None):
    return _build_overview_payload(patient_id)


@app.post("/checkin")
async def submit_checkin(payload: Dict[str, Any] = Body(...)):
    answers = payload.get("answers") or []
    if not isinstance(answers, list) or not answers:
        raise HTTPException(status_code=400, detail="At least one check-in answer is required.")
    patient_id = payload.get("patient_id") or DEFAULT_PATIENT_ID
    records = _load_analysis_records(patient_id)
    if not records:
        raise HTTPException(status_code=400, detail="No consultation found yet. Capture a visit first.")

    grouped_answers: Dict[str, List[Dict[str, Any]]] = {}
    for answer in answers:
        visit_id = answer.get("visit_id")
        if not visit_id:
            continue
        grouped_answers.setdefault(visit_id, []).append(answer)

    updated_visits: Dict[str, Dict[str, Any]] = {}
    for record in records:
        visit_id = record.get("id")
        if not visit_id:
            continue
        state = _load_visit_state(patient_id, visit_id)
        if not state:
            state = _generate_overview_state(record)
        visit_answers = grouped_answers.get(visit_id)
        if visit_answers:
            state = _generate_overview_state(record, prev_state=state, checkin_answers=visit_answers)
        _save_visit_state(patient_id, visit_id, state)
        updated_visits[visit_id] = state

    checkin_id = uuid.uuid4().hex
    timestamp = datetime.utcnow().isoformat()
    notes = (payload.get("notes") or "").strip()
    checkin_record = {
        "id": checkin_id,
        "created_at": timestamp,
        "answers": answers,
        "notes": notes,
    }
    patient_root = _patient_root(patient_id)
    checkin_path = patient_root / f"checkin-{checkin_id}.json"
    with checkin_path.open("w", encoding="utf-8") as file_obj:
        json.dump(checkin_record, file_obj, ensure_ascii=False, indent=2)

    overview_payload = _build_overview_payload(patient_id)
    overview_payload["checkin_record"] = checkin_record
    return overview_payload


@app.post("/verify-medication")
async def verify_medication(
    patient_id: str | None = Form(None),
    photo: UploadFile = File(...),
):
    resolved_patient_id = patient_id or DEFAULT_PATIENT_ID
    photo_bytes = await photo.read()
    if not photo_bytes:
        raise HTTPException(status_code=400, detail="Photo is required.")

    references = _collect_prescription_images(resolved_patient_id)
    if not references:
        raise HTTPException(
            status_code=400,
            detail="No prescription photos are available for your current treatments.",
        )

    prompt_text = _build_medication_verification_prompt(references)
    parts: List[Dict[str, Any]] = [
        {"text": prompt_text},
        {"text": "Medication photo to verify."},
        {
            "inline_data": {
                "mime_type": photo.content_type or "image/jpeg",
                "data": base64.b64encode(photo_bytes).decode("utf-8"),
            }
        },
    ]

    for ref in references:
        try:
            with ref["path"].open("rb") as file_obj:
                encoded = base64.b64encode(file_obj.read()).decode("utf-8")
        except OSError as exc:
            logger.warning("Unable to read prescription image %s: %s", ref["path"], exc)
            continue
        parts.append(
            {
                "text": f"Reference prescription image for {ref.get('title') or 'Visit'} ({ref.get('doctor') or 'Doctor'})."
            }
        )
        parts.append(
            {
                "inline_data": {
                    "mime_type": ref.get("mime_type") or "image/jpeg",
                    "data": encoded,
                }
            }
        )

    result = _call_gemini(parts)
    verification = {
        "match": bool(result.get("match")),
        "matched_medication": result.get("matched_medication") or "",
        "confidence": result.get("confidence", 0),
        "message": result.get("message")
        or ("The medication appears to match the prescription." if result.get("match") else "The medication may not match the prescription."),
        "recommendation": result.get("recommendation")
        or ("You can use this medication as prescribed." if result.get("match") else "Please double-check with your pharmacist or doctor before using it."),
    }
    return {"verification": verification}

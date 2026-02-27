MedVisit Architecture
=====================

High-Level Overview
-------------------
```
┌────────────────────────┐        HTTPS        ┌──────────────────────────────┐
│  MedVisit Frontend     │  ─────────────────▶ │   FastAPI Backend (uvicorn)  │
│  (Single Page App)     │  ◀───────────────── │   dev/main.py                │
│  - Tailwind UI         │    JSON responses   │   • /analyze                 │
│  - Service Worker &    │                    │   • /checkin                 │
│    Manifest (PWA)      │                    │   • /analyses                │
│  - LocalStorage profile│                    │   • /verify-medication       │
└──────────┬─────────────┘                    └──────────┬───────────────────┘
           │ fetch / data                                  │
           │                                               │ reads/writes
           ▼                                               ▼
    Browser APIs (MediaRecorder,                 dev/DATA/<patient>/visits/<id>/
    Web Audio, File inputs)                     ├─ analysis.json
                                                ├─ state.json
                                                ├─ audio files (*.webm/mp3…)
                                                └─ prescription images (.jpg/.png)
                                                            │
                                                            ▼
                                              Google Gemini 3 Pro (GenAI SDK)
                                              • Visit analysis (audio + photo)
                                              • Check-in reasoning
                                              • Medication verification
```

Frontend Components
-------------------
- **Navigation Shell**: Responsive sidebar/bottom nav + sticky mobile header. Handles tab switching without page reloads.
- **Views**:
  * *Overview*: Greeting, medication verification CTA, dynamic treatment timeline, Safety & Alerts, and check-in workflow.
  * *Capture*: Recording/upload UI with live spectrogram, prescription upload, doctor selection, and Gemini analysis results.
  * *History*: Searchable visit list, analysis detail modal with audio player, doctor badges, and prescription preview.
  * *Profile*: Patient metrics, allergies, and settings modal (mirrors onboarding fields).
- **Local State**:
  * `patientProfile` from `localStorage` (name, auto-generated ID, biometric data, allergies, primary doctor).
  * `overviewData`, `currentTreatmentPlan`, `analysesData` fetched from backend.
  * Recording state managed via `MediaRecorder` and Web Audio analyser nodes.
- **PWA Layer**:
  * `manifest.json` references icons from `dev/static/icons/`.
  * `sw.js` caches shell assets, enabling install prompts and offline support on HTTPS origins.

Backend Components
------------------
- **FastAPI Application (`dev/main.py`)**
  * Serves Jinja template (`/`) plus static assets (`/static`, `/data`).
  * REST endpoints:
    - `POST /analyze`: Accepts audio, optional prescription photo, notes, patient/doctor info; uploads to Gemini; stores analysis + state.
    - `POST /checkin`: Persists daily answers, invokes Gemini for updated treatment statuses and alerts.
    - `GET /analyses`: Returns visit list with computed status and search filtering.
    - `POST /verify-medication`: Sends user-uploaded box photo + prescription references to Gemini for conformity checks.
  * Helper modules manage patient directories, visit IDs, slugification, and legacy data migration.

- **Storage Layout (`dev/DATA`)**
  * Per patient slug directory (generated from onboarding ID or default).
  * Each visit has its own folder containing audio, image assets, `analysis.json`, and `state.json`.
  * State files cache Gemini outputs (treatment plan, alerts, current day index, symptom questions) for quick reloads.

- **Gemini Integration**
  * Google GenAI client instantiated with API key from `.env`.
  * Different prompt templates for visit analysis, check-ins, and verification to ensure JSON outputs.
  * Responses parsed and validated before persisting; fallback logging helps diagnose malformed JSON.

Data Flow
---------
1. **Onboarding**: Patient fills form → frontend saves profile locally → subsequent API calls include `patient_id` + `patient_name`.
2. **Capture Flow**:
   - User records or uploads audio (`MediaRecorder` / file input) and optional prescription photo; chooses doctor.
   - `startAnalysis()` builds `FormData` and POSTs to `/analyze`.
   - Backend stores media, calls Gemini, generates `analysis.json` + `state.json`, and returns updated overview snapshot.
   - Frontend refreshes Overview & History views without reload.
3. **Check-in Flow**:
   - Frontend fetches current symptom questions from overview data.
   - Sequential UI collects answers, then POSTs to `/checkin`.
   - Backend stores answers, prompts Gemini to update plan statuses + alerts, and responds with refreshed overview data.
4. **History View**:
   - On load or search, frontend calls `/analyses`.
   - Backend reads all visit folders, ensuring states exist, attaches doctor names and tags, and returns JSON list.
5. **Medication Verification**:
   - User opens modal, uploads photo. Frontend POSTs to `/verify-medication`.
   - Backend gathers relevant prescription images, creates prompt for Gemini, and returns match status + recommendation.

Deployment Considerations
-------------------------
- Dockerfile builds FastAPI app, includes `dev/static` assets and `dev/DATA` mount for persistence.
- `deploy-aws-2023.sh` / `setup-aws-2023.sh` automate provisioning and container rollout.
- Ensure `GEMINI_API_KEY`, `GEMINI_MODEL`, and optional defaults are present in `.env`.

Future Enhancements
-------------------
- Centralized auth/session if MedVisit evolves beyond single-patient local profiles.
- Streaming Gemini responses for faster perceived latency.
- Automated tests (FastAPI + Playwright) to cover capture flow, check-ins, history modal, and verification pipeline.

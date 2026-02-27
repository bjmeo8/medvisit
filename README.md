# MedVisit

MedVisit is a Gemini-powered companion webapp/PWA that helps patients capture their consultations, follow treatments, verify medications, and maintain a structured visit history. The project bundles a Tailwind-based SPA (served through FastAPI/Jinja) with a Python backend that orchestrates Gemini calls for audio analysis, safety reasoning, check-ins, and medication verification.

---

## Highlights

- **Patient onboarding + profile management** saved locally (name, biometrics, allergies, primary doctor, generated ID).
- **Visit capture** via native recording (MediaRecorder + Web Audio spectrogram) or audio uploads, plus prescription photo attachment.
- **Gemini visit analysis** that returns transcripts, summaries, treatment plans, and safety alerts; results feed Overview & History views.
- **Daily check-ins** with sequential questions; Gemini marks medications Taken/Missed and refreshes alerts in real time.
- **Medication verification** modal that compares uploaded box photos against prescription images using Gemini vision.
- **History explorer** with search, doctor badges, and detailed modal (transcript sections, tags, playable audio).
- **Installable PWA** with custom icons, manifest, and service worker caching for mobile use.

---

## Repository Layout

```
dev/
  main.py                # FastAPI entrypoint
  templates/medvisit.html# SPA template w/ Tailwind + JS
  static/                # Icons, manifest, service worker, styles
  DATA/                  # Runtime storage (per-patient visit folders)
  Dockerfile             # Container build
  deploy-aws-2023.sh     # Sample deployment automation
docs/
  architecture.md        # High-level diagrams
  medvisit-specs.txt     # Product spec & behavior guide
  gemini-text-generation.md # Prompting notes
```

---

## Prerequisites

- Python **3.10+**
- Node is not required (Tailwind via CDN)
- Google Gemini API access with `GEMINI_API_KEY`
- (Optional) Docker / Docker Compose for containerized runs

---

## Local Setup

1. **Clone & enter repo**
   ```bash
   git clone <repo-url>
   cd medvisit/dev
   ```

2. **Create virtual environment**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**
   Create `dev/.env` based on `.env.example` (if provided) or manually:
   ```
   GEMINI_API_KEY=your-key
   GEMINI_MODEL=gemini-3-pro-preview  # optional override
   DEFAULT_DOCTOR=Dr. Example
   ```

5. **Run FastAPI server**
   ```bash
   uvicorn main:app --reload
   ```
   Visit `http://localhost:8000` to load MedVisit.

---

## Using MedVisit Locally

1. **Onboarding**
   - On first load the overlay prompts for name, weight, height, blood type, allergies, and primary doctor. Patient ID is auto-generated and saved to `localStorage`.

2. **Capture a visit**
   - Switch to *Capture* tab.
   - Record audio using the REC button or upload an `.mp3/.m4a/.wav/.webm`.
   - Attach a prescription photo (optional), adjust doctor name per consultation, add notes, and click **Analyze Visit**.
   - Backend stores files under `dev/DATA/<patient>/visits/<id>/` and Gemini returns the structured analysis.

3. **Review Overview**
   - “Your Treatment” shows the current day plus other days; tap a card for medication details.
   - “Safety & Alerts” cards update when new analyses or check-ins arrive.
   - Use “Verify your medication” to upload a pill-box photo for Gemini validation.

4. **History + audio playback**
   - Search visits by doctor, symptoms, or summary keywords.
   - Click a card for full details, transcript sections, and playable audio. Modal automatically pauses audio when closed.

5. **Check-ins**
   - Answer the sequential prompts in the Overview card.
   - Gemini merges results to mark medications Taken/Missed and update alerts without jumping days.

---

## Data Storage

- `dev/DATA/` holds patient folders with visit subdirectories containing:
  - `analysis.json`: canonical visit data.
  - `state.json`: cached overview state (treatment plan, safety alerts, symptom questions).
  - Uploaded media (audio + prescription photos).
- Static mount `/data` serves these files for playback and previews. Persist this directory in production (volume mount or object storage syncing).

---

## Testing & Validation

Currently manual:
- Verify onboarding persistence across reloads.
- Run capture flow (both recording and uploads) and confirm Overview/History refresh.
- Execute multiple check-ins per day ensuring day indexes stay correct.
- Use medication verification modal with valid/invalid images to confirm Gemini messaging.

Automated test scaffolding is TBD; consider adding FastAPI unit tests plus Playwright integration coverage for critical flows.

---

## Deployment

### Docker
```bash
cd dev
docker build -t medvisit .
docker run -p 8000:8000 --env-file .env -v $(pwd)/DATA:/app/DATA medvisit
```
(Mount `DATA` to persist visits.)

### Scripts
`setup-aws-2023.sh` and `deploy-aws-2023.sh` illustrate provisioning + rollout steps (Docker install, pulling image, running container). Customize based on your infrastructure.

### PWA Requirements
- Serve over HTTPS for install prompts.
- Ensure `/static/manifest.json` and `/static/sw.js` paths remain accessible.
- Provide valid icons (already generated from `dev/static/medvisit-logo-hd.png`).

---

## Troubleshooting

- **“Could not import module `app`”**: start uvicorn from `dev/` (`uvicorn main:app --reload`) or adjust Docker CMD to `python -m uvicorn main:app`.
- **Gemini errors / malformed JSON**: backend logs raw responses; check console for prompt/response details.
- **Audio recorder unavailable**: browser lacks `MediaRecorder`. Use the upload button instead.
- **Mobile file picker not showing audio**: inputs accept `audio/*` plus explicit extensions (`.mp3`, `.m4a`, `.aac`, `.wav`, `.ogg`, `.webm`).

---

## References

- [docs/medvisit-specs.txt](docs/medvisit-specs.txt) – detailed product requirements.
- [docs/architecture.md](docs/architecture.md) – architecture diagram + component breakdown.
- [docs/gemini-text-generation.md](docs/gemini-text-generation.md) – prompting notes / guidelines.

Contributions and bug reports are welcome! Open issues/PRs describing the scenario, expected behavior, and reproduction steps.

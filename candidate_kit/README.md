# Employee Attendance & Analytics API

This project implements a FastAPI service backed by MongoDB for employee attendance tracking, shift validation, overtime calculation, attendance analytics, and correction workflows.

## Project flow

Read the files in this order:

1. `PROBLEM_STATEMENT.docx` — task description and evaluation criteria.
2. `openapi.yaml` — API contract and business rules (R1 to R10).
3. `DATA_MODEL.md` — MongoDB schema and sample documents.
4. `app/main.py` — application code. All logic must live in this file.
5. `REVIEW.md` and `DECISIONS.md` — review notes and design explanations.

The `sample_data/` directory contains the sample MongoDB Extended JSON documents, and `sample_seed.py` loads them into MongoDB for local testing. The grader dataset is larger and includes additional edge cases.

## Local setup

### Prerequisites

- Python 3.10+
- MongoDB 6.0 or newer
- A local MongoDB instance or MongoDB Atlas instance

### Linux / macOS

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Optional: set environment variables in your shell or a local .env file
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

### Windows (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Optional: set environment variables in PowerShell or a local .env file
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"

python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

### MongoDB quick start

```bash
docker run -p 27017:27017 mongo:7
```

MongoDB 6.0+ is required because the project uses MongoDB features such as `$densify` and `$setWindowFields`. A MongoDB Atlas M0 free tier also works.

## Run the API

```bash
uvicorn app.main:app --port 8000
```

Swagger UI is available at:

`http://127.0.0.1:8000/docs`

## Submission requirements

Submit a public Git repository containing:

- `app/main.py`
- `requirements.txt`
- `REVIEW.md`
- `DECISIONS.md`
- `README.md`

Do not include:

- `.env` files
- secrets or credentials
- Dockerfile

## Notes

- Review notes for the starter code are in `REVIEW.md`; design decisions are recorded in `DECISIONS.md`.
- API timestamps are epoch milliseconds, while MongoDB stores BSON dates internally.
- Dates, months, and shift times are represented as IST strings in the business logic.
- The implementation follows the contract and all required endpoints are covered.


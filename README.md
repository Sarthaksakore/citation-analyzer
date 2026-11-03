# Citation Analyzer

Multi-source citation analysis, verification, deduplication, and researcher
disambiguation. The project includes a FastAPI backend and a React/Vite
frontend.

## Requirements

- Python 3.9 or newer
- Node.js 18 or newer and npm
- Git

## Setup

Clone the repository and enter the project directory:

```powershell
git clone https://github.com/Sarthaksakore/citation-analyzer.git
cd citation-analyzer
```

### Backend

Create and activate a virtual environment, then install the Python
dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and add any optional provider keys or email
addresses you want to use. The application uses SQLite by default and creates
`citation_analyzer.db` automatically on startup.

Start the API:

```powershell
uvicorn app.main:app --reload
```

The API runs at <http://127.0.0.1:8000>. Interactive API documentation is
available at <http://127.0.0.1:8000/docs>.

### Frontend

Open a second terminal in the project directory, install the JavaScript
dependencies, and start Vite:

```powershell
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173> in a browser. The Vite development server proxies
API requests to the backend at `http://127.0.0.1:8000`.

## Tests and checks

Run backend tests from the project root:

```powershell
pytest
```

Run frontend linting and a production build:

```powershell
cd frontend
npm run lint
npm run build
```

## Optional provider configuration

OpenAlex and Crossref work without API keys, although setting the email
variables in `.env` enables their polite request pools. Semantic Scholar,
Google Scholar, IEEE Xplore, Scopus, and Web of Science settings are optional;
see `.env.example` for the supported variables.

Never commit `.env`, API keys, the local SQLite database, virtual environments,
or generated build output.

## Data completeness

Analysis responses include `provider_status` and `pipeline_errors`. The
dashboard shows a **Data completeness warning** whenever a provider returns no
records, fails for one or more works, or cannot prove that pagination reached
the end. Counts shown by the dashboard are fetched records only; they are
never estimates of records that could not be retrieved.

Google Scholar profile and citing-paper requests follow the provider's
pagination links and report fetched pages, failed citation groups, and whether
pagination completed. A name-based Scholar search without a resolved author
profile is intentionally marked partial because it cannot represent the full
profile.

Successful provider responses are cached in memory for 15 minutes by default.
This avoids repeating identical requests when a profile is reloaded or a
cached result is viewed. Configure the TTL and cache size with
`API_CACHE_TTL_SECONDS`, `API_CACHE_MAX_ENTRIES`, and
`SCHOLAR_CACHE_TTL_SECONDS`.
# Roomvu Weekly ROI Cohort Report

A self-updating reveal.js deck that pulls live cohort data from Metabase and
publishes to GitHub Pages every Wednesday morning.

---

## Quick start (local)

```bash
cd roomvu-weekly-report
pip install -r requirements.txt

export METABASE_URL=https://your-metabase.example.com
export METABASE_API_KEY=your_api_key_here

python build.py
# Writes ../index.html — open it in a browser
```

### PDF export

Open `index.html?print-pdf` in Chrome, then **File → Print → Save as PDF**.
Each slide is one page; tables scale to fit.

---

## Secrets to set in GitHub

Go to **Settings → Secrets and variables → Actions** in your repo and add:

| Secret | Where to get it |
|---|---|
| `METABASE_URL` | Base URL of your Metabase instance, e.g. `https://metabase.example.com` |
| `METABASE_API_KEY` | Metabase Admin → Settings → Authentication → API Keys |

---

## GitHub Pages setup

1. Go to **Settings → Pages**.
2. Under *Source*, select **GitHub Actions**.
3. The workflow (`weekly.yml`) handles the rest on each run.

---

## Adding a vertical / slide

1. Find the Metabase question ID (it's in the URL when you open the question).
2. Add a block to `config.yaml`:

```yaml
slides:
  - id: my_new_vertical_roi
    metabase_question: 12345
    title: "Meta · My Vertical — ROI Cohort (Weekly)"
    render: cohort_table
```

No code change needed — the renderer auto-detects week columns.

---

## Architecture

```
Metabase API (X-API-KEY)
  → build.py fetches JSON per question
  → Jinja2 renders reveal.js deck (one <section> per vertical)
  → writes index.html at repo root
  → GitHub Actions commits + publishes to GitHub Pages weekly
```

Data is baked into the HTML at build time. The browser does **not** query
Metabase — the deck works offline after the build step.

---

## Roadmap

- **AI analysis** slide type is planned for v2 (not built yet — see config.yaml
  comment and `render: ai_analysis` placeholder).

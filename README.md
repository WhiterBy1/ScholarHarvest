# ScholarHarvest

Search, filter, and download scientific papers at scale.

Built on [OpenAlex](https://openalex.org) (CC0 metadata) with [Scimago](https://www.scimagojr.com) quartile filtering. Legal, reproducible, and designed for systematic literature reviews.

## What it does

1. **Searches** OpenAlex for papers matching your queries (title + abstract)
2. **Filters** by journal quartile (Q1/Q2) using Scimago data
3. **Downloads** Open Access PDFs in parallel (validates integrity)
4. **Exports** CSV + BibTeX for Zotero, Mendeley, Rayyan, Overleaf
5. **Lists** paywall articles separately (recover via institutional access)

## Quick Start

### Option A: Command Line (recommended for bulk downloads)

```bash
git clone https://github.com/YOUR_USER/ScholarHarvest.git
cd ScholarHarvest
pip install -r requirements.txt

# Simple search
python scholar_harvest.py \
  --email you@university.edu \
  --queries "photoplethysmography vascular disease" "PPG wearable sensor"

# With config file
cp config_example.yaml config.yaml
# Edit config.yaml with your queries
python scholar_harvest.py --config config.yaml
```

### Option B: Web Interface

```bash
pip install -r requirements.txt
streamlit run streamlit_app.py
```

Opens a browser with a full GUI — no coding needed.

**Deploy free on Streamlit Cloud:** Fork this repo, go to [share.streamlit.io](https://share.streamlit.io), and point it to `streamlit_app.py`.

## CLI Usage

```
python scholar_harvest.py --email you@uni.edu --queries "topic 1" "topic 2"
```

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | — | Path to YAML config file |
| `--email` | — | Your email (required for API access) |
| `--queries` | — | Search queries (space-separated, quote each) |
| `--scimago` | — | Path to Scimago CSV file |
| `--quartiles` | Q1 Q2 | Accepted quartiles |
| `--year-from` | 2000 | Start year |
| `--year-to` | 2026 | End year |
| `--top` | 100 | Results per query (100 = 1 API call) |
| `--threads` | 6 | Parallel PDF download threads |
| `--no-pdfs` | false | Skip PDF downloads |
| `--output` | output/ | Output directory |
| `--limits` | — | Show API limitations and exit |

## Output

```
output/
  corpus_metadata.csv      # Full metadata (import to Rayyan/Zotero)
  corpus.bib               # BibTeX (Mendeley/Overleaf)
  paywall_articles.csv     # Paid articles (use institutional access)
  pdfs/                    # Downloaded Open Access PDFs
  _progreso.json           # Progress file (enables resume)
```

## API Limitations

| Limit | Value |
|-------|-------|
| Daily API calls | ~50,000 (resets midnight UTC) |
| Cost per call | $0.001 (free daily allowance) |
| Rate with email | ~10 requests/second |
| Rate without email | ~1 request/second |
| Results per page | max 200 |

**Tips:**
- Use `--top 100` (default): 1 API call per query, gets the most cited papers
- Specific queries > broad queries (fewer results, more relevant)
- Progress is saved — re-run to resume after budget resets
- Split large searches across multiple days

## Scimago Setup

1. Go to [scimagojr.com/journalrank.php](https://www.scimagojr.com/journalrank.php)
2. (Optional) Filter by subject area
3. Click **Download data**
4. Save the CSV and pass it via `--scimago` or in `config.yaml`

Without it, quartile filtering is skipped.

## For Systematic Reviews (PRISMA)

This tool handles the **Identification** phase of PRISMA:

```
Identification  → ScholarHarvest (this tool)
    ↓
Screening       → Import CSV to Rayyan (rayyan.ai) for title/abstract screening
    ↓
Eligibility     → Full-text review of included articles
    ↓
Inclusion       → Final corpus for your review
```

Report these numbers in your PRISMA flow diagram:
- Records identified through OpenAlex: (total from CSV)
- After deduplication: (unique articles in CSV)
- Screened: (your Rayyan screening count)
- Included: (your final selection)

## Deploy on Streamlit Cloud (free)

1. Fork this repo
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Connect your GitHub account
4. Select this repo and `streamlit_app.py`
5. Deploy

Your colleagues can use it from any browser — no installation needed.

## Legal

- **Metadata + Abstracts**: OpenAlex, CC0 license (free, legal, standard practice)
- **PDFs**: Only Open Access articles (legal downloads)
- **Paywall articles**: NOT downloaded — listed separately for institutional access
- **Scimago data**: Free to download for research use

## License

MIT — use freely for research, teaching, or commercial purposes.

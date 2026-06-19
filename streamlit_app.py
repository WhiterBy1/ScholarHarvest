"""
ScholarHarvest — Web Interface (Streamlit)
Deploy free on Streamlit Cloud: https://share.streamlit.io
"""

import csv
import io
import json
import random
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

st.set_page_config(
    page_title="ScholarHarvest",
    page_icon="📚",
    layout="wide",
)

OPENALEX = "https://api.openalex.org/works"


# ============================================================================
# Scimago
# ============================================================================
@st.cache_data
def load_scimago(uploaded_file):
    if not uploaded_file:
        return {}
    lookup = {}
    content = uploaded_file.getvalue().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(content), delimiter=";")
    col_issn = next((c for c in reader.fieldnames if c.strip().lower() == "issn"), None)
    col_q = next((c for c in reader.fieldnames if "best quartile" in c.strip().lower()), None)
    if not col_issn or not col_q:
        return {}
    for row in reader:
        q = (row.get(col_q) or "").strip()
        if q not in {"Q1", "Q2", "Q3", "Q4"}:
            continue
        for issn in (row.get(col_issn) or "").split(","):
            issn = re.sub(r"[^0-9Xx]", "", issn).upper()
            if len(issn) == 8:
                if issn not in lookup or q < lookup[issn]:
                    lookup[issn] = q
    return lookup


def get_quartile(work, scimago):
    src = (work.get("primary_location") or {}).get("source") or {}
    issns = []
    if src.get("issn_l"):
        issns.append(src["issn_l"])
    if src.get("issn"):
        issns.extend(src["issn"])
    best = None
    for issn in issns:
        ni = re.sub(r"[^0-9Xx]", "", issn or "").upper()
        q = scimago.get(ni)
        if q and (best is None or q < best):
            best = q
    return best


def rebuild_abstract(inv):
    if not inv:
        return ""
    try:
        pos = [(i, w) for w, idxs in inv.items() for i in idxs]
        pos.sort()
        return " ".join(w for _, w in pos)
    except Exception:
        return ""


def authors_str(work):
    return "; ".join(
        (a.get("author") or {}).get("display_name", "")
        for a in work.get("authorships", [])
        if (a.get("author") or {}).get("display_name")
    )


def journal_str(work):
    return ((work.get("primary_location") or {}).get("source") or {}).get("display_name", "")


def pdf_url(work):
    loc = work.get("best_oa_location") or {}
    return loc.get("pdf_url") or (work.get("open_access") or {}).get("oa_url")


# ============================================================================
# UI
# ============================================================================
st.title("📚 ScholarHarvest")
st.markdown("**Search, filter, and export scientific literature from OpenAlex**")

with st.expander("⚠️ API Limitations & Legal Info", expanded=False):
    st.markdown("""
    **OpenAlex API (data source):**
    - Free tier: ~50,000 calls/day (resets midnight UTC)
    - 1 query (top 100) = 1 API call
    - Requires email for polite pool access (faster)

    **PDF Downloads:**
    - Only Open Access articles (legal)
    - ~50% of OA articles have direct PDF links
    - Paid articles exported separately for institutional access

    **Scimago Quartiles:**
    - Download CSV from [scimagojr.com](https://www.scimagojr.com/journalrank.php)
    - Upload it below to filter by journal quality
    """)

# --- Sidebar config ---
with st.sidebar:
    st.header("⚙️ Configuration")

    email = st.text_input("Email (required for API)", placeholder="you@university.edu")

    scimago_file = st.file_uploader("Scimago CSV (optional)", type="csv",
                                     help="Download from scimagojr.com")

    quartiles = st.multiselect("Quartiles", ["Q1", "Q2", "Q3", "Q4"],
                               default=["Q1", "Q2"])

    col1, col2 = st.columns(2)
    with col1:
        year_from = st.number_input("Year from", 1990, 2026, 2000)
    with col2:
        year_to = st.number_input("Year to", 1990, 2026, 2026)

    top_n = st.slider("Results per query", 10, 200, 100, step=10,
                      help="100 = 1 API call per query")

    st.markdown("---")
    st.markdown(f"**API calls per search:** ~1 per query")
    st.markdown(f"**Daily limit:** ~50,000 calls")

# --- Main area ---
queries_text = st.text_area(
    "Search queries (one per line)",
    placeholder="photoplethysmography peripheral arterial disease\nPPG wearable sensor vascular\nwearable blood flow monitoring device",
    height=200,
)

queries = [q.strip() for q in queries_text.strip().split("\n") if q.strip()]

if queries:
    st.info(f"**{len(queries)} queries** — will use **{len(queries)} API calls** "
            f"(of ~50,000 daily limit)")

# --- Search button ---
if st.button("🔍 Search", type="primary", disabled=not email or not queries):
    scimago = load_scimago(scimago_file) if scimago_file else {}
    quartiles_set = set(quartiles) if quartiles else None
    filter_q = len(scimago) > 0 and quartiles_set

    if filter_q:
        st.success(f"Scimago loaded: {len(scimago)} ISSNs")
    elif scimago_file:
        st.warning("Could not parse Scimago CSV")
    else:
        st.warning("No Scimago CSV — skipping quartile filter")

    seen = set()
    corpus = []
    progress = st.progress(0, text="Starting...")

    for i, query in enumerate(queries):
        progress.progress((i) / len(queries), text=f"[{i+1}/{len(queries)}] {query}")

        filters = [f"from_publication_date:{year_from}-01-01",
                   f"to_publication_date:{year_to}-12-31",
                   "is_paratext:false", "type:article"]
        params = {
            "search": query,
            "filter": ",".join(filters),
            "per-page": top_n,
            "sort": "cited_by_count:desc",
            "mailto": email,
            "select": ("id,doi,title,publication_year,cited_by_count,"
                       "authorships,primary_location,open_access,"
                       "best_oa_location,abstract_inverted_index,language,type"),
        }

        try:
            time.sleep(1.5 + random.uniform(0, 1))
            r = requests.get(OPENALEX, params=params, timeout=60)

            if r.status_code == 429:
                st.error("⛔ API budget exhausted — resets at midnight UTC. Try again tomorrow.")
                break

            r.raise_for_status()
            data = r.json()

            for w in data.get("results", []):
                wid = w.get("id")
                if not wid or wid in seen:
                    continue
                if filter_q:
                    q = get_quartile(w, scimago)
                    if q not in quartiles_set:
                        continue
                    w["_quartile"] = q
                else:
                    w["_quartile"] = "NA"
                seen.add(wid)
                corpus.append(w)

        except requests.RequestException as e:
            st.warning(f"Error on query '{query}': {e}")
            continue

    progress.progress(1.0, text="Done!")

    if not corpus:
        st.warning("No results found.")
    else:
        corpus.sort(key=lambda w: w.get("cited_by_count", 0), reverse=True)

        st.success(f"**{len(corpus)} articles** found and deduplicated")

        # Store in session
        st.session_state["corpus"] = corpus

# --- Results display ---
if "corpus" in st.session_state:
    corpus = st.session_state["corpus"]

    tab1, tab2, tab3 = st.tabs(["📊 Results", "📥 Downloads", "📈 Stats"])

    with tab1:
        st.subheader(f"{len(corpus)} articles (sorted by citations)")
        for i, k in enumerate(corpus[:50]):
            cites = k.get("cited_by_count", 0)
            q = k.get("_quartile", "?")
            title = k.get("title", "Untitled")
            year = k.get("publication_year", "")
            journal = journal_str(k)
            doi = (k.get("doi") or "").replace("https://doi.org/", "")
            is_oa = (k.get("open_access") or {}).get("is_oa", False)
            oa_badge = "🟢 OA" if is_oa else "🔴 Paid"

            with st.expander(f"**[{q}]** {cites} cites — {title[:90]}"):
                st.markdown(f"**Journal:** {journal} ({year})")
                st.markdown(f"**Authors:** {authors_str(k)[:200]}")
                st.markdown(f"**DOI:** {doi}")
                st.markdown(f"**Access:** {oa_badge}")
                abstract = rebuild_abstract(k.get("abstract_inverted_index"))
                if abstract:
                    st.markdown(f"**Abstract:** {abstract[:500]}...")

        if len(corpus) > 50:
            st.info(f"Showing top 50 of {len(corpus)}. Download CSV for full list.")

    with tab2:
        st.subheader("Export")

        # CSV
        csv_buf = io.StringIO()
        w = csv.writer(csv_buf)
        w.writerow(["openalex_id", "doi", "title", "authors", "journal",
                    "year", "quartile", "citations", "open_access", "pdf_url", "abstract"])
        for k in corpus:
            w.writerow([
                k.get("id", ""),
                (k.get("doi") or "").replace("https://doi.org/", ""),
                k.get("title", ""),
                authors_str(k),
                journal_str(k),
                k.get("publication_year", ""),
                k.get("_quartile", ""),
                k.get("cited_by_count", ""),
                (k.get("open_access") or {}).get("is_oa", False),
                pdf_url(k) or "",
                rebuild_abstract(k.get("abstract_inverted_index")),
            ])

        col1, col2 = st.columns(2)
        with col1:
            st.download_button(
                "📄 Download CSV",
                csv_buf.getvalue(),
                "corpus_metadata.csv",
                "text/csv",
            )

        # BibTeX
        def esc(s):
            return (s or "").replace("{", "").replace("}", "").replace("&", "\\&")

        bib_lines = []
        for i, k in enumerate(corpus, 1):
            doi = (k.get("doi") or "").replace("https://doi.org/", "")
            bib_lines.append(
                f"@article{{ref{i:05d},\n"
                f"  title = {{{esc(k.get('title'))}}},\n"
                f"  author = {{{esc(authors_str(k))}}},\n"
                f"  journal = {{{esc(journal_str(k))}}},\n"
                f"  year = {{{k.get('publication_year','')}}},\n"
                f"  doi = {{{doi}}},\n"
                f"}}\n"
            )

        with col2:
            st.download_button(
                "📚 Download BibTeX",
                "\n".join(bib_lines),
                "corpus.bib",
                "text/plain",
            )

        st.markdown("---")
        st.markdown("**Note:** PDF bulk download is available in the CLI version. "
                    "Install from [GitHub](https://github.com) and run: "
                    "`python scholar_harvest.py --config config.yaml`")

    with tab3:
        st.subheader("Statistics")

        n_oa = sum(1 for k in corpus if (k.get("open_access") or {}).get("is_oa"))
        n_paid = len(corpus) - n_oa

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total Articles", len(corpus))
        col2.metric("Open Access", n_oa)
        col3.metric("Paywall", n_paid)
        col4.metric("With PDF URL", sum(1 for k in corpus if pdf_url(k)))

        # Year distribution
        years = {}
        for k in corpus:
            y = k.get("publication_year", 0)
            if y:
                years[y] = years.get(y, 0) + 1
        if years:
            import pandas as pd
            df = pd.DataFrame(sorted(years.items()), columns=["Year", "Articles"])
            st.bar_chart(df.set_index("Year"))

        # Quartile distribution
        quarts = {}
        for k in corpus:
            q = k.get("_quartile", "Unknown")
            quarts[q] = quarts.get(q, 0) + 1
        if quarts:
            st.markdown("**By Quartile:**")
            for q in sorted(quarts):
                st.markdown(f"- **{q}**: {quarts[q]} articles")


# Footer
st.markdown("---")
st.markdown(
    "Built with [OpenAlex](https://openalex.org) (CC0 data) | "
    "[GitHub](https://github.com) | "
    "Made for scientists 🔬"
)

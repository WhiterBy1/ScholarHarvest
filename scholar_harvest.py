#!/usr/bin/env python3
"""
ScholarHarvest — Scientific Literature Harvester
=================================================
Search, filter, and download scientific papers from OpenAlex.

Features:
  - Search by topic with multiple queries
  - Filter by Scimago journal quartile (Q1/Q2/Q3/Q4)
  - Download Open Access PDFs in parallel
  - Resume interrupted downloads
  - Validate PDF integrity
  - Export CSV + BibTeX
  - Shows API budget and limitations transparently

Data source: OpenAlex (https://openalex.org) — CC0 metadata, legal.
PDFs: Only Open Access articles (legal downloads).

Usage:
  python scholar_harvest.py --config config.yaml
  python scholar_harvest.py --email you@uni.edu --queries "topic1" "topic2"

API Limitations (displayed at startup):
  - Free tier: ~50,000 API calls/day (resets midnight UTC)
  - Each query with top_per_query=100 uses 1 API call
  - Polite pool (faster): requires valid email
  - Rate limit: ~10 req/s with email, ~1 req/s without
"""

import argparse
import csv
import json
import os
import random
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

__version__ = "1.0.0"

# ============================================================================
# COLORS for terminal output
# ============================================================================
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    DIM = "\033[2m"

    @staticmethod
    def disable():
        for attr in ("RESET", "BOLD", "RED", "GREEN", "YELLOW", "BLUE", "CYAN", "DIM"):
            setattr(C, attr, "")


if sys.platform == "win32":
    os.system("")  # enable ANSI on Windows


# ============================================================================
# API LIMITS INFO
# ============================================================================
LIMITS_TEXT = f"""
{C.BOLD}{C.CYAN}============================================================
  ScholarHarvest v{__version__} — API Limitations
============================================================{C.RESET}

  {C.BOLD}OpenAlex API (data source):{C.RESET}
    - Free tier: ~50,000 calls/day (resets midnight UTC)
    - 1 query (top 100 results) = 1 API call
    - With email (polite pool): ~10 req/s
    - Without email: ~1 req/s, more 429 errors
    - Budget: $0.001 per call, daily free allowance

  {C.BOLD}PDF Downloads:{C.RESET}
    - Only Open Access articles (legal)
    - ~50% of OA articles have direct PDF links
    - Some links redirect to HTML (not real PDFs)
    - Corrupt PDFs are auto-detected and retried
    - Paid articles listed separately (use institutional access)

  {C.BOLD}Scimago Quartiles:{C.RESET}
    - Download CSV from: https://www.scimagojr.com/journalrank.php
    - Click "Download data" (free, no account needed)
    - Without it: no quartile filtering applied

  {C.BOLD}Tips for large harvests:{C.RESET}
    - Use specific queries (fewer results = fewer API calls)
    - top_per_query=100 uses only 1 call per query
    - Split across multiple days if budget runs out
    - Progress is saved — re-run to resume

{C.CYAN}============================================================{C.RESET}
"""

# ============================================================================
# OpenAlex engine
# ============================================================================
OPENALEX = "https://api.openalex.org/works"
PROGRESO_FILE = Path("_progreso.json")
_lock = Lock()


def check_api_budget(email):
    """Check remaining API budget before starting."""
    try:
        r = requests.get(OPENALEX, params={
            "search": "test", "per-page": 1, "mailto": email
        }, timeout=15)
        if r.status_code == 429:
            body = r.json()
            msg = body.get("message", "")
            print(f"\n  {C.RED}{C.BOLD}API BUDGET EXHAUSTED{C.RESET}")
            print(f"  {C.YELLOW}{msg}{C.RESET}")
            print(f"  Resets at midnight UTC (~7PM Colombia, ~6PM EST)")
            return False
        elif r.status_code == 200:
            print(f"  {C.GREEN}API available{C.RESET}")
            return True
        else:
            print(f"  {C.YELLOW}API returned {r.status_code} — proceeding anyway{C.RESET}")
            return True
    except Exception as e:
        print(f"  {C.RED}Cannot reach API: {e}{C.RESET}")
        return False


def load_scimago(path):
    lookup = {}
    if not path or not Path(path).exists():
        print(f"  {C.YELLOW}Scimago CSV not found: '{path}'{C.RESET}")
        print(f"  {C.DIM}Download from https://www.scimagojr.com/journalrank.php{C.RESET}")
        return lookup
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f, delimiter=";")
            col_issn = next((c for c in reader.fieldnames if c.strip().lower() == "issn"), None)
            col_q = next((c for c in reader.fieldnames if "best quartile" in c.strip().lower()), None)
            if not col_issn or not col_q:
                print(f"  {C.RED}Scimago columns not recognized: {reader.fieldnames}{C.RESET}")
                return lookup
            for row in reader:
                q = (row.get(col_q) or "").strip()
                if q not in {"Q1", "Q2", "Q3", "Q4"}:
                    continue
                for issn in (row.get(col_issn) or "").split(","):
                    issn = re.sub(r"[^0-9Xx]", "", issn).upper()
                    if len(issn) == 8:
                        if issn not in lookup or q < lookup[issn]:
                            lookup[issn] = q
        print(f"  {C.GREEN}Scimago loaded: {len(lookup)} ISSNs{C.RESET}")
    except Exception as e:
        print(f"  {C.RED}Error reading Scimago: {e}{C.RESET}")
    return lookup


def norm_issn(issn):
    return re.sub(r"[^0-9Xx]", "", issn or "").upper()


def get_quartile(work, scimago):
    src = (work.get("primary_location") or {}).get("source") or {}
    issns = []
    if src.get("issn_l"):
        issns.append(src["issn_l"])
    if src.get("issn"):
        issns.extend(src["issn"])
    best = None
    for issn in issns:
        q = scimago.get(norm_issn(issn))
        if q and (best is None or q < best):
            best = q
    return best


def rebuild_abstract(inv):
    if not inv:
        return ""
    try:
        positions = [(i, w) for w, idxs in inv.items() for i in idxs]
        positions.sort()
        return " ".join(w for _, w in positions)
    except Exception:
        return ""


def search_top(query, seen, scimago, quartiles_ok, email, top_n,
               year_from, year_to, articles_only=True):
    filters = [f"from_publication_date:{year_from}-01-01",
               f"to_publication_date:{year_to}-12-31",
               "is_paratext:false"]
    if articles_only:
        filters.append("type:article")

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

    time.sleep(3 + random.uniform(0, 2))

    for attempt in range(6):
        try:
            r = requests.get(OPENALEX, params=params, timeout=90)
            if r.status_code == 429:
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                msg = body.get("message", "Rate limited")
                if "budget" in msg.lower() or "funds" in msg.lower():
                    print(f"\n  {C.RED}DAILY BUDGET EXHAUSTED — resets at midnight UTC{C.RESET}")
                    print(f"  {C.DIM}{msg}{C.RESET}")
                    return None  # signal to stop
                wait = min(15 * (2 ** attempt), 300) + random.uniform(2, 8)
                print(f"  {C.DIM}429 — wait {wait:.0f}s (attempt {attempt+1}){C.RESET}")
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            results = []
            filter_quartile = len(scimago) > 0 and quartiles_ok
            for w in data.get("results", []):
                wid = w.get("id")
                if not wid or wid in seen:
                    continue
                if filter_quartile:
                    q = get_quartile(w, scimago)
                    if q not in quartiles_ok:
                        continue
                    w["_quartile"] = q
                else:
                    w["_quartile"] = "NA"
                seen.add(wid)
                results.append(w)
            return results
        except Exception as e:
            if attempt < 5:
                time.sleep(5 * (attempt + 1))
            else:
                print(f"  {C.RED}ERROR: {e}{C.RESET}")
                return []
    return []


# ============================================================================
# Export helpers
# ============================================================================
def authors_str(work):
    try:
        return "; ".join(
            (a.get("author") or {}).get("display_name", "")
            for a in work.get("authorships", [])
            if (a.get("author") or {}).get("display_name")
        )
    except Exception:
        return ""


def journal_str(work):
    try:
        return ((work.get("primary_location") or {}).get("source") or {}).get("display_name", "")
    except Exception:
        return ""


def pdf_url(work):
    try:
        loc = work.get("best_oa_location") or {}
        return loc.get("pdf_url") or (work.get("open_access") or {}).get("oa_url")
    except Exception:
        return None


def save_csv(corpus, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["openalex_id", "doi", "title", "authors", "journal",
                    "year", "quartile", "citations", "open_access",
                    "pdf_url", "abstract"])
        for k in corpus:
            try:
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
            except Exception:
                continue


def save_bibtex(corpus, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    def esc(s):
        return (s or "").replace("{", "").replace("}", "").replace("&", "\\&")
    with open(path, "w", encoding="utf-8") as f:
        for i, k in enumerate(corpus, 1):
            doi = (k.get("doi") or "").replace("https://doi.org/", "")
            f.write(
                f"@article{{ref{i:05d},\n"
                f"  title = {{{esc(k.get('title'))}}},\n"
                f"  author = {{{esc(authors_str(k))}}},\n"
                f"  journal = {{{esc(journal_str(k))}}},\n"
                f"  year = {{{k.get('publication_year','')}}},\n"
                f"  doi = {{{doi}}},\n"
                f"  note = {{cited: {k.get('cited_by_count', 0)}}},\n"
                f"}}\n"
            )


def save_paywall_list(corpus, path):
    closed = [k for k in corpus if not (k.get("open_access") or {}).get("is_oa")]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["doi", "title", "journal", "year", "quartile", "citations"])
        for k in closed:
            w.writerow([
                (k.get("doi") or "").replace("https://doi.org/", ""),
                k.get("title", ""), journal_str(k),
                k.get("publication_year", ""), k.get("_quartile", ""),
                k.get("cited_by_count", ""),
            ])
    return len(closed)


# ============================================================================
# PDF downloads
# ============================================================================
def is_valid_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


def download_pdf(session, url, dest):
    if dest.exists() and is_valid_pdf(dest):
        return "exists"
    for attempt in range(3):
        try:
            r = session.get(url, timeout=120, stream=True)
            r.raise_for_status()
            ctype = r.headers.get("Content-Type", "")
            if "pdf" not in ctype.lower() and not url.lower().endswith(".pdf"):
                return "not_pdf"
            tmp = dest.with_suffix(".tmp")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
            if tmp.stat().st_size > 1024 and is_valid_pdf(tmp):
                tmp.replace(dest)
                return "ok"
            else:
                tmp.unlink(missing_ok=True)
                return "invalid"
        except Exception:
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    return "error"


def download_pdfs_parallel(corpus, pdf_dir, threads=6):
    pdf_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": "ScholarHarvest/1.0"})

    jobs = []
    already = 0
    for w in corpus:
        url = pdf_url(w)
        if not url:
            continue
        doi = (w.get("doi") or w.get("id", "")).split("/")[-1]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)[:80]
        dest = pdf_dir / f"{safe}.pdf"
        if dest.exists() and is_valid_pdf(dest):
            already += 1
            continue
        jobs.append((url, dest))

    if not jobs:
        print(f"  {C.GREEN}All PDFs already downloaded ({already} files){C.RESET}")
        return already, 0, 0

    print(f"  Already downloaded: {already}")
    print(f"  Queued: {len(jobs)}")
    print(f"  Threads: {threads}")
    print()

    ok = errors = 0
    with ThreadPoolExecutor(max_workers=threads) as pool:
        futures = {pool.submit(download_pdf, session, url, dest): i
                   for i, (url, dest) in enumerate(jobs)}
        for fut in as_completed(futures):
            idx = futures[fut]
            result = fut.result()
            if result == "ok":
                ok += 1
            elif result in ("error", "not_pdf", "invalid"):
                errors += 1
            done = idx + 1
            if done % 50 == 0 or done == len(jobs):
                pct = done * 100 // len(jobs)
                bar = "=" * (pct // 2) + "-" * (50 - pct // 2)
                print(f"\r  [{bar}] {pct}% | {ok} downloaded, {errors} failed", end="", flush=True)

    print()
    return already, ok, errors


# ============================================================================
# Progress management
# ============================================================================
def load_progress(path):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"completed_queries": [], "seen_ids": []}


def save_progress(prog, path):
    with _lock:
        try:
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(prog, f, ensure_ascii=False)
            tmp.replace(path)
        except Exception:
            pass


# ============================================================================
# MAIN
# ============================================================================
def harvest(config):
    email = config["email"]
    queries = config["queries"]
    scimago_path = config.get("scimago_csv", "")
    quartiles_ok = set(config.get("quartiles", ["Q1", "Q2"]))
    year_from = config.get("year_from", 2000)
    year_to = config.get("year_to", 2026)
    top_n = config.get("top_per_query", 100)
    do_pdfs = config.get("download_pdfs", True)
    threads = config.get("pdf_threads", 6)
    out_dir = Path(config.get("output_dir", "output"))

    start = datetime.now()

    # Show limitations
    print(LIMITS_TEXT)

    # Check API
    print(f"{C.BOLD}Checking API status...{C.RESET}")
    if not check_api_budget(email):
        print(f"\n{C.RED}Cannot proceed — API budget exhausted.{C.RESET}")
        print(f"Re-run after midnight UTC or add funds at https://openalex.org/pricing")
        return

    # Load Scimago
    print(f"\n{C.BOLD}Loading Scimago quartiles...{C.RESET}")
    scimago = load_scimago(scimago_path)

    # Show plan
    api_calls = len(queries)
    print(f"\n{C.BOLD}{C.CYAN}{'='*60}{C.RESET}")
    print(f"{C.BOLD}  HARVEST PLAN{C.RESET}")
    print(f"{C.CYAN}{'='*60}{C.RESET}")
    print(f"  Queries:        {len(queries)}")
    print(f"  Top per query:  {top_n}")
    print(f"  API calls:      ~{api_calls} (of ~50,000 daily)")
    print(f"  Quartiles:      {', '.join(sorted(quartiles_ok)) if quartiles_ok else 'No filter'}")
    print(f"  Years:          {year_from}-{year_to}")
    print(f"  PDF threads:    {threads}")
    print(f"  Output:         {out_dir.resolve()}")
    print(f"{C.CYAN}{'='*60}{C.RESET}\n")

    # Load progress
    prog_file = out_dir / "_progreso.json"
    prog = load_progress(prog_file)
    completed = set(prog.get("completed_queries", []))
    seen = set(prog.get("seen_ids", []))

    corpus = []
    budget_exhausted = False

    # Harvest
    print(f"{C.BOLD}Harvesting...{C.RESET}\n")
    for i, query in enumerate(queries, 1):
        if query in completed:
            print(f"  {C.DIM}[{i}/{len(queries)}] {query} (cached){C.RESET}")
            continue

        results = search_top(query, seen, scimago, quartiles_ok, email,
                             top_n, year_from, year_to)

        if results is None:
            budget_exhausted = True
            print(f"\n{C.YELLOW}Stopping — daily budget exhausted. Re-run tomorrow.{C.RESET}")
            break

        corpus.extend(results)
        q_display = f"{C.BOLD}+{len(results)}{C.RESET}"
        print(f"  [{i}/{len(queries)}] {query}")
        print(f"           {C.GREEN}{q_display} new (total: {len(corpus)}){C.RESET}")

        completed.add(query)
        prog["completed_queries"] = list(completed)
        prog["seen_ids"] = list(seen)
        save_progress(prog, prog_file)

    # Sort by citations
    corpus.sort(key=lambda w: w.get("cited_by_count", 0), reverse=True)

    if not corpus:
        if budget_exhausted:
            print(f"\n{C.YELLOW}No results yet. Re-run when API budget resets.{C.RESET}")
        else:
            print(f"\n{C.YELLOW}No results found. Try broader queries.{C.RESET}")
        return

    # Export
    print(f"\n{C.BOLD}Exporting {len(corpus)} articles...{C.RESET}")

    csv_path = out_dir / "corpus_metadata.csv"
    save_csv(corpus, csv_path)
    print(f"  {C.GREEN}CSV   -> {csv_path}{C.RESET}")

    bib_path = out_dir / "corpus.bib"
    save_bibtex(corpus, bib_path)
    print(f"  {C.GREEN}BibTeX -> {bib_path}{C.RESET}")

    n_closed = save_paywall_list(corpus, out_dir / "paywall_articles.csv")
    print(f"  {C.GREEN}Paywall list -> {n_closed} articles{C.RESET}")

    # Top 10
    print(f"\n{C.BOLD}Top 10 most cited:{C.RESET}")
    for k in corpus[:10]:
        cites = k.get("cited_by_count", 0)
        q = k.get("_quartile", "?")
        title = (k.get("title") or "")[:75]
        print(f"  {C.CYAN}[{q}]{C.RESET} {cites:>5} cites | {title}")

    # PDFs
    if do_pdfs:
        print(f"\n{C.BOLD}Downloading PDFs...{C.RESET}")
        pdf_dir = out_dir / "pdfs"
        already, downloaded, failed = download_pdfs_parallel(corpus, pdf_dir, threads)
        total_pdfs = already + downloaded
    else:
        total_pdfs = 0
        downloaded = 0
        failed = 0

    # Summary
    end = datetime.now()
    duration = end - start

    print(f"\n{C.BOLD}{C.CYAN}{'='*60}{C.RESET}")
    print(f"{C.BOLD}  HARVEST COMPLETE{C.RESET}")
    print(f"{C.CYAN}{'='*60}{C.RESET}")
    print(f"  Duration:     {duration}")
    print(f"  Articles:     {len(corpus)}")
    print(f"  Open Access:  {len(corpus) - n_closed}")
    print(f"  Paywall:      {n_closed}")
    if do_pdfs:
        print(f"  PDFs:         {total_pdfs} ({downloaded} new)")
        if failed:
            print(f"  PDF errors:   {failed}")
    print(f"  Output:       {out_dir.resolve()}")
    if budget_exhausted:
        remaining = len(queries) - len(completed)
        print(f"\n  {C.YELLOW}Note: {remaining} queries remaining (budget exhausted)")
        print(f"  Re-run this same command tomorrow to continue.{C.RESET}")
    print(f"{C.CYAN}{'='*60}{C.RESET}")


def main():
    parser = argparse.ArgumentParser(
        description="ScholarHarvest — Scientific Literature Harvester",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scholar_harvest.py --config config.yaml
  python scholar_harvest.py --email you@uni.edu --queries "PPG vascular" "wearable blood flow"
  python scholar_harvest.py --email you@uni.edu --queries "AI drug discovery" --top 200 --quartiles Q1
        """
    )
    parser.add_argument("--config", help="Path to YAML config file")
    parser.add_argument("--email", help="Your institutional email (required)")
    parser.add_argument("--queries", nargs="+", help="Search queries")
    parser.add_argument("--scimago", help="Path to Scimago CSV")
    parser.add_argument("--quartiles", nargs="+", default=["Q1", "Q2"],
                        help="Accepted quartiles (default: Q1 Q2)")
    parser.add_argument("--year-from", type=int, default=2000)
    parser.add_argument("--year-to", type=int, default=2026)
    parser.add_argument("--top", type=int, default=100,
                        help="Results per query (default: 100)")
    parser.add_argument("--threads", type=int, default=6,
                        help="PDF download threads (default: 6)")
    parser.add_argument("--no-pdfs", action="store_true",
                        help="Skip PDF downloads")
    parser.add_argument("--output", default="output", help="Output directory")
    parser.add_argument("--limits", action="store_true",
                        help="Show API limitations and exit")

    args = parser.parse_args()

    if args.limits:
        print(LIMITS_TEXT)
        return

    config = {}

    if args.config:
        if not HAS_YAML:
            print(f"{C.RED}PyYAML not installed. Run: pip install pyyaml{C.RESET}")
            sys.exit(1)
        with open(args.config, encoding="utf-8") as f:
            config = yaml.safe_load(f)

    if args.email:
        config["email"] = args.email
    if args.queries:
        config["queries"] = args.queries
    if args.scimago:
        config["scimago_csv"] = args.scimago
    if args.quartiles:
        config["quartiles"] = args.quartiles
    config.setdefault("year_from", args.year_from)
    config.setdefault("year_to", args.year_to)
    config.setdefault("top_per_query", args.top)
    config.setdefault("pdf_threads", args.threads)
    config["download_pdfs"] = not args.no_pdfs
    config.setdefault("output_dir", args.output)

    if not config.get("email"):
        print(f"{C.RED}Email required. Use --email or set in config.yaml{C.RESET}")
        sys.exit(1)
    if not config.get("queries"):
        print(f"{C.RED}No queries. Use --queries or set in config.yaml{C.RESET}")
        sys.exit(1)

    try:
        harvest(config)
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Interrupted. Progress saved — re-run to continue.{C.RESET}")
    except Exception as e:
        print(f"\n{C.RED}Error: {e}{C.RESET}")
        traceback.print_exc()
        print(f"\n{C.YELLOW}Progress saved — re-run to continue.{C.RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()

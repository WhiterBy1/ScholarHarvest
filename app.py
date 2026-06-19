"""
ScholarHarvest — Desktop App
Standalone GUI for searching and downloading scientific papers.
"""

import csv
import json
import os
import random
import re
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk
import requests

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

__version__ = "1.0.0"
OPENALEX = "https://api.openalex.org/works"


# ============================================================================
# Engine (same logic, no external deps beyond requests)
# ============================================================================
class HarvestEngine:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"ScholarHarvest/{__version__}",
            "Accept-Encoding": "gzip",
        })
        self.scimago = {}
        self.stop_flag = False

    def load_scimago(self, path):
        self.scimago = {}
        if not path or not Path(path).exists():
            return 0
        with open(path, encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f, delimiter=";")
            col_issn = next((c for c in reader.fieldnames if c.strip().lower() == "issn"), None)
            col_q = next((c for c in reader.fieldnames if "best quartile" in c.strip().lower()), None)
            if not col_issn or not col_q:
                return -1
            for row in reader:
                q = (row.get(col_q) or "").strip()
                if q not in {"Q1", "Q2", "Q3", "Q4"}:
                    continue
                for issn in (row.get(col_issn) or "").split(","):
                    issn = re.sub(r"[^0-9Xx]", "", issn).upper()
                    if len(issn) == 8:
                        if issn not in self.scimago or q < self.scimago[issn]:
                            self.scimago[issn] = q
        return len(self.scimago)

    def get_quartile(self, work):
        src = (work.get("primary_location") or {}).get("source") or {}
        issns = []
        if src.get("issn_l"):
            issns.append(src["issn_l"])
        if src.get("issn"):
            issns.extend(src["issn"])
        best = None
        for issn in issns:
            ni = re.sub(r"[^0-9Xx]", "", issn or "").upper()
            q = self.scimago.get(ni)
            if q and (best is None or q < best):
                best = q
        return best

    def check_api(self, email):
        try:
            r = self.session.get(OPENALEX, params={
                "search": "test", "per-page": 1, "mailto": email
            }, timeout=15)
            if r.status_code == 429:
                return False, r.json().get("message", "Budget exhausted")
            return True, "OK"
        except Exception as e:
            return False, str(e)

    def search(self, query, seen, quartiles_ok, email, top_n, year_from, year_to):
        if self.stop_flag:
            return []

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

        time.sleep(3 + random.uniform(0, 2))

        for attempt in range(6):
            if self.stop_flag:
                return []
            try:
                r = self.session.get(OPENALEX, params=params, timeout=90)
                if r.status_code == 429:
                    body = r.json() if "json" in r.headers.get("content-type", "") else {}
                    msg = body.get("message", "")
                    if "budget" in msg.lower():
                        return None
                    wait = min(15 * (2 ** attempt), 300) + random.uniform(2, 8)
                    time.sleep(wait)
                    continue
                if r.status_code >= 500:
                    time.sleep(10 * (attempt + 1))
                    continue
                r.raise_for_status()
                data = r.json()
                results = []
                filter_q = len(self.scimago) > 0 and quartiles_ok
                for w in data.get("results", []):
                    wid = w.get("id")
                    if not wid or wid in seen:
                        continue
                    if filter_q:
                        q = self.get_quartile(w)
                        if q not in quartiles_ok:
                            continue
                        w["_quartile"] = q
                    else:
                        w["_quartile"] = "NA"
                    seen.add(wid)
                    results.append(w)
                return results
            except Exception:
                if attempt < 5:
                    time.sleep(5 * (attempt + 1))
                else:
                    return []
        return []

    @staticmethod
    def authors_str(work):
        try:
            return "; ".join(
                (a.get("author") or {}).get("display_name", "")
                for a in work.get("authorships", [])
                if (a.get("author") or {}).get("display_name")
            )
        except Exception:
            return ""

    @staticmethod
    def journal_str(work):
        try:
            return ((work.get("primary_location") or {}).get("source") or {}).get("display_name", "")
        except Exception:
            return ""

    @staticmethod
    def pdf_url(work):
        try:
            loc = work.get("best_oa_location") or {}
            return loc.get("pdf_url") or (work.get("open_access") or {}).get("oa_url")
        except Exception:
            return None

    @staticmethod
    def rebuild_abstract(inv):
        if not inv:
            return ""
        try:
            pos = [(i, w) for w, idxs in inv.items() for i in idxs]
            pos.sort()
            return " ".join(w for _, w in pos)
        except Exception:
            return ""

    def is_valid_pdf(self, path):
        try:
            with open(path, "rb") as f:
                return f.read(5) == b"%PDF-"
        except Exception:
            return False

    def download_pdf(self, url, dest):
        if dest.exists() and self.is_valid_pdf(dest):
            return "exists"
        for attempt in range(3):
            if self.stop_flag:
                return "stopped"
            try:
                r = self.session.get(url, timeout=120, stream=True)
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if "pdf" not in ctype.lower() and not url.lower().endswith(".pdf"):
                    return "not_pdf"
                tmp = dest.with_suffix(".tmp")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                if tmp.stat().st_size > 1024 and self.is_valid_pdf(tmp):
                    tmp.replace(dest)
                    return "ok"
                tmp.unlink(missing_ok=True)
                return "invalid"
            except Exception:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        return "error"


# ============================================================================
# GUI
# ============================================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title(f"ScholarHarvest v{__version__}")
        self.geometry("1000x750")
        self.minsize(800, 600)

        self.engine = HarvestEngine()
        self.corpus = []
        self.running = False

        self._build_ui()

    def _build_ui(self):
        # Main container
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # ---- Top: Config ----
        config_frame = ctk.CTkFrame(self)
        config_frame.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        config_frame.grid_columnconfigure(1, weight=1)

        # Email
        ctk.CTkLabel(config_frame, text="Email:").grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.email_var = ctk.StringVar(value="")
        ctk.CTkEntry(config_frame, textvariable=self.email_var, placeholder_text="you@university.edu",
                     width=300).grid(row=0, column=1, padx=5, pady=5, sticky="w")

        # Scimago
        ctk.CTkLabel(config_frame, text="Scimago CSV:").grid(row=0, column=2, padx=10, pady=5, sticky="w")
        self.scimago_var = ctk.StringVar(value="")
        ctk.CTkEntry(config_frame, textvariable=self.scimago_var, width=250,
                     placeholder_text="(optional)").grid(row=0, column=3, padx=5, pady=5, sticky="w")
        ctk.CTkButton(config_frame, text="Browse", width=70,
                      command=self._browse_scimago).grid(row=0, column=4, padx=5, pady=5)

        # Row 2: Year, Quartiles, Top N
        ctk.CTkLabel(config_frame, text="Years:").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        year_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        year_frame.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        self.year_from_var = ctk.StringVar(value="2000")
        self.year_to_var = ctk.StringVar(value="2026")
        ctk.CTkEntry(year_frame, textvariable=self.year_from_var, width=60).pack(side="left")
        ctk.CTkLabel(year_frame, text=" to ").pack(side="left")
        ctk.CTkEntry(year_frame, textvariable=self.year_to_var, width=60).pack(side="left")

        ctk.CTkLabel(config_frame, text="Quartiles:").grid(row=1, column=2, padx=10, pady=5, sticky="w")
        q_frame = ctk.CTkFrame(config_frame, fg_color="transparent")
        q_frame.grid(row=1, column=3, columnspan=2, padx=5, pady=5, sticky="w")
        self.q1_var = ctk.BooleanVar(value=True)
        self.q2_var = ctk.BooleanVar(value=True)
        self.q3_var = ctk.BooleanVar(value=False)
        self.q4_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(q_frame, text="Q1", variable=self.q1_var, width=50).pack(side="left", padx=3)
        ctk.CTkCheckBox(q_frame, text="Q2", variable=self.q2_var, width=50).pack(side="left", padx=3)
        ctk.CTkCheckBox(q_frame, text="Q3", variable=self.q3_var, width=50).pack(side="left", padx=3)
        ctk.CTkCheckBox(q_frame, text="Q4", variable=self.q4_var, width=50).pack(side="left", padx=3)

        # ---- Queries ----
        queries_frame = ctk.CTkFrame(self)
        queries_frame.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        queries_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(queries_frame, text="Search Queries (one per line):").grid(
            row=0, column=0, padx=10, pady=(5, 0), sticky="w")

        self.queries_text = ctk.CTkTextbox(queries_frame, height=100)
        self.queries_text.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        self.queries_text.insert("1.0", "photoplethysmography peripheral arterial disease\n"
                                        "PPG wearable sensor vascular detection\n"
                                        "wearable blood flow monitoring device")

        # Buttons
        btn_frame = ctk.CTkFrame(queries_frame, fg_color="transparent")
        btn_frame.grid(row=2, column=0, padx=10, pady=5, sticky="ew")

        self.search_btn = ctk.CTkButton(btn_frame, text="Search", command=self._start_search,
                                        fg_color="#2563eb", width=120, height=35)
        self.search_btn.pack(side="left", padx=5)

        self.download_btn = ctk.CTkButton(btn_frame, text="Download PDFs",
                                          command=self._start_download,
                                          fg_color="#16a34a", width=140, height=35, state="disabled")
        self.download_btn.pack(side="left", padx=5)

        self.export_btn = ctk.CTkButton(btn_frame, text="Export CSV + BibTeX",
                                        command=self._export,
                                        fg_color="#9333ea", width=160, height=35, state="disabled")
        self.export_btn.pack(side="left", padx=5)

        self.stop_btn = ctk.CTkButton(btn_frame, text="Stop", command=self._stop,
                                      fg_color="#dc2626", width=80, height=35, state="disabled")
        self.stop_btn.pack(side="left", padx=5)

        # API info label
        self.api_label = ctk.CTkLabel(btn_frame, text="API: ~50,000 calls/day | 1 query = 1 call",
                                      text_color="gray")
        self.api_label.pack(side="right", padx=10)

        # ---- Results ----
        results_frame = ctk.CTkFrame(self)
        results_frame.grid(row=2, column=0, padx=10, pady=5, sticky="nsew")
        results_frame.grid_columnconfigure(0, weight=1)
        results_frame.grid_rowconfigure(0, weight=1)

        self.results_text = ctk.CTkTextbox(results_frame, font=("Consolas", 12))
        self.results_text.grid(row=0, column=0, padx=5, pady=5, sticky="nsew")
        self.results_text.insert("1.0",
            "Welcome to ScholarHarvest!\n\n"
            "HOW TO USE:\n"
            "  1. Enter your institutional email\n"
            "  2. (Optional) Load Scimago CSV for quartile filtering\n"
            "  3. Write your search queries (one per line)\n"
            "  4. Click 'Search' to find articles\n"
            "  5. Click 'Download PDFs' to get Open Access papers\n"
            "  6. Click 'Export' to save CSV + BibTeX\n\n"
            "API LIMITATIONS:\n"
            "  - Free: ~50,000 API calls/day (resets midnight UTC)\n"
            "  - Each query uses 1 API call (top 100 results)\n"
            "  - Only Open Access PDFs are downloaded (legal)\n"
            "  - Paid articles listed separately for institutional access\n\n"
            "DATA SOURCE: OpenAlex (openalex.org) — CC0 metadata\n"
            "QUARTILES: Scimago (scimagojr.com) — download CSV free\n"
        )

        # ---- Bottom: Progress ----
        bottom_frame = ctk.CTkFrame(self)
        bottom_frame.grid(row=3, column=0, padx=10, pady=(0, 10), sticky="ew")
        bottom_frame.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(bottom_frame)
        self.progress.grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        self.progress.set(0)

        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(bottom_frame, textvariable=self.status_var).grid(
            row=1, column=0, padx=10, pady=(0, 5), sticky="w")

    def _browse_scimago(self):
        path = filedialog.askopenfilename(
            title="Select Scimago CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if path:
            self.scimago_var.set(path)

    def _log(self, msg):
        self.results_text.insert("end", msg + "\n")
        self.results_text.see("end")

    def _clear_log(self):
        self.results_text.delete("1.0", "end")

    def _get_quartiles(self):
        q = set()
        if self.q1_var.get(): q.add("Q1")
        if self.q2_var.get(): q.add("Q2")
        if self.q3_var.get(): q.add("Q3")
        if self.q4_var.get(): q.add("Q4")
        return q

    def _get_queries(self):
        text = self.queries_text.get("1.0", "end").strip()
        return [q.strip() for q in text.split("\n") if q.strip()]

    def _set_running(self, running):
        self.running = running
        state_normal = "normal" if not running else "disabled"
        state_stop = "normal" if running else "disabled"
        self.search_btn.configure(state=state_normal)
        self.stop_btn.configure(state=state_stop)
        if not running and self.corpus:
            self.download_btn.configure(state="normal")
            self.export_btn.configure(state="normal")

    def _stop(self):
        self.engine.stop_flag = True
        self.status_var.set("Stopping...")

    # ---- Search ----
    def _start_search(self):
        email = self.email_var.get().strip()
        if not email:
            messagebox.showerror("Error", "Email is required for API access.")
            return
        queries = self._get_queries()
        if not queries:
            messagebox.showerror("Error", "Enter at least one search query.")
            return

        self.engine.stop_flag = False
        self._set_running(True)
        threading.Thread(target=self._run_search, args=(email, queries), daemon=True).start()

    def _run_search(self, email, queries):
        self._clear_log()
        self._log(f"ScholarHarvest v{__version__}")
        self._log("=" * 55)

        # Check API
        self.status_var.set("Checking API...")
        ok, msg = self.engine.check_api(email)
        if not ok:
            self._log(f"\nAPI ERROR: {msg}")
            self._log("Budget resets at midnight UTC (~7PM Colombia)")
            self._set_running(False)
            return
        self._log(f"API: Available")

        # Load Scimago
        scimago_path = self.scimago_var.get().strip()
        if scimago_path:
            self.status_var.set("Loading Scimago...")
            n = self.engine.load_scimago(scimago_path)
            if n > 0:
                self._log(f"Scimago: {n} ISSNs loaded")
            elif n == -1:
                self._log("Scimago: Could not parse CSV columns")
            else:
                self._log("Scimago: File not found")
        else:
            self._log("Scimago: Not loaded (no quartile filter)")

        quartiles = self._get_quartiles()
        year_from = int(self.year_from_var.get())
        year_to = int(self.year_to_var.get())

        self._log(f"\nQuartiles: {', '.join(sorted(quartiles)) if quartiles else 'All'}")
        self._log(f"Years: {year_from}-{year_to}")
        self._log(f"Queries: {len(queries)} (~{len(queries)} API calls)")
        self._log("=" * 55 + "\n")

        seen = set()
        self.corpus = []

        for i, query in enumerate(queries):
            if self.engine.stop_flag:
                self._log("\nStopped by user.")
                break

            self.status_var.set(f"Searching {i+1}/{len(queries)}: {query[:40]}...")
            self.progress.set((i) / len(queries))

            results = self.engine.search(query, seen, quartiles, email,
                                         100, year_from, year_to)

            if results is None:
                self._log(f"\nBUDGET EXHAUSTED — resets midnight UTC")
                break

            self.corpus.extend(results)
            self._log(f"[{i+1}/{len(queries)}] {query}")
            self._log(f"         +{len(results)} new (total: {len(self.corpus)})")

        self.corpus.sort(key=lambda w: w.get("cited_by_count", 0), reverse=True)
        self.progress.set(1.0)

        # Summary
        n_oa = sum(1 for k in self.corpus if (k.get("open_access") or {}).get("is_oa"))
        n_pdf = sum(1 for k in self.corpus if self.engine.pdf_url(k))

        self._log(f"\n{'='*55}")
        self._log(f"RESULTS: {len(self.corpus)} articles")
        self._log(f"  Open Access:    {n_oa}")
        self._log(f"  Paywall:        {len(self.corpus) - n_oa}")
        self._log(f"  With PDF link:  {n_pdf}")
        self._log(f"{'='*55}")

        if self.corpus:
            self._log(f"\nTop 10 most cited:")
            for k in self.corpus[:10]:
                c = k.get("cited_by_count", 0)
                q = k.get("_quartile", "?")
                t = (k.get("title") or "")[:70]
                self._log(f"  [{q}] {c:>5} cites | {t}")

        self.status_var.set(f"Done: {len(self.corpus)} articles found")
        self._set_running(False)

    # ---- Download PDFs ----
    def _start_download(self):
        if not self.corpus:
            return
        out_dir = filedialog.askdirectory(title="Select folder for PDFs")
        if not out_dir:
            return
        self.engine.stop_flag = False
        self._set_running(True)
        threading.Thread(target=self._run_download, args=(Path(out_dir),), daemon=True).start()

    def _run_download(self, pdf_dir):
        pdf_dir.mkdir(parents=True, exist_ok=True)

        jobs = []
        already = 0
        for w in self.corpus:
            url = self.engine.pdf_url(w)
            if not url:
                continue
            doi = (w.get("doi") or w.get("id", "")).split("/")[-1]
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)[:80]
            dest = pdf_dir / f"{safe}.pdf"
            if dest.exists() and self.engine.is_valid_pdf(dest):
                already += 1
                continue
            jobs.append((url, dest))

        self._log(f"\nPDF DOWNLOAD")
        self._log(f"  Already downloaded: {already}")
        self._log(f"  Queued: {len(jobs)}")
        self._log(f"  Destination: {pdf_dir}")

        if not jobs:
            self._log("  Nothing to download!")
            self._set_running(False)
            return

        ok = errors = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self.engine.download_pdf, url, dest): i
                       for i, (url, dest) in enumerate(jobs)}
            for fut in as_completed(futures):
                if self.engine.stop_flag:
                    break
                idx = futures[fut]
                result = fut.result()
                if result == "ok":
                    ok += 1
                elif result in ("error", "not_pdf", "invalid"):
                    errors += 1
                done = idx + 1
                self.progress.set(done / len(jobs))
                self.status_var.set(f"PDFs: {done}/{len(jobs)} | OK: {ok} | Errors: {errors}")

        self._log(f"\n  Downloaded: {ok}")
        self._log(f"  Failed: {errors}")
        self._log(f"  Total in folder: {already + ok}")
        self.status_var.set(f"PDFs done: {already + ok} total")
        self._set_running(False)

    # ---- Export ----
    def _export(self):
        if not self.corpus:
            return
        out_dir = filedialog.askdirectory(title="Select export folder")
        if not out_dir:
            return
        out = Path(out_dir)

        # CSV
        csv_path = out / "corpus_metadata.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["openalex_id", "doi", "title", "authors", "journal",
                        "year", "quartile", "citations", "open_access", "pdf_url", "abstract"])
            for k in self.corpus:
                try:
                    w.writerow([
                        k.get("id", ""),
                        (k.get("doi") or "").replace("https://doi.org/", ""),
                        k.get("title", ""),
                        self.engine.authors_str(k),
                        self.engine.journal_str(k),
                        k.get("publication_year", ""),
                        k.get("_quartile", ""),
                        k.get("cited_by_count", ""),
                        (k.get("open_access") or {}).get("is_oa", False),
                        self.engine.pdf_url(k) or "",
                        self.engine.rebuild_abstract(k.get("abstract_inverted_index")),
                    ])
                except Exception:
                    continue

        # BibTeX
        bib_path = out / "corpus.bib"
        with open(bib_path, "w", encoding="utf-8") as f:
            def esc(s):
                return (s or "").replace("{", "").replace("}", "").replace("&", "\\&")
            for i, k in enumerate(self.corpus, 1):
                doi = (k.get("doi") or "").replace("https://doi.org/", "")
                f.write(
                    f"@article{{ref{i:05d},\n"
                    f"  title = {{{esc(k.get('title'))}}},\n"
                    f"  author = {{{esc(self.engine.authors_str(k))}}},\n"
                    f"  journal = {{{esc(self.engine.journal_str(k))}}},\n"
                    f"  year = {{{k.get('publication_year','')}}},\n"
                    f"  doi = {{{doi}}},\n"
                    f"}}\n"
                )

        # Paywall list
        closed = [k for k in self.corpus if not (k.get("open_access") or {}).get("is_oa")]
        pay_path = out / "paywall_articles.csv"
        with open(pay_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["doi", "title", "journal", "year", "quartile", "citations"])
            for k in closed:
                w.writerow([
                    (k.get("doi") or "").replace("https://doi.org/", ""),
                    k.get("title", ""), self.engine.journal_str(k),
                    k.get("publication_year", ""), k.get("_quartile", ""),
                    k.get("cited_by_count", ""),
                ])

        self._log(f"\nEXPORTED to {out}")
        self._log(f"  CSV:     {csv_path.name} ({len(self.corpus)} articles)")
        self._log(f"  BibTeX:  {bib_path.name}")
        self._log(f"  Paywall: {pay_path.name} ({len(closed)} articles)")
        self.status_var.set(f"Exported to {out}")
        messagebox.showinfo("Export Complete",
                           f"Saved {len(self.corpus)} articles to:\n{out}")


if __name__ == "__main__":
    app = App()
    app.mainloop()

"""
ScholarHarvest v2 — Desktop App
Full GUI with detailed PDF diagnostics and pre-filtering.
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
from tkinter import ttk, filedialog, messagebox
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
import requests

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

__version__ = "3.1.0"
OPENALEX = "https://api.openalex.org/works"
OPENALEX_AUTHORS = "https://api.openalex.org/authors"


# ============================================================================
# Engine
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
                body = r.json()
                return False, body.get("message", "Budget exhausted — resets midnight UTC")
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
                    if "budget" in body.get("message", "").lower():
                        return None
                    time.sleep(min(15 * (2 ** attempt), 300) + random.uniform(2, 8))
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

    def verify_pdf_url(self, url):
        """HEAD request to check if URL actually serves a PDF."""
        try:
            r = self.session.head(url, timeout=20, allow_redirects=True)
            ctype = r.headers.get("Content-Type", "").lower()
            clength = r.headers.get("Content-Length", "")
            final_url = r.url
            size_kb = int(clength) // 1024 if clength.isdigit() else None

            if r.status_code == 403:
                return {"downloadable": False, "reason": "Forbidden (403) — paywall",
                        "http": 403, "content_type": ctype, "size_kb": size_kb}
            if r.status_code == 404:
                return {"downloadable": False, "reason": "Not found (404)",
                        "http": 404, "content_type": ctype, "size_kb": size_kb}
            if r.status_code >= 400:
                return {"downloadable": False, "reason": f"HTTP {r.status_code}",
                        "http": r.status_code, "content_type": ctype, "size_kb": size_kb}

            if "pdf" in ctype:
                return {"downloadable": True, "reason": "PDF confirmed",
                        "http": r.status_code, "content_type": ctype, "size_kb": size_kb}
            if "html" in ctype:
                return {"downloadable": False, "reason": "HTML page (paywall/landing)",
                        "http": r.status_code, "content_type": ctype, "size_kb": size_kb}
            if "octet-stream" in ctype and url.lower().endswith(".pdf"):
                return {"downloadable": True, "reason": "Binary stream (.pdf URL)",
                        "http": r.status_code, "content_type": ctype, "size_kb": size_kb}

            return {"downloadable": False, "reason": f"Not PDF ({ctype[:40]})",
                    "http": r.status_code, "content_type": ctype, "size_kb": size_kb}
        except requests.Timeout:
            return {"downloadable": False, "reason": "Timeout", "http": 0, "content_type": "", "size_kb": None}
        except Exception as e:
            return {"downloadable": False, "reason": f"Error: {str(e)[:40]}",
                    "http": 0, "content_type": "", "size_kb": None}

    def download_pdf(self, url, dest):
        if dest.exists() and self._is_valid_pdf(dest):
            return {"status": "exists", "size_kb": dest.stat().st_size // 1024}
        for attempt in range(3):
            if self.stop_flag:
                return {"status": "stopped", "size_kb": None}
            try:
                r = self.session.get(url, timeout=120, stream=True)
                r.raise_for_status()
                ctype = r.headers.get("Content-Type", "")
                if "pdf" not in ctype.lower() and not url.lower().endswith(".pdf"):
                    return {"status": "not_pdf", "size_kb": None,
                            "detail": f"Content-Type: {ctype[:50]}"}
                tmp = dest.with_suffix(".tmp")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                size = tmp.stat().st_size
                if size > 1024 and self._is_valid_pdf(tmp):
                    tmp.replace(dest)
                    return {"status": "ok", "size_kb": size // 1024}
                else:
                    tmp.unlink(missing_ok=True)
                    return {"status": "invalid", "size_kb": size // 1024,
                            "detail": "No %PDF- header" if size > 1024 else f"Too small ({size}B)"}
            except requests.HTTPError as e:
                return {"status": "http_error", "size_kb": None,
                        "detail": f"HTTP {e.response.status_code}" if e.response else str(e)}
            except requests.Timeout:
                if attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                return {"status": "timeout", "size_kb": None}
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                return {"status": "error", "size_kb": None, "detail": str(e)[:60]}
        return {"status": "error", "size_kb": None}

    def fetch_author_details(self, author_id, email):
        """Fetch h-index, works_count, cited_by_count from OpenAlex /authors endpoint."""
        try:
            time.sleep(0.3 + random.uniform(0, 0.3))
            r = self.session.get(author_id, params={"mailto": email,
                "select": "id,display_name,summary_stats,works_count,cited_by_count"}, timeout=20)
            if r.status_code == 429:
                return None
            if r.status_code != 200:
                return {"h_index": "?", "works": "?", "global_cited": "?"}
            data = r.json()
            stats = data.get("summary_stats") or {}
            return {
                "h_index": stats.get("h_index", "?"),
                "works": data.get("works_count", "?"),
                "global_cited": data.get("cited_by_count", "?"),
            }
        except Exception:
            return {"h_index": "?", "works": "?", "global_cited": "?"}

    def analyze_authors(self, corpus):
        """Analyze authors from corpus data (no API calls)."""
        authors = {}
        for w in corpus:
            cites = w.get("cited_by_count", 0)
            quartile = w.get("_quartile", "")
            for a in w.get("authorships", []):
                author = a.get("author") or {}
                aid = author.get("id", "")
                name = author.get("display_name", "")
                if not name:
                    continue
                if aid not in authors:
                    authors[aid] = {
                        "id": aid, "name": name,
                        "papers": 0, "total_cites": 0,
                        "q1_papers": 0, "q2_papers": 0,
                        "journals": set(), "years": [],
                        "best_quartile": "Q4",
                        "h_index": None, "works": None, "global_cited": None,
                    }
                rec = authors[aid]
                rec["papers"] += 1
                rec["total_cites"] += cites
                if quartile == "Q1":
                    rec["q1_papers"] += 1
                if quartile == "Q2":
                    rec["q2_papers"] += 1
                if quartile and quartile < rec["best_quartile"]:
                    rec["best_quartile"] = quartile
                journal = self.journal_str(w)
                if journal:
                    rec["journals"].add(journal)
                y = w.get("publication_year")
                if y:
                    rec["years"].append(y)

        result = []
        for rec in authors.values():
            rec["avg_cites"] = round(rec["total_cites"] / rec["papers"], 1) if rec["papers"] else 0
            rec["journals_count"] = len(rec["journals"])
            rec["year_range"] = f"{min(rec['years'])}-{max(rec['years'])}" if rec["years"] else ""
            # Score: papers in corpus * 2 + avg_cites + Q1 papers * 5 + Q2 papers * 2
            rec["score"] = rec["papers"] * 2 + rec["avg_cites"] + rec["q1_papers"] * 5 + rec["q2_papers"] * 2
            result.append(rec)

        result.sort(key=lambda x: x["score"], reverse=True)
        return result

    @staticmethod
    def _is_valid_pdf(path):
        try:
            with open(path, "rb") as f:
                return f.read(5) == b"%PDF-"
        except Exception:
            return False

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


# ============================================================================
# Treeview style helper
# ============================================================================
def setup_treeview_style():
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Custom.Treeview",
                     background="#2b2b2b", foreground="white",
                     fieldbackground="#2b2b2b", rowheight=24,
                     font=("Segoe UI", 10))
    style.configure("Custom.Treeview.Heading",
                     background="#1f538d", foreground="white",
                     font=("Segoe UI", 10, "bold"))
    style.map("Custom.Treeview",
              background=[("selected", "#1f538d")],
              foreground=[("selected", "white")])


def make_sortable(tree):
    """Make all columns in a Treeview sortable by clicking the header."""
    def _sort(col, reverse):
        data = [(tree.set(iid, col), iid) for iid in tree.get_children("")]
        try:
            data.sort(key=lambda x: float(x[0]) if x[0].replace(".", "", 1).replace("-", "", 1).isdigit() else float("inf"),
                      reverse=reverse)
        except Exception:
            data.sort(key=lambda x: x[0].lower(), reverse=reverse)
        for i, (_, iid) in enumerate(data):
            tree.move(iid, "", i)
        arrow = " v" if reverse else " ^"
        for c in tree["columns"]:
            text = tree.heading(c, "text").rstrip(" ^").rstrip(" v")
            tree.heading(c, text=text)
        text = tree.heading(col, "text").rstrip(" ^").rstrip(" v")
        tree.heading(col, text=text + arrow, command=lambda: _sort(col, not reverse))

    for col in tree["columns"]:
        tree.heading(col, command=lambda c=col: _sort(c, False))


# ============================================================================
# GUI
# ============================================================================
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"ScholarHarvest v{__version__}")
        self.geometry("1100x800")
        self.minsize(900, 650)

        self.engine = HarvestEngine()
        self.corpus = []
        self.running = False
        self.pdf_jobs = []

        setup_treeview_style()
        self._build_ui()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # ---- Top: Config ----
        config_frame = ctk.CTkFrame(self)
        config_frame.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="ew")
        config_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(config_frame, text="Email:").grid(row=0, column=0, padx=10, pady=5, sticky="w")
        self.email_var = ctk.StringVar()
        ctk.CTkEntry(config_frame, textvariable=self.email_var,
                     placeholder_text="you@university.edu", width=280).grid(row=0, column=1, padx=5, pady=5, sticky="w")

        ctk.CTkLabel(config_frame, text="Scimago:").grid(row=0, column=2, padx=10, pady=5, sticky="w")
        self.scimago_var = ctk.StringVar()
        ctk.CTkEntry(config_frame, textvariable=self.scimago_var, width=220,
                     placeholder_text="(optional)").grid(row=0, column=3, padx=5, pady=5, sticky="w")
        ctk.CTkButton(config_frame, text="...", width=35,
                      command=self._browse_scimago).grid(row=0, column=4, padx=5, pady=5)

        # Row 2
        ctk.CTkLabel(config_frame, text="Years:").grid(row=1, column=0, padx=10, pady=5, sticky="w")
        yf = ctk.CTkFrame(config_frame, fg_color="transparent")
        yf.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        self.year_from_var = ctk.StringVar(value="2000")
        self.year_to_var = ctk.StringVar(value="2026")
        ctk.CTkEntry(yf, textvariable=self.year_from_var, width=55).pack(side="left")
        ctk.CTkLabel(yf, text=" — ").pack(side="left")
        ctk.CTkEntry(yf, textvariable=self.year_to_var, width=55).pack(side="left")

        ctk.CTkLabel(config_frame, text="Quartiles:").grid(row=1, column=2, padx=10, pady=5, sticky="w")
        qf = ctk.CTkFrame(config_frame, fg_color="transparent")
        qf.grid(row=1, column=3, columnspan=2, padx=5, pady=5, sticky="w")
        self.q_vars = {}
        for q in ("Q1", "Q2", "Q3", "Q4"):
            v = ctk.BooleanVar(value=q in ("Q1", "Q2"))
            self.q_vars[q] = v
            ctk.CTkCheckBox(qf, text=q, variable=v, width=50).pack(side="left", padx=4)

        # ---- Tabs ----
        self.tabs = ctk.CTkTabview(self)
        self.tabs.grid(row=1, column=0, padx=10, pady=5, sticky="nsew")

        self.tab_search = self.tabs.add("Search")
        self.tab_results = self.tabs.add("Results")
        self.tab_authors = self.tabs.add("Authors")
        self.tab_downloads = self.tabs.add("Downloads")

        self._build_search_tab()
        self._build_results_tab()
        self._build_authors_tab()
        self._build_downloads_tab()

        # ---- Bottom ----
        bot = ctk.CTkFrame(self)
        bot.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")
        bot.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(bot)
        self.progress.grid(row=0, column=0, padx=10, pady=5, sticky="ew")
        self.progress.set(0)

        sf = ctk.CTkFrame(bot, fg_color="transparent")
        sf.grid(row=1, column=0, padx=10, pady=(0, 5), sticky="ew")
        sf.grid_columnconfigure(0, weight=1)
        self.status_var = ctk.StringVar(value="Ready")
        ctk.CTkLabel(sf, textvariable=self.status_var, anchor="w").grid(row=0, column=0, sticky="w")
        self.api_label = ctk.CTkLabel(sf, text="API: ~50,000 calls/day | Resets midnight UTC",
                                      text_color="gray", anchor="e")
        self.api_label.grid(row=0, column=1, sticky="e")

    # ---- Search Tab ----
    def _build_search_tab(self):
        t = self.tab_search
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(t, text="Search Queries (one per line):",
                     font=("Segoe UI", 13, "bold")).grid(row=0, column=0, padx=10, pady=(5, 0), sticky="w")

        self.queries_text = ctk.CTkTextbox(t, font=("Consolas", 12))
        self.queries_text.grid(row=1, column=0, padx=10, pady=5, sticky="nsew")
        self.queries_text.insert("1.0",
            "photoplethysmography peripheral arterial disease\n"
            "PPG wearable sensor vascular detection\n"
            "wearable blood flow monitoring device")

        bf = ctk.CTkFrame(t, fg_color="transparent")
        bf.grid(row=2, column=0, padx=10, pady=5, sticky="ew")

        self.search_btn = ctk.CTkButton(bf, text="Search OpenAlex", command=self._start_search,
                                        fg_color="#2563eb", width=150, height=38,
                                        font=("Segoe UI", 13, "bold"))
        self.search_btn.pack(side="left", padx=5)

        self.stop_btn = ctk.CTkButton(bf, text="Stop", command=self._stop,
                                      fg_color="#dc2626", width=80, height=38, state="disabled")
        self.stop_btn.pack(side="left", padx=5)

        self.search_info = ctk.CTkLabel(bf, text="", text_color="gray")
        self.search_info.pack(side="right", padx=10)

    # ---- Results Tab ----
    def _build_results_tab(self):
        t = self.tab_results
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(1, weight=1)

        # Stats bar
        self.stats_frame = ctk.CTkFrame(t)
        self.stats_frame.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        self.stat_labels = {}
        for i, (key, label) in enumerate([
            ("total", "Total"), ("oa", "Open Access"), ("paywall", "Paywall"),
            ("with_pdf", "PDF links"), ("q1", "Q1"), ("q2", "Q2")
        ]):
            ctk.CTkLabel(self.stats_frame, text=f"{label}:", font=("Segoe UI", 11)).grid(
                row=0, column=i*2, padx=(10 if i == 0 else 5, 2), pady=5)
            lbl = ctk.CTkLabel(self.stats_frame, text="0", font=("Segoe UI", 12, "bold"),
                               text_color="#60a5fa")
            lbl.grid(row=0, column=i*2+1, padx=(0, 10), pady=5)
            self.stat_labels[key] = lbl

        # Treeview
        cols = ("title", "journal", "year", "quartile", "citations", "access", "pdf")
        self.results_tree = ttk.Treeview(t, columns=cols, show="headings", style="Custom.Treeview")
        self.results_tree.heading("title", text="Title")
        self.results_tree.heading("journal", text="Journal")
        self.results_tree.heading("year", text="Year")
        self.results_tree.heading("quartile", text="Q")
        self.results_tree.heading("citations", text="Cites")
        self.results_tree.heading("access", text="Access")
        self.results_tree.heading("pdf", text="PDF URL")

        self.results_tree.column("title", width=350, minwidth=200)
        self.results_tree.column("journal", width=200, minwidth=100)
        self.results_tree.column("year", width=50, minwidth=40, anchor="center")
        self.results_tree.column("quartile", width=35, minwidth=30, anchor="center")
        self.results_tree.column("citations", width=55, minwidth=40, anchor="center")
        self.results_tree.column("access", width=55, minwidth=40, anchor="center")
        self.results_tree.column("pdf", width=80, minwidth=60, anchor="center")

        scroll = ttk.Scrollbar(t, orient="vertical", command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=scroll.set)
        self.results_tree.grid(row=1, column=0, padx=(5, 0), pady=5, sticky="nsew")
        scroll.grid(row=1, column=1, pady=5, sticky="ns")
        make_sortable(self.results_tree)

        # Buttons
        bf = ctk.CTkFrame(t, fg_color="transparent")
        bf.grid(row=2, column=0, columnspan=2, padx=5, pady=5, sticky="ew")

        ctk.CTkButton(bf, text="Export CSV + BibTeX", command=self._export,
                      fg_color="#9333ea", width=160, height=35).pack(side="left", padx=5)

    # ---- Authors Tab ----
    def _build_authors_tab(self):
        t = self.tab_authors
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(2, weight=1)

        # Controls
        af = ctk.CTkFrame(t)
        af.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        self.analyze_btn = ctk.CTkButton(af, text="Analyze Authors", command=self._start_analyze,
                                         fg_color="#2563eb", width=150, height=35,
                                         font=("Segoe UI", 12, "bold"))
        self.analyze_btn.pack(side="left", padx=5, pady=5)

        self.hindex_btn = ctk.CTkButton(af, text="Fetch h-index (optional, uses API)",
                                        command=self._start_hindex,
                                        fg_color="#d97706", width=250, height=35, state="disabled")
        self.hindex_btn.pack(side="left", padx=5, pady=5)

        self.author_export_btn = ctk.CTkButton(af, text="Export Authors CSV",
                                               command=self._export_authors,
                                               fg_color="#9333ea", width=150, height=35, state="disabled")
        self.author_export_btn.pack(side="left", padx=5, pady=5)

        self.author_info = ctk.CTkLabel(af, text="", text_color="gray")
        self.author_info.pack(side="right", padx=10)

        # Stats
        asf = ctk.CTkFrame(t)
        asf.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="ew")
        self.author_stat_labels = {}
        for i, (key, label, color) in enumerate([
            ("total_authors", "Authors", "#60a5fa"),
            ("multi_paper", "2+ papers", "#22c55e"),
            ("top_q1", "Most Q1 papers", "#f59e0b"),
            ("top_cited", "Most cited", "#ef4444"),
        ]):
            ctk.CTkLabel(asf, text=f"{label}:", font=("Segoe UI", 11)).grid(
                row=0, column=i*2, padx=(10 if i == 0 else 5, 2), pady=5)
            lbl = ctk.CTkLabel(asf, text="—", font=("Segoe UI", 12, "bold"), text_color=color)
            lbl.grid(row=0, column=i*2+1, padx=(0, 10), pady=5)
            self.author_stat_labels[key] = lbl

        # Treeview
        a_cols = ("rank", "name", "papers", "q1", "q2", "total_cites", "avg_cites",
                  "best_q", "score", "h_index", "global_works", "global_cited", "years")
        self.authors_tree = ttk.Treeview(t, columns=a_cols, show="headings", style="Custom.Treeview")
        self.authors_tree.heading("rank", text="#")
        self.authors_tree.heading("name", text="Author")
        self.authors_tree.heading("papers", text="Papers")
        self.authors_tree.heading("q1", text="Q1")
        self.authors_tree.heading("q2", text="Q2")
        self.authors_tree.heading("total_cites", text="Cites")
        self.authors_tree.heading("avg_cites", text="Avg")
        self.authors_tree.heading("best_q", text="Best Q")
        self.authors_tree.heading("score", text="Score")
        self.authors_tree.heading("h_index", text="h-index")
        self.authors_tree.heading("global_works", text="Total Works")
        self.authors_tree.heading("global_cited", text="Total Cited")
        self.authors_tree.heading("years", text="Years")

        self.authors_tree.column("rank", width=35, minwidth=30, anchor="center")
        self.authors_tree.column("name", width=200, minwidth=120)
        self.authors_tree.column("papers", width=55, minwidth=40, anchor="center")
        self.authors_tree.column("q1", width=35, minwidth=30, anchor="center")
        self.authors_tree.column("q2", width=35, minwidth=30, anchor="center")
        self.authors_tree.column("total_cites", width=60, minwidth=40, anchor="center")
        self.authors_tree.column("avg_cites", width=50, minwidth=35, anchor="center")
        self.authors_tree.column("best_q", width=50, minwidth=35, anchor="center")
        self.authors_tree.column("score", width=50, minwidth=35, anchor="center")
        self.authors_tree.column("h_index", width=55, minwidth=40, anchor="center")
        self.authors_tree.column("global_works", width=80, minwidth=50, anchor="center")
        self.authors_tree.column("global_cited", width=80, minwidth=50, anchor="center")
        self.authors_tree.column("years", width=80, minwidth=60, anchor="center")

        a_scroll = ttk.Scrollbar(t, orient="vertical", command=self.authors_tree.yview)
        self.authors_tree.configure(yscrollcommand=a_scroll.set)
        self.authors_tree.grid(row=2, column=0, padx=(5, 0), pady=5, sticky="nsew")
        a_scroll.grid(row=2, column=1, pady=5, sticky="ns")
        make_sortable(self.authors_tree)

        self.author_data = []

    def _start_analyze(self):
        if not self.corpus:
            messagebox.showinfo("Info", "Search for articles first.")
            return
        self.status_var.set("Analyzing authors...")
        self.authors_tree.delete(*self.authors_tree.get_children())
        threading.Thread(target=self._run_analyze, daemon=True).start()

    def _run_analyze(self):
        self.author_data = self.engine.analyze_authors(self.corpus)

        self.authors_tree.delete(*self.authors_tree.get_children())
        for i, a in enumerate(self.author_data, 1):
            self.authors_tree.insert("", "end", values=(
                i, a["name"], a["papers"], a["q1_papers"], a["q2_papers"],
                a["total_cites"], a["avg_cites"], a["best_quartile"],
                round(a["score"], 1),
                a.get("h_index") or "—",
                a.get("works") or "—",
                a.get("global_cited") or "—",
                a["year_range"],
            ))

        multi = sum(1 for a in self.author_data if a["papers"] >= 2)
        top_q1 = self.author_data[0]["name"] if self.author_data and self.author_data[0]["q1_papers"] > 0 else "—"
        # Find who has most Q1 papers
        by_q1 = sorted(self.author_data, key=lambda x: x["q1_papers"], reverse=True)
        top_q1_name = by_q1[0]["name"] if by_q1 and by_q1[0]["q1_papers"] > 0 else "—"
        top_cited_name = self.author_data[0]["name"] if self.author_data else "—"

        self.author_stat_labels["total_authors"].configure(text=str(len(self.author_data)))
        self.author_stat_labels["multi_paper"].configure(text=str(multi))
        self.author_stat_labels["top_q1"].configure(text=top_q1_name[:25])
        self.author_stat_labels["top_cited"].configure(text=top_cited_name[:25])

        self.hindex_btn.configure(state="normal")
        self.author_export_btn.configure(state="normal")
        self.author_info.configure(
            text=f"Score = papers*2 + avg_cites + Q1*5 + Q2*2 | h-index columns empty until fetched")
        self.status_var.set(f"Analyzed {len(self.author_data)} authors")
        self.tabs.set("Authors")

    def _start_hindex(self):
        if not self.author_data:
            return
        email = self.email_var.get().strip()
        if not email:
            messagebox.showerror("Error", "Email required for API.")
            return

        # Only fetch for top authors (with 2+ papers or top 100)
        candidates = [a for a in self.author_data if a["papers"] >= 2 and a.get("id")]
        if len(candidates) > 200:
            candidates = candidates[:200]

        if not candidates:
            messagebox.showinfo("Info", "No authors with 2+ papers and OpenAlex ID.")
            return

        result = messagebox.askyesno(
            "Fetch h-index",
            f"This will make {len(candidates)} API calls to get h-index for authors with 2+ papers.\n\n"
            f"Uses {len(candidates)} of your ~50,000 daily calls.\n\nProceed?")
        if not result:
            return

        self.engine.stop_flag = False
        self._set_running(True)
        threading.Thread(target=self._run_hindex, args=(candidates, email), daemon=True).start()

    def _run_hindex(self, candidates, email):
        id_to_details = {}
        budget_out = False

        for i, author in enumerate(candidates):
            if self.engine.stop_flag:
                break
            aid = author["id"]
            self.status_var.set(f"Fetching h-index: {i+1}/{len(candidates)} — {author['name'][:30]}")
            self.progress.set((i + 1) / len(candidates))

            details = self.engine.fetch_author_details(aid, email)
            if details is None:
                budget_out = True
                self.status_var.set("API budget exhausted during h-index fetch")
                break
            id_to_details[aid] = details

        # Update author_data
        for a in self.author_data:
            d = id_to_details.get(a["id"])
            if d:
                a["h_index"] = d["h_index"]
                a["works"] = d["works"]
                a["global_cited"] = d["global_cited"]

        # Re-sort by a combined score that includes h-index
        for a in self.author_data:
            h = a.get("h_index")
            h_bonus = h * 3 if isinstance(h, (int, float)) else 0
            a["score"] = a["papers"] * 2 + a["avg_cites"] + a["q1_papers"] * 5 + a["q2_papers"] * 2 + h_bonus

        self.author_data.sort(key=lambda x: x["score"], reverse=True)

        # Refresh tree
        self.authors_tree.delete(*self.authors_tree.get_children())
        for i, a in enumerate(self.author_data, 1):
            self.authors_tree.insert("", "end", values=(
                i, a["name"], a["papers"], a["q1_papers"], a["q2_papers"],
                a["total_cites"], a["avg_cites"], a["best_quartile"],
                round(a["score"], 1),
                a.get("h_index") if a.get("h_index") is not None else "—",
                a.get("works") if a.get("works") is not None else "—",
                a.get("global_cited") if a.get("global_cited") is not None else "—",
                a["year_range"],
            ))

        fetched = sum(1 for a in self.author_data if a.get("h_index") is not None)
        self.author_info.configure(
            text=f"h-index fetched for {fetched} authors | Score now includes h-index*3")
        self.progress.set(1.0)
        msg = f"h-index done: {fetched} authors"
        if budget_out:
            msg += " (budget exhausted, partial)"
        self.status_var.set(msg)
        self._set_running(False)

    def _export_authors(self):
        if not self.author_data:
            return
        out_dir = filedialog.askdirectory(title="Select export folder")
        if not out_dir:
            return
        out = Path(out_dir)
        csv_path = out / "authors_ranking.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["rank", "name", "openalex_id", "papers_in_corpus", "q1_papers",
                        "q2_papers", "total_cites", "avg_cites", "best_quartile",
                        "score", "h_index", "global_works", "global_cited",
                        "journals_count", "year_range"])
            for i, a in enumerate(self.author_data, 1):
                w.writerow([
                    i, a["name"], a["id"], a["papers"], a["q1_papers"],
                    a["q2_papers"], a["total_cites"], a["avg_cites"],
                    a["best_quartile"], round(a["score"], 1),
                    a.get("h_index", ""), a.get("works", ""),
                    a.get("global_cited", ""), a["journals_count"], a["year_range"],
                ])
        self.status_var.set(f"Authors exported to {csv_path}")
        messagebox.showinfo("Export", f"Authors ranking saved:\n{csv_path}\n\n{len(self.author_data)} authors")

    # ---- Downloads Tab ----
    def _build_downloads_tab(self):
        t = self.tab_downloads
        t.grid_columnconfigure(0, weight=1)
        t.grid_rowconfigure(2, weight=1)

        # Controls
        df = ctk.CTkFrame(t)
        df.grid(row=0, column=0, padx=5, pady=5, sticky="ew")

        self.verify_btn = ctk.CTkButton(df, text="1. Verify PDF Links", command=self._start_verify,
                                        fg_color="#d97706", width=160, height=35,
                                        font=("Segoe UI", 12, "bold"))
        self.verify_btn.pack(side="left", padx=5, pady=5)

        self.dl_btn = ctk.CTkButton(df, text="2. Download Verified PDFs", command=self._start_download,
                                    fg_color="#16a34a", width=200, height=35, state="disabled",
                                    font=("Segoe UI", 12, "bold"))
        self.dl_btn.pack(side="left", padx=5, pady=5)

        self.dl_info = ctk.CTkLabel(df, text="", text_color="gray")
        self.dl_info.pack(side="right", padx=10)

        # Download stats
        self.dl_stats_frame = ctk.CTkFrame(t)
        self.dl_stats_frame.grid(row=1, column=0, padx=5, pady=(0, 5), sticky="ew")
        self.dl_stat_labels = {}
        for i, (key, label, color) in enumerate([
            ("verified", "Verified PDF", "#22c55e"),
            ("html", "HTML/Paywall", "#ef4444"),
            ("error", "Error/Timeout", "#f59e0b"),
            ("downloaded", "Downloaded", "#3b82f6"),
            ("failed", "Download Failed", "#ef4444"),
        ]):
            ctk.CTkLabel(self.dl_stats_frame, text=f"{label}:", font=("Segoe UI", 11)).grid(
                row=0, column=i*2, padx=(10 if i == 0 else 5, 2), pady=5)
            lbl = ctk.CTkLabel(self.dl_stats_frame, text="0",
                               font=("Segoe UI", 12, "bold"), text_color=color)
            lbl.grid(row=0, column=i*2+1, padx=(0, 10), pady=5)
            self.dl_stat_labels[key] = lbl

        # Download tree
        dl_cols = ("title", "status", "reason", "http", "content_type", "size")
        self.dl_tree = ttk.Treeview(t, columns=dl_cols, show="headings", style="Custom.Treeview")
        self.dl_tree.heading("title", text="Title")
        self.dl_tree.heading("status", text="Status")
        self.dl_tree.heading("reason", text="Reason")
        self.dl_tree.heading("http", text="HTTP")
        self.dl_tree.heading("content_type", text="Content-Type")
        self.dl_tree.heading("size", text="Size")

        self.dl_tree.column("title", width=300, minwidth=150)
        self.dl_tree.column("status", width=80, minwidth=60, anchor="center")
        self.dl_tree.column("reason", width=200, minwidth=100)
        self.dl_tree.column("http", width=45, minwidth=35, anchor="center")
        self.dl_tree.column("content_type", width=150, minwidth=80)
        self.dl_tree.column("size", width=60, minwidth=40, anchor="center")

        dl_scroll = ttk.Scrollbar(t, orient="vertical", command=self.dl_tree.yview)
        self.dl_tree.configure(yscrollcommand=dl_scroll.set)
        self.dl_tree.grid(row=2, column=0, padx=(5, 0), pady=5, sticky="nsew")
        dl_scroll.grid(row=2, column=1, pady=5, sticky="ns")
        make_sortable(self.dl_tree)

    # ---- Helpers ----
    def _browse_scimago(self):
        path = filedialog.askopenfilename(filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if path:
            self.scimago_var.set(path)

    def _get_quartiles(self):
        return {q for q, v in self.q_vars.items() if v.get()}

    def _get_queries(self):
        return [q.strip() for q in self.queries_text.get("1.0", "end").strip().split("\n") if q.strip()]

    def _set_running(self, running):
        self.running = running
        self.search_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def _stop(self):
        self.engine.stop_flag = True
        self.status_var.set("Stopping...")

    def _update_stats(self):
        n_oa = sum(1 for k in self.corpus if (k.get("open_access") or {}).get("is_oa"))
        n_pdf = sum(1 for k in self.corpus if self.engine.pdf_url(k))
        n_q1 = sum(1 for k in self.corpus if k.get("_quartile") == "Q1")
        n_q2 = sum(1 for k in self.corpus if k.get("_quartile") == "Q2")
        self.stat_labels["total"].configure(text=str(len(self.corpus)))
        self.stat_labels["oa"].configure(text=str(n_oa))
        self.stat_labels["paywall"].configure(text=str(len(self.corpus) - n_oa))
        self.stat_labels["with_pdf"].configure(text=str(n_pdf))
        self.stat_labels["q1"].configure(text=str(n_q1))
        self.stat_labels["q2"].configure(text=str(n_q2))

    # ---- Search ----
    def _start_search(self):
        email = self.email_var.get().strip()
        if not email:
            messagebox.showerror("Error", "Email required for API access.")
            return
        queries = self._get_queries()
        if not queries:
            messagebox.showerror("Error", "Enter at least one query.")
            return
        self.search_info.configure(text=f"{len(queries)} queries = {len(queries)} API calls")
        self.engine.stop_flag = False
        self._set_running(True)
        threading.Thread(target=self._run_search, args=(email, queries), daemon=True).start()

    def _run_search(self, email, queries):
        self.status_var.set("Checking API budget...")
        ok, msg = self.engine.check_api(email)
        if not ok:
            self.status_var.set(f"API unavailable: {msg}")
            messagebox.showerror("API Error", msg)
            self._set_running(False)
            return

        scimago_path = self.scimago_var.get().strip()
        if scimago_path:
            self.status_var.set("Loading Scimago...")
            n = self.engine.load_scimago(scimago_path)
            if n <= 0:
                self.status_var.set("Scimago not loaded — no quartile filter")

        quartiles = self._get_quartiles()
        year_from = int(self.year_from_var.get())
        year_to = int(self.year_to_var.get())

        seen = set()
        self.corpus = []
        self.results_tree.delete(*self.results_tree.get_children())

        for i, query in enumerate(queries):
            if self.engine.stop_flag:
                break
            self.status_var.set(f"[{i+1}/{len(queries)}] {query[:50]}...")
            self.progress.set(i / len(queries))

            results = self.engine.search(query, seen, quartiles, email, 100, year_from, year_to)
            if results is None:
                self.status_var.set("Daily budget exhausted — retry after midnight UTC")
                messagebox.showwarning("Budget", "API budget exhausted.\nResets at midnight UTC (~7PM Colombia).")
                break

            self.corpus.extend(results)
            for w in results:
                is_oa = (w.get("open_access") or {}).get("is_oa", False)
                has_pdf = "Yes" if self.engine.pdf_url(w) else "No"
                self.results_tree.insert("", "end", values=(
                    (w.get("title") or "")[:80],
                    self.engine.journal_str(w)[:40],
                    w.get("publication_year", ""),
                    w.get("_quartile", "?"),
                    w.get("cited_by_count", 0),
                    "OA" if is_oa else "Paid",
                    has_pdf,
                ))

        self.corpus.sort(key=lambda w: w.get("cited_by_count", 0), reverse=True)
        self.results_tree.delete(*self.results_tree.get_children())
        for w in self.corpus:
            is_oa = (w.get("open_access") or {}).get("is_oa", False)
            has_pdf = "Yes" if self.engine.pdf_url(w) else "No"
            self.results_tree.insert("", "end", values=(
                (w.get("title") or "")[:80],
                self.engine.journal_str(w)[:40],
                w.get("publication_year", ""),
                w.get("_quartile", "?"),
                w.get("cited_by_count", 0),
                "OA" if is_oa else "Paid",
                has_pdf,
            ))

        self._update_stats()
        self.progress.set(1.0)
        self.status_var.set(f"Found {len(self.corpus)} articles")
        if self.corpus:
            self.verify_btn.configure(state="normal")
            self.tabs.set("Results")
        self._set_running(False)

    # ---- Verify PDF Links ----
    def _start_verify(self):
        if not self.corpus:
            return
        self.engine.stop_flag = False
        self._set_running(True)
        self.dl_tree.delete(*self.dl_tree.get_children())
        threading.Thread(target=self._run_verify, daemon=True).start()

    def _run_verify(self):
        candidates = [(w, self.engine.pdf_url(w)) for w in self.corpus if self.engine.pdf_url(w)]
        self.status_var.set(f"Verifying {len(candidates)} PDF links...")
        self.tabs.set("Downloads")

        self.pdf_jobs = []
        verified = html = errors = 0

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(self.engine.verify_pdf_url, url): (w, url)
                       for w, url in candidates}
            for i, fut in enumerate(as_completed(futures)):
                if self.engine.stop_flag:
                    break
                w, url = futures[fut]
                info = fut.result()
                title = (w.get("title") or "")[:60]
                size_str = f"{info['size_kb']} KB" if info.get("size_kb") else "—"

                if info["downloadable"]:
                    status_text = "PDF"
                    verified += 1
                    self.pdf_jobs.append((w, url))
                elif "html" in info.get("reason", "").lower() or "paywall" in info.get("reason", "").lower():
                    status_text = "SKIP"
                    html += 1
                else:
                    status_text = "ERROR"
                    errors += 1

                self.dl_tree.insert("", "0" if info["downloadable"] else "end", values=(
                    title, status_text, info["reason"],
                    info.get("http", ""), info.get("content_type", "")[:35], size_str
                ))

                done = i + 1
                self.progress.set(done / len(candidates))
                self.status_var.set(f"Verifying: {done}/{len(candidates)} | "
                                   f"PDF: {verified} | Skip: {html} | Error: {errors}")

                self.dl_stat_labels["verified"].configure(text=str(verified))
                self.dl_stat_labels["html"].configure(text=str(html))
                self.dl_stat_labels["error"].configure(text=str(errors))

        self.progress.set(1.0)
        self.dl_info.configure(text=f"{verified} PDFs ready to download")
        self.status_var.set(f"Verification done: {verified} downloadable, {html} paywalls, {errors} errors")

        if self.pdf_jobs:
            self.dl_btn.configure(state="normal")
        self._set_running(False)

    # ---- Download PDFs ----
    def _start_download(self):
        if not self.pdf_jobs:
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
        for w, url in self.pdf_jobs:
            doi = (w.get("doi") or w.get("id", "")).split("/")[-1]
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi)[:80]
            dest = pdf_dir / f"{safe}.pdf"
            jobs.append((w, url, dest))

        self.status_var.set(f"Downloading {len(jobs)} verified PDFs...")
        downloaded = failed = 0

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self.engine.download_pdf, url, dest): (i, w)
                       for i, (w, url, dest) in enumerate(jobs)}
            for fut in as_completed(futures):
                if self.engine.stop_flag:
                    break
                idx, w = futures[fut]
                result = fut.result()

                if result["status"] in ("ok", "exists"):
                    downloaded += 1
                else:
                    failed += 1

                done = idx + 1
                self.progress.set(done / len(jobs))
                self.status_var.set(f"Downloading: {done}/{len(jobs)} | "
                                   f"OK: {downloaded} | Failed: {failed}")
                self.dl_stat_labels["downloaded"].configure(text=str(downloaded))
                self.dl_stat_labels["failed"].configure(text=str(failed))

        self.progress.set(1.0)
        self.status_var.set(f"Done: {downloaded} PDFs in {pdf_dir}")
        messagebox.showinfo("Download Complete",
                           f"Downloaded: {downloaded}\nFailed: {failed}\nFolder: {pdf_dir}")
        self._set_running(False)

    # ---- Export ----
    def _export(self):
        if not self.corpus:
            messagebox.showinfo("Info", "Search first.")
            return
        out_dir = filedialog.askdirectory(title="Select export folder")
        if not out_dir:
            return
        out = Path(out_dir)

        csv_path = out / "corpus_metadata.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["openalex_id", "doi", "title", "authors", "journal",
                        "year", "quartile", "citations", "open_access", "pdf_url", "abstract"])
            for k in self.corpus:
                try:
                    w.writerow([
                        k.get("id", ""), (k.get("doi") or "").replace("https://doi.org/", ""),
                        k.get("title", ""), self.engine.authors_str(k), self.engine.journal_str(k),
                        k.get("publication_year", ""), k.get("_quartile", ""),
                        k.get("cited_by_count", ""), (k.get("open_access") or {}).get("is_oa", False),
                        self.engine.pdf_url(k) or "",
                        self.engine.rebuild_abstract(k.get("abstract_inverted_index")),
                    ])
                except Exception:
                    continue

        bib_path = out / "corpus.bib"
        with open(bib_path, "w", encoding="utf-8") as f:
            def esc(s):
                return (s or "").replace("{", "").replace("}", "").replace("&", "\\&")
            for i, k in enumerate(self.corpus, 1):
                doi = (k.get("doi") or "").replace("https://doi.org/", "")
                f.write(f"@article{{ref{i:05d},\n  title = {{{esc(k.get('title'))}}},\n"
                        f"  author = {{{esc(self.engine.authors_str(k))}}},\n"
                        f"  journal = {{{esc(self.engine.journal_str(k))}}},\n"
                        f"  year = {{{k.get('publication_year','')}}},\n  doi = {{{doi}}},\n}}\n")

        closed = [k for k in self.corpus if not (k.get("open_access") or {}).get("is_oa")]
        pay_path = out / "paywall_articles.csv"
        with open(pay_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["doi", "title", "journal", "year", "quartile", "citations"])
            for k in closed:
                w.writerow([(k.get("doi") or "").replace("https://doi.org/", ""),
                            k.get("title", ""), self.engine.journal_str(k),
                            k.get("publication_year", ""), k.get("_quartile", ""), k.get("cited_by_count", "")])

        self.status_var.set(f"Exported {len(self.corpus)} articles to {out}")
        messagebox.showinfo("Export Complete",
                           f"CSV: {csv_path.name}\nBibTeX: {bib_path.name}\n"
                           f"Paywall: {pay_path.name} ({len(closed)} articles)\n\nFolder: {out}")


if __name__ == "__main__":
    app = App()
    app.mainloop()

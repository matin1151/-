"""Local offline search service for the Qanoonyar SQLite databases."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"


def normalize(value: str) -> str:
    value = value.translate(str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه"}))
    value = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def fts_query(value: str) -> str:
    parts = re.findall(r"[\w۰-۹]+", normalize(value), flags=re.UNICODE)
    return " AND ".join('"' + part.replace('"', '""') + '"*' for part in parts[:10])


def records(db: Path, sql: str, args=()):
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, args)]


class Handler(SimpleHTTPRequestHandler):
    db: Path
    private: bool

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

    def log_message(self, fmt, *args):
        print("[qanoonyar] " + (fmt % args))

    def send_json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.api(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path in ("/", "/index.html"):
            self.path = "/index.html"
        return super().do_GET()

    def api(self, path, query):
        term = normalize((query.get("q") or [""])[0])[:180]
        if path == "/api/coverage":
            data = records(self.db, "SELECT verification_status, COUNT(*) AS count FROM heading GROUP BY verification_status")
            materials = records(self.db, "SELECT COUNT(*) AS count FROM legal_material") if self.has_table("legal_material") else [{"count": 0}]
            queue = records(self.db, "SELECT status,COUNT(*) AS count FROM research_queue GROUP BY status") if self.has_table("research_queue") else []
            docs = records(self.db, "SELECT COUNT(*) AS count FROM document")
            pending_ocr = records(self.db, "SELECT COUNT(*) AS count FROM document_page WHERE extraction_status='ocr_pending'") if self.private and self.has_table("document_page") else [{"count": 0}]
            self.send_json({"private": self.private, "headings": data, "materials": materials[0]["count"], "research_queue": queue, "company_documents": docs[0]["count"] if self.private else 0, "ocr_pending_pages": pending_ocr[0]["count"]})
            return
        if path == "/api/headings":
            parent = (query.get("parent_id") or [None])[0]
            if term:
                q = fts_query(term)
                try:
                    rows = records(self.db, "SELECT h.id,h.parent_id,h.ordinal,h.title,h.page_start,h.verification_status FROM heading_fts f JOIN heading h ON h.id=f.rowid WHERE heading_fts MATCH ? ORDER BY h.page_start LIMIT 100", (q,))
                except sqlite3.OperationalError:
                    rows = records(self.db, "SELECT id,parent_id,ordinal,title,page_start,verification_status FROM heading WHERE title LIKE ? ORDER BY page_start LIMIT 100", (f"%{term}%",))
            elif parent:
                rows = records(self.db, "SELECT id,parent_id,ordinal,title,page_start,verification_status FROM heading WHERE parent_id=? ORDER BY id LIMIT 250", (parent,))
            else:
                rows = records(self.db, "SELECT id,parent_id,ordinal,title,page_start,verification_status FROM heading WHERE parent_id IS NULL ORDER BY id LIMIT 250")
            self.send_json(rows)
            return
        if path == "/api/search":
            if not term:
                self.send_json({"query": term, "results": []}); return
            q = fts_query(term)
            results = []
            try:
                results += records(self.db, "SELECT 'material' AS kind,m.id,m.title,m.summary AS excerpt,m.provision_ref AS reference,s.url AS source_url,s.title AS source_title,m.currentness_note FROM legal_material_fts f JOIN legal_material m ON m.id=f.rowid JOIN source s ON s.id=m.source_id WHERE legal_material_fts MATCH ? ORDER BY bm25(legal_material_fts) LIMIT 30", (q,))
            except sqlite3.OperationalError:
                pass
            try:
                results += records(self.db, "SELECT 'heading' AS kind,h.id,h.title,h.evidence AS excerpt,CAST(h.page_start AS TEXT) AS reference,NULL AS source_url,'فهرست اسکن‌شده' AS source_title,h.verification_status AS currentness_note FROM heading_fts f JOIN heading h ON h.id=f.rowid WHERE heading_fts MATCH ? ORDER BY bm25(heading_fts) LIMIT 30", (q,))
            except sqlite3.OperationalError:
                pass
            if self.private and self.has_table("document_page_fts"):
                try:
                    results += records(self.db, "SELECT 'company' AS kind,p.id,d.title,p.page_text AS excerpt,('صفحه '||p.page_number) AS reference,NULL AS source_url,'سند اختصاصی شرکت' AS source_title,p.extraction_status AS currentness_note FROM document_page_fts f JOIN document_page p ON p.id=f.rowid JOIN document d ON d.id=p.document_id WHERE document_page_fts MATCH ? ORDER BY bm25(document_page_fts) LIMIT 30", (q,))
                except sqlite3.OperationalError:
                    pass
            self.send_json({"query": term, "results": results[:75]})
            return
        if path == "/api/report":
            headings=records(self.db,"SELECT COUNT(*) AS count FROM heading")[0]["count"]
            materials=records(self.db,"SELECT s.title AS source,COUNT(*) AS count FROM legal_material m JOIN source s ON s.id=m.source_id GROUP BY s.id ORDER BY s.id")
            queue=records(self.db,"SELECT status,COUNT(*) AS count FROM research_queue GROUP BY status")
            docs=records(self.db,"SELECT d.id,d.title,d.page_count,d.access_status,d.notes FROM document d ORDER BY d.id")
            self.send_json({"private":self.private,"headings":headings,"materials_by_source":materials,"queue":queue,"documents":docs})
            return
        self.send_json({"error": "not_found"}, 404)

    def has_table(self, table):
        return bool(records(self.db, "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)))


def main():
    parser = argparse.ArgumentParser(description="Run Qanoonyar locally")
    parser.add_argument("--private", action="store_true", help="open the private company database on this local session")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    filename = "qanoonnameh_private.sqlite3" if args.private else "qanoonnameh_public.sqlite3"
    db = OUTPUTS / filename
    if not db.exists():
        db = ROOT / filename
    if not db.exists():
        db = ROOT / filename
    if not db.exists():
        raise SystemExit(f"Database not found: {db}")
    Handler.db = db
    Handler.private = args.private
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"قانون‌یار آماده است: http://127.0.0.1:{args.port} ({'خصوصی' if args.private else 'عمومی'})")
    print("برای توقف، پنجره اجرا را ببندید یا Ctrl+C بزنید.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

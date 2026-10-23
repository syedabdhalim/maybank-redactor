"""Maybank Redactor - local-only server.

Runs on 127.0.0.1, keeps uploaded statements in memory only, and uses
PyMuPDF true redaction (text under the boxes is removed, not just covered).
"""

import base64
import json
import os
import re
import socket
import sys
import threading
import uuid
import webbrowser
from collections import OrderedDict
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import fitz  # PyMuPDF

HOST = "127.0.0.1"
PORT = int(os.environ.get("REDACTOR_PORT", 3090))
MAX_UPLOAD = 50 * 1024 * 1024
MAX_DOCS = 8
ROOT = Path(__file__).resolve().parent

DATE_RE = re.compile(r"^\d{2}/\d{2}$")
MONEY_RE = re.compile(r"^[\d,]+\.\d{2}(CR)?$")
NUMBER_RE = re.compile(r"^[\d,]+(\.\d{2})?(CR|-)?$")
STMT_DATE_RE = re.compile(r"^(\d{2}) ([A-Z]{3}) (\d{2})$")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}

# Labels whose trailing figure leaks account totals.
BALANCE_LABELS = (
    "YOUR PREVIOUS STATEMENT BALANCE",
    "YOUR COMBINED CREDIT LIMIT",
    "TOTAL CREDIT THIS MONTH",
    "TOTAL DEBIT THIS MONTH",
    "SUB TOTAL/JUMLAH",
)
NOT_CONTINUATION = re.compile(r"^(TOTAL|SUB|YOUR|JUMLAH|\(JUMLAH|KOMBINASI|MAYBANK)")

# fitz is not thread-safe; serialise every call into it.
FITZ_LOCK = threading.Lock()
DOCS: "OrderedDict[str, dict]" = OrderedDict()


class NeedsPassword(Exception):
    pass


def open_pdf(data: bytes, password: str = "") -> fitz.Document:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise ValueError("Bukan fail PDF yang sah.") from None
    if doc.needs_pass and not doc.authenticate(password):
        doc.close()
        raise NeedsPassword()
    return doc


# ---------- parsing ----------

def page_lines(page):
    """Group words into visual lines, top to bottom, left to right."""
    words = sorted(page.get_text("words"), key=lambda w: (round(w[3], 1), w[0]))
    lines = []
    for w in words:
        rect = fitz.Rect(w[:4])
        if lines and abs(lines[-1]["y1"] - rect.y1) < 2.5:
            lines[-1]["words"].append((rect, w[4]))
        else:
            lines.append({"y1": rect.y1, "words": [(rect, w[4])]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w[0].x0)
        ln["rect"] = fitz.Rect(ln["words"][0][0])
        for r, _ in ln["words"]:
            ln["rect"] |= r
        ln["text"] = " ".join(t for _, t in ln["words"])
    return lines


def row_band(lines, first, last, x0, x1, pad=2.0):
    """Rect covering lines[first..last], padded but never past the midpoint to a neighbour."""
    top = min(lines[i]["rect"].y0 for i in range(first, last + 1))
    bottom = max(lines[i]["rect"].y1 for i in range(first, last + 1))
    y0 = top - pad
    y1 = bottom + pad
    if first > 0:
        y0 = max(y0, (lines[first - 1]["rect"].y1 + top) / 2)
    if last + 1 < len(lines):
        y1 = min(y1, (bottom + lines[last + 1]["rect"].y0) / 2)
    return fitz.Rect(x0, y0, x1, y1)


def text_segments(lines, first, last, gap=6.0):
    """One box per run of adjacent words (date, description, location, amount...)."""
    boxes = []
    for i in range(first, last + 1):
        run = None
        prev_x1 = None
        for r, _ in lines[i]["words"]:
            if run is not None and r.x0 - prev_x1 <= gap:
                run |= r
            else:
                if run is not None:
                    boxes.append(run)
                run = fitz.Rect(r)
            prev_x1 = r.x1
        if run is not None:
            boxes.append(run)
    return [fitz.Rect(b.x0 - 1.5, b.y0 - 0.5, b.x1 + 1.5, b.y1 + 0.5) for b in boxes]


def word_box(rect, px=0.8, py=-1.0):
    """Box for a single word/line. Shrunk vertically: some statement rows sit closer
    together than their glyph boxes are tall, and a full-height box would eat the
    neighbouring row."""
    return fitz.Rect(rect.x0 - px, rect.y0 - py, rect.x1 + px, rect.y1 + py)


def card_runs(words):
    """Yield runs of digit-only words that form a 15-16 digit card number."""
    i = 0
    while i < len(words):
        j = i
        while j < len(words) and words[j][1].isdigit():
            j += 1
        run = words[i:j]
        if 15 <= sum(len(t) for _, t in run) <= 16 and len(run) >= 2:
            yield run
        i = max(j, i + 1)


def to_iso(ddmm, stmt):
    d, m = (int(x) for x in ddmm.split("/"))
    y = stmt.year if m <= stmt.month else stmt.year - 1
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_statement(doc):
    stmt = None
    p0 = page_lines(doc[0])
    # Statement date sits left of the payment due date in the header box.
    candidates = []
    for ln in p0:
        for i in range(len(ln["words"]) - 2):
            trio = ln["words"][i:i + 3]
            m = STMT_DATE_RE.match(" ".join(t for _, t in trio))
            if m and m.group(2) in MONTHS:
                candidates.append((trio[0][0].x0, m))
    if candidates:
        _, m = min(candidates, key=lambda c: c[0])
        stmt = date(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    else:
        stmt = date.today()

    txns, fields, cards = [], [], []
    current_card = None
    holder = ""

    for pno, page in enumerate(doc):
        lines = page_lines(page)
        width = page.rect.width
        left, right = 24.0, width - 24.0
        for ln in lines:
            if ln["words"][0][1] == "Posting":
                left = ln["words"][0][0].x0 - 3

        # Card numbers anywhere: mask all groups except the last.
        for ln in lines:
            for run in card_runs(ln["words"]):
                for r, t in run[:-1]:
                    fields.append({"category": "card", "page": pno, "rect": word_box(r), "text": t})

        # Page 1 header: name/address block and the summary table figures.
        if pno == 0:
            summary_top = next((ln["rect"].y0 for ln in lines if ln["text"].startswith("Account Number")), None)
            warning_top = next((ln["rect"].y0 for ln in lines if ln["text"].startswith("WARNING")), None)
            for ln in lines:
                y = ln["rect"].y0
                if summary_top and 90 < y < summary_top - 30:
                    left_words = [(r, t) for r, t in ln["words"] if r.x0 < 300]
                    if left_words:
                        if not holder:
                            holder = " ".join(t for _, t in left_words)
                        box = fitz.Rect(left_words[0][0])
                        for r, _ in left_words:
                            box |= r
                        fields.append({"category": "identity", "page": pno, "rect": word_box(box),
                                       "text": " ".join(t for _, t in left_words)})
                if summary_top and warning_top and summary_top + 5 < y < warning_top:
                    for r, t in ln["words"]:
                        if MONEY_RE.match(t) and r.x0 > 160:
                            fields.append({"category": "balances", "page": pno, "rect": word_box(r), "text": t})

        i = 0
        while i < len(lines):
            ln = lines[i]
            words = ln["words"]
            text = ln["text"]

            # Card section header: "MAYBANK 2 GOLD AMEX : 3700 000000 00000"
            if ":" in [t for _, t in words]:
                k = [t for _, t in words].index(":")
                digits = "".join(t for _, t in words[k + 1:] if t.isdigit())
                if 15 <= len(digits) <= 16:
                    name = " ".join(t for _, t in words[:k])
                    current_card = {"id": len(cards), "name": name, "last4": digits[-4:], "page": pno}
                    cards.append(current_card)
                    if i > 0 and ln["rect"].y0 - lines[i - 1]["rect"].y1 < 8:
                        prev = lines[i - 1]
                        fields.append({"category": "identity", "page": pno,
                                       "rect": word_box(prev["rect"]), "text": prev["text"]})

            for label in BALANCE_LABELS:
                if text.startswith(label):
                    for r, t in words:
                        if NUMBER_RE.match(t) and any(ch.isdigit() for ch in t) and r.x0 > 300:
                            fields.append({"category": "balances", "page": pno, "rect": word_box(r), "text": t})

            is_txn = (len(words) >= 3 and DATE_RE.match(words[0][1]) and DATE_RE.match(words[1][1])
                      and words[0][0].x0 < 100)
            if not is_txn:
                i += 1
                continue

            amount_word = words[-1] if MONEY_RE.match(words[-1][1]) and words[-1][0].x0 > 480 else None
            body = words[2:-1] if amount_word else words[2:]
            desc = " ".join(t for r, t in body if r.x0 < 395)
            location = " ".join(t for r, t in body if r.x0 >= 395)

            # Continuation lines (e.g. "TRANSACTED AMOUNT USD 19.99") belong to this row.
            last = i
            extra = []
            while last + 1 < len(lines):
                nxt = lines[last + 1]
                nw = nxt["words"]
                if (DATE_RE.match(nw[0][1]) or nw[0][0].x0 < 150
                        or nxt["rect"].y0 - lines[last]["rect"].y1 > 8
                        or NOT_CONTINUATION.match(nxt["text"])):
                    break
                extra.append(nxt["text"])
                last += 1

            amount = None
            credit = False
            if amount_word:
                raw = amount_word[1]
                credit = raw.endswith("CR")
                amount = float(raw.removesuffix("CR").replace(",", ""))

            txns.append({
                "id": f"t{len(txns)}",
                "page": pno,
                "card": current_card["id"] if current_card else None,
                "post_date": to_iso(words[0][1], stmt),
                "txn_date": to_iso(words[1][1], stmt),
                "post_raw": words[0][1],
                "txn_raw": words[1][1],
                "desc": desc,
                "location": location,
                "extra": " | ".join(extra),
                "amount": amount,
                "credit": credit,
                "rect": row_band(lines, i, last, left, right),
                "segments": text_segments(lines, i, last),
            })
            i = last + 1

    return {
        "statement_date": stmt.isoformat(),
        "holder": holder,
        "cards": cards,
        "transactions": txns,
        "fields": fields,
        "pages": [{"w": p.rect.width, "h": p.rect.height} for p in doc],
    }


# ---------- planning & redaction ----------

HIGHLIGHT = (1.0, 0.91, 0.35)
HEX_RE = re.compile(r"^#([0-9a-fA-F]{6})$")


def highlight_color(value):
    """'#rrggbb' from the UI to a fitz RGB tuple; anything else falls back to yellow."""
    m = HEX_RE.match(value or "")
    if not m:
        return HIGHLIGHT
    h = m.group(1)
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def page_range(opts, n):
    """1-based inclusive page_from/page_to from the UI, clamped to the document."""
    try:
        lo = int(opts.get("page_from") or 1)
        hi = int(opts.get("page_to") or n)
    except (TypeError, ValueError):
        raise ValueError("Julat page tak sah.") from None
    lo, hi = max(1, lo), min(n, hi)
    if lo > hi:
        raise ValueError("Julat page tak sah: page awal lebih besar dari page akhir.")
    return set(range(lo - 1, hi))


def build_plan(parsed, keep_ids, opts, highlight_ids=()):
    highlight = set(highlight_ids)
    keep = set(keep_ids) | highlight  # a highlighted row is always kept
    boxes = []
    full_rows = opts.get("style") == "rows"
    for t in parsed["transactions"]:
        if t["id"] not in keep:
            for r in ([t["rect"]] if full_rows else t["segments"]):
                boxes.append({"page": t["page"], "rect": r, "kind": "txn", "txn": t["id"]})
    for f in parsed["fields"]:
        if opts.get(f["category"]):
            boxes.append({"page": f["page"], "rect": f["rect"], "kind": f["category"]})

    n = len(parsed["pages"])
    if opts.get("drop_pages"):
        kept = [t for t in parsed["transactions"] if t["id"] in keep]
        # Page 1 always stays, plus each kept card's section header so rows keep their context.
        keep_pages = {0} | {t["page"] for t in kept}
        keep_pages |= {c["page"] for c in parsed["cards"] if any(t["card"] == c["id"] for t in kept)}
    else:
        keep_pages = set(range(n))
    in_range = page_range(opts, n)
    keep_pages &= in_range
    if not keep_pages:
        raise ValueError("Tiada page untuk di-export dalam julat ni.")
    dropped = [p for p in range(n) if p not in keep_pages]
    boxes = [b for b in boxes if b["page"] in keep_pages]
    highlights = [{"page": t["page"], "rect": t["rect"], "txn": t["id"]}
                  for t in parsed["transactions"] if t["id"] in highlight and t["page"] in keep_pages]
    return {"boxes": boxes, "dropped": dropped, "highlights": highlights,
            "out_of_range": [p for p in range(n) if p not in in_range]}


def rect_list(r):
    return [round(r.x0, 2), round(r.y0, 2), round(r.x1, 2), round(r.y1, 2)]


def leaked_words(page, rects):
    """Words on the page that still intersect a redaction box (shrunk by 1pt)."""
    leaks = []
    words = page.get_text("words")
    for rect in rects:
        inner = fitz.Rect(rect.x0 + 1, rect.y0 + 1, rect.x1 - 1, rect.y1 - 1)
        for w in words:
            wr = fitz.Rect(w[:4])
            if wr.intersects(inner) and (wr & inner).get_area() > 0.5:
                leaks.append(w[4])
    return leaks


def redact(entry, keep_ids, opts, highlight_ids=()):
    parsed = entry["parsed"]
    plan = build_plan(parsed, keep_ids, opts, highlight_ids)
    doc = open_pdf(entry["data"], entry["password"])
    try:
        by_page = {}
        for b in plan["boxes"]:
            by_page.setdefault(b["page"], []).append(b["rect"])
        for pno, rects in by_page.items():
            page = doc[pno]
            for r in rects:
                page.add_redact_annot(r, fill=(0, 0, 0))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)

        # Marker drawn underneath the page content (rows have no background fill),
        # so the text stays crisp black on the marker colour.
        fill = highlight_color(opts.get("hl_color"))
        for h in plan["highlights"]:
            doc[h["page"]].draw_rect(h["rect"], color=None, fill=fill, width=0, overlay=False)

        keep_pages = [p for p in range(doc.page_count) if p not in plan["dropped"]]
        if len(keep_pages) < doc.page_count:
            doc.select(keep_pages)

        doc.scrub(hidden_text=False, redactions=False, clean_pages=True)
        doc.set_metadata({})
        out = doc.tobytes(garbage=4, deflate=True, clean=True, encryption=fitz.PDF_ENCRYPT_NONE)
    finally:
        doc.close()

    # Re-open the final bytes and prove nothing survived under any box.
    check = fitz.open(stream=out, filetype="pdf")
    try:
        new_index = {old: new for new, old in enumerate(keep_pages)}
        leaks = []
        for pno, rects in by_page.items():
            leaks += leaked_words(check[new_index[pno]], rects)
        if check.page_count != len(keep_pages):
            leaks.append("<page count mismatch>")
    finally:
        check.close()
    if leaks:
        raise RuntimeError(f"Verification failed, {len(leaks)} word(s) still present: {leaks[:5]}")

    kept = [t for t in parsed["transactions"] if t["id"] in set(keep_ids) | set(highlight_ids)]
    report = {
        "kept": len(kept),
        "highlighted": len(plan["highlights"]),
        "redacted_rows": len({b["txn"] for b in plan["boxes"] if b["kind"] == "txn"}),
        "boxes": len(plan["boxes"]),
        "pages_out": len(keep_pages),
        "pages_dropped": [p + 1 for p in plan["dropped"]],
        "verified": True,
    }
    return out, report


# ---------- HTTP ----------

def public_view(parsed):
    def conv(item):
        out = dict(item)
        out.pop("segments", None)
        out["rect"] = rect_list(item["rect"])
        return out
    return {
        "statement_date": parsed["statement_date"],
        "holder": parsed["holder"],
        "cards": parsed["cards"],
        "pages": parsed["pages"],
        "transactions": [conv(t) for t in parsed["transactions"]],
        "field_counts": {c: sum(1 for f in parsed["fields"] if f["category"] == c)
                         for c in ("balances", "card", "identity")},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MaybankRedactor/1.0"

    def log_message(self, fmt, *args):
        # Keep the console quiet; never log query strings (they may hold ids).
        pass

    def send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, status, body, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError("File too large")
        return self.rfile.read(length)

    def get_entry(self, doc_id):
        entry = DOCS.get(doc_id)
        if entry:
            DOCS.move_to_end(doc_id)
        return entry

    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self.send_bytes(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/page":
            q = parse_qs(url.query)
            entry = self.get_entry(q.get("id", [""])[0])
            if not entry:
                return self.send_json(404, {"error": "Dokumen tiada. Upload semula."})
            n = int(q.get("n", ["0"])[0])
            key = ("png", n)
            if key not in entry["cache"]:
                with FITZ_LOCK:
                    doc = open_pdf(entry["data"], entry["password"])
                    try:
                        pix = doc[n].get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
                        entry["cache"][key] = pix.tobytes("png")
                    finally:
                        doc.close()
            return self.send_bytes(200, entry["cache"][key], "image/png")
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        url = urlparse(self.path)
        try:
            if url.path == "/api/open":
                data = self.read_body()
                password = unquote(self.headers.get("X-Password") or "")
                with FITZ_LOCK:
                    try:
                        doc = open_pdf(data, password)
                    except NeedsPassword:
                        return self.send_json(401, {"needs_password": True,
                                                    "error": "PDF ni berkata laluan." if password == ""
                                                    else "Kata laluan salah."})
                    try:
                        parsed = parse_statement(doc)
                    finally:
                        doc.close()
                if not parsed["transactions"]:
                    return self.send_json(422, {"error": "Tak jumpa transaksi. Ini bukan penyata kad kredit Maybank?"})
                doc_id = uuid.uuid4().hex
                DOCS[doc_id] = {"data": data, "password": password, "parsed": parsed, "cache": {}}
                while len(DOCS) > MAX_DOCS:
                    DOCS.popitem(last=False)
                return self.send_json(200, {"id": doc_id, **public_view(parsed)})

            if url.path in ("/api/plan", "/api/redact"):
                req = json.loads(self.read_body() or b"{}")
                entry = self.get_entry(req.get("id", ""))
                if not entry:
                    return self.send_json(404, {"error": "Dokumen tiada. Upload semula."})
                keep = req.get("keep", [])
                highlight = req.get("highlight", [])
                opts = req.get("options", {})
                if url.path == "/api/plan":
                    plan = build_plan(entry["parsed"], keep, opts, highlight)
                    return self.send_json(200, {
                        "boxes": [{**b, "rect": rect_list(b["rect"])} for b in plan["boxes"]],
                        "highlights": [{**h, "rect": rect_list(h["rect"])} for h in plan["highlights"]],
                        "dropped": plan["dropped"],
                        "out_of_range": plan["out_of_range"],
                    })
                with FITZ_LOCK:
                    out, report = redact(entry, keep, opts, highlight)
                hdr = base64.b64encode(json.dumps(report).encode()).decode()
                return self.send_bytes(200, out, "application/pdf", {"X-Redaction-Report": hdr})

            if url.path == "/api/close":
                req = json.loads(self.read_body() or b"{}")
                DOCS.pop(req.get("id", ""), None)
                return self.send_json(200, {"ok": True})
        except ValueError as exc:  # bad input from the UI (page range etc.)
            return self.send_json(400, {"error": str(exc)})
        except Exception as exc:  # surface the reason to the UI, never a half-done PDF
            return self.send_json(500, {"error": str(exc)})
        self.send_json(404, {"error": "Not found"})


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second instance bind the same port silently,
    # and the browser keeps hitting whichever one answers first (often stale code).
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    url = f"http://{HOST}:{PORT}/"
    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        print(f"Port {PORT} dah digunakan. Redactor mungkin dah jalan dalam window lain:")
        print(f"  {url}")
        print("Tutup window tu dulu kalau nak restart dengan code terbaru.")
        sys.exit(1)
    print(f"Maybank Redactor running at {url}  (Ctrl+C to stop)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

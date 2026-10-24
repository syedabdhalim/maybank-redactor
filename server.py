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

# Layout of the Maybank statement, in PDF points (page is 612 wide).
TABLE_MARGIN = 24.0   # transaction table inset from the page edge
HEADER_BOTTOM = 90    # bottom of the title bar on page 1
POSTING_MAX_X = 100   # posting date column ends before this
DESC_X = 160          # description column starts here
LOCATION_X = 395      # location sub-column inside the description column
AMOUNT_MIN_X = 480    # amount column starts after this

# fitz is not thread-safe; serialise every call into it.
FITZ_LOCK = threading.Lock()
DOCS: "OrderedDict[str, dict]" = OrderedDict()


class NeedsPassword(Exception):
    pass


def open_pdf(data: bytes, password: str = "") -> fitz.Document:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise ValueError("Not a valid PDF file.") from None
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
    """One box per run of adjacent words (date, description, location, amount...).
    Returns (box, word rects inside it) pairs; boxes are fitted later by fit_box."""
    segments = []
    for i in range(first, last + 1):
        run, own, prev_x1 = None, [], None
        for r, _ in lines[i]["words"]:
            if run is not None and r.x0 - prev_x1 <= gap:
                run |= r
                own.append(r)
            else:
                if run is not None:
                    segments.append((run, own))
                run, own = fitz.Rect(r), [r]
            prev_x1 = r.x1
        if run is not None:
            segments.append((run, own))
    return [(fitz.Rect(b.x0 - 1.5, b.y0 - 0.5, b.x1 + 1.5, b.y1 + 0.5), own) for b, own in segments]


def word_box(rect, px=0.8, py=0.5):
    return fitz.Rect(rect.x0 - px, rect.y0 - py, rect.x1 + px, rect.y1 + py)


def fit_box(box, own, page_words):
    """Shrink a redaction box vertically so it touches no word outside `own`.

    MuPDF removes every glyph whose box touches a redaction rect. On tightly spaced
    lines the glyph boxes of neighbouring rows overlap, so a box sized to its own
    words would also wipe the row above or below. Text under the shrunk box is still
    removed in full, since touching is enough.
    """
    box = fitz.Rect(box)
    mid = (box.y0 + box.y1) / 2
    for w in page_words:
        if any(w == o for o in own):
            continue
        if w.x0 >= box.x1 or w.x1 <= box.x0 or w.y0 >= box.y1 or w.y1 <= box.y0:
            continue
        if (w.y0 + w.y1) / 2 > mid:
            box.y1 = min(box.y1, w.y0 - 0.2)
        else:
            box.y0 = max(box.y0, w.y1 + 0.2)
    if box.y1 - box.y0 < 1.0:  # neighbours closer than this can't be separated; keep a sliver
        box.y0, box.y1 = mid - 0.5, mid + 0.5
    return box


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
    warnings = []
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
        warnings.append("Statement date not found, so transaction years are guessed from today's date.")

    txns, fields, cards = [], [], []
    current_card = None

    def add_field(category, pno, rect, text, own):
        fields.append({"category": category, "page": pno, "rect": rect, "text": text, "own": own})

    for pno, page in enumerate(doc):
        lines = page_lines(page)
        page_words = [r for ln in lines for r, _ in ln["words"]]
        first_field, first_txn = len(fields), len(txns)
        width = page.rect.width
        left, right = TABLE_MARGIN, width - TABLE_MARGIN
        for ln in lines:
            if ln["words"][0][1] == "Posting":
                left = ln["words"][0][0].x0 - 3

        # Card numbers anywhere: mask all groups except the last.
        for ln in lines:
            for run in card_runs(ln["words"]):
                for r, t in run[:-1]:
                    add_field("card", pno, word_box(r), t, [r])

        # Page 1 header: name/address block and the summary table figures.
        if pno == 0:
            summary_top = next((ln["rect"].y0 for ln in lines if ln["text"].startswith("Account Number")), None)
            warning_top = next((ln["rect"].y0 for ln in lines if ln["text"].startswith("WARNING")), None)
            for ln in lines:
                y = ln["rect"].y0
                # Mailing block: left half of the page, between the title bar and the summary table.
                if summary_top and HEADER_BOTTOM < y < summary_top - 30:
                    left_words = [(r, t) for r, t in ln["words"] if r.x0 < width / 2]
                    if left_words:
                        box = fitz.Rect(left_words[0][0])
                        for r, _ in left_words:
                            box |= r
                        add_field("identity", pno, word_box(box), " ".join(t for _, t in left_words),
                                  [r for r, _ in left_words])
                if summary_top and warning_top and summary_top + 5 < y < warning_top:
                    for r, t in ln["words"]:
                        if MONEY_RE.match(t) and r.x0 > DESC_X:
                            add_field("balances", pno, word_box(r), t, [r])

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
                    current_card = {"id": len(cards), "name": name, "last4": digits[-4:], "page": pno,
                                    "totals": {}}
                    cards.append(current_card)
                    if i > 0 and ln["rect"].y0 - lines[i - 1]["rect"].y1 < 8:
                        prev = lines[i - 1]
                        add_field("identity", pno, word_box(prev["rect"]), prev["text"],
                                  [r for r, _ in prev["words"]])

            for label in BALANCE_LABELS:
                if text.startswith(label):
                    for r, t in words:
                        if NUMBER_RE.match(t) and any(ch.isdigit() for ch in t) and r.x0 > page.rect.width / 2:
                            add_field("balances", pno, word_box(r), t, [r])
                            if current_card and label.startswith("TOTAL"):
                                kind = "credit" if "CREDIT" in label else "debit"
                                current_card["totals"][kind] = float(t.replace(",", ""))

            is_txn = (len(words) >= 3 and DATE_RE.match(words[0][1]) and DATE_RE.match(words[1][1])
                      and words[0][0].x0 < POSTING_MAX_X)
            if not is_txn:
                if DATE_RE.match(words[0][1]) and words[0][0].x0 < DESC_X:
                    warnings.append(f"Page {pno + 1}: a dated line was not recognised as a transaction "
                                    f"and will not be redacted. Check the preview.")
                i += 1
                continue

            amount_word = words[-1] if MONEY_RE.match(words[-1][1]) and words[-1][0].x0 > AMOUNT_MIN_X else None
            body = words[2:-1] if amount_word else words[2:]
            desc = " ".join(t for r, t in body if r.x0 < LOCATION_X)
            location = " ".join(t for r, t in body if r.x0 >= LOCATION_X)

            # Continuation lines (e.g. "TRANSACTED AMOUNT USD 19.99") belong to this row.
            last = i
            extra = []
            while last + 1 < len(lines):
                nxt = lines[last + 1]
                nw = nxt["words"]
                if (DATE_RE.match(nw[0][1]) or nw[0][0].x0 < DESC_X - 10
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

        # Fit every box found on this page so none of them reaches into a neighbouring line.
        for f in fields[first_field:]:
            f["rect"] = fit_box(f["rect"], f.pop("own"), page_words)
        for t in txns[first_txn:]:
            t["segments"] = [fit_box(box, own, page_words) for box, own in t["segments"]]

    warnings += reconcile(cards, txns)
    return {
        "statement_date": stmt.isoformat(),
        "cards": cards,
        "transactions": txns,
        "fields": fields,
        "warnings": warnings,
        "pages": [{"w": p.rect.width, "h": p.rect.height} for p in doc],
    }


def reconcile(cards, txns):
    """Cross-check parsed rows against the totals printed on the statement.

    The parser only redacts rows it recognises. If a row was missed, the sums here
    won't match the statement's own TOTAL DEBIT / TOTAL CREDIT, and the user is told
    to check the preview before sending the file anywhere.
    """
    warnings = []
    for c in cards:
        label = f"{c['name']} ..{c['last4']}"
        rows = [t for t in txns if t["card"] == c["id"] and t["amount"] is not None]
        parsed = {
            "debit": round(sum(t["amount"] for t in rows if not t["credit"]), 2),
            "credit": round(sum(t["amount"] for t in rows if t["credit"]), 2),
        }
        for kind in ("debit", "credit"):
            printed = c["totals"].get(kind)
            if printed is None:
                warnings.append(f"{label}: total {kind} not found on the statement, so rows could not be cross-checked.")
            elif abs(printed - parsed[kind]) > 0.005:
                warnings.append(f"{label}: recognised {kind}s add up to {parsed[kind]:,.2f} but the statement says "
                                f"{printed:,.2f}. Some rows may not have been recognised; check the preview.")
    return warnings


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
        raise ValueError("Invalid page range.") from None
    lo, hi = max(1, lo), min(n, hi)
    if lo > hi:
        raise ValueError("Invalid page range: start page is after end page.")
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
        raise ValueError("No pages left to export in this range.")
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


def word_key(w):
    return (w[4], round(w[0], 1), round(w[1], 1))


def redact(entry, keep_ids, opts, highlight_ids=()):
    parsed = entry["parsed"]
    plan = build_plan(parsed, keep_ids, opts, highlight_ids)
    doc = open_pdf(entry["data"], entry["password"])
    try:
        by_page = {}
        for b in plan["boxes"]:
            by_page.setdefault(b["page"], []).append(b["rect"])
        keep_pages = [p for p in range(doc.page_count) if p not in plan["dropped"]]

        # Everything on an exported page that no box touches must come through intact.
        expected = {}
        for pno in keep_pages:
            rects = by_page.get(pno, [])
            expected[pno] = [w for w in doc[pno].get_text("words")
                             if not any(fitz.Rect(w[:4]).intersects(r) for r in rects)]

        for pno, rects in by_page.items():
            page = doc[pno]
            for r in rects:
                page.add_redact_annot(r, fill=(0, 0, 0))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE)

        # Marker drawn underneath the page content (rows have no background fill),
        # so the text stays crisp black on the marker colour.
        fill = highlight_color(opts.get("hl_color"))
        for h in plan["highlights"]:
            doc[h["page"]].draw_rect(h["rect"], color=None, fill=fill, width=0, overlay=False)

        if len(keep_pages) < doc.page_count:
            doc.select(keep_pages)

        # Copy only the page content into a fresh file. This leaves behind annotations,
        # form fields, links, bookmarks, the tagged-PDF structure tree, metadata and the
        # original file ID, none of which apply_redactions touches.
        clean = fitz.open()
        try:
            clean.insert_pdf(doc, links=False, annots=False, widgets=False)
            clean.set_metadata({})
            out = clean.tobytes(garbage=4, deflate=True, clean=True)
        finally:
            clean.close()
    finally:
        doc.close()

    # Re-open the final bytes and check both directions: nothing left under a box,
    # and nothing outside the boxes lost.
    check = fitz.open(stream=out, filetype="pdf")
    try:
        new_index = {old: new for new, old in enumerate(keep_pages)}
        leaks, damaged = [], []
        for pno in keep_pages:
            page = check[new_index[pno]]
            leaks += leaked_words(page, by_page.get(pno, []))
            if page.first_annot:
                leaks.append("<annotation>")
            present = {word_key(w) for w in page.get_text("words")}
            damaged += [w[4] for w in expected[pno] if word_key(w) not in present]
        if check.page_count != len(keep_pages):
            leaks.append("<page count mismatch>")
    finally:
        check.close()
    if leaks:
        raise RuntimeError(f"Check failed, {len(leaks)} item(s) still present under redaction: {leaks[:5]}")

    warnings = []
    if damaged:
        sample = ", ".join(damaged[:4])
        warnings.append(f"{len(damaged)} word(s) that should have been kept are missing ({sample}). "
                        "Open the PDF and check it, or try the Full-row blocks style.")
    keep = set(keep_ids) | set(highlight_ids)
    report = {
        "kept": sum(1 for t in parsed["transactions"] if t["id"] in keep and t["page"] in new_index),
        "highlighted": len(plan["highlights"]),
        "redacted_rows": len({b["txn"] for b in plan["boxes"] if b["kind"] == "txn"}),
        "pages_out": len(keep_pages),
        "pages_dropped": [p + 1 for p in plan["dropped"]],
        "warnings": warnings,
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
        "cards": parsed["cards"],
        "pages": parsed["pages"],
        "transactions": [conv(t) for t in parsed["transactions"]],
        "warnings": parsed["warnings"],
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
                return self.send_json(404, {"error": "Document not found. Please upload it again."})
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
                                                    "error": "This PDF is password-protected." if password == ""
                                                    else "Wrong password."})
                    try:
                        parsed = parse_statement(doc)
                    finally:
                        doc.close()
                if not parsed["transactions"]:
                    return self.send_json(422, {"error": "No transactions found. Is this a Maybank credit card statement?"})
                doc_id = uuid.uuid4().hex
                DOCS[doc_id] = {"data": data, "password": password, "parsed": parsed, "cache": {}}
                while len(DOCS) > MAX_DOCS:
                    DOCS.popitem(last=False)
                return self.send_json(200, {"id": doc_id, **public_view(parsed)})

            if url.path in ("/api/plan", "/api/redact"):
                req = json.loads(self.read_body() or b"{}")
                entry = self.get_entry(req.get("id", ""))
                if not entry:
                    return self.send_json(404, {"error": "Document not found. Please upload it again."})
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
        print(f"Port {PORT} is already in use. The redactor may already be running in another window:")
        print(f"  {url}")
        print("Close that window first if you want to restart with the latest code.")
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

"""Generate a fake Maybank-style credit card statement for demos and testing.

Everything in it is made up: name, address, card numbers and transactions.
The layout mirrors the real statement closely enough for server.py to parse.

    python demo/make_sample.py            -> demo/sample-statement.pdf
"""

import sys
from pathlib import Path

import fitz

W, H = 612, 887
YELLOW = (1.0, 0.78, 0.17)
FONT, BOLD = "helv", "hebo"
COLS = (23, 82, 160, 485, 588)  # table column edges
ROW = 14.5

CARD_A = ("SAMPLE PLATINUM MASTERCARD", "4000 1234 5678 9010")
CARD_B = ("SAMPLE GOLD AMEX", "3700 123456 78901")

# (posting, transaction, description, location, amount, extra line)
TXNS_A = [
    ("09/03", "08/03", "PETRONAS JLN DEMO", "KUALA LUMPUR  MY", "80.00", None),
    ("10/03", "09/03", "MYDIN MALL CONTOH", "SHAH ALAM     MY", "64.35", None),
    ("11/03", "11/03", "GRAB RIDES-EC", "PETALING JAYA MY", "18.00", None),
    ("12/03", "11/03", "UNIFI HOME FIBRE", "KUALA LUMPUR  MY", "129.00", None),
    ("13/03", "12/03", "SPOTIFY        M4", "STOCKHOLM     SE", "16.90", None),
    ("14/03", "13/03", "STARBUCKS BANGSAR", "KUALA LUMPUR  MY", "21.50", None),
    ("15/03", "14/03", "TNB ELECTRICITY BILL", "KUALA LUMPUR  MY", "142.80", None),
    ("16/03", "15/03", "SHOPEE MY MARKETPLACE", "KUALA LUMPUR  MY", "57.90", None),
    ("17/03", "16/03", "KFC TAMAN CONTOH", "KUALA LUMPUR  MY", "32.40", None),
    ("18/03", "17/03", "TOUCH N GO RELOAD", "KUALA LUMPUR  MY", "50.00", None),
    ("19/03", "18/03", "AEON BIG DEMO MALL", "CHERAS        MY", "211.65", None),
    ("20/03", "19/03", "CLINIC SIHAT SDN BHD", "KUALA LUMPUR  MY", "95.00", None),
    ("21/03", "20/03", "FARMASI CONTOH", "KUALA LUMPUR  MY", "38.70", None),
    ("22/03", "21/03", "SHELL JLN SAMPLE", "SEREMBAN      MY", "70.00", None),
    ("23/03", "22/03", "PARKING DEMO TOWER", "KUALA LUMPUR  MY", "12.00", None),
    ("24/03", "23/03", "GITHUB INC     M4", "SAN FRANCISCO US", "18.77", "TRANSACTED AMOUNT    USD    4.00"),
    ("25/03", "25/03", "PYMT@MAYBANK2U.COM", "", "1,250.00CR", None),
    ("26/03", "25/03", "HOTEL CONTOH IPOH", "IPOH          MY", "268.00", None),
    ("26/03", "25/03", "PLUS TOLL RELOAD", "IPOH          MY", "40.00", None),
    ("27/03", "26/03", "NASI KANDAR DEMO", "IPOH          MY", "27.60", None),
    ("28/03", "27/03", "PETRONAS TAPAH", "TAPAH         MY", "75.00", None),
    ("29/03", "28/03", "GRAB FOOD", "PETALING JAYA MY", "34.20", None),
    ("30/03", "29/03", "IKEA DEMO RESTAURANT", "KUALA LUMPUR  MY", "45.30", None),
    ("31/03", "30/03", "COWAY MALAYSIA  M4", "KUALA LUMPUR  MY", "75.00", None),
    ("01/04", "31/03", "MR DIY CONTOH", "KUALA LUMPUR  MY", "19.90", None),
    ("02/04", "01/04", "AIR SELANGOR BILL", "SHAH ALAM     MY", "48.20", None),
    ("03/04", "02/04", "LAZADA MY", "KUALA LUMPUR  MY", "88.00", None),
    ("04/04", "03/04", "GSC CINEMA DEMO", "KUALA LUMPUR  MY", "36.00", None),
    ("05/04", "04/04", "TEALIVE SAMPLE", "KUALA LUMPUR  MY", "9.90", None),
    ("06/04", "05/04", "PETRONAS JLN DEMO", "KUALA LUMPUR  MY", "60.00", None),
    ("07/04", "06/04", "WATSONS CONTOH", "KUALA LUMPUR  MY", "42.15", None),
    ("08/04", "07/04", "GRAB RIDES-EC", "PETALING JAYA MY", "22.00", None),
]
TXNS_B = [
    ("12/03", "11/03", "ZUS COFFEE DEMO", "KUALA LUMPUR  MY", "13.90", None),
    ("20/03", "19/03", "CALTEX JLN SAMPLE", "KUANTAN       MY", "65.00", None),
    ("26/03", "26/03", "PYMT@MAYBANK2U.COM", "", "88.40CR", None),
    ("01/04", "31/03", "CASH REBATE", "", "4.20CR", None),
]


def text(page, x, y, s, size=8.0, font=FONT, color=(0, 0, 0)):
    page.insert_text((x, y), s, fontsize=size, fontname=font, color=color)


def right(page, x1, y, s, size=8.0, font=FONT):
    text(page, x1 - fitz.get_text_length(s, fontname=font, fontsize=size), y, s, size, font)


def header(page, n, total):
    page.draw_rect(fitz.Rect(23, 21, 588, 50), color=None, fill=YELLOW)
    text(page, 30, 40, "SAMPLE BANK", 13, BOLD)
    text(page, 181, 37, "STATEMENT OF CREDIT CARD ACCOUNT", 10, BOLD)
    text(page, 181, 48, "PENYATA AKAUN KAD KREDIT", 9, BOLD)
    text(page, 25, 61, "SAMPLE DATA - NOT A REAL STATEMENT", 7, BOLD, color=(0.75, 0.1, 0.1))
    page.draw_rect(fitz.Rect(380, 68, 588, 88), color=(0, 0, 0), width=0.6)
    text(page, 405, 80, "Page/Halaman")
    text(page, 527, 80, f"{n:03d} OF {total:03d}")


def table(page, top, bottom):
    """Column header bar plus outer border and column rules."""
    page.draw_rect(fitz.Rect(COLS[0], top, COLS[-1], top + 26), color=None, fill=(0, 0, 0))
    labels = [("Posting Date /", "Tarikh Pos"), ("Transaction Date /", "Tarikh Transaksi"),
              ("Transaction Description /", "Huraian Transaksi"), ("Amount(RM) /", "Amaun(RM)")]
    for (a, b), x0, x1 in zip(labels, COLS[:-1], COLS[1:]):
        mid = (x0 + x1) / 2
        text(page, mid - fitz.get_text_length(a, FONT, 7) / 2, top + 11, a, 7, color=(1, 1, 1))
        text(page, mid - fitz.get_text_length(b, FONT, 7) / 2, top + 20, b, 7, color=(1, 1, 1))
    page.draw_rect(fitz.Rect(COLS[0], top, COLS[-1], bottom), color=(0, 0, 0), width=0.6)
    for x in COLS[1:-1]:
        page.draw_line((x, top + 26), (x, bottom), color=(0, 0, 0), width=0.6)


def card_block(page, y, card, limit, prev):
    name, number = card
    text(page, 166, y, "AHMAD FAIZAL", 8, BOLD)
    text(page, 166, y + 13, f"{name}  :  {number}", 8, BOLD)
    text(page, 166, y + 26, name, 8, BOLD)
    text(page, 166, y + 47, "YOUR COMBINED CREDIT LIMIT (RM)", 8, BOLD)
    text(page, 404, y + 47, limit, 8, BOLD)
    text(page, 166, y + 59, "KOMBINASI HAD KREDIT ANDA (RM)", 6)
    text(page, 166, y + 81, "YOUR PREVIOUS STATEMENT BALANCE", 8, BOLD)
    right(page, 575, y + 81, prev, 8, BOLD)
    text(page, 166, y + 93, "JUMLAH PENYATA TERAKHIR", 6)
    return y + 115


def rows(page, y, txns):
    for post, txn, desc, loc, amt, extra in txns:
        text(page, 42, y, post)
        text(page, 113, y, txn)
        text(page, 166, y, desc)
        if loc:
            text(page, 404, y, loc)
        right(page, 575, y, amt)
        y += ROW
        if extra:
            text(page, 166, y, extra)
            y += ROW
    return y


def totals(page, y, credit, debit, sub):
    text(page, 166, y, "TOTAL CREDIT THIS MONTH", 8, BOLD)
    text(page, 404, y, credit, 8, BOLD)
    text(page, 166, y + 8, "(JUMLAH KREDIT)", 6)
    text(page, 166, y + 21, "TOTAL DEBIT THIS MONTH", 8, BOLD)
    text(page, 404, y + 21, debit, 8, BOLD)
    text(page, 166, y + 29, "(JUMLAH DEBIT)", 6)
    right(page, 481, y + 52, "SUB TOTAL/JUMLAH")
    right(page, 575, y + 52, sub)
    return y + 75


def build(out: Path):
    doc = fitz.open()
    total_pages = 2

    # ----- page 1 -----
    p = doc.new_page(width=W, height=H)
    header(p, 1, total_pages)
    for i, line in enumerate(["ENCIK AHMAD FAIZAL BIN ROSLAN", "12-3A, Residensi Contoh",
                              "Jalan Demo 1/2, Taman Sampel", "Kuala Lumpur"]):
        text(p, 25, 104 + i * 8, line, 7)
    p.draw_rect(fitz.Rect(380, 96, 588, 140), color=(0, 0, 0), width=0.6)
    p.draw_line((484, 96), (484, 140), color=(0, 0, 0), width=0.6)
    text(p, 401, 107, "Statement Date/", 7)
    text(p, 405, 115, "Tarikh Penyata", 7)
    text(p, 501, 107, "Payment Due Date/", 7)
    text(p, 492, 115, "Tarikh Akhir Pembayaran", 7)
    text(p, 414, 133, "08 APR 26", 8, BOLD)
    text(p, 521, 132, "28 APR 26", 8, BOLD)

    p.draw_rect(fitz.Rect(23, 214, 588, 300), color=(0, 0, 0), width=0.6)
    p.draw_rect(fitz.Rect(23, 214, 588, 228), color=None, fill=YELLOW)
    for x, label in [(36, "Account Number/Nombor Akaun"), (182, "Current Balance/Baki Semasa"),
                     (313, "Minimum Payment/Bayaran Minima"), (455, "Amount To Be Paid/Amaun dibayar")]:
        text(p, x, 223, label, 7)
    text(p, 52, 238, CARD_A[1])
    right(p, 287, 238, "1,880.62")
    right(p, 439, 238, "94.03")
    text(p, 52, 246, CARD_B[1])
    right(p, 287, 246, "74.70")
    right(p, 439, 246, "25.00")

    p.draw_rect(fitz.Rect(23, 312, 588, 460), color=(0, 0, 0), fill=(1, 0.93, 0.6), width=0.6)
    text(p, 141, 327, "WARNING ON PAYING ONLY MINIMUM MONTHLY REPAYMENT /", 10, BOLD)
    text(p, 48, 358, "If you make only the minimum payment each period, you will pay more in interest", 9)
    text(p, 48, 370, "and it will take you longer to pay off your balance.", 9)

    table(p, 470, 845)
    y = card_block(p, 520, CARD_A, "10,000", "1,180.40")
    rows(p, y, TXNS_A[:14])

    # ----- page 2 -----
    p = doc.new_page(width=W, height=H)
    header(p, 2, total_pages)
    table(p, 95, 845)
    y = rows(p, 131, TXNS_A[14:])
    y = totals(p, y + 8, "1,250.00", "1,950.22", "1,880.62")
    y = card_block(p, y + 10, CARD_B, "10,000", "88.40")
    y = rows(p, y, TXNS_B)
    totals(p, y + 8, "92.60", "78.90", "74.70")

    doc.set_metadata({"title": "Sample statement (fake data)"})
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out, garbage=4, deflate=True)
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("sample-statement.pdf")
    print(build(target))

#!/usr/bin/env python
"""Generate the berthcast test fixture: a realistic SEA food-distribution upload.

Writes four CSVs into this folder (inventory / sales / suppliers / purchase
orders) shaped exactly like a real client upload, so they can be (a) uploaded
to the site by hand for a smoke test, or (b) fed to tests/test_dummy_data_fixture.py
to drive the whole pipeline offline.

The catalogue is ~130 items: ~120 ordinary filler SKUs (mostly healthy) plus a
handful of PLANTED items whose numbers are hand-picked to exercise one pipeline
branch each — name drift, dead SKU, missing-sales, split rows, total rows,
thousands-separated money, lead-time-aware thresholds. The planted names are
listed in PLANTED below and asserted on by the test.

Deterministic (seeded), so the same files come out every run.

    python fixtures/generate_dummy_data.py
"""
import csv
import os
import random

HERE = os.path.dirname(os.path.abspath(__file__))

# Sales cover exactly these three months (DD/MM/YYYY — Singapore's own format),
# so the date parser reports "3 months" and velocity = total / 3.
MONTH_DATES = ["15/04/2026", "15/05/2026", "15/06/2026"]
N_MONTHS = len(MONTH_DATES)

CUSTOMERS = ["FairPrice", "Sheng Siong", "Cold Storage", "Giant", "Prime Mart",
             "RedMart", "Hao Mart", "U Stars"]

# (template, category, uom). Brand x template gives the filler catalogue.
TEMPLATES = [
    ("UHT Milk 1L", "DAIRY", "CTN"), ("Salted Butter 227g", "DAIRY", "CTN"),
    ("Cheddar Slices 250g", "DAIRY", "CTN"), ("Drinking Yoghurt 700g", "DAIRY", "CTN"),
    ("Instant Coffee 200g", "BEVERAGE", "CTN"), ("Chocolate Malt 1kg", "BEVERAGE", "CTN"),
    ("Orange Juice 1L", "BEVERAGE", "CTN"), ("Jasmine Rice 5kg", "DRY GOODS", "BAG"),
    ("Plain Flour 1kg", "DRY GOODS", "BAG"), ("Fine Sugar 1kg", "DRY GOODS", "BAG"),
    ("Instant Noodles 5s", "DRY GOODS", "CTN"), ("Potato Chips 160g", "SNACKS", "CTN"),
    ("Cream Crackers 400g", "SNACKS", "CTN"), ("Light Soy Sauce 640ml", "CONDIMENTS", "CTN"),
    ("Oyster Sauce 510g", "CONDIMENTS", "CTN"), ("Chilli Sauce 320g", "CONDIMENTS", "CTN"),
    ("Frozen Dumplings 400g", "FROZEN", "CTN"), ("Frozen Fries 1kg", "FROZEN", "CTN"),
    ("Canned Tuna 150g", "CANNED", "CTN"), ("Canned Sardine 215g", "CANNED", "CTN"),
    ("Wholemeal Bread 400g", "BAKERY", "PKT"),
]
BRANDS = ["Cowhead", "Emborg", "Marigold", "Nestle", "Ayam Brand", "Maggi",
          "Yeo's", "F&N", "Khong Guan", "Julie's", "Prima", "Golden Churn",
          "President", "Kewpie", "Lee Kum Kee", "Lotus", "Meadow Fresh",
          "SunRice", "Knife", "Naturel"]

N_FILLER = 120


def _norm(s):
    """Casefold + keep alnum only — same idea as the app's match key. Used here
    only to stop a filler item colliding with a planted name."""
    return "".join(ch for ch in s.lower() if ch.isalnum())


# ── Planted items: name -> the case it proves. inv rows + sales rows are built
# below. Numbers chosen against N_MONTHS=3 so the verdict is unambiguous. ──────
PLANTED_INV = [
    # name, category, uom, stock  (a list value => the item appears on >1 row)
    ("COWHEAD UHT MILK FULL CREAM 1L", "DAIRY", "CTN", "0"),       # OOS + sales => CRITICAL (name-drift sales)
    ("EMBORG MATURE CHEDDAR BLOCK 200G", "DAIRY", "CTN", "150"),   # ms 1.25 => LOW
    ("SUNRICE PREMIUM JASMINE RICE 5KG", "DRY GOODS", "BAG", "800"),  # ms 8 => HEALTHY
    ("RETIRED PRALINE SPREAD 250G", "SPREADS", "CTN", "40"),       # has sales, sold 0 => DEAD
    ("NEW LAUNCH OAT MILK BARISTA 1L", "DAIRY", "CTN", "60"),      # no sales, stocked => HEALTHY (never DEAD)
    ("DISCONTINUED USB GADGET", "MISC", "PCS", "0"),               # no sales, 0 stock => DEAD
    ("MILO ACTIV-GO POWDER REFILL 1.8KG", "BEVERAGE", "CTN", "300"),  # thousands money parses
]
# ANCHOR butter appears as TWO warehouse rows, same unit -> agent sums to 250.
PLANTED_SPLIT = ("ANCHOR PROFESSIONAL UNSALTED BUTTER 5KG", "DAIRY", "CTN", ["100", "150"])
# A summary line that must be dropped, never analysed as a product.
PLANTED_TOTAL = ("Grand Total", "", "", "9999")

PLANTED_NAMES = ([n for n, *_ in PLANTED_INV]
                 + [PLANTED_SPLIT[0], PLANTED_TOTAL[0]])

# Planted sales: name -> per-month qty (one row per entry in MONTH_DATES unless
# noted). Supplier filled only where it should flow through.
PLANTED_SALES = {
    # name (as it appears in the SALES sheet)             qty/mo, price, supplier
    "COWHEAD UHT MILK FULL CREAM 1L  ← out of stock": (300, 1.85, "Global Dairy Imports"),  # drift annotation
    "EMBORG MATURE CHEDDAR BLOCK 200G":                    (120, 4.20, ""),
    "SUNRICE PREMIUM JASMINE RICE 5KG":                    (100, 12.50, "SunRice Trading"),
    "ANCHOR PROFESSIONAL UNSALTED BUTTER 5KG":             (50, 38.00, ""),
    "MILO ACTIV-GO POWDER REFILL 1.8KG":                   (80, 15.50, ""),  # net amount written "1,240.00"
    "RETIRED PRALINE SPREAD 250G":                         (0, 6.00, ""),    # zero-qty rows => DEAD
}

SUPPLIERS = [
    ("Global Dairy Imports", "Import", "New Zealand"),
    ("Euro Cheese Importers", "Import", "Netherlands"),
    ("SunRice Trading", "Local", "Singapore"),
    ("Nestle Distribution SG", "Local", "Singapore"),
    ("Yeo Hiap Seng", "Local", "Singapore"),
    ("Ayam Brand Asia", "Import", "Malaysia"),
    ("Khong Guan Flour", "Local", "Singapore"),
    ("Frozen Foods Co", "Local", "Singapore"),
]
# Item -> supplier links. COWHEAD (import) gives the lead-time-aware CRITICAL;
# SunRice (local) exercises the local lead time on a healthy item.
PURCHASE_ORDERS = [
    ("COWHEAD UHT MILK FULL CREAM 1L", "Global Dairy Imports", "1500"),
    ("SUNRICE PREMIUM JASMINE RICE 5KG", "SunRice Trading", "600"),
    ("MILO ACTIV-GO POWDER REFILL 1.8KG", "Nestle Distribution SG", "400"),
]


def _money(n):
    """Plain US/UK money string, e.g. 1240.0 -> '1240.00'."""
    return f"{n:.2f}"


def build():
    rng = random.Random(42)
    used = {_norm(n) for n in PLANTED_NAMES}

    inv_rows = []   # (name, category, uom, stock)
    sales_rows = []  # (date, name, qty, price, net, customer, supplier)

    # ── Filler catalogue ─────────────────────────────────────────────────────
    combos = [(b, t, c, u) for (t, c, u) in TEMPLATES for b in BRANDS]
    rng.shuffle(combos)
    n_low = 0
    for brand, tmpl, cat, uom in combos:
        if len(inv_rows) >= N_FILLER:
            break
        name = f"{brand} {tmpl}"
        if _norm(name) in used:
            continue
        used.add(_norm(name))

        avg = rng.choice([20, 30, 40, 50, 60, 80, 100, 120, 150, 200])
        # Most filler is healthy (>3 months cover); ~1 in 10 left tight for realism.
        if n_low < 10 and rng.random() < 0.12:
            months_cover = rng.choice([1, 2])      # LOW
            n_low += 1
        else:
            months_cover = rng.randint(4, 12)      # HEALTHY
        stock = avg * months_cover
        inv_rows.append((name, cat, uom, str(stock)))

        price = round(rng.uniform(2.0, 30.0), 2)
        for d in MONTH_DATES:
            sales_rows.append((d, name, str(avg), _money(price),
                               _money(avg * price), rng.choice(CUSTOMERS), ""))

    # ── Planted inventory ────────────────────────────────────────────────────
    for name, cat, uom, stock in PLANTED_INV:
        inv_rows.append((name, cat, uom, stock))
    a_name, a_cat, a_uom, a_stocks = PLANTED_SPLIT
    for s in a_stocks:                              # two warehouse rows
        inv_rows.append((a_name, a_cat, a_uom, s))
    inv_rows.append(PLANTED_TOTAL)                  # the Grand Total line

    # ── Planted sales ────────────────────────────────────────────────────────
    for sales_name, (qty, price, supplier) in PLANTED_SALES.items():
        for d in MONTH_DATES:
            net = _money(qty * price)
            if "MILO" in sales_name:               # force a thousands separator
                net = f"{qty * price:,.2f}"
            sales_rows.append((d, sales_name, str(qty), _money(price), net,
                               rng.choice(CUSTOMERS), supplier))

    _write("inventory.csv",
           ["Item Description", "Category", "UOM", "Qty On Hand"], inv_rows)
    _write("sales.csv",
           ["Date", "Item Description", "Qty", "Selling Price", "Net Amount",
            "Customer", "Supplier"], sales_rows)
    _write("suppliers.csv",
           ["Supplier Name", "Supplier Type", "Country"], SUPPLIERS)
    _write("purchase_orders.csv",
           ["PO Date", "Item Description", "Supplier Name", "Order Qty"],
           [("01/04/2026", it, sup, q) for (it, sup, q) in PURCHASE_ORDERS])

    return {"inventory": len(inv_rows), "sales": len(sales_rows),
            "suppliers": len(SUPPLIERS), "purchase_orders": len(PURCHASE_ORDERS)}


def _write(fname, header, rows):
    with open(os.path.join(HERE, fname), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


if __name__ == "__main__":
    counts = build()
    print("Wrote fixtures to", HERE)
    for k, v in counts.items():
        print(f"  {k:16} {v} rows")

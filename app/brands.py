"""Commission brands and the rules that decide whether a receipt line earns commission.

A brand has `aliases` (words that identify it on a receipt). Some brands only pay
commission on one line, so they also have `requires`: one of those words must
appear too. If the brand matches but the line word is missing, the item is saved
as no-commission and flagged so a person decides.
"""
import re
import unicodedata

SEED_BRANDS = [
    {"name": "Penhaligon's", "aliases": ["penhaligon", "penhaligons", "penh"]},
    {"name": "Byredo", "aliases": ["byredo"]},
    {"name": "Creed", "aliases": ["creed"]},
    {"name": "Parfums de Marly", "aliases": ["parfums de marly", "marly", "pdm"]},
    {"name": "Memo", "aliases": ["memo"]},
    {"name": "Initio", "aliases": ["initio"]},
    {"name": "Loewe", "aliases": ["loewe"]},
    {"name": "Bvlgari", "aliases": ["bvlgari", "bulgari", "bvl"]},
    {"name": "Maison Margiela Replica", "aliases": ["margiela", "replica", "mmm"], "requires": ["replica"]},
    {"name": "Dolce & Gabbana Velvet Collection", "aliases": ["dolce", "gabbana", "d&g", "dg"], "requires": ["velvet"]},
    {"name": "Hugo Boss Private Line", "aliases": ["boss", "hugo boss"], "requires": ["collection", "private"]},
    {"name": "Versace Atelier Collection", "aliases": ["versace"], "requires": ["atelier"]},
    {"name": "Kilian", "aliases": ["kilian"]},
    {"name": "Montale", "aliases": ["montale"]},
    {"name": "Tiziana Terenzi", "aliases": ["tiziana", "terenzi"]},
    {"name": "Mancera", "aliases": ["mancera"]},
    {"name": "Balmain", "aliases": ["balmain"]},
    {"name": "Brunello Cucinelli", "aliases": ["cucinelli", "brunello"]},
    {"name": "Chloé Atelier des Fleurs", "aliases": ["chloe"], "requires": ["atelier", "fleurs"]},
]


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation: "Chloé  ATELIER" -> "chloe atelier"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"[’']", "", text)
    return re.sub(r"[^a-z0-9&]+", " ", text).strip()


def _has_word(haystack: str, needle: str) -> bool:
    needle = normalize(needle)
    if not needle:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None


def match_brand(text: str, brands: list[dict]):
    """Return (brand, line_ok) for the first active brand named in `text`, else (None, False).

    line_ok is False when the brand needs a specific line and the text doesn't name it.
    Brands whose line requirement is satisfied win over ones where it is not.
    """
    hay = normalize(text)
    partial = None
    for b in brands:
        if not b.get("active", True):
            continue
        if not any(_has_word(hay, a) for a in b["aliases"] + [b["name"]]):
            continue
        if not b["requires"] or any(_has_word(hay, r) for r in b["requires"]):
            return b, True
        partial = partial or b
    return (partial, False) if partial else (None, False)


def classify(conn, brands: list[dict], description: str, barcode: str = "",
             ai_brand: str = "", ai_commission_brand: str = "", ai_sure: bool = False) -> dict:
    """Decide how one receipt line is tagged.

    Order of trust: a barcode saved before > the written rules > the AI's reading.
    Returns brand_id (None = no commission), brand_text, needs_review, hint.
    """
    by_id = {b["id"]: b for b in brands}
    barcode = (barcode or "").strip()

    if barcode:
        known = conn.execute("SELECT brand_id, brand_text FROM products WHERE barcode = ?", (barcode,)).fetchone()
        if known:
            b = by_id.get(known["brand_id"])
            return {"brand_id": b["id"] if b else None,
                    "brand_text": b["name"] if b else known["brand_text"],
                    "needs_review": False, "hint": "Tagged the same way as last time."}

    brand, line_ok = match_brand(f"{description} {ai_brand}", brands)
    if brand and line_ok:
        return {"brand_id": brand["id"], "brand_text": brand["name"], "needs_review": False, "hint": ""}
    if brand:
        return {"brand_id": None, "brand_text": ai_brand or brand["aliases"][0].title(), "needs_review": True,
                "hint": f"Commission is only paid on {brand['name']}. "
                        f"Pick it below if this product is from that line.",
                "suggest_brand_id": brand["id"]}

    # The rules found nothing; the AI may have recognised an abbreviation.
    ai_match = next((b for b in brands if b.get("active", True)
                     and normalize(b["name"]) == normalize(ai_commission_brand)), None)
    if ai_match:
        needs = bool(ai_match["requires"]) or not ai_sure
        return {"brand_id": ai_match["id"], "brand_text": ai_match["name"], "needs_review": needs,
                "hint": "Brand read from an abbreviation. Check it's right." if needs else ""}

    return {"brand_id": None, "brand_text": ai_brand or "", "needs_review": False, "hint": ""}

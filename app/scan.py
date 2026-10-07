"""Read a receipt photo with the Anthropic API and return structured fields."""
import base64
import io
import os

import httpx
from PIL import Image, ImageOps

API_URL = "https://api.anthropic.com/v1/messages"
MAX_EDGE = 2000


class ScanUnavailable(Exception):
    """Scanning can't run right now; the message is safe to show the user."""


def prepare_image(raw: bytes) -> bytes:
    """Fix phone rotation, shrink, and re-encode as JPEG."""
    try:
        img = Image.open(io.BytesIO(raw))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:
        raise ValueError("That file isn't a photo this app can open. Try a JPEG or PNG.") from exc
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def _tool(brand_names: list[str]) -> dict:
    text = {"type": "string"}
    return {
        "name": "record_receipt",
        "description": "Record every field read from the duty free receipt photo.",
        "input_schema": {
            "type": "object",
            "properties": {
                "receipt_no": {**text, "description": "The 'Transaction Seq. No.' value."},
                "terminal": {**text, "description": "The 'Terminal Id' value."},
                "store_ref": {**text, "description": "The code after 'Sales/Ventes:'."},
                "sale_date": {**text, "description": "Date of sale as YYYY-MM-DD, from the POS line near the bottom."},
                "sale_time": {**text, "description": "Time of sale as HH:MM (24h)."},
                "dep_date": {**text, "description": "Departure date as YYYY-MM-DD."},
                "destination": text,
                "final_dest": text,
                "flight_no": text,
                "passenger_name": text,
                "served_by": {**text, "description": "The 'Served by' staff number."},
                "payment_method": {**text, "description": "e.g. AMEX, VISA, CASH. Never the card number."},
                "auth_no": {**text, "description": "The 'Authorization No.' value."},
                "printed_total": {"type": "number", "description": "The 'Total in: CAD' amount."},
                "club_avolta": {"type": "boolean",
                                "description": "True only if the receipt shows a Club Avolta / loyalty 5% discount."},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "barcode": text,
                            "description": {**text, "description": "Product name and size, e.g. 'MEMO ITALIAN LEATHER 75ML'."},
                            "brand": {**text, "description": "Full brand name as best you can tell, for any brand."},
                            "commission_brand": {
                                "type": ["string", "null"],
                                "enum": brand_names + [None],
                                "description": "The exact list entry this product belongs to, or null if none.",
                            },
                            "sure": {"type": "boolean", "description": "False if the brand or line is a guess."},
                            "qty": {"type": "integer"},
                            "list_price": {"type": "number", "description": "Line price before any discount."},
                            "paid_price": {"type": "number", "description": "Line price after discount (same as list_price if none)."},
                        },
                        "required": ["description", "list_price", "paid_price"],
                    },
                },
                "unreadable": {"type": "array", "items": text,
                               "description": "Names of fields that were blurry, cut off or missing."},
            },
            "required": ["receipt_no", "sale_date", "items"],
        },
    }


PROMPT = """This is a photo of a paper receipt from a duty free shop at Toronto Pearson airport.
Read it and call record_receipt with what is printed.

Rules:
- Copy values exactly as printed. If something is blurry, cut off or missing, leave it empty and
  list the field in `unreadable`. Do not guess numbers.
- Dates on these receipts are day first: "06/10/2026" and "06.10.26" both mean 6 October 2026.
- Each product is a barcode line with a price, followed by description lines. Merge the description
  lines into one short description without repeating the same words.
- Leave out lines priced 0.00 that are bags or packaging (for example "Cash n Carry" bags).
- Prices are in CAD and there is no tax. If a discount is printed, list_price is the price before it
  and paid_price is the price after it.
- Never return any part of a card number.
- commission_brand must be one of these exact names, or null. Several entries cover only one
  specific line of a brand, so use them only when the product is from that line:
{brands}
"""


def scan(image_jpeg: bytes, brands: list[dict]) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise ScanUnavailable("Photo reading isn't switched on yet: the ANTHROPIC_API_KEY setting is missing. "
                              "The photo is attached, so you can type the details in.")
    names = [b["name"] for b in brands if b.get("active", True)]
    body = {
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        "max_tokens": 2000,
        "tools": [_tool(names)],
        "tool_choice": {"type": "tool", "name": "record_receipt"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.b64encode(image_jpeg).decode()}},
                {"type": "text", "text": PROMPT.format(brands="\n".join(f"  - {n}" for n in names))},
            ],
        }],
    }
    try:
        resp = httpx.post(API_URL, json=body, timeout=90, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    except httpx.HTTPError as exc:
        raise ScanUnavailable("Couldn't reach the photo reader. The photo is attached, so you can type the "
                              "details in or try again.") from exc
    if resp.status_code == 401:
        raise ScanUnavailable("The API key was rejected. Check ANTHROPIC_API_KEY in Railway.")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise ScanUnavailable(f"The photo reader returned an error ({resp.status_code}). {detail}".strip())
    for block in resp.json().get("content", []):
        if block.get("type") == "tool_use":
            return block.get("input", {})
    raise ScanUnavailable("The photo reader didn't return any receipt details. Try a clearer photo.")

"""End-to-end checks using the real Memo receipt from 6 Oct 2026 as the fixture."""
import io
import os
import tempfile

import pytest

os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["APP_PASSWORD"] = "test-pass"
os.environ.pop("ANTHROPIC_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from app import db, main  # noqa: E402
from app.brands import classify, match_brand  # noqa: E402


@pytest.fixture()
def client():
    conn = db.connect()
    for t in ("items", "receipts", "products"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("UPDATE settings SET value='0' WHERE key='commission_rate'")
    conn.execute("UPDATE settings SET value='paid' WHERE key='commission_basis'")
    conn.commit(); conn.close()
    c = TestClient(main.app)
    assert c.post("/api/login", json={"password": "test-pass"}).status_code == 200
    return c


def brand_id(client, name):
    return next(b["id"] for b in client.get("/api/brands").json() if b["name"] == name)


def memo_receipt(client, **over):
    r = {"receipt_no": "2950", "terminal": "3", "sale_date": "2026-10-06", "sale_time": "20:22",
         "store_ref": "CATK-03-01920", "dep_date": "2026-10-06", "destination": "DOH", "final_dest": "DOH",
         "flight_no": "QR0768", "passenger_name": "JAFRI SHAHVEER ZURIA", "served_by": "63649572",
         "payment_method": "AMEX", "auth_no": "880645", "printed_total_cents": 41700,
         "items": [{"barcode": "3700458603040", "description": "MEMO ITALIAN LEATHER 75ML",
                    "brand_id": brand_id(client, "Memo"), "list_cents": 41700, "paid_cents": 41700}]}
    r.update(over)
    return r


def test_login_required():
    c = TestClient(main.app)
    assert c.get("/api/month/2026-10").status_code == 401
    assert c.post("/api/login", json={"password": "nope"}).status_code == 401


def test_all_19_brands_seeded(client):
    assert len(client.get("/api/brands").json()) == 19


def test_memo_receipt_roundtrip_and_commission(client):
    client.put("/api/settings", json={"commission_rate": 2.5, "commission_basis": "paid"})
    rid = client.post("/api/receipts", json=memo_receipt(client)).json()["id"]
    r = client.get(f"/api/receipts/{rid}").json()
    assert r["passenger_name"] == "JAFRI SHAHVEER ZURIA" and r["flight_no"] == "QR0768"
    assert r["total_cents"] == 41700 and r["commission_sales_cents"] == 41700
    assert r["commission_cents"] == 1043          # 2.5% of 417.00 = 10.425 -> 10.43
    assert r["total_mismatch"] is False
    m = client.get("/api/month/2026-10").json()
    assert m["summary"]["receipts"] == 1 and m["summary"]["commission_cents"] == 1043
    assert m["summary"]["brands"][0]["name"] == "Memo"
    assert client.get("/api/month/2026-11").json()["summary"]["receipts"] == 0


def test_duplicate_blocked_but_other_terminal_allowed(client):
    assert client.post("/api/receipts", json=memo_receipt(client)).status_code == 200
    dup = client.post("/api/receipts", json=memo_receipt(client))
    assert dup.status_code == 409 and "existing_id" in dup.json()
    assert client.post("/api/receipts", json=memo_receipt(client, terminal="5")).status_code == 200
    assert client.post("/api/receipts", json=memo_receipt(client, sale_date="2026-11-02")).status_code == 200


def test_mixed_receipt_splits_totals_and_discount_basis(client):
    creed = brand_id(client, "Creed")
    body = memo_receipt(client, receipt_no="3001", club_avolta=True, printed_total_cents=57000, items=[
        {"barcode": "1", "description": "CREED AVENTUS 100ML", "brand_id": creed, "list_cents": 40000, "paid_cents": 38000},
        {"barcode": "2", "description": "CHANEL BLEU 100ML", "brand_id": None, "brand_text": "Chanel",
         "list_cents": 20000, "paid_cents": 19000}])
    client.put("/api/settings", json={"commission_rate": 10, "commission_basis": "paid"})
    rid = client.post("/api/receipts", json=body).json()["id"]
    r = client.get(f"/api/receipts/{rid}").json()
    assert (r["commission_sales_cents"], r["other_sales_cents"], r["total_cents"]) == (38000, 19000, 57000)
    assert r["commission_cents"] == 3800
    client.put("/api/settings", json={"commission_rate": 10, "commission_basis": "list"})
    assert client.get(f"/api/receipts/{rid}").json()["commission_cents"] == 4000
    s = client.get("/api/month/2026-10").json()["summary"]
    assert [b["name"] for b in s["brands"]] == ["Creed", "Other brands"]


def test_printed_total_mismatch_is_flagged(client):
    rid = client.post("/api/receipts", json=memo_receipt(client, printed_total_cents=40000)).json()["id"]
    assert client.get(f"/api/receipts/{rid}").json()["total_mismatch"] is True
    assert client.get("/api/month/2026-10").json()["summary"]["total_mismatch"] == 1


def test_brand_rules():
    conn = db.connect(); brands = db.brand_rows(conn)
    name = lambda text: (lambda b, ok: (b["name"] if b else None, ok))(*match_brand(text, brands))
    assert name("MEMO ITALIAN LEATHER 75ML") == ("Memo", True)
    assert name("PDM LAYTON EDP 125ML") == ("Parfums de Marly", True)
    assert name("CHLOÉ ATELIER DES FLEURS CEDRUS") == ("Chloé Atelier des Fleurs", True)
    assert name("CHLOE NOMADE EDP 75ML") == ("Chloé Atelier des Fleurs", False)
    assert name("D&G VELVET DESERT OUD") == ("Dolce & Gabbana Velvet Collection", True)
    assert name("DOLCE GABBANA LIGHT BLUE") == ("Dolce & Gabbana Velvet Collection", False)
    assert name("BOSS BOTTLED EDT 100ML") == ("Hugo Boss Private Line", False)
    assert name("VERSACE EROS EDT") == ("Versace Atelier Collection", False)
    assert name("REPLICA JAZZ CLUB 100ML") == ("Maison Margiela Replica", True)
    assert name("CHANEL NO 5") == (None, False)
    assert name("MEMORY FOAM PILLOW") == (None, False)       # "memo" must be a whole word
    partial = classify(conn, brands, "VERSACE EROS EDT")
    assert partial["brand_id"] is None and partial["needs_review"] is True
    abbrev = classify(conn, brands, "TT KIRKE 100ML", ai_commission_brand="Tiziana Terenzi", ai_sure=False)
    assert abbrev["brand_id"] is not None and abbrev["needs_review"] is True
    conn.close()


def test_barcode_is_remembered_after_saving(client):
    versace = brand_id(client, "Versace Atelier Collection")
    body = memo_receipt(client, receipt_no="3002", printed_total_cents=None, items=[
        {"barcode": "8011003999", "description": "VERS JASMIN AU SOLEIL", "brand_id": versace,
         "list_cents": 30000, "paid_cents": 30000}])
    client.post("/api/receipts", json=body)
    again = client.post("/api/classify", json={"description": "VERS JASMIN AU SOLEIL", "barcode": "8011003999"}).json()
    assert again["brand_id"] == versace and again["needs_review"] is False


def test_scan_draft_drops_bag_lines_and_tags_brand(client):
    conn = db.connect()
    draft = main._draft_from_scan(conn, {
        "receipt_no": "2950", "terminal": "3", "sale_date": "2026-10-06", "sale_time": "20:22", "printed_total": 417.00,
        "items": [{"barcode": "3700458603040", "description": "MEMO ITALIAN LEATHER 75ML", "brand": "Memo Paris",
                   "commission_brand": "Memo", "sure": True, "qty": 1, "list_price": 417.0, "paid_price": 417.0},
                  {"barcode": "877596001472", "description": "Cash n Carry New Medium Slim", "list_price": 0, "paid_price": 0}]})
    conn.close()
    assert len(draft["items"]) == 1 and draft["printed_total_cents"] == 41700
    assert draft["items"][0]["brand_text"] == "Memo" and draft["items"][0]["needs_review"] is False


def test_photo_upload_without_key_keeps_photo(client):
    buf = io.BytesIO(); Image.new("RGB", (3000, 4000), "white").save(buf, "JPEG")
    res = client.post("/api/scan", files={"photo": ("r.jpg", buf.getvalue(), "image/jpeg")}).json()
    assert res["draft"] is None and "ANTHROPIC_API_KEY" in res["problem"]
    img = client.get(f"/api/images/{res['image_id']}")
    assert img.status_code == 200 and max(Image.open(io.BytesIO(img.content)).size) == 2000
    assert client.post("/api/scan", files={"photo": ("x.txt", b"hello", "text/plain")}).status_code == 400


def test_search_edit_delete_export(client):
    rid = client.post("/api/receipts", json=memo_receipt(client)).json()["id"]
    for q in ("2950", "jafri", "QR0768", "italian", "880645"):
        assert len(client.get("/api/receipts", params={"q": q}).json()) == 1, q
    assert client.get("/api/receipts", params={"q": "zzz"}).json() == []
    edited = memo_receipt(client, notes="customer returned to ask about refund")
    assert client.put(f"/api/receipts/{rid}", json=edited).status_code == 200
    csv_text = client.get("/api/export/2026-10.csv").text
    assert "JAFRI SHAHVEER ZURIA" in csv_text and "417.00" in csv_text and csv_text.count("\n") == 2
    assert client.delete(f"/api/receipts/{rid}").status_code == 200
    assert client.get(f"/api/receipts/{rid}").status_code == 404


def test_bad_input_rejected(client):
    assert client.post("/api/receipts", json=memo_receipt(client, sale_date="06/10/2026")).status_code == 422
    assert client.post("/api/receipts", json=memo_receipt(client, items=[])).status_code == 422
    assert client.post("/api/receipts", json=memo_receipt(client, receipt_no=" ")).status_code == 422


def test_scan_reply_parsing():
    from app.scan import ScanUnavailable, _extract
    fields = {"receipt_no": "2950", "sale_date": "2026-10-06", "items": []}
    assert _extract({"content": [{"type": "text", "text": "ok"}, {"type": "tool_use", "input": fields}]}) == fields
    assert _extract({"content": [{"type": "text", "text": 'Here it is:\n```json\n{"receipt_no": "2950", "sale_date": "2026-10-06", "items": []}\n```'}]}) == fields
    with pytest.raises(ScanUnavailable):
        _extract({"content": [{"type": "text", "text": "I can't read this photo."}]})

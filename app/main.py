"""Receipt Vault: a private log of duty free receipts with monthly commission totals."""
import csv
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.sessions import SessionMiddleware

from . import db, scan
from .brands import classify

STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 15 * 1024 * 1024
ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT"))


def _secret_key() -> str:
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = db.data_dir() / "secret.key"   # kept on the volume so logins survive redeploys
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


def _clear_abandoned_photos():
    """Remove photos from scans that were never saved as a receipt (older than two days)."""
    conn = db.connect()
    try:
        kept = {r["image_id"] for r in conn.execute("SELECT image_id FROM receipts WHERE image_id IS NOT NULL")}
    finally:
        conn.close()
    for f in (db.data_dir() / "images").glob("*.jpg"):
        if f.stem not in kept and time.time() - f.stat().st_mtime > 2 * 86400:
            f.unlink(missing_ok=True)


db.init()
_clear_abandoned_photos()
app = FastAPI(title="Receipt Vault", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=_secret_key(), max_age=60 * 60 * 24 * 30,
                   same_site="lax", https_only=ON_RAILWAY)


# ---------- helpers ----------

def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def require_login(request: Request):
    if not request.session.get("ok"):
        raise HTTPException(401, "Log in first.")


def to_cents(value) -> int:
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def commission_cents(basis_cents: int, rate_percent: float) -> int:
    return int((Decimal(basis_cents) * Decimal(str(rate_percent)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def item_commission(item: dict, settings: dict, brand_rates: dict) -> int:
    """Commission in cents for one stored item. Zero for non-commission brands."""
    if item["brand_id"] is None:
        return 0
    rate = brand_rates.get(item["brand_id"])
    if rate is None:
        rate = settings["commission_rate"]
    basis = item["list_cents"] if settings["commission_basis"] == "list" else item["paid_cents"]
    return commission_cents(basis, rate)


def load_receipts(conn, where: str = "", params: tuple = ()) -> list[dict]:
    """Receipts with their items and computed totals, newest first."""
    settings = db.get_settings(conn)
    brands = {b["id"]: b for b in db.brand_rows(conn)}
    rates = {i: b["rate"] for i, b in brands.items()}
    receipts = [dict(r) for r in conn.execute(
        f"SELECT * FROM receipts {where} ORDER BY sale_date DESC, sale_time DESC, id DESC", params)]
    if not receipts:
        return []
    ids = [r["id"] for r in receipts]
    marks = ",".join("?" * len(ids))
    by_receipt: dict[int, list] = {i: [] for i in ids}
    for row in conn.execute(f"SELECT * FROM items WHERE receipt_id IN ({marks}) ORDER BY position, id", ids):
        it = dict(row)
        it["needs_review"] = bool(it["needs_review"])
        it["commissionable"] = it["brand_id"] is not None
        it["brand_name"] = brands[it["brand_id"]]["name"] if it["commissionable"] else (it["brand_text"] or "Other brand")
        it["commission_cents"] = item_commission(it, settings, rates)
        by_receipt[it["receipt_id"]].append(it)
    for r in receipts:
        items = by_receipt[r["id"]]
        r["club_avolta"] = bool(r["club_avolta"])
        r["items"] = items
        r["total_cents"] = sum(i["paid_cents"] for i in items)
        r["commission_sales_cents"] = sum(i["paid_cents"] for i in items if i["commissionable"])
        r["other_sales_cents"] = r["total_cents"] - r["commission_sales_cents"]
        r["commission_cents"] = sum(i["commission_cents"] for i in items)
        r["needs_review"] = any(i["needs_review"] for i in items)
        r["total_mismatch"] = r["printed_total_cents"] is not None and r["printed_total_cents"] != r["total_cents"]
    return receipts


def summarize(receipts: list[dict]) -> dict:
    by_brand: dict[str, dict] = {}
    for r in receipts:
        for i in r["items"]:
            key = i["brand_name"] if i["commissionable"] else "__other__"
            b = by_brand.setdefault(key, {"name": i["brand_name"] if i["commissionable"] else "Other brands",
                                          "commissionable": i["commissionable"], "units": 0,
                                          "sales_cents": 0, "commission_cents": 0})
            b["units"] += i["qty"]
            b["sales_cents"] += i["paid_cents"]
            b["commission_cents"] += i["commission_cents"]
    brands = sorted(by_brand.values(), key=lambda b: (not b["commissionable"], -b["sales_cents"]))
    return {
        "receipts": len(receipts),
        "total_cents": sum(r["total_cents"] for r in receipts),
        "commission_sales_cents": sum(r["commission_sales_cents"] for r in receipts),
        "other_sales_cents": sum(r["other_sales_cents"] for r in receipts),
        "commission_cents": sum(r["commission_cents"] for r in receipts),
        "needs_review": sum(1 for r in receipts if r["needs_review"]),
        "total_mismatch": sum(1 for r in receipts if r["total_mismatch"]),
        "brands": brands,
    }


# ---------- request models ----------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ItemIn(BaseModel):
    barcode: str = Field("", max_length=40)
    description: str = Field(..., min_length=1, max_length=200)
    brand_id: Optional[int] = None
    brand_text: str = Field("", max_length=80)
    qty: int = Field(1, ge=1, le=99)
    list_cents: int = Field(..., ge=0, le=10_000_000)
    paid_cents: int = Field(..., ge=0, le=10_000_000)
    needs_review: bool = False


class ReceiptIn(BaseModel):
    receipt_no: str = Field(..., min_length=1, max_length=40)
    terminal: str = Field("", max_length=20)
    sale_date: str
    sale_time: str = Field("", max_length=5)
    store_ref: str = Field("", max_length=40)
    dep_date: str = Field("", max_length=10)
    destination: str = Field("", max_length=40)
    final_dest: str = Field("", max_length=40)
    flight_no: str = Field("", max_length=20)
    passenger_name: str = Field("", max_length=120)
    served_by: str = Field("", max_length=40)
    payment_method: str = Field("", max_length=40)
    auth_no: str = Field("", max_length=40)
    club_avolta: bool = False
    printed_total_cents: Optional[int] = Field(None, ge=0, le=100_000_000)
    notes: str = Field("", max_length=2000)
    image_id: Optional[str] = None
    items: list[ItemIn] = Field(..., min_length=1, max_length=60)

    @field_validator("sale_date")
    @classmethod
    def _valid_date(cls, v):
        if not DATE_RE.match(v or ""):
            raise ValueError("Sale date is required (YYYY-MM-DD).")
        date.fromisoformat(v)
        return v

    @field_validator("receipt_no")
    @classmethod
    def _has_number(cls, v):
        if not (v or "").strip():
            raise ValueError("Receipt number is required.")
        return v.strip()

    @field_validator("terminal", "flight_no", "passenger_name", "destination", "final_dest")
    @classmethod
    def _strip(cls, v):
        return (v or "").strip()

    @field_validator("image_id")
    @classmethod
    def _image(cls, v):
        if v and not re.fullmatch(r"[0-9a-f]{32}", v):
            raise ValueError("Bad photo reference.")
        return v or None


RECEIPT_COLS = ["receipt_no", "terminal", "sale_date", "sale_time", "month", "store_ref", "dep_date", "destination",
                "final_dest", "flight_no", "passenger_name", "served_by", "payment_method", "auth_no",
                "club_avolta", "printed_total_cents", "notes", "image_id"]


def _write_items(conn, receipt_id: int, items: list[ItemIn]):
    valid = {r["id"] for r in conn.execute("SELECT id FROM brands")}
    conn.execute("DELETE FROM items WHERE receipt_id = ?", (receipt_id,))
    for pos, it in enumerate(items):
        brand_id = it.brand_id if it.brand_id in valid else None
        conn.execute(
            "INSERT INTO items (receipt_id, position, barcode, description, brand_id, brand_text, qty, "
            "list_cents, paid_cents, needs_review) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, pos, it.barcode.strip(), it.description.strip(), brand_id, it.brand_text.strip(),
             it.qty, it.list_cents, it.paid_cents, int(it.needs_review)))
        # Remember the decision for this barcode once a person has confirmed it.
        if it.barcode.strip() and not it.needs_review:
            conn.execute(
                "INSERT INTO products (barcode, description, brand_id, brand_text) VALUES (?,?,?,?) "
                "ON CONFLICT (barcode) DO UPDATE SET description = excluded.description, "
                "brand_id = excluded.brand_id, brand_text = excluded.brand_text",
                (it.barcode.strip(), it.description.strip(), brand_id, it.brand_text.strip()))


def _receipt_values(body: ReceiptIn) -> list:
    d = body.model_dump()
    d["month"] = body.sale_date[:7]
    d["club_avolta"] = int(body.club_avolta)
    return [d[c] for c in RECEIPT_COLS]


def _duplicate(conn, body: ReceiptIn, ignore_id: int = 0):
    row = conn.execute("SELECT id FROM receipts WHERE terminal = ? AND receipt_no = ? AND sale_date = ? AND id != ?",
                       (body.terminal, body.receipt_no, body.sale_date, ignore_id)).fetchone()
    if row:
        raise HTTPException(409, detail={"message": f"Receipt {body.receipt_no} from this terminal and date "
                                                    f"is already saved.", "existing_id": row["id"]})


# ---------- login ----------

_fails: dict[str, list[float]] = {}


class LoginIn(BaseModel):
    password: str = Field(..., max_length=200)


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    expected = os.environ.get("APP_PASSWORD", "")
    if not expected:
        raise HTTPException(503, "No password is set for this app. Add APP_PASSWORD in Railway.")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0].strip()
    recent = [t for t in _fails.get(ip, []) if time.time() - t < 300]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many wrong tries. Wait 5 minutes.")
    if not hmac.compare_digest(body.password.encode(), expected.encode()):
        _fails[ip] = recent + [time.time()]
        raise HTTPException(401, "Wrong password.")
    _fails.pop(ip, None)
    request.session["ok"] = True
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"logged_in": bool(request.session.get("ok")),
            "scan_ready": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())}


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- photos and scanning ----------

def _draft_from_scan(conn, raw: dict) -> dict:
    """Turn the AI's reading into a draft receipt, tagging each line with the brand rules."""
    brands = db.brand_rows(conn)
    items = []
    for it in raw.get("items") or []:
        list_c, paid_c = to_cents(it.get("list_price")), to_cents(it.get("paid_price"))
        if list_c == 0 and paid_c == 0:
            continue  # carrier bags and other free lines
        tag = classify(conn, brands, it.get("description", ""), it.get("barcode", ""),
                       ai_brand=it.get("brand") or "", ai_commission_brand=it.get("commission_brand") or "",
                       ai_sure=bool(it.get("sure")))
        items.append({"barcode": str(it.get("barcode") or ""), "description": str(it.get("description") or ""),
                      "qty": int(it.get("qty") or 1), "list_cents": list_c or paid_c, "paid_cents": paid_c or list_c,
                      **tag})
    text = lambda k: str(raw.get(k) or "").strip()
    printed = raw.get("printed_total")
    return {
        "receipt_no": text("receipt_no"), "terminal": text("terminal"), "store_ref": text("store_ref"),
        "sale_date": text("sale_date"), "sale_time": text("sale_time")[:5], "dep_date": text("dep_date"),
        "destination": text("destination"), "final_dest": text("final_dest"), "flight_no": text("flight_no"),
        "passenger_name": text("passenger_name"), "served_by": text("served_by"),
        "payment_method": text("payment_method"), "auth_no": text("auth_no"),
        "club_avolta": bool(raw.get("club_avolta")),
        "printed_total_cents": to_cents(printed) if printed is not None else None,
        "items": items, "unreadable": [str(u) for u in raw.get("unreadable") or []],
    }


@app.post("/api/scan", dependencies=[Depends(require_login)])
def scan_receipt(photo: UploadFile = File(...), read: bool = True, conn=Depends(get_conn)):
    """Store the photo; if `read`, also have the AI fill in a draft."""
    raw = photo.file.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, "That photo is too large (15 MB limit).")
    try:
        jpeg = scan.prepare_image(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    image_id = uuid.uuid4().hex
    (db.data_dir() / "images" / f"{image_id}.jpg").write_bytes(jpeg)
    if not read:
        return {"image_id": image_id, "draft": None, "problem": None}
    try:
        draft = _draft_from_scan(conn, scan.scan(jpeg, db.brand_rows(conn)))
    except scan.ScanUnavailable as exc:
        return {"image_id": image_id, "draft": None, "problem": str(exc)}
    return {"image_id": image_id, "draft": draft, "problem": None}


@app.get("/api/images/{image_id}", dependencies=[Depends(require_login)])
def image(image_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", image_id):
        raise HTTPException(404)
    path = db.data_dir() / "images" / f"{image_id}.jpg"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


class ClassifyIn(BaseModel):
    description: str = Field("", max_length=200)
    barcode: str = Field("", max_length=40)


@app.post("/api/classify", dependencies=[Depends(require_login)])
def classify_line(body: ClassifyIn, conn=Depends(get_conn)):
    return classify(conn, db.brand_rows(conn), body.description, body.barcode)


# ---------- receipts ----------

@app.get("/api/months", dependencies=[Depends(require_login)])
def months(conn=Depends(get_conn)):
    return [r["month"] for r in conn.execute("SELECT DISTINCT month FROM receipts ORDER BY month DESC")]


@app.get("/api/month/{month}", dependencies=[Depends(require_login)])
def month_view(month: str, conn=Depends(get_conn)):
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "Month must look like 2026-10.")
    receipts = load_receipts(conn, "WHERE month = ?", (month,))
    return {"month": month, "summary": summarize(receipts), "receipts": receipts,
            "settings": db.get_settings(conn)}


@app.get("/api/receipts", dependencies=[Depends(require_login)])
def search(q: str = "", conn=Depends(get_conn)):
    q = q.strip()
    if not q:
        return []
    like = f"%{q}%"
    return load_receipts(conn, """WHERE receipt_no LIKE ? OR passenger_name LIKE ? OR flight_no LIKE ?
        OR auth_no LIKE ? OR destination LIKE ? OR notes LIKE ?
        OR id IN (SELECT receipt_id FROM items WHERE description LIKE ? OR barcode LIKE ? OR brand_text LIKE ?)
        """, (like,) * 9)[:100]


@app.get("/api/receipts/{receipt_id}", dependencies=[Depends(require_login)])
def get_receipt(receipt_id: int, conn=Depends(get_conn)):
    found = load_receipts(conn, "WHERE id = ?", (receipt_id,))
    if not found:
        raise HTTPException(404, "That receipt doesn't exist any more.")
    return found[0]


@app.post("/api/receipts", dependencies=[Depends(require_login)])
def create_receipt(body: ReceiptIn, conn=Depends(get_conn)):
    _duplicate(conn, body)
    try:
        cur = conn.execute(f"INSERT INTO receipts ({', '.join(RECEIPT_COLS)}) "
                           f"VALUES ({', '.join('?' * len(RECEIPT_COLS))})", _receipt_values(body))
        _write_items(conn, cur.lastrowid, body.items)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(409, detail={"message": "That receipt is already saved."})
    return {"id": cur.lastrowid}


@app.put("/api/receipts/{receipt_id}", dependencies=[Depends(require_login)])
def update_receipt(receipt_id: int, body: ReceiptIn, conn=Depends(get_conn)):
    old = conn.execute("SELECT image_id FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if not old:
        raise HTTPException(404, "That receipt doesn't exist any more.")
    _duplicate(conn, body, ignore_id=receipt_id)
    if body.image_id is None:
        body.image_id = old["image_id"]
    conn.execute(f"UPDATE receipts SET {', '.join(c + ' = ?' for c in RECEIPT_COLS)} WHERE id = ?",
                 _receipt_values(body) + [receipt_id])
    _write_items(conn, receipt_id, body.items)
    conn.commit()
    return {"id": receipt_id}


@app.delete("/api/receipts/{receipt_id}", dependencies=[Depends(require_login)])
def delete_receipt(receipt_id: int, conn=Depends(get_conn)):
    row = conn.execute("SELECT image_id FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    if not row:
        raise HTTPException(404, "That receipt doesn't exist any more.")
    conn.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    conn.commit()
    if row["image_id"]:
        (db.data_dir() / "images" / f"{row['image_id']}.jpg").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/export/{month}.csv", dependencies=[Depends(require_login)])
def export_csv(month: str, conn=Depends(get_conn)):
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise HTTPException(400, "Month must look like 2026-10.")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Receipt no", "Terminal", "Sale date", "Sale time", "Store ref", "Passenger", "Flight",
                "Destination", "Final destination", "Departure date", "Served by", "Payment", "Authorization no",
                "Club Avolta 5%", "Barcode", "Product", "Brand", "Commission brand", "Qty",
                "Price before discount", "Price paid", "Commission"])
    money = lambda c: f"{c / 100:.2f}"
    for r in reversed(load_receipts(conn, "WHERE month = ?", (month,))):
        for i in r["items"]:
            w.writerow([r["receipt_no"], r["terminal"], r["sale_date"], r["sale_time"], r["store_ref"],
                        r["passenger_name"], r["flight_no"], r["destination"], r["final_dest"], r["dep_date"],
                        r["served_by"], r["payment_method"], r["auth_no"], "yes" if r["club_avolta"] else "no",
                        i["barcode"], i["description"], i["brand_name"], "yes" if i["commissionable"] else "no",
                        i["qty"], money(i["list_cents"]), money(i["paid_cents"]), money(i["commission_cents"])])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="receipts-{month}.csv"'})


# ---------- brands and settings ----------

class BrandIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    requires: list[str] = Field(default_factory=list, max_length=12)
    rate: Optional[float] = Field(None, ge=0, le=100)
    active: bool = True


def _words(words: list[str]) -> str:
    return json.dumps([w.strip().lower() for w in words if w.strip()][:12])


@app.get("/api/brands", dependencies=[Depends(require_login)])
def list_brands(conn=Depends(get_conn)):
    return db.brand_rows(conn)


@app.post("/api/brands", dependencies=[Depends(require_login)])
def add_brand(body: BrandIn, conn=Depends(get_conn)):
    try:
        cur = conn.execute("INSERT INTO brands (name, aliases, requires, rate, active) VALUES (?,?,?,?,?)",
                           (body.name.strip(), _words(body.aliases), _words(body.requires), body.rate, int(body.active)))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"{body.name.strip()} is already on the list.")
    return {"id": cur.lastrowid}


@app.put("/api/brands/{brand_id}", dependencies=[Depends(require_login)])
def edit_brand(brand_id: int, body: BrandIn, conn=Depends(get_conn)):
    try:
        cur = conn.execute("UPDATE brands SET name = ?, aliases = ?, requires = ?, rate = ?, active = ? WHERE id = ?",
                           (body.name.strip(), _words(body.aliases), _words(body.requires), body.rate,
                            int(body.active), brand_id))
        conn.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"{body.name.strip()} is already on the list.")
    if cur.rowcount == 0:
        raise HTTPException(404, "That brand doesn't exist any more.")
    return {"id": brand_id}


class SettingsIn(BaseModel):
    commission_rate: float = Field(..., ge=0, le=100)
    commission_basis: str = Field(..., pattern="^(paid|list)$")


@app.get("/api/settings", dependencies=[Depends(require_login)])
def read_settings(conn=Depends(get_conn)):
    return db.get_settings(conn)


@app.put("/api/settings", dependencies=[Depends(require_login)])
def write_settings(body: SettingsIn, conn=Depends(get_conn)):
    for k, v in (("commission_rate", str(body.commission_rate)), ("commission_basis", body.commission_basis)):
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (k, v))
    conn.commit()
    return db.get_settings(conn)


# ---------- the page ----------

@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
    return JSONResponse(detail, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", [])[1:]).replace("_", " ")
    msg = str(first.get("msg", "Something in the form isn't valid.")).replace("Value error, ", "")
    return JSONResponse({"message": f"{field}: {msg}" if field else msg}, status_code=422)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

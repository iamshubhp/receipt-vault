# Receipt Vault

A private log of duty free receipts with monthly commission totals.

Photograph a receipt, the app reads it (receipt number, flight, passenger, each perfume, prices),
you confirm, and it is saved. Each line is tagged as a commission brand or not, and every month
shows commission sales, other sales, and commission earned.

## How it is put together

| File | What it does |
| --- | --- |
| `app/main.py` | Web server: login, receipts, monthly totals, search, CSV export |
| `app/brands.py` | The commission brand list and the rules that tag a receipt line |
| `app/scan.py` | Sends the photo to the Anthropic API and gets structured fields back |
| `app/db.py` | SQLite tables |
| `app/static/` | The phone interface (plain HTML, CSS, JavaScript) |
| `tests/test_app.py` | Tests, using a real receipt as the fixture |

A receipt is unique by terminal + receipt number + sale date, because a register's counter can
repeat on another terminal or after a reset.

## Settings (environment variables)

| Name | Needed | Purpose |
| --- | --- | --- |
| `APP_PASSWORD` | yes | Password for the login screen |
| `ANTHROPIC_API_KEY` | for photo reading | Without it you can still attach a photo and type the details |
| `DATA_DIR` | yes on Railway | Folder for the database and photos. Point it at a volume (`/data`) |
| `ANTHROPIC_MODEL` | no | Defaults to `claude-sonnet-5-5` |

## Run locally

```
pip install -r requirements.txt pytest
APP_PASSWORD=something uvicorn app.main:app --reload
python -m pytest
```

## Privacy

Receipts carry passenger names and flights. The app sits behind a password, is not indexed by
search engines, and never stores card numbers.

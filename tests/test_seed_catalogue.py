"""Boot seeding of the supplier catalogue (bug 2: catalogue after login).

Pins the ``_startup`` wiring (``seed_catalogue`` before ``seed_example_items``
— a fresh boot with a CSV never creates the 12 ``EX-*`` demo rows), the
create-only/no-prune/no-raise policy, the CSV source resolution (explicit env
with **no fallback** → the operator's data-dir copy; the supplier's master
data is deliberately NOT committed — decision B), the degraded-model path
(rows land ``embedding=None``, ``backfill_embeddings`` repairs them), the
honest source labels of the single final ``Catalogue: N item(s) — source: …``
line, and the original bug's full chain at the API level (boot seed + flagged
login → forced change → catalogue).

Hermetic by design: the in-process tests run on their OWN sqlite engine
(same DDL the app needs) and never read or mutate the shared 12-demo fixture
database. The suite-wide default is ``HCRM_CATALOGUE_IMPORT=0`` (conftest);
every test here opts back in with ``monkeypatch.setenv``. No test touches
``./data``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import app.embeddings
import app.seed
from app.models import Item
from scripts import _biogan as bio
from tests.conftest import _fake_embed_text_blob
from tests.test_import_biogan import ean13, supplier, write_csv

REPO_ROOT = Path(__file__).resolve().parent.parent
#: The shared offline supplier fixture (3 data rows + 1 blank) — the
# stand-in for the operator's CSV, which is not committed (decision B).
FIXTURE_CSV = Path(__file__).parent / "fixtures" / "biogan" / "masterdata.csv"
FIXTURE_SKUS = {"10005", "1134", "1509b"}


@pytest.fixture()
def catalogue_on(monkeypatch):
    # Opt in explicitly, but do not inherit an operator's source overrides:
    # default-resolution tests must be deterministic under a hostile env.
    monkeypatch.setenv("HCRM_CATALOGUE_IMPORT", "1")
    monkeypatch.delenv("HCRM_CATALOGUE_CSV", raising=False)
    monkeypatch.delenv("HCRM_CATALOGUE_IMAGES_DIR", raising=False)


@pytest.fixture()
def seed_engine(tmp_path):
    """A hermetic engine: own sqlite file, full app DDL (tables + the
    last-admin triggers) — the seed tests never touch the shared 12-demo
    fixture database."""
    import app.database as database

    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}",
                           connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with engine.begin() as connection:
        for ddl in database.TRIGGER_DDL:
            connection.exec_driver_sql(ddl)
    return engine


def _items(engine) -> list[Item]:
    with Session(engine) as session:
        return list(session.exec(select(Item).order_by(Item.sku)).all())


def _snapshot(engine) -> dict:
    return {i.sku: (i.name, i.price_cents, i.stock, i.updated_at,
                    hashlib.sha256(i.embedding or b"").hexdigest())
            for i in _items(engine)}


def _count(engine) -> int:
    with Session(engine) as session:
        return len(session.exec(select(Item.id)).all())


def _api(method: str, base: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base.rstrip("/") + path, method=method, data=data,
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


# --- the boot seed, in the _startup order -------------------------------------


def test_fixture_csv_seeds_the_catalogue_and_suppresses_demo(
        catalogue_on, monkeypatch, seed_engine):
    monkeypatch.setenv("HCRM_CATALOGUE_CSV", str(FIXTURE_CSV))
    with Session(seed_engine) as session:
        status = app.seed.seed_catalogue(session)
        app.seed.seed_example_items(session)
        app.seed.backfill_embeddings(session)
    items = _items(seed_engine)
    assert {i.sku for i in items} == FIXTURE_SKUS
    assert {i.category for i in items} == {"Biogan"}
    assert not any(i.sku.startswith("EX-") for i in items)  # demo suppressed
    # _startup's final line: the import created rows, so the label is honest.
    assert status["source"] == "supplier-csv"
    assert status["created"] == 3
    assert app.seed.final_catalogue_source(status, 0, len(items)) == "supplier-csv"


def test_no_csv_falls_back_to_the_demo_seed(catalogue_on, monkeypatch,
                                            seed_engine):
    monkeypatch.delenv("HCRM_CATALOGUE_CSV", raising=False)
    # No env CSV and no data-dir CSV (the suite's temp dir) → no source.
    with Session(seed_engine) as session:
        status = app.seed.seed_catalogue(session)
        app.seed.seed_example_items(session)
    items = _items(seed_engine)
    assert len(items) == 12
    assert all(i.sku.startswith("EX-") for i in items)
    # The final line's source: the demo seed fired on a fresh database…
    assert app.seed.final_catalogue_source(status, 0, 12) == "demo-seed"
    # …and "none" over an already-populated DB is honest about it: the
    # existing catalogue is what exists (decision B2 pin).
    assert app.seed.final_catalogue_source({"source": "none"}, 7, 7) == "existing"
    # An import that created nothing over an empty DB is not "supplier-csv":
    # the rows on screen are the demo fallback (B2 pin, all-refused/blank case).
    assert app.seed.final_catalogue_source(
        {"source": "supplier-csv", "created": 0}, 0, 12) == "demo-seed"


def test_no_csv_boot_names_the_provisioning_step(catalogue_on, seed_engine,
                                                caplog):
    """The only guidance a fresh-clone operator gets must not vanish."""
    with Session(seed_engine) as session:
        status = app.seed.seed_catalogue(session)
    assert status["source"] == "none"
    messages = [r.getMessage() for r in caplog.records]
    assert any("no supplier CSV" in m for m in messages), messages
    assert any("HCRM_CATALOGUE_CSV" in m for m in messages), messages
    assert any(app.database.DATA_DIR in m for m in messages), messages


def test_opt_out_disables_the_import(monkeypatch, seed_engine):
    monkeypatch.setenv("HCRM_CATALOGUE_IMPORT", "0")
    with Session(seed_engine) as session:
        status = app.seed.seed_catalogue(session)
        app.seed.seed_example_items(session)  # the fallback still runs
    assert status == {"source": "existing (import disabled)"}
    assert len(_items(seed_engine)) == 12
    # The final line names the opt-out on every path — fresh boot or not.
    assert app.seed.final_catalogue_source(status, 0, 12) == \
        "existing (import disabled)"
    assert app.seed.final_catalogue_source(status, 252, 252) == \
        "existing (import disabled)"


def test_env_csv_missing_is_loud_and_never_falls_back(
        catalogue_on, monkeypatch, seed_engine, caplog):
    monkeypatch.setenv("HCRM_CATALOGUE_CSV", "/nonexistent/master.csv")
    with Session(seed_engine) as session:
        status = app.seed.seed_catalogue(session)
    assert status["source"] == "none"
    assert _items(seed_engine) == []  # no fallback of any kind
    assert any("is set but unreadable" in r.message for r in caplog.records)


def test_malformed_csv_rolls_back_and_the_boot_continues(
        catalogue_on, monkeypatch, tmp_path, seed_engine, caplog):
    bad = tmp_path / "bad.csv"
    bad.write_bytes(b'Varenummer,Varenavn\n"quoted\nfield",x\n')
    monkeypatch.setenv("HCRM_CATALOGUE_CSV", str(bad))
    with Session(seed_engine) as session:
        status = app.seed.seed_catalogue(session)   # never raises
        app.seed.seed_example_items(session)        # the boot continues
    assert status == {"source": "failed (rolled back)"}
    assert len(_items(seed_engine)) == 12
    assert any("rolled back" in r.message for r in caplog.records)
    assert any("import_biogan.py" in r.message for r in caplog.records)
    assert app.seed.final_catalogue_source(status, 0, 12) == \
        "failed (rolled back)"


def test_second_boot_writes_nothing_and_a_live_shaped_db_is_stable(
        catalogue_on, monkeypatch, seed_engine):
    # A live-shaped DB: the historical demo rows predate the import
    # (12 demo + the fixture's 3 supplier rows).
    monkeypatch.setenv("HCRM_CATALOGUE_CSV", str(FIXTURE_CSV))
    with Session(seed_engine) as session:
        app.seed.seed_example_items(session)
        app.seed.seed_catalogue(session)
    assert _count(seed_engine) == 15
    before = _snapshot(seed_engine)
    embed_calls = []
    monkeypatch.setattr(
        app.embeddings, "embed_text_blob",
        lambda text: embed_calls.append(text) or pytest.fail(
            "second boot must not perform embedding work"))
    with Session(seed_engine) as session:
        status = app.seed.seed_catalogue(session)
        app.seed.seed_example_items(session)
    assert embed_calls == []
    assert status["report"]["counts"].get("created", 0) == 0
    assert status["report"]["counts"].get("updated", 0) == 0
    assert _snapshot(seed_engine) == before  # no row, embedding or timestamp churn
    assert _count(seed_engine) == 15         # demo rows are never pruned at boot
    # Decision B2 pin: an import that created nothing over a populated
    # database must not claim "supplier-csv" — the catalogue already existed.
    assert status["created"] == 0
    assert app.seed.final_catalogue_source(status, 15, 15) == "existing"


def test_model_outage_creates_null_rows_and_backfill_repairs(
        catalogue_on, monkeypatch, tmp_path, seed_engine):
    csv = write_csv(tmp_path / "master.csv",
                    [supplier("1001", ean=ean13(1)),
                     supplier("1002", ean=ean13(2))])
    monkeypatch.setenv("HCRM_CATALOGUE_CSV", str(csv))
    monkeypatch.setattr(app.embeddings, "embed_text_blob", lambda text: None)
    with Session(seed_engine) as session:
        report = app.seed.seed_catalogue(session)["report"]
    items = _items(seed_engine)
    assert len(items) == 2
    assert all(i.embedding is None for i in items)  # visible, keyword-searchable
    assert sum(1 for r in report["rows"]
               if "embedding_backfill_pending" in r["review"]) == 2
    # A later healthy boot repairs them (app.seed's own name is patched).
    monkeypatch.setattr(app.seed, "embed_text_blob", _fake_embed_text_blob)
    with Session(seed_engine) as session:
        app.seed.backfill_embeddings(session)
    assert all(i.embedding is not None for i in _items(seed_engine))


# --- source resolution and file parity ----------------------------------------


def test_data_dir_csv_is_the_kiosk_source(catalogue_on, seed_engine):
    # The data dir (app.database.DATA_DIR, the suite's temp dir) gets the
    # operator-shaped CSV: that is what the kiosk boots on.
    import app.database
    data_csv = Path(app.database.DATA_DIR) / "Biogan - masterdata - Sheet.csv"
    data_csv.parent.mkdir(parents=True, exist_ok=True)
    write_csv(data_csv, [supplier("2001", ean=ean13(9))])
    try:
        with Session(seed_engine) as session:
            status = app.seed.seed_catalogue(session)
        assert [i.sku for i in _items(seed_engine)] == ["2001"]
        assert status["source"] == "supplier-csv"
        assert app.seed.final_catalogue_source(status, 0, 1) == "supplier-csv"
    finally:
        data_csv.unlink(missing_ok=True)


def test_csv_filename_matches_the_biogan_default():
    assert app.seed._DATA_DIR_CSV_NAME == bio.default_csv_path().name


# --- real-boot integration (lifespan order, DATABASE_URL, the final line) ------


def test_flagged_login_change_then_catalogue_end_to_end(tmp_path):
    """The original bug's full chain, at the API level (decision B3): a real
    boot with a supplier CSV, the seeded FLAGGED admin logs in (the API 403s
    the catalogue until the change), the forced password change succeeds,
    and the same session then sees the Biogan catalogue with no EX-* rows.
    The JS half (changePassword → _enterCatalogue) is pinned by
    tests/js/flaggedlogin.test.mjs."""
    from scripts._hcrm import self_host
    with self_host(pbkdf2_iterations=1000,
                   extra_env={"HCRM_CATALOGUE_IMPORT": "1",
                              "HCRM_CATALOGUE_CSV": str(FIXTURE_CSV)}) as server:
        # The seeded default admin is flagged; the change is mandatory.
        login = _api("POST", server.base, "/api/auth/login",
                     {"email": "admin@shop.local", "password": "changeme"})
        assert login["user"]["must_change_password"] is True
        token = {"Authorization": f"Bearer {login['token']}"}
        try:
            _api("GET", server.base, "/api/items?limit=1", headers=token)
            raise AssertionError("the flagged account must be 403-gated")
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
        _api("POST", server.base, "/api/auth/change-password",
             {"current_password": "changeme",
              "new_password": "kiosk-password-1"}, headers=token)
        page = _api("GET", server.base, "/api/items?limit=100", headers=token)
        assert {i["sku"] for i in page} == FIXTURE_SKUS
        assert all(i["category"] == "Biogan" for i in page)   # no EX-* rows
        cats = _api("GET", server.base, "/api/items/categories", headers=token)
        assert cats == ["Biogan"]
        assert "Catalogue: 3 item(s) — source: supplier-csv" in server.log_tail(80)


def test_startup_lifespan_seeds_the_catalogue_before_the_demo(tmp_path):
    csv = write_csv(tmp_path / "master.csv",
                    [supplier("3001", ean=ean13(11)),
                     supplier("3002", ean=ean13(12))])
    from scripts._hcrm import self_host
    with self_host(pbkdf2_iterations=1000,
                   extra_env={"HCRM_CATALOGUE_IMPORT": "1",
                              "HCRM_CATALOGUE_CSV": str(csv)}) as server:
        login = _api("POST", server.base, "/api/auth/login",
                     {"email": "admin", "password": "admin"})
        token = {"Authorization": f"Bearer {login['token']}"}
        page = _api("GET", server.base, "/api/items?limit=100", headers=token)
        assert {i["sku"] for i in page} == {"3001", "3002"}
        assert all(i["category"] == "Biogan" for i in page)  # no EX-* rows
        cats = _api("GET", server.base, "/api/items/categories", headers=token)
        assert cats == ["Biogan"]
        # _startup's single final line, on the real boot path:
        assert "Catalogue: 2 item(s) — source: supplier-csv" in server.log_tail(80)


def test_database_url_boot_seeds_that_database(tmp_path):
    csv = write_csv(tmp_path / "master.csv",
                    [supplier("4001", ean=ean13(21)),
                     supplier("4002", ean=ean13(22))])
    alt_db = tmp_path / "alt.db"
    data_dir = tmp_path / "data-dir"
    snippet = (
        "import json, os, sqlite3\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "with TestClient(app) as c:\n"
        "    con = sqlite3.connect(os.environ['ALT_DB'])\n"
        "    out = {'rows': con.execute('select count(*) from items')"
        ".fetchone()[0],\n"
        "           'ex': con.execute(\"select count(*) from items where "
        "sku like 'EX-%'\").fetchone()[0]}\n"
        "    con.close()\n"
        "    print('JSON>>' + json.dumps(out))\n"
    )
    env = {**os.environ,
           "PYTHONPATH": str(REPO_ROOT),
           "HCRM_DATA_DIR": str(data_dir),
           "DATABASE_URL": f"sqlite:///{alt_db}",
           "ALT_DB": str(alt_db),
           "HCRM_CATALOGUE_IMPORT": "1",
           "HCRM_CATALOGUE_CSV": str(csv),
           "HCRM_EMBED_MODEL": "bogus/model",   # degraded boot: NULL rows land
           "HCRM_PBKDF2_ITERATIONS": "1000"}
    proc = subprocess.run([sys.executable, "-c", snippet], cwd=REPO_ROOT,
                          env=env, capture_output=True, text=True, timeout=240)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(next(l for l in proc.stdout.splitlines()
                          if l.startswith("JSON>>"))[6:])
    assert out == {"rows": 2, "ex": 0}
    # The rows landed in the DATABASE_URL file — never in the data dir's
    # default location (decision d: the app's own engine, by construction).
    assert not (data_dir / "hcrm.db").exists()
    # The final catalogue line fired on this real boot path too.
    assert "Catalogue: 2 item(s) — source: supplier-csv" in proc.stderr

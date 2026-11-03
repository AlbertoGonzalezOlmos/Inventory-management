"""Example data: seed the supplier catalogue and demo items, backfill embeddings."""

import logging
import os
from pathlib import Path

from sqlmodel import Session, select

from app.embeddings import embed_text_blob, item_embedding_text
from app.models import Item

logger = logging.getLogger("hcrm")

EXAMPLE_ITEMS = [
    {
        "sku": "EX-001",
        "name": "Wireless Mouse",
        "description": (
            "Ergonomic wireless mouse with silent clicks, a 2.4 GHz USB receiver "
            "and an 18-month battery life. Comfortable for all-day office work."
        ),
        "category": "Electronics",
        "price_cents": 2499,
        "stock": 42,
    },
    {
        "sku": "EX-002",
        "name": "Mechanical Keyboard",
        "description": (
            "Compact 65% mechanical keyboard with hot-swappable brown switches, "
            "per-key RGB backlight and a detachable USB-C cable."
        ),
        "category": "Electronics",
        "price_cents": 8990,
        "stock": 15,
    },
    {
        "sku": "EX-003",
        "name": "Bluetooth Headphones",
        "description": (
            "Over-ear wireless headphones with active noise cancellation and a "
            "30-hour battery life, perfect for commuting and travel."
        ),
        "category": "Electronics",
        "price_cents": 12900,
        "stock": 8,
    },
    {
        "sku": "EX-004",
        "name": "Chef Knife 20 cm",
        "description": (
            "Forged stainless steel chef knife with an evenly balanced handle, "
            "ideal for precise chopping, dicing and slicing in the kitchen."
        ),
        "category": "Kitchen",
        "price_cents": 5950,
        "stock": 20,
    },
    {
        "sku": "EX-005",
        "name": "Cast Iron Skillet",
        "description": (
            "Pre-seasoned 26 cm cast iron skillet, oven safe and perfect for "
            "searing steaks, frying eggs or baking cornbread."
        ),
        "category": "Kitchen",
        "price_cents": 3995,
        "stock": 12,
    },
    {
        "sku": "EX-006",
        "name": "Electric Kettle",
        "description": (
            "Fast 1.7 L electric kettle with precise temperature control and a "
            "keep-warm function, great for tea and pour-over coffee."
        ),
        "category": "Kitchen",
        "price_cents": 4500,
        "stock": 25,
    },
    {
        "sku": "EX-007",
        "name": "The Pragmatic Programmer",
        "description": (
            "Classic software engineering book covering timeless coding practices, "
            "debugging techniques, refactoring and career growth for developers."
        ),
        "category": "Books",
        "price_cents": 3499,
        "stock": 30,
    },
    {
        "sku": "EX-008",
        "name": "Atomic Habits",
        "description": (
            "Bestselling self-improvement guide about building good habits and "
            "breaking bad ones through small, consistent daily changes."
        ),
        "category": "Books",
        "price_cents": 2190,
        "stock": 50,
    },
    {
        "sku": "EX-009",
        "name": "Hiking Backpack 30L",
        "description": (
            "Lightweight 30-litre hiking backpack with an included rain cover, "
            "ventilated back panel and hip belt pockets for day treks."
        ),
        "category": "Outdoor",
        "price_cents": 7400,
        "stock": 18,
    },
    {
        "sku": "EX-010",
        "name": "Insulated Water Bottle",
        "description": (
            "Double-walled stainless steel 750 ml bottle that keeps drinks cold "
            "for 24 hours or hot for 12 hours, with a leak-proof lid."
        ),
        "category": "Outdoor",
        "price_cents": 2750,
        "stock": 60,
    },
    {
        "sku": "EX-011",
        "name": "A5 Dotted Notebook",
        "description": (
            "Lay-flat A5 dotted notebook with 192 pages of thick 120 gsm paper, "
            "an elastic closure and a ribbon bookmark for bullet journaling."
        ),
        "category": "Office",
        "price_cents": 1290,
        "stock": 80,
    },
    {
        "sku": "EX-012",
        "name": "LED Desk Lamp",
        "description": (
            "Adjustable LED desk lamp with five colour temperatures, a stepless "
            "dimmer and a built-in USB charging port for your phone."
        ),
        "category": "Office",
        "price_cents": 3540,
        "stock": 22,
    },
]


def seed_example_items(session: Session) -> None:
    """Insert demo items so the software can be tested immediately."""
    existing = session.exec(select(Item).limit(1)).first()
    if existing is not None:
        return

    for spec in EXAMPLE_ITEMS:
        item = Item(**spec)
        item.embedding = embed_text_blob(
            item_embedding_text(item.name, item.description)
        )
        session.add(item)
    session.commit()
    logger.warning(
        "Seeded %d example items (SKUs EX-001…EX-012).", len(EXAMPLE_ITEMS)
    )


def backfill_embeddings(session: Session) -> None:
    """Compute embeddings for items that do not have one yet."""
    items = session.exec(select(Item).where(Item.embedding.is_(None))).all()
    if not items:
        return

    updated = 0
    for item in items:
        blob = embed_text_blob(item_embedding_text(item.name, item.description))
        if blob is None:  # model unavailable — try again on next startup
            break
        item.embedding = blob
        session.add(item)
        updated += 1

    if updated:
        session.commit()
        logger.info("Backfilled embeddings for %d item(s).", updated)


# --- the supplier catalogue (Biogan): create-only boot seeding ---------------

#: The data-dir master CSV's name — MUST equal scripts/_biogan.py's
#: default_csv_path().name (the media.py twin-rule pattern; a drift means
#: the boot seed looks where the crawler never writes). Test-pinned.
_DATA_DIR_CSV_NAME = "Biogan - masterdata - Sheet.csv"


def _catalogue_import_enabled() -> bool:
    """HCRM_CATALOGUE_IMPORT, the HCRM_DEV_ADMIN idiom: 0/false/no/off (case-
    insensitive) disables; unset, empty or any other value keeps it on."""
    return os.environ.get("HCRM_CATALOGUE_IMPORT", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _catalogue_source() -> tuple[str, Path] | None:
    """Resolve the supplier CSV: explicit env (no fallback) → the operator's
    data-dir copy (what the kiosk uses). None when no source exists — the
    supplier's master data is deliberately NOT committed to the repo
    (orchestrator decision B): a fresh clone provisions the CSV once (crawl,
    or copy the operator's file into the data dir / point
    HCRM_CATALOGUE_CSV at it)."""
    import app.database  # local: DATA_DIR is computed at import time

    env_csv = os.environ.get("HCRM_CATALOGUE_CSV")
    if env_csv:
        # Explicit means explicit: a set-but-unreadable path is an operator
        # error worth a loud line, never a silent fallback to another file.
        return "supplier-csv", Path(env_csv)
    data_dir_csv = Path(app.database.DATA_DIR) / _DATA_DIR_CSV_NAME
    if data_dir_csv.is_file():
        return "supplier-csv", data_dir_csv
    return None


def _catalogue_images_dir() -> Path:
    """The media route's twin rule: $HCRM_CATALOGUE_IMAGES_DIR, else
    <data_dir>/Catalogue-images. A missing dir is not an error (images
    degrade to the placeholder)."""
    import app.database

    env = os.environ.get("HCRM_CATALOGUE_IMAGES_DIR")
    if env:
        return Path(env)
    return Path(app.database.DATA_DIR) / "Catalogue-images"


def seed_catalogue(session: Session) -> dict:
    """Create-only supplier-catalogue seed, called from ``_startup`` before
    ``seed_example_items`` (a successful import is what keeps a fresh boot
    free of the 12 ``EX-*`` demo rows).

    Never mutates existing rows (``no_update``), never prunes, and never
    raises: any failure rolls the caller's session back, logs one loud
    warning naming the manual fix, and lets the boot continue with the demo
    seed. Rows land with ``embedding=None`` when the model is unavailable;
    ``backfill_embeddings`` (which runs right after) repairs them.

    Returns a status dict for :func:`final_catalogue_source` — the single
    final ``Catalogue:`` line is logged by ``_startup`` once the demo seed
    has run, on EVERY path; only the mid-seed diagnostics (no CSV (with
    how to provision one), an unreadable explicit CSV, a failed import,
    refused/bad rows, pending backfill) are logged here.
    """
    import app.database

    if not _catalogue_import_enabled():
        return {"source": "existing (import disabled)"}
    source = _catalogue_source()
    if source is None:
        logger.warning(
            "Catalogue: no supplier CSV — not seeding. Provision one: drop "
            "'Biogan - masterdata - Sheet.csv' into the data dir (%s), or "
            "point HCRM_CATALOGUE_CSV at a file",
            app.database.DATA_DIR)
        return {"source": "none"}
    source_kind, csv_path = source
    if not csv_path.is_file():
        # Only reachable for the explicit env path (the other was is_file).
        logger.error(
            "catalogue: HCRM_CATALOGUE_CSV=%s is set but unreadable — not seeding",
            csv_path)
        return {"source": "none"}
    try:
        # Lazy import: scripts.import_biogan imports app.seed (EXAMPLE_ITEMS),
        # so a top-level import here would be a cycle.
        from scripts.import_biogan import import_masterdata_file
        report = import_masterdata_file(
            session, csv_path, images_dir=_catalogue_images_dir(),
            no_update=True, prune_demo=False, create_without_embedding=True)
    except Exception as exc:  # a broken catalogue must not take the kiosk down
        session.rollback()
        logger.warning(
            "Catalogue: import of %s failed (%s) — rolled back; manual retry: "
            "uv run python scripts/import_biogan.py --csv %s --db %s",
            csv_path, exc, csv_path, app.database.DATABASE_URL)
        return {"source": "failed (rolled back)"}
    counts = report["counts"]
    pending = sum(1 for row in report["rows"]
                  if "embedding_backfill_pending" in row["review"])
    if pending:
        logger.warning("Catalogue: %d row(s) await embedding backfill "
                       "(model unavailable at seed time)", pending)
    if counts.get("refused") or counts.get("bad_row"):
        logger.warning("Catalogue: %d refused / %d bad row(s) skipped — see the "
                       "importer's --report for detail",
                       counts.get("refused", 0), counts.get("bad_row", 0))
    return {"source": "supplier-csv",
            "created": counts.get("created", 0), "report": report}


def final_catalogue_source(status: dict, items_before: int,
                           items_after: int) -> str:
    """The honest source label for ``_startup``'s single final line.

    ``supplier-csv`` only when the import actually created rows; an import
    that created nothing over a populated database is ``existing`` (the
    live kiosk's every-boot case), and ``demo-seed`` when the demo fallback
    filled a fresh database (with or without a CSV). ``none`` with nothing
    seeded is ``existing`` — an empty catalogue is what exists.
    """
    source = status["source"]
    if source in ("supplier-csv", "none"):
        if status.get("created", 0) > 0:
            return "supplier-csv"
        return "demo-seed" if items_after > items_before else "existing"
    return source  # existing (import disabled) / failed (rolled back)

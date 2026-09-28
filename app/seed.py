"""Example data: seed demo items on first run and backfill missing embeddings."""

import logging

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

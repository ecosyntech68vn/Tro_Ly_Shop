"""
SQLite database layer cho bot.
Tables:
  orders            — đơn hàng pending/paid
  unmatched         — giao dịch không khớp (admin review)
  product_links     — link Drive cho mỗi SKU (cập nhật được)
"""
import sqlite3
import os
import string
import random
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "bot.db")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            code         TEXT PRIMARY KEY,
            chat_id      INTEGER NOT NULL,
            sku          TEXT NOT NULL,
            amount       INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            bank_ref     TEXT,
            drive_link   TEXT,
            created_at   TEXT NOT NULL,
            paid_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_orders_chat ON orders(chat_id);
        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

        CREATE TABLE IF NOT EXISTS unmatched (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_id        TEXT,
            amount       INTEGER,
            content      TEXT,
            bank_ref     TEXT,
            ts           TEXT
        );

        CREATE TABLE IF NOT EXISTS product_links (
            sku          TEXT PRIMARY KEY,
            url          TEXT,
            updated_at   TEXT
        );
        """)


def gen_code():
    """Tạo mã đơn ngẫu nhiên: TXN + 6 ký tự."""
    chars = string.ascii_uppercase + string.digits
    return "TXN" + "".join(random.choices(chars, k=6))


def create_order(chat_id, sku, amount):
    """Tạo đơn mới, trả về mã đơn."""
    code = gen_code()
    with conn() as c:
        # Đảm bảo unique (cực hiếm trùng)
        while c.execute("SELECT 1 FROM orders WHERE code=?", (code,)).fetchone():
            code = gen_code()
        c.execute(
            "INSERT INTO orders(code, chat_id, sku, amount, status, created_at) "
            "VALUES(?, ?, ?, ?, 'pending', ?)",
            (code, chat_id, sku, amount, datetime.utcnow().isoformat())
        )
    return code


def get_pending_order_by_code(code):
    """Lấy đơn pending theo mã. Returns: (code, chat_id, sku, amount, status) or None"""
    with conn() as c:
        row = c.execute(
            "SELECT code, chat_id, sku, amount, status FROM orders "
            "WHERE code=? AND status='pending'",
            (code,)
        ).fetchone()
        return tuple(row) if row else None


def get_order_by_chat(chat_id):
    """Lấy đơn gần nhất của 1 chat. Returns: (code, sku, amount, status, drive_link) or None"""
    with conn() as c:
        row = c.execute(
            "SELECT code, sku, amount, status, drive_link FROM orders "
            "WHERE chat_id=? ORDER BY created_at DESC LIMIT 1",
            (chat_id,)
        ).fetchone()
        return tuple(row) if row else None


def mark_order_paid(code, bank_ref, drive_link):
    with conn() as c:
        c.execute(
            "UPDATE orders SET status='paid', bank_ref=?, drive_link=?, paid_at=? "
            "WHERE code=? AND status='pending'",
            (bank_ref, drive_link, datetime.utcnow().isoformat(), code)
        )


def log_unmatched_payment(tx_id, amount, content, ref):
    with conn() as c:
        c.execute(
            "INSERT INTO unmatched(tx_id, amount, content, bank_ref, ts) "
            "VALUES(?, ?, ?, ?, ?)",
            (str(tx_id), amount, content, ref, datetime.utcnow().isoformat())
        )


def get_unmatched_payments(limit=10):
    with conn() as c:
        rows = c.execute(
            "SELECT tx_id, amount, content, bank_ref, ts FROM unmatched "
            "ORDER BY id DESC LIMIT ?",
            (limit,)
        ).fetchall()
        return [tuple(r) for r in rows]


def update_product_link(sku, url):
    with conn() as c:
        c.execute(
            "INSERT INTO product_links(sku, url, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(sku) DO UPDATE SET url=excluded.url, updated_at=excluded.updated_at",
            (sku, url, datetime.utcnow().isoformat())
        )


def get_product_link(sku):
    with conn() as c:
        row = c.execute("SELECT url FROM product_links WHERE sku=?", (sku,)).fetchone()
        return row["url"] if row else None

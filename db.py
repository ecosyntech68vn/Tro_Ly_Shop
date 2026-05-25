"""
Database layer cho bot — hỗ trợ SQLite (dev) và PostgreSQL (production).
Tự động dùng PostgreSQL nếu có DATABASE_URL, fallback SQLite nếu không.
"""
import os
import re
import string
import random
from datetime import datetime, timedelta
from contextlib import contextmanager
from importlib import import_module

DB_PATH = os.environ.get("DB_PATH", "bot.db")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

PENDING_TIMEOUT_MINUTES = int(os.environ.get("PENDING_TIMEOUT_MINUTES", "120"))

USE_PG = bool(DATABASE_URL)


def _pg_connect():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _sqlite_connect():
    import sqlite3
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _db():
    if USE_PG:
        import psycopg2
        c = psycopg2.connect(DATABASE_URL)
        return c
    else:
        import sqlite3
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        return c


class _DB:
    """Wrapper đồng bộ SQLite và PostgreSQL."""
    def __init__(self, conn):
        self._conn = conn
        self._pg = USE_PG
        if USE_PG:
            import psycopg2.extras
            self._cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        else:
            self._cur = conn  # sqlite3 conn acts as cursor via execute()

    def execute(self, sql, params=None):
        if self._pg:
            pg_sql = re.sub(r"(?<!\?)\?(?!\?)", "%s", sql)
            self._cur.execute(pg_sql, params or ())
            return self._cur
        else:
            return self._conn.execute(sql, params or ())

    def commit(self):
        self._conn.commit()

    def close(self):
        if self._pg:
            self._cur.close()
        self._conn.close()


@contextmanager
def conn():
    db = _DB(_db())
    try:
        yield db
        db.commit()
    finally:
        db.close()


def _q(sql):
    """Chuyển placeholder ? -> %s cho PostgreSQL."""
    if not USE_PG:
        return sql
    return re.sub(r"(?<!\?)\?(?!\?)", "%s", sql)


@contextmanager
def conn():
    c = _db()
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _exec(ddl_sqlite, ddl_pg=None):
    """Execute DDL script, tự động chọn dialect theo database."""
    ddl = ddl_pg if USE_PG else ddl_sqlite
    if USE_PG:
        import psycopg2
        c = psycopg2.connect(DATABASE_URL)
        cur = c.cursor()
        for stmt in ddl.split(";"):
            s = stmt.strip()
            if s:
                cur.execute(s)
        c.commit()
        cur.close()
        c.close()
    else:
        import sqlite3
        c = sqlite3.connect(DB_PATH)
        c.executescript(ddl)
        c.close()


def init_db():
    from agent_db import init_agent_tables
    init_agent_tables()
    _exec(
        """
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
        """,
        """
        CREATE TABLE IF NOT EXISTS orders (
            code         TEXT PRIMARY KEY,
            chat_id      BIGINT NOT NULL,
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
            id           SERIAL PRIMARY KEY,
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
        """
    )


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
    """Lấy đơn pending theo mã (chưa expired). Returns: tuple or None."""
    cutoff = (datetime.utcnow() - timedelta(minutes=PENDING_TIMEOUT_MINUTES)).isoformat()
    with conn() as c:
        row = c.execute(
            "SELECT code, chat_id, sku, amount, status FROM orders "
            "WHERE code=? AND status='pending' AND created_at >= ?",
            (code, cutoff)
        ).fetchone()
        return tuple(row) if row else None


def get_order_status(code):
    """Trả về trạng thái thực tế của 1 mã đơn (kèm timeout check).

    Returns dict:
      {"status": "not_found"}
      {"status": "expired", "code", "chat_id", "sku", "amount", "created_at", "minutes_ago"}
      {"status": "paid",    "code", "chat_id", "sku", "amount", "paid_at"}
      {"status": "pending", "code", "chat_id", "sku", "amount", "created_at"}
    """
    now = datetime.utcnow()
    cutoff = now - timedelta(minutes=PENDING_TIMEOUT_MINUTES)
    with conn() as c:
        row = c.execute(
            "SELECT code, chat_id, sku, amount, status, created_at, paid_at "
            "FROM orders WHERE code=?",
            (code,)
        ).fetchone()
        if not row:
            return {"status": "not_found"}
        r = dict(row)
        if r["status"] == "paid":
            return {**r, "status": "paid"}
        # status = pending → check expired
        try:
            created = datetime.fromisoformat(r["created_at"])
            minutes_ago = int((now - created).total_seconds() // 60)
            if created < cutoff:
                return {**r, "status": "expired", "minutes_ago": minutes_ago}
            return {**r, "status": "pending", "minutes_ago": minutes_ago}
        except Exception:
            return {**r, "status": r["status"]}


def expire_stale_orders():
    """Mark các đơn pending quá timeout thành 'expired'.
    Có thể gọi từ scheduler/cron để gọn DB. Returns: list of (code, chat_id, sku)."""
    cutoff = (datetime.utcnow() - timedelta(minutes=PENDING_TIMEOUT_MINUTES)).isoformat()
    with conn() as c:
        rows = c.execute(
            "SELECT code, chat_id, sku FROM orders "
            "WHERE status='pending' AND created_at < ?",
            (cutoff,)
        ).fetchall()
        cancelled = [tuple(r) for r in rows]
        if cancelled:
            c.execute(
                "UPDATE orders SET status='expired' WHERE status='pending' AND created_at < ?",
                (cutoff,)
            )
    return cancelled


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


def get_unmatched_payments_page(page=0, page_size=5):
    """Paginated unmatched payments. Returns (rows, total_count)."""
    offset = page * page_size
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM unmatched").fetchone()[0]
        rows = c.execute(
            "SELECT tx_id, amount, content, bank_ref, ts FROM unmatched "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset)
        ).fetchall()
        return [tuple(r) for r in rows], total


def get_today_stats():
    """Thống kê hôm nay. Returns dict."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with conn() as c:
        today_orders = c.execute(
            "SELECT COUNT(*) FROM orders WHERE status='paid' AND paid_at >= ?",
            (today,)
        ).fetchone()[0]
        today_revenue = c.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status='paid' AND paid_at >= ?",
            (today,)
        ).fetchone()[0]
        total_paid = c.execute(
            "SELECT COUNT(*) FROM orders WHERE status='paid'"
        ).fetchone()[0]
        total_revenue = c.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status='paid'"
        ).fetchone()[0]
        pending = c.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        ).fetchone()[0]
        unmatched = c.execute(
            "SELECT COUNT(*) FROM unmatched"
        ).fetchone()[0]
    return {
        "today_orders": today_orders,
        "today_revenue": today_revenue,
        "total_paid": total_paid,
        "total_revenue": total_revenue,
        "pending": pending,
        "unmatched": unmatched,
    }


def update_product_link(sku, url):
    with conn() as c:
        if USE_PG:
            c.execute(
                "INSERT INTO product_links(sku, url, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(sku) DO UPDATE SET url=EXCLUDED.url, updated_at=EXCLUDED.updated_at",
                (sku, url, datetime.utcnow().isoformat())
            )
        else:
            c.execute(
                "INSERT INTO product_links(sku, url, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(sku) DO UPDATE SET url=excluded.url, updated_at=excluded.updated_at",
                (sku, url, datetime.utcnow().isoformat())
            )


def get_product_link(sku):
    with conn() as c:
        row = c.execute("SELECT url FROM product_links WHERE sku=?", (sku,)).fetchone()
        return row["url"] if row else None


def get_pending_orders(page=0, page_size=5):
    """Lấy danh sách đơn pending chưa hết hạn."""
    cutoff = (datetime.utcnow() - timedelta(minutes=PENDING_TIMEOUT_MINUTES)).isoformat()
    offset = page * page_size
    with conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending' AND created_at >= ?",
            (cutoff,)
        ).fetchone()[0]
        rows = c.execute(
            "SELECT code, chat_id, sku, amount, created_at FROM orders "
            "WHERE status='pending' AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (cutoff, page_size, offset)
        ).fetchall()
        return [tuple(r) for r in rows], total


def get_recent_orders(days=7):
    """Lấy đơn gần đây để báo cáo."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    with conn() as c:
        rows = c.execute(
            "SELECT code, sku, amount, status, created_at, paid_at FROM orders "
            "WHERE created_at >= ? ORDER BY created_at DESC LIMIT 20",
            (since,)
        ).fetchall()
        return [tuple(r) for r in rows]

"""
Agent database layer — subscriptions, onboarding profiles, chat history.
"""
import json
import hashlib
import logging
from datetime import datetime, timedelta
from db import conn, USE_PG

log = logging.getLogger(__name__)

# ========== PLANS ==========

AGENT_PLANS = {
    "agent_basic": {
        "name": "AI Agent Cơ Bản",
        "price": 99000,
        "model": "gemini",
        "daily_msgs": 30,
        "desc": "Gemini AI, 30 tin/ngày, 1 Agent"
    },
    "agent_pro": {
        "name": "AI Agent Pro",
        "price": 199000,
        "model": "claude",
        "daily_msgs": 100,
        "desc": "Claude AI, 100 tin/ngày, 3 Agent"
    },
    "agent_business": {
        "name": "AI Agent Business",
        "price": 499000,
        "model": "gpt4",
        "daily_msgs": 500,
        "desc": "GPT-4, 500 tin/ngày, không giới hạn Agent"
    },
}


# ========== GROUP MODE FUNCTIONS ==========

def register_group(group_chat_id, owner_chat_id):
    """Register a group when bot is added. Returns existing record if any."""
    now = datetime.utcnow().isoformat()
    with conn() as c:
        existing = c.execute(
            "SELECT * FROM agent_groups WHERE group_chat_id=?", (group_chat_id,)
        ).fetchone()
        if existing:
            ex = dict(existing)
            if ex["status"] == "inactive":
                c.execute(
                    "UPDATE agent_groups SET status='pending', owner_chat_id=? WHERE group_chat_id=?",
                    (owner_chat_id, group_chat_id)
                )
            return dict(existing)
        c.execute(
            "INSERT INTO agent_groups(group_chat_id, owner_chat_id, status, created_at) VALUES(?, ?, 'pending', ?)",
            (group_chat_id, owner_chat_id, now)
        )
        return None


def activate_group(group_chat_id):
    """Mark a group as active after owner claims it."""
    now = datetime.utcnow().isoformat()
    with conn() as c:
        c.execute(
            "UPDATE agent_groups SET status='active', activated_at=? WHERE group_chat_id=?",
            (now, group_chat_id)
        )


def deactivate_group(group_chat_id):
    with conn() as c:
        c.execute(
            "UPDATE agent_groups SET status='inactive' WHERE group_chat_id=?", (group_chat_id,)
        )


def get_owner_for_group(group_chat_id):
    """Get the owner chat_id for a group. Only returns if active."""
    with conn() as c:
        row = c.execute(
            "SELECT owner_chat_id FROM agent_groups WHERE group_chat_id=? AND status='active'",
            (group_chat_id,)
        ).fetchone()
        return row["owner_chat_id"] if row else None


def get_groups_for_owner(owner_chat_id):
    """List all groups owned by a subscriber."""
    with conn() as c:
        rows = c.execute(
            "SELECT group_chat_id, status, created_at, activated_at FROM agent_groups WHERE owner_chat_id=?",
            (owner_chat_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_agent_group_stats():
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM agent_groups").fetchone()[0]
        active = c.execute("SELECT COUNT(*) FROM agent_groups WHERE status='active'").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM agent_groups WHERE status='pending'").fetchone()[0]
        return {"total": total, "active": active, "pending": pending}


def set_group_mode(group_chat_id, mode):
    if mode not in ("mention", "smart", "auto"):
        return False
    with conn() as c:
        c.execute("UPDATE agent_groups SET mode=? WHERE group_chat_id=?", (mode, group_chat_id))
    return True


def get_group_mode(group_chat_id):
    with conn() as c:
        row = c.execute("SELECT mode FROM agent_groups WHERE group_chat_id=?", (group_chat_id,)).fetchone()
        return row["mode"] if row else "mention"


_ABBR = {"bn": "bao nhieu", "ko": "khong", "k": "khong",
         "dc": "duoc", "vs": "voi", "đc": "duoc",
         "sp": "san pham", "shop": "shop", "ship": "ship",
         "ntn": "nhu the nao", "sao": "the nao", "s": "sao",
         "luon": "luon", "l": "luon", "r": "roi"}


def _nd(s):
    """Remove diacritics + strip punctuation + expand common abbreviations."""
    import unicodedata, re
    t = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    t = re.sub(r'[^\w\s]', ' ', t.lower())
    words = t.split()
    expanded = [_ABBR.get(w, w) for w in words]
    return " ".join(expanded)


def is_question_message(text):
    """Detect if a message is likely a customer question (for smart mode)."""
    t = text.lower().strip()
    nd = _nd(t)

    if "?" in nd:
        return True

    qs = ("cho em hoi", "cho minh hoi", "cho toi hoi",
          "shop oi", "ad oi", "admin oi",
          "chi oi", "anh oi", "em oi",
          "minh oi", "ban qtv oi", "ban admin oi")
    for s in qs:
        if nd.startswith(s):
            return True

    kw = ("gia", "bao nhieu", "con khong", "co khong", "ship",
          "giao hang", "dat hang", "mua", "thanh toan",
          "chuyen khoan", "size", "mau", "mau", "chat lieu",
          "con hang", "het hang", "khi nao", "lam sao",
          "huong dan", "cach", "dia chi", "so dien thoai")
    for k in kw:
        if k in nd:
            return True
    return False


# ========== WEB WIDGET TOKEN ==========

def get_or_create_web_token(chat_id):
    """Get existing token or generate a new one for the web widget."""
    with conn() as c:
        row = c.execute("SELECT web_token FROM agent_profiles WHERE chat_id=?", (chat_id,)).fetchone()
        if row and row["web_token"]:
            return row["web_token"]
    token = hashlib.sha256(f"web_{chat_id}_{datetime.utcnow().isoformat()}".encode()).hexdigest()[:16]
    with conn() as c:
        c.execute("UPDATE agent_profiles SET web_token=? WHERE chat_id=?", (token, chat_id))
    return token


def get_owner_by_web_token(token):
    """Look up owner chat_id by web widget token."""
    with conn() as c:
        row = c.execute(
            "SELECT chat_id FROM agent_profiles WHERE web_token=? AND onboarding_done=1",
            (token,)
        ).fetchone()
        return row["chat_id"] if row else None


# ========== PRODUCT CATALOG ==========

def import_products_from_csv(owner_chat_id, csv_text):
    """Parse CSV and insert/update products. Returns (count, error)."""
    import csv, io
    reader = csv.DictReader(io.StringIO(csv_text))
    now = datetime.utcnow().isoformat()
    count = 0
    with conn() as c:
        # Clear old products for clean import
        c.execute("DELETE FROM agent_products WHERE owner_chat_id=?", (owner_chat_id,))
        for row in reader:
            name = (row.get("name") or row.get("Name") or "").strip()
            if not name:
                continue
            sku = (row.get("sku") or row.get("SKU") or row.get("ma") or row.get("Mã") or "").strip()
            price_raw = (row.get("price") or row.get("Price") or row.get("gia") or row.get("Giá") or "0").strip()
            try:
                price = int(float(price_raw.replace(",", "").replace(".", "")))
            except:
                price = 0
            stock_raw = (row.get("stock") or row.get("Stock") or row.get("ton") or row.get("Tồn") or "0").strip()
            try:
                stock = int(stock_raw)
            except:
                stock = 0
            desc = (row.get("description") or row.get("Description") or row.get("mota") or row.get("Mô tả") or "").strip()
            cat = (row.get("category") or row.get("Category") or row.get("danh muc") or row.get("Danh mục") or "").strip()
            attrs = {}
            for k, v in row.items():
                if k.lower() in ("size", "color", "mau", "màu", "chat lieu", "chất liệu"):
                    attrs[k.strip()] = v.strip()
            c.execute(
                """INSERT INTO agent_products(owner_chat_id, sku, name, description, price, category, attributes, stock, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (owner_chat_id, sku, name, desc, price, cat, json.dumps(attrs, ensure_ascii=False), stock, now)
            )
            count += 1
    return count


def search_products(owner_chat_id, query, limit=5):
    """Keyword search across product name, description, sku, category.
    Python-side matching (handles Vietnamese Unicode correctly).
    Max 500 candidates loaded per shop to bound memory."""
    keywords = query.lower().split()
    if not keywords:
        return []
    with conn() as c:
        rows = c.execute(
            "SELECT * FROM agent_products WHERE owner_chat_id=? LIMIT 500",
            (owner_chat_id,)
        ).fetchall()
    scored = []
    for row in rows:
        d = dict(row)
        haystack = f"{d['name']} {d.get('description','')} {d.get('category','')} {d.get('sku','')}".lower()
        score = sum(1 for kw in keywords if kw in haystack)
        if score:
            scored.append((score, d))
    scored.sort(key=lambda x: -x[0])
    return [r[1] for r in scored[:limit]]


def get_product_count(owner_chat_id):
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM agent_products WHERE owner_chat_id=?", (owner_chat_id,)
        ).fetchone()
        return row[0] if row else 0


def clear_products(owner_chat_id):
    with conn() as c:
        c.execute("DELETE FROM agent_products WHERE owner_chat_id=?", (owner_chat_id,))


# ========== LEARNED RESPONSES (từ sửa lỗi) ==========

def save_correction(owner_chat_id, question, answer):
    """Store a corrected answer for a question."""
    keywords = " ".join(sorted(set(_nd(question).split())))
    now = datetime.utcnow().isoformat()
    with conn() as c:
        c.execute(
            """INSERT INTO agent_learned(owner_chat_id, keywords, question, answer, created_at)
               VALUES(?, ?, ?, ?, ?)""",
            (owner_chat_id, keywords, question, answer, now)
        )


def find_correction_match(owner_chat_id, query, threshold=0.45):
    """Find a saved correction matching the query by keyword overlap.
    SQL pre-filter (any keyword match) + Python scoring on reduced set.
    Returns (question, answer) or None."""
    query_kw = set(_nd(query).split())
    if not query_kw:
        return None
    # SQL: only load rows that share at least one keyword
    clauses = " OR ".join(["keywords LIKE ?" for _ in query_kw])
    params = [owner_chat_id] + [f"%{kw}%" for kw in query_kw]
    with conn() as c:
        rows = c.execute(
            f"SELECT question, answer, keywords FROM agent_learned WHERE owner_chat_id=? AND ({clauses}) LIMIT 200",
            params
        ).fetchall()
    best = None
    best_score = 0
    for row in rows:
        cor_kw = set(row["keywords"].split())
        overlap = len(query_kw & cor_kw)
        score = overlap / max(len(query_kw), len(cor_kw))
        if score > best_score:
            best_score = score
            best = (row["question"], row["answer"])
    return best if best_score >= threshold else None


def count_corrections(owner_chat_id):
    with conn() as c:
        row = c.execute(
            "SELECT COUNT(*) FROM agent_learned WHERE owner_chat_id=?", (owner_chat_id,)
        ).fetchone()
        return row[0] if row else 0


def format_products_text(products):
    """Format product list for prompt injection."""
    if not products:
        return ""
    lines = ["SẢN PHẨM CỦA SHOP:"]
    for p in products:
        parts = [p["name"]]
        if p.get("sku"):
            parts.append(f"({p['sku']})")
        if p.get("price"):
            parts.append(f"{p['price']:,}đ")
        if p.get("color"):
            parts.append(f"màu {p['color']}")
        if p.get("stock", 0) > 0:
            parts.append(f"còn {p['stock']}")
        elif p.get("stock") is not None:
            parts.append("hết hàng")
        lines.append("- " + " ".join(parts))
        if p.get("description"):
            lines.append(f"  {p['description']}")
    return "\n".join(lines)


def _inject_product_context(system, owner_chat_id, user_message):
    """If message looks like a product query, inject catalog results into system prompt."""
    keywords = ("giá", "bao nhiêu", "còn", "hết", "mua", "đặt", "size",
                "màu", "mẫu", "sản phẩm", "hàng", "ship", "catalog",
                "có", "loại", "loai", "sp", "spham", "danh mục")
    nd_lower = _nd(user_message)
    if not any(k in nd_lower for k in keywords):
        return system
    products = search_products(owner_chat_id, user_message, limit=5)
    if not products:
        return system
    pt = format_products_text(products)
    return f"{system}\n\n{pt}\n\nKhi khách hỏi về sản phẩm, hãy dùng thông tin trên để trả lời chính xác."


def build_agent_prompt(chat_id, user_message):
    """Build the full prompt for the AI model."""
    profile = get_shop_profile(chat_id)
    sub = get_subscription(chat_id)
    if not profile:
        return None, ""

    industry_knowledge = get_industry_knowledge(profile.get("industry", ""))
    voice_map = {"chuyen-nghiep": "Chuyên nghiệp, lịch sự, dùng số liệu",
                 "than-thien": "Thân thiện, gần gũi, tự nhiên như người Việt",
                 "hai-huoc": "Hài hước, trẻ trung, thoải mái, có tiếng cười"}
    voice = voice_map.get(profile.get("brand_voice", ""), "Tự nhiên, thân thiện")

    system = f"""Bạn là trợ lý AI bán hàng cho shop {profile.get('shop_name', 'không tên')}.

THÔNG TIN SHOP:
- Ngành: {profile.get('industry', '')}
- Giọng văn: {voice}
- Khách hàng mục tiêu: {profile.get('target_customer', '')}
- Sản phẩm: {profile.get('product_info', '')}
- FAQ: {profile.get('faq', '')}

KIẾN THỨC NGÀNH:
{industry_knowledge}

HƯỚNG DẪN:
- Trả lời bằng tiếng Việt tự nhiên
- Luôn giữ giọng văn phù hợp với shop
- Khi được hỏi về sản phẩm, hãy dùng đúng thông tin từ hồ sơ shop
- Nếu không biết, nói thật là không biết — đừng bịa
- Kết thúc bằng một câu hỏi hoặc gợi ý nếu phù hợp"""

    system_with_products = _inject_product_context(system, chat_id, user_message)
    return system_with_products, f"{system_with_products}\n\nNgười dùng: {user_message}\n\nTrợ lý:"


def build_group_agent_prompt(owner_chat_id, user_message, sender_name):
    """Build prompt for group chat — agent represents the shop to end customers."""
    profile = get_shop_profile(owner_chat_id)
    if not profile:
        return None, ""

    industry_knowledge = get_industry_knowledge(profile.get("industry", ""))
    voice_map = {"chuyen-nghiep": "Chuyên nghiệp, lịch sự, dùng số liệu",
                 "than-thien": "Thân thiện, gần gũi, tự nhiên như người Việt",
                 "hai-huoc": "Hài hước, trẻ trung, thoải mái, có tiếng cười"}
    voice = voice_map.get(profile.get("brand_voice", ""), "Tự nhiên, thân thiện")

    system = f"""Bạn là trợ lý bán hàng tự động của shop {profile.get('shop_name', 'không tên')}.
Bạn đang ở trong một group chat với khách hàng của shop.

THÔNG TIN SHOP:
- Ngành: {profile.get('industry', '')}
- Giọng văn: {voice}
- Sản phẩm: {profile.get('product_info', '')}
- FAQ: {profile.get('faq', '')}

KIẾN THỨC NGÀNH:
{industry_knowledge}

HƯỚNG DẪN:
- Trả lời bằng tiếng Việt tự nhiên, thân thiện, lịch sự
- Xưng hô với khách hàng bằng "bạn", "anh/chị" tuỳ ngữ cảnh
- Trả lời ngắn gọn, đúng trọng tâm câu hỏi
- Khi được hỏi về sản phẩm, hãy dùng đúng thông tin từ hồ sơ shop
- Nếu không biết, nói "em sẽ nhờ shop hỗ trợ thêm ạ"
- Luôn giữ thái độ hỗ trợ, không tranh luận với khách hàng
- KHÔNG chào hỏi dài dòng — trả lời thẳng câu hỏi"""

    system_with_products = _inject_product_context(system, owner_chat_id, user_message)
    return system_with_products, f"{system_with_products}\n\nKhách hàng ({sender_name}): {user_message}\n\nTrợ lý:"



MONTHLY_DAYS = 30

# ========== DB FUNCTIONS ==========

def init_agent_tables():
    """Create agent tables. Called from db.init_db()."""
    from db import _exec
    _exec(
        """
        CREATE TABLE IF NOT EXISTS agent_subscriptions (
            chat_id      INTEGER PRIMARY KEY,
            plan         TEXT NOT NULL,
            model        TEXT NOT NULL DEFAULT 'gemini',
            status       TEXT NOT NULL DEFAULT 'active',
            daily_msgs   INTEGER DEFAULT 30,
            msgs_today   INTEGER DEFAULT 0,
            msg_date     TEXT,
            expires_at   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            renewed_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_profiles (
            chat_id       INTEGER PRIMARY KEY,
            shop_name     TEXT,
            industry      TEXT,
            brand_voice   TEXT,
            product_info  TEXT,
            target_customer TEXT,
            faq           TEXT,
            onboarding_done INTEGER DEFAULT 0,
            updated_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_onboarding (
            chat_id       INTEGER PRIMARY KEY,
            step          TEXT DEFAULT 'shop_name',
            data          TEXT DEFAULT '{}',
            updated_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_chats (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_chat_id INTEGER NOT NULL,
            customer_id   TEXT NOT NULL DEFAULT 'owner',
            role          TEXT NOT NULL,
            message       TEXT NOT NULL,
            model_used    TEXT,
            tokens_used   INTEGER DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_chats_owner ON agent_chats(owner_chat_id);
        CREATE INDEX IF NOT EXISTS idx_agent_chats_customer ON agent_chats(owner_chat_id, customer_id);

        CREATE TABLE IF NOT EXISTS agent_groups (
            group_chat_id INTEGER PRIMARY KEY,
            owner_chat_id INTEGER NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            mode         TEXT NOT NULL DEFAULT 'mention',
            created_at   TEXT NOT NULL,
            activated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_products (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_chat_id INTEGER NOT NULL,
            sku           TEXT,
            name          TEXT NOT NULL,
            description   TEXT,
            price         INTEGER,
            category      TEXT,
            attributes    TEXT,
            stock         INTEGER DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_products_owner ON agent_products(owner_chat_id);

        CREATE TABLE IF NOT EXISTS agent_learned (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_chat_id INTEGER NOT NULL,
            keywords      TEXT NOT NULL,
            question      TEXT NOT NULL,
            answer        TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_learned_owner ON agent_learned(owner_chat_id);
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_subscriptions (
            chat_id      BIGINT PRIMARY KEY,
            plan         TEXT NOT NULL,
            model        TEXT NOT NULL DEFAULT 'gemini',
            status       TEXT NOT NULL DEFAULT 'active',
            daily_msgs   INTEGER DEFAULT 30,
            msgs_today   INTEGER DEFAULT 0,
            msg_date     TEXT,
            expires_at   TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            renewed_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_profiles (
            chat_id       BIGINT PRIMARY KEY,
            shop_name     TEXT,
            industry      TEXT,
            brand_voice   TEXT,
            product_info  TEXT,
            target_customer TEXT,
            faq           TEXT,
            onboarding_done INTEGER DEFAULT 0,
            updated_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_onboarding (
            chat_id       BIGINT PRIMARY KEY,
            step          TEXT DEFAULT 'shop_name',
            data          TEXT DEFAULT '{}',
            updated_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_chats (
            id            SERIAL PRIMARY KEY,
            owner_chat_id BIGINT NOT NULL,
            customer_id   TEXT NOT NULL DEFAULT 'owner',
            role          TEXT NOT NULL,
            message       TEXT NOT NULL,
            model_used    TEXT,
            tokens_used   INTEGER DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_chats_owner ON agent_chats(owner_chat_id);
        CREATE INDEX IF NOT EXISTS idx_agent_chats_customer ON agent_chats(owner_chat_id, customer_id);

        CREATE TABLE IF NOT EXISTS agent_groups (
            group_chat_id BIGINT PRIMARY KEY,
            owner_chat_id BIGINT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            mode         TEXT NOT NULL DEFAULT 'mention',
            created_at   TEXT NOT NULL,
            activated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS agent_products (
            id            SERIAL PRIMARY KEY,
            owner_chat_id BIGINT NOT NULL,
            sku           TEXT,
            name          TEXT NOT NULL,
            description   TEXT,
            price         INTEGER,
            category      TEXT,
            attributes    TEXT,
            stock         INTEGER DEFAULT 0,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_products_owner ON agent_products(owner_chat_id);

        CREATE TABLE IF NOT EXISTS agent_learned (
            id            SERIAL PRIMARY KEY,
            owner_chat_id BIGINT NOT NULL,
            keywords      TEXT NOT NULL,
            question      TEXT NOT NULL,
            answer        TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_agent_learned_owner ON agent_learned(owner_chat_id);
        """
    )

    # Migration: add columns that may not exist on older tables
    for tbl_col, pg_sql in [
        ("agent_groups ADD COLUMN mode TEXT DEFAULT 'mention'",
         "ALTER TABLE agent_groups ADD COLUMN mode TEXT DEFAULT 'mention'"),
        ("agent_profiles ADD COLUMN web_token TEXT",
         "ALTER TABLE agent_profiles ADD COLUMN web_token TEXT"),
    ]:
        try:
            _exec(f"ALTER TABLE {tbl_col}", pg_sql)
        except Exception:
            pass  # column already exists

    # Index: composite index for dashboard ORDER BY created_at DESC queries
    try:
        _exec(
            "CREATE INDEX IF NOT EXISTS idx_agent_chats_created ON agent_chats(owner_chat_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_agent_chats_created ON agent_chats(owner_chat_id, created_at)"
        )
    except Exception:
        pass

    log.info("Agent tables initialized.")


def create_subscription(chat_id, plan, model, daily_msgs):
    """Create or renew a subscription. Returns expiry date."""
    expires = (datetime.utcnow() + timedelta(days=MONTHLY_DAYS)).isoformat()
    now = datetime.utcnow().isoformat()
    with conn() as c:
        existing = c.execute(
            "SELECT status, expires_at FROM agent_subscriptions WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if existing:
            # Extend from current expiry (if active) or from now
            old_expiry = existing["expires_at"]
            try:
                base = datetime.fromisoformat(old_expiry)
                if existing["status"] != "active" or base < datetime.utcnow():
                    base = datetime.utcnow()
            except:
                base = datetime.utcnow()
            new_expires = (base + timedelta(days=MONTHLY_DAYS)).isoformat()
            c.execute(
                """UPDATE agent_subscriptions 
                   SET plan=?, model=?, status='active', daily_msgs=?, msgs_today=0, msg_date=?, 
                       expires_at=?, renewed_at=?
                   WHERE chat_id=?""",
                (plan, model, daily_msgs, datetime.utcnow().strftime("%Y-%m-%d"), new_expires, now, chat_id)
            )
            return new_expires
        else:
            c.execute(
                """INSERT INTO agent_subscriptions(chat_id, plan, model, status, daily_msgs, 
                   msgs_today, msg_date, expires_at, created_at, renewed_at)
                   VALUES(?, ?, ?, 'active', ?, 0, ?, ?, ?, ?)""",
                (chat_id, plan, model, daily_msgs, datetime.utcnow().strftime("%Y-%m-%d"), expires, now, now)
            )
            return expires


def get_subscription(chat_id):
    """Get subscription info. Returns dict or None. Auto-checks expiry."""
    with conn() as c:
        row = c.execute(
            "SELECT * FROM agent_subscriptions WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if not row:
            return None
        sub = dict(row)
        # Check expiry
        try:
            expires = datetime.fromisoformat(sub["expires_at"])
            if expires < datetime.utcnow() and sub["status"] == "active":
                # Auto-expire
                c.execute(
                    "UPDATE agent_subscriptions SET status='expired' WHERE chat_id=?",
                    (chat_id,)
                )
                sub["status"] = "expired"
        except:
            pass
        # Reset daily counter if new day
        today = datetime.utcnow().strftime("%Y-%m-%d")
        if sub.get("msg_date") != today:
            c.execute(
                "UPDATE agent_subscriptions SET msgs_today=0, msg_date=? WHERE chat_id=?",
                (today, chat_id)
            )
            sub["msgs_today"] = 0
        return sub


def can_send_message(chat_id):
    """Check if subscription is active and under daily limit."""
    sub = get_subscription(chat_id)
    if not sub or sub["status"] != "active":
        return False, "Hết hạn"
    if sub["msgs_today"] >= sub["daily_msgs"]:
        return False, "Hết tin nhắn trong ngày"
    return True, "ok"


def increment_msg_count(chat_id):
    with conn() as c:
        c.execute(
            "UPDATE agent_subscriptions SET msgs_today = msgs_today + 1 WHERE chat_id=?",
            (chat_id,)
        )


def save_onboarding_state(chat_id, step, data=None):
    now = datetime.utcnow().isoformat()
    data_json = json.dumps(data or {}, ensure_ascii=False)
    with conn() as c:
        existing = c.execute(
            "SELECT 1 FROM agent_onboarding WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if existing:
            c.execute(
                "UPDATE agent_onboarding SET step=?, data=?, updated_at=? WHERE chat_id=?",
                (step, data_json, now, chat_id)
            )
        else:
            c.execute(
                "INSERT INTO agent_onboarding(chat_id, step, data, updated_at) VALUES(?, ?, ?, ?)",
                (chat_id, step, data_json, now)
            )


def get_onboarding_state(chat_id):
    with conn() as c:
        row = c.execute(
            "SELECT step, data FROM agent_onboarding WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if not row:
            return None
        return {"step": row["step"], "data": json.loads(row["data"])}


def clear_onboarding_state(chat_id):
    with conn() as c:
        c.execute("DELETE FROM agent_onboarding WHERE chat_id=?", (chat_id,))


def save_shop_profile(chat_id, shop_name, industry, brand_voice, product_info, target_customer, faq):
    now = datetime.utcnow().isoformat()
    with conn() as c:
        existing = c.execute(
            "SELECT 1 FROM agent_profiles WHERE chat_id=?", (chat_id,)
        ).fetchone()
        if existing:
            c.execute(
                """UPDATE agent_profiles SET shop_name=?, industry=?, brand_voice=?, 
                   product_info=?, target_customer=?, faq=?, onboarding_done=1, updated_at=?
                   WHERE chat_id=?""",
                (shop_name, industry, brand_voice, product_info, target_customer, faq, now, chat_id)
            )
        else:
            c.execute(
                """INSERT INTO agent_profiles(chat_id, shop_name, industry, brand_voice,
                   product_info, target_customer, faq, onboarding_done, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (chat_id, shop_name, industry, brand_voice, product_info, target_customer, faq, now)
            )


def get_shop_profile(chat_id):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM agent_profiles WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return dict(row) if row else None


def is_onboarding_complete(chat_id):
    with conn() as c:
        row = c.execute(
            "SELECT onboarding_done FROM agent_profiles WHERE chat_id=?", (chat_id,)
        ).fetchone()
        return bool(row and row["onboarding_done"])


def get_industry_knowledge(industry):
    """Load industry knowledge file content by name."""
    mapping = {
        "thoi-trang": "06-thoi-trang.md",
        "f-b": "07-f-b.md",
        "lam-dep": "08-lam-dep.md",
        "me-be": "09-me-be.md",
        "cong-nghe": "10-cong-nghe.md",
        "noi-that": "11-noi-that.md",
        "suc-khoe": "12-suc-khoe.md",
        "giao-duc": "13-giao-duc.md",
        "du-lich": "14-du-lich.md",
        "dich-vu": "15-dich-vu-agency.md",
    }
    filename = mapping.get(industry)
    if not filename:
        return ""
    path = f"copywriter-viet-pro/knowledge/{filename}"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except:
        log.warning(f"Cannot read knowledge file: {path}")
        return ""


def save_chat(owner_chat_id, customer_id, role, message, model_used=None, tokens=0):
    now = datetime.utcnow().isoformat()
    with conn() as c:
        c.execute(
            """INSERT INTO agent_chats(owner_chat_id, customer_id, role, message, model_used, tokens_used, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (owner_chat_id, customer_id, role, message, model_used, tokens, now)
        )


def get_recent_conversation(owner_chat_id, customer_id="owner", limit=10):
    """Get recent chat history for context."""
    with conn() as c:
        rows = c.execute(
            """SELECT role, message FROM agent_chats 
               WHERE owner_chat_id=? AND customer_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (owner_chat_id, customer_id, limit)
        ).fetchall()
        return list(reversed(rows))


def get_agent_stats():
    """Admin stats for agent module."""
    with conn() as c:
        total = c.execute("SELECT COUNT(*) FROM agent_subscriptions WHERE status='active'").fetchone()[0]
        basic = c.execute("SELECT COUNT(*) FROM agent_subscriptions WHERE status='active' AND plan='agent_basic'").fetchone()[0]
        pro = c.execute("SELECT COUNT(*) FROM agent_subscriptions WHERE status='active' AND plan='agent_pro'").fetchone()[0]
        biz = c.execute("SELECT COUNT(*) FROM agent_subscriptions WHERE status='active' AND plan='agent_business'").fetchone()[0]
        today = datetime.utcnow().strftime("%Y-%m-%d")
        msgs_today = c.execute(
            "SELECT COALESCE(SUM(msgs_today), 0) FROM agent_subscriptions WHERE msg_date=?", (today,)
        ).fetchone()[0]
        total_chats = c.execute("SELECT COUNT(*) FROM agent_chats").fetchone()[0]
        expired = c.execute("SELECT COUNT(*) FROM agent_subscriptions WHERE status='expired'").fetchone()[0]
        return {
            "total_active": total,
            "basic": basic,
            "pro": pro,
            "business": biz,
            "msgs_today": msgs_today,
            "total_chats": total_chats,
            "expired": expired,
        }

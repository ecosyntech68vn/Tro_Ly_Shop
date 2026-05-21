"""
AI Thực Chiến — Bot Telegram + Sepay tự động giao hàng
======================================================
Mục đích:
  1. Khách chat bot → nhận STK + mã đơn
  2. Khách chuyển khoản Vietcombank với mã đơn
  3. Sepay nhận biến động VCB → push webhook
  4. Bot tự xác thực + gửi link Drive cho khách

Tác giả: Tạ Quang Thuận · AI Thực Chiến · 2026
"""

import os
import re
import logging
from urllib.parse import quote
from flask import Flask, request, jsonify, abort
import requests

from config import (
    BOT_TOKEN, SEPAY_API_KEY, BANK_ACCOUNT, BANK_NAME, BANK_OWNER,
    ADMIN_CHAT_ID, PRODUCTS, BASE_URL
)
from db import (
    init_db, create_order, mark_order_paid, get_pending_order_by_code, get_order_status, expire_stale_orders, PENDING_TIMEOUT_MINUTES,
    get_order_by_chat, log_unmatched_payment, get_unmatched_payments,
    update_product_link, get_product_link, conn as db_conn
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# VietQR — chuẩn QR thanh toán Napas, mọi app banking VN quét được.
# BIN code cho từng ngân hàng tại: https://api.vietqr.io/v2/banks
BANK_BINS = {
    "Vietcombank": "970436", "VCB": "970436",
    "MB Bank": "970422", "MBBank": "970422", "MB": "970422",
    "Techcombank": "970407", "TCB": "970407",
    "BIDV": "970418",
    "VietinBank": "970415", "CTG": "970415",
    "ACB": "970416",
    "TPBank": "970423", "TPB": "970423",
    "MSB": "970426",
    "OCB": "970448",
    "Sacombank": "970403", "STB": "970403",
    "VPBank": "970432", "VPB": "970432",
}

def build_vietqr_url(amount, content):
    """Tạo URL ảnh QR thanh toán VietQR cho 1 đơn."""
    bin_code = BANK_BINS.get(BANK_NAME, "970436")  # default VCB
    return (
        f"https://img.vietqr.io/image/{bin_code}-{BANK_ACCOUNT}-compact2.png"
        f"?amount={amount}"
        f"&addInfo={quote(content)}"
        f"&accountName={quote(BANK_OWNER)}"
    )

# Init DB ngay khi module load (cho gunicorn, không chỉ __main__)
init_db()
log.info("Database initialized.")


# ============================================================
# TELEGRAM HELPERS
# ============================================================

import time as _time


def tg_send(chat_id, text, reply_markup=None):
    """Gửi tin nhắn tới user qua Telegram Bot API (có retry)."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for attempt in range(3):
        try:
            r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=10)
            if r.ok:
                return
            log.error(f"Telegram send failed (attempt {attempt+1}): {r.status_code} {r.text}")
            # Fallback: lỗi parse Markdown (400) thường do link Drive chứa '_' / '*' / '['.
            if r.status_code == 400 and "parse_mode" in payload:
                payload.pop("parse_mode", None)
                continue
            if r.status_code in (429, 502, 503):
                _time.sleep(1 + attempt)
                continue
        except Exception as e:
            log.exception(f"Telegram send exception (attempt {attempt+1}): {e}")
            _time.sleep(1 + attempt)


def tg_send_photo(chat_id, photo_url, caption=None):
    """Gửi ảnh (QR code) kèm caption HTML tới user."""
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "parse_mode": "HTML",
    }
    if caption:
        payload["caption"] = caption
    try:
        r = requests.post(f"{TG_API}/sendPhoto", json=payload, timeout=15)
        if not r.ok:
            log.error(f"Telegram sendPhoto failed: {r.status_code} {r.text}")
            return False
        return True
    except Exception as e:
        log.exception(f"Telegram sendPhoto exception: {e}")
        return False


def tg_keyboard():
    """Inline keyboard cho menu chính."""
    return {
        "inline_keyboard": [
            [{"text": "Mua Combo (199.000đ)", "callback_data": "mua_combo"}],
            [{"text": "Mua Claude (99.000đ)", "callback_data": "mua_claude"}],
            [{"text": "Mua OpenCode (149.000đ)", "callback_data": "mua_opencode"}],
            [{"text": "Kiểm tra đơn", "callback_data": "trang_thai"}],
            [{"text": "Liên hệ tác giả", "callback_data": "lien_he"}],
        ]
    }


# ============================================================
# LINK RESOLVER
# ============================================================

def resolve_drive_link(sku):
    """Lấy link Drive cho 1 SKU.
    Ưu tiên DB (admin /set_link) → fallback link mặc định trong config.PRODUCTS.
    Default đảm bảo link KHÔNG mất khi Render free reset DB ephemeral (redeploy/sleep)."""
    return get_product_link(sku) or PRODUCTS.get(sku, {}).get("drive_link")


# ============================================================
# BOT MESSAGE HANDLERS
# ============================================================

def handle_start(chat_id, first_name):
    text = (
        f"Xin chào *{first_name}*,\n\n"
        "Tôi là trợ lý bán hàng tự động của anh *Tạ Quang Thuận — AI Thực Chiến*.\n\n"
        "Bộ sản phẩm hiện có:\n\n"
        "*Combo Full Pack* — 199.000đ (tiết kiệm 49k)\n"
        "  └ Trọn bộ Claude + OpenCode, 4 cấp độ cho mỗi bản\n\n"
        "*Claude AI Thực Chiến* — 99.000đ\n"
        "  └ Cho dân văn phòng, sinh viên, không cần biết code, dveloper\n\n"
        "*OpenCode Thực Chiến* — 149.000đ\n"
        "  └ Cho dân văn phòng, không cần biết code, developer, tech lead\n\n"
        "Chọn sản phẩm bên dưới hoặc gõ:\n"
        "/mua\\_combo — mua combo\n"
        "/mua\\_claude — mua Claude\n"
        "/mua\\_opencode — mua OpenCode\n"
        "/trang\\_thai — kiểm tra đơn\n"
        "/lien\\_he — gặp anh Thuận"
    )
    tg_send(chat_id, text, reply_markup=tg_keyboard())


def handle_mua(chat_id, sku):
    """Tạo đơn hàng mới + gửi QR thanh toán + hướng dẫn."""
    if sku not in PRODUCTS:
        tg_send(chat_id, "Sản phẩm không tồn tại. Vui lòng Gõ /start để xem menu.")
        return

    prod = PRODUCTS[sku]
    code = create_order(chat_id, sku, prod["price"])
    ck_content = f"MUA {code}"
    qr_url = build_vietqr_url(prod["price"], ck_content)

    # ETA tuỳ chế độ
    if SEPAY_API_KEY:
        eta = "⏱ Bot tự động gửi link tải trong 30 giây sau khi nhận tiền."
    else:
        eta = ("⏱ Nếu Sepay lỗi, anh Thuận xác nhận thủ công trong 3-10 phút "
               "(giờ làm việc 9:00–22:00 hàng ngày).")

    # Caption HTML (an toàn hơn Markdown vì underscore trong /lien_he không phá parser)
    caption = (
        f"<b>Đơn hàng #{code}</b> — {prod['price']:,}đ\n"
        f"Sản phẩm: <b>{prod['name']}</b>\n\n"
        f"<b>📱 Cách 1 — Quét QR (nhanh nhất):</b>\n"
        f"Mở app ngân hàng (VCB / MB / MoMo / ZaloPay…) → Anh/chị bấm Quét QR → quét ảnh trên.\n"
        f"App tự điền STK, số tiền và nội dung. Anh/chị chỉ xác nhận chuyển.\n\n"
        f"<b>✍️ Cách 2 — Chuyển thủ công:</b>\n"
        f"Ngân hàng: <b>{BANK_NAME}</b>\n"
        f"STK: <code>{BANK_ACCOUNT}</code>\n"
        f"Chủ TK: <b>{BANK_OWNER}</b>\n"
        f"Nội dung: <code>{ck_content}</code>\n\n"
        f"{eta}\n"
        f"<i>Anh /chị Cần hỗ trợ? Vui lòng Gõ /lien_he</i>"
    )

    ok = tg_send_photo(chat_id, qr_url, caption)
    if not ok:
        # Fallback: gửi plain text nếu QR/HTML fail
        tg_send(chat_id,
                f"Đơn hàng #{code} — {prod['price']:,}đ\n\n"
                f"Chuyển khoản:\n"
                f"NH: {BANK_NAME}\n"
                f"STK: {BANK_ACCOUNT}\n"
                f"Chủ TK: {BANK_OWNER}\n"
                f"Số tiền: {prod['price']:,}đ\n"
                f"Nội dung: {ck_content}\n\n"
                f"(QR tạm thời lỗi, vui lòng chuyển thủ công theo thông tin trên)")

    log.info(f"Created order {code} for chat {chat_id} sku={sku}")


def handle_trang_thai(chat_id):
    order = get_order_by_chat(chat_id)
    if not order:
        tg_send(chat_id, "Anh/chị chưa có đơn hàng. Vui lòng Gõ /start để xem sản phẩm.")
        return

    code, sku, amount, status, drive_link = order
    prod = PRODUCTS.get(sku, {"name": sku})

    # Dùng get_order_status để biết trạng thái thực (có check timeout, không phụ thuộc
    # vào việc expire_stale_orders đã chạy hay chưa).
    info = get_order_status(code)
    st = info.get("status", status)

    if st == "paid":
        link = drive_link or resolve_drive_link(sku) or "[Đang cập nhật — vui lòng liên hệ anh Thuận]"
        text = (
            f"*Đơn #{code}* — Đã thanh toán\n"
            f"Sản phẩm: {prod['name']}\n"
            f"Số tiền: {amount:,}đ\n\n"
            f"Link tải: {link}\n\n"
            f"Cảm ơn anh/chị đã mua hàng, Chúc anh chị thực hành tốt và đạt nhiều thành tựu."
        )
    elif st == "expired":
        text = (
            f"*Đơn #{code}* — *Đã hết hạn*\n"
            f"Sản phẩm: {prod['name']}\n\n"
            f"Đơn này quá *{PENDING_TIMEOUT_MINUTES} phút* chưa nhận được thanh toán nên đã hết hạn.\n\n"
            f"Anh/chị Vui lòng tạo đơn mới: /mua\\_combo, /mua\\_claude hoặc /mua\\_opencode "
            f"rồi chuyển khoản trong vòng {PENDING_TIMEOUT_MINUTES} phút.\n\n"
            f"Anh/chị Đã chuyển tiền cho đơn cũ? Gõ /lien\\_he để anh Thuận xử lý thủ công ạ."
        )
    else:  # pending
        text = (
            f"*Đơn #{code}* — *Chưa thanh toán*\n"
            f"Sản phẩm: {prod['name']}\n"
            f"Số tiền: {amount:,}đ\n\n"
            f"Hệ thống chưa nhận được chuyển khoản.\n\n"
            f"Vui lòng kiểm tra:\n"
            f"1. Số tiền chuyển đúng *{amount:,}đ*\n"
            f"2. Nội dung CK đúng *MUA {code}*\n"
            f"3. Đợi thêm 1–2 phút (ngân hàng có thể delay)\n\n"
            f"Nếu anh/chị đã chuyển đúng, Vui lòng gõ /lien\\_he."
        )
    tg_send(chat_id, text)


def handle_lien_he(chat_id):
    text = (
        "*Kênh liên hệ trực tiếp anh Thuận:*\n\n"
        "Email: thuanktqd.mba@gmail.com\n"
        "Giờ hỗ trợ: Chủ Nhật 9:00–12:00 (giờ Việt Nam)\n\n"
        "Khi nhắn, anh/chị gửi kèm:\n"
        "1. Mã đơn (nếu có)\n"
        "2. Ảnh chụp giao dịch ngân hàng\n"
        "3. Vấn đề gặp phải"
    )
    tg_send(chat_id, text)


def handle_admin(chat_id, text):
    """Lệnh admin — chỉ chat_id trong ADMIN_CHAT_ID mới được dùng."""
    if str(chat_id) != str(ADMIN_CHAT_ID):
        tg_send(chat_id, "Lệnh này chỉ dành cho admin.")
        return

    parts = text.strip().split()
    cmd = parts[0].lower()

    if cmd == "/unmatched":
        rows = get_unmatched_payments(limit=10)
        if not rows:
            tg_send(chat_id, "Không có giao dịch không khớp.")
            return
        msg = "*Giao dịch không khớp (10 gần nhất):*\n\n"
        for tx_id, amount, content, ref, ts in rows:
            msg += f"`{ts}` — {amount:,}đ — {content} — ref:{ref}\n"
        tg_send(chat_id, msg)

    elif cmd == "/set_link" and len(parts) >= 3:
        sku = parts[1]
        url = parts[2]
        if sku not in PRODUCTS:
            tg_send(chat_id, f"SKU không tồn tại. Có: {list(PRODUCTS.keys())}")
            return
        if not url.startswith(("https://", "http://")):
            tg_send(chat_id, "URL không hợp lệ. Phải bắt đầu bằng http:// hoặc https://")
            return
        update_product_link(sku, url)
        tg_send(chat_id, f"Đã cập nhật link cho *{sku}*:\n{url}")

    elif cmd == "/confirm" and len(parts) >= 2:
        # Manual fallback: admin confirm thủ công khi Sepay/email VCB lỗi/delay
        # Strip ký tự '#' nếu admin gõ kèm (vd /confirm #TXNxxx)
        code = parts[1].upper().lstrip("#").strip()
        info = get_order_status(code)

        if info["status"] == "not_found":
            tg_send(chat_id,
                    f"⚠️ Không tìm thấy đơn *#{code}* trong hệ thống.\n\n"
                    f"Kiểm tra lại:\n"
                    f"· Mã đơn có đúng không (3 chữ TXN + 6 ký tự)?\n"
                    f"· Khách đã gõ /mua\\_xxx để tạo đơn chưa?\n\n"
                    f"Xem /unmatched để kiểm tra giao dịch lạ.")
            return

        if info["status"] == "expired":
            mins = info.get("minutes_ago", 0)
            tg_send(chat_id,
                    f"⌛ Đơn *#{code}* đã hết hạn ({mins} phút trước, quá {PENDING_TIMEOUT_MINUTES} phút).\n\n"
                    f"Đơn này đã được mark `expired` — không thể confirm nữa.\n"
                    f"Hỏi khách gõ lại /mua\\_combo (hoặc /mua\\_claude, /mua\\_opencode) để tạo đơn mới, sau đó CK + /confirm trong vòng {PENDING_TIMEOUT_MINUTES} phút.")
            return

        if info["status"] == "paid":
            tg_send(chat_id,
                    f"✅ Đơn *#{code}* ĐÃ được thanh toán từ trước.\n"
                    f"Paid at: `{info.get('paid_at', '?')}`\n"
                    f"Không cần confirm lại — link đã gửi cho khách rồi.")
            return

        # status = pending (chưa hết hạn) → tiến hành confirm
        customer_chat_id = info["chat_id"]
        sku = info["sku"]
        expected_amount = info["amount"]
        drive_link = resolve_drive_link(sku)
        # Chặn confirm nếu chưa set link: tránh mark paid + gửi placeholder rác cho khách.
        if not drive_link:
            tg_send(chat_id,
                    f"⚠️ Chưa có link Drive cho *{sku}* — đơn *#{code}* CHƯA bị mark paid.\n\n"
                    f"Set link trước rồi /confirm lại:\n"
                    f"`/set_link {sku} <url_drive>`")
            return
        mark_order_paid(code, f"MANUAL-{chat_id}", drive_link)

        prod = PRODUCTS.get(sku, {"name": sku})
        # Gửi cho khách
        tg_send(customer_chat_id,
                f"Đã nhận thanh toán *{expected_amount:,}đ* cho đơn *#{code}*.\n"
                f"Sản phẩm: *{prod['name']}*\n\n"
                f"*Link tải:*\n{drive_link}\n\n"
                f"Cảm ơn anh/chị đã mua hàng. Chúc anh chị thực hành tốt và đạt được nhiều thành công.\n"
                f"Mọi vấn đề về sản phẩm, anh/chị gõ /lien\\_he.")
        # Báo admin
        tg_send(chat_id,
                f"✅ Đã confirm đơn *#{code}* (manual) — {prod['name']} — {expected_amount:,}đ.\n"
                f"Đã gửi link cho khách (chat\\_id: `{customer_chat_id}`).")

    elif cmd == "/expire_stale":
        # Admin command: chạy thủ công để dọn đơn pending quá hạn
        cancelled = expire_stale_orders()
        if not cancelled:
            tg_send(chat_id, f"Không có đơn pending nào quá {PENDING_TIMEOUT_MINUTES} phút.")
        else:
            lines = [f"Đã mark `expired` {len(cancelled)} đơn:"]
            for c, _cid, sku in cancelled[:20]:
                lines.append(f"· `{c}` ({sku})")
            tg_send(chat_id, "\n".join(lines))

    elif cmd == "/sale_stats":
        with db_conn() as c:
            stats = c.execute(
                "SELECT sku, COUNT(*) as cnt, SUM(amount) as total "
                "FROM orders WHERE status='paid' GROUP BY sku"
            ).fetchall()
            pending = c.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
        msg = "*Thống kê doanh số:*\n\n"
        if not stats:
            msg += "_Chưa có đơn nào được thanh toán._\n"
        else:
            total_revenue = 0
            for row in stats:
                sku, cnt, total = row["sku"], row["cnt"], row["total"]
                prod_name = PRODUCTS.get(sku, {}).get("name", sku)
                msg += f"• *{prod_name}*: {cnt} đơn — {total:,}đ\n"
                total_revenue += total
            msg += f"\n*Tổng doanh thu:* {total_revenue:,}đ"
        msg += f"\n*Đơn pending:* {pending}"
        tg_send(chat_id, msg)

    elif cmd == "/admin_help":
        msg = (
            "*Lệnh admin:*\n\n"
            "*Vận hành đơn:*\n"
            "`/confirm TXNxxx` — confirm đơn thủ công (khi Sepay lỗi/delay)\n"
            "  · Bot phân biệt rõ: not\\_found / expired / paid / pending\n"
            "  · Timeout pending: " + str(PENDING_TIMEOUT_MINUTES) + " phút\n"
            "`/expire_stale` — dọn đơn pending quá hạn (mark expired)\n"
            "`/unmatched` — xem 10 GD không khớp gần nhất\n\n"
            "*Cấu hình sản phẩm:*\n"
            "`/set_link <sku> <url>` — cập nhật link Drive\n"
            "  SKU: `mua_combo` | `mua_claude` | `mua_opencode`\n\n"
            "*Báo cáo:*\n"
            "`/sale_stats` — doanh số theo SKU\n\n"
            "`/admin_help` — xem trợ giúp này"
        )
        tg_send(chat_id, msg)

    else:
        tg_send(chat_id, "Lệnh admin không hợp lệ. Gõ /admin\\_help.")


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "AI Thực Chiến Bot"})


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Nhận update từ Telegram Bot API."""
    update = request.get_json(silent=True) or {}
    log.info(f"TG update: {update.get('update_id')}")

    # Callback query (inline button click)
    if "callback_query" in update:
        cq = update["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        data = cq.get("data", "")
        # Ack callback
        requests.post(f"{TG_API}/answerCallbackQuery",
                      json={"callback_query_id": cq["id"]}, timeout=5)

        if data.startswith("mua_"):
            handle_mua(chat_id, data)
        elif data == "trang_thai":
            handle_trang_thai(chat_id)
        elif data == "lien_he":
            handle_lien_he(chat_id)
        return jsonify({"ok": True})

    # Regular message
    msg = update.get("message")
    if not msg:
        return jsonify({"ok": True})

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    first_name = msg["from"].get("first_name", "bạn")

    # Admin commands
    admin_cmds = ("/unmatched", "/set_link", "/admin_help",
                  "/confirm", "/sale_stats", "/expire_stale")
    if any(text.startswith(c) for c in admin_cmds):
        handle_admin(chat_id, text)
        return jsonify({"ok": True})

    # User commands
    if text == "/start":
        handle_start(chat_id, first_name)
    elif text == "/mua_combo":
        handle_mua(chat_id, "mua_combo")
    elif text == "/mua_claude":
        handle_mua(chat_id, "mua_claude")
    elif text == "/mua_opencode":
        handle_mua(chat_id, "mua_opencode")
    elif text == "/trang_thai":
        handle_trang_thai(chat_id)
    elif text == "/lien_he":
        handle_lien_he(chat_id)
    else:
        tg_send(chat_id,
                "Tôi chưa hiểu lệnh đó. Anh/chị hãy Gõ /start để xem menu chính.")

    return jsonify({"ok": True})


@app.route("/sepay-webhook", methods=["POST"])
def sepay_webhook():
    if not SEPAY_API_KEY:
        log.info("Sepay webhook called but SEPAY_API_KEY empty — manual mode")
        return jsonify({"ok": True, "msg": "Manual mode"}), 200

    auth = request.headers.get("Authorization", "")
    if auth != f"Apikey {SEPAY_API_KEY}":
        log.warning(f"Sepay webhook unauthorized: {auth[:20]}...")
        abort(401)

    data = request.get_json(silent=True) or {}
    log.info(f"Sepay webhook: {data.get('id')} amount={data.get('transferAmount')}")

    if data.get("transferType") != "in":
        return jsonify({"ok": True, "msg": "Not incoming, skip"})

    amount = int(data.get("transferAmount", 0))
    content = (data.get("content") or "").upper()
    ref = data.get("referenceCode", "")
    tx_id = data.get("id")

    result = process_payment(amount, content, ref, source="Sepay")
    return jsonify({"ok": result["ok"], "msg": result["msg"]}), result.get("http_status", 200)


@app.route("/vcb-email", methods=["POST"])
def vcb_email_webhook():
    secret = os.environ.get("VCB_EMAIL_SECRET", "")
    if not secret:
        log.warning("VCB_EMAIL_SECRET not set, /vcb-email disabled")
        return jsonify({"ok": True, "msg": "Disabled"}), 200

    if request.headers.get("X-Auth-Token") != secret:
        log.warning(f"VCB email webhook unauthorized: {request.headers.get('X-Auth-Token', '')[:10]}...")
        abort(401)

    data = request.get_json(silent=True) or {}
    body = data.get("body") or ""
    log.info(f"VCB email received: {((data.get('subject') or '')[:60])}")

    # Chỉ xử lý email credit (tiền vào)
    body_low = body.lower()
    has_plus_amount = bool(re.search(r"\+\s*[\d.,]+\s*vnd", body_low))
    has_credit_kw = any(kw in body_low for kw in [
        "ghi có", "tiền vào", "tien vao", "phát sinh có", "credit",
        "biến động tăng", "số dư tăng", "ghi co",
    ])
    if not (has_plus_amount or has_credit_kw):
        return jsonify({"ok": True, "msg": "Not credit email, skip"}), 200

    # Extract số tiền
    amount = 0
    m = re.search(r"\+\s*([\d.,]+)\s*VND", body)
    if not m:
        m = re.search(r"[Ss]ố tiền[:\s]*([\d.,]+)", body)
    if not m:
        m = re.search(r"[Tt]ăng[:\s]*([\d.,]+)", body)
    if m:
        raw = m.group(1).replace(".", "").replace(",", "")
        try:
            amount = int(raw)
        except ValueError:
            amount = 0

    if amount == 0:
        log.warning("VCB email: cannot parse amount")
        return jsonify({"ok": True, "msg": "No amount found"}), 200

    # Extract reference
    ref_match = re.search(r"FT\d{10,15}", body.upper())
    ref = ref_match.group(0) if ref_match else f"VCB-EMAIL-{tx_id_safe()}"

    # Content để match order
    content = body.upper()

    result = process_payment(amount, content, ref, source="VCB-email")
    return jsonify({"ok": result["ok"], "msg": result["msg"]}), result.get("http_status", 200)


def tx_id_safe():
    """Generate short unique ID for logging."""
    import time
    return str(int(time.time() * 1000))[-8:]


def notify_admin_unmatched(amount, content, ref, reason):
    if not ADMIN_CHAT_ID:
        return
    text = (
        f"*GIAO DỊCH KHÔNG KHỚP*\n\n"
        f"Số tiền: {amount:,}đ\n"
        f"Nội dung: `{content}`\n"
        f"Ref: {ref}\n"
        f"Lý do: {reason}\n\n"
        f"Kiểm tra thủ công."
    )
    tg_send(ADMIN_CHAT_ID, text)


def process_payment(amount, content, ref, source="unknown"):
    """Xử lý thanh toán chung cho cả Sepay và VCB email.

    Returns: dict với status và message để webhook trả về.
    """
    # 1. Extract order code
    order_code = None
    for token in content.upper().replace(".", " ").replace(",", " ").split():
        if token.startswith("TXN") and len(token) >= 6:
            order_code = token
            break
        if token.startswith("MUA") and len(token) > 3:
            candidate = token[3:]
            if candidate.startswith("TXN"):
                order_code = candidate
                break

    if not order_code:
        log_unmatched_payment(ref, amount, content[:200], ref)
        notify_admin_unmatched(amount, content[:150], ref, reason="Không tìm thấy mã đơn")
        return {"ok": True, "msg": "No order code", "http_status": 200}

    # 2. Look up pending order
    order = get_pending_order_by_code(order_code)
    if not order:
        log_unmatched_payment(ref, amount, content[:200], ref)
        notify_admin_unmatched(amount, content[:150], ref,
                               reason=f"Mã {order_code} không tồn tại hoặc đã thanh toán")
        return {"ok": True, "msg": "Order not found or already paid", "http_status": 200}

    code, chat_id, sku, expected_amount, status = order

    # 3. Verify amount (cho phép sai số ±100đ)
    if amount < expected_amount - 100:
        log_unmatched_payment(ref, amount, content[:200], ref)
        notify_admin_unmatched(amount, content[:150], ref,
                               reason=f"Thiếu tiền: nhận {amount}, cần {expected_amount} (đơn #{code})")
        tg_send(chat_id,
                f"Đã nhận {amount:,}đ cho đơn #{code} nhưng *thiếu {expected_amount-amount:,}đ*.\n"
                f"Vui lòng chuyển bù hoặc liên hệ tác giả.")
        return {"ok": True, "msg": "Underpaid", "http_status": 200}

    # 4. Resolve drive link
    prod = PRODUCTS.get(sku, {"name": sku})
    drive_link = resolve_drive_link(sku)
    if not drive_link:
        tg_send(chat_id,
                f"Đã nhận thanh toán *{amount:,}đ* cho đơn *#{code}* ✅\n"
                f"Link tải đang được chuẩn bị, anh Thuận sẽ gửi cho anh/chị trong ít phút.\n"
                f"Anh/chị Cần gấp? Hãy Gõ /lien\\_he.")
        if ADMIN_CHAT_ID:
            tg_send(ADMIN_CHAT_ID,
                    f"🚨 ĐÃ THU TIỀN ({source}) nhưng CHƯA set link Drive!\n"
                    f"Đơn #{code} · {sku} · {amount:,}đ · ref:{ref}\n"
                    f"Làm ngay: `/set_link {sku} <url>` → rồi `/confirm {code}`.")
        log.warning(f"{source}: Order {code} money received but no link for {sku} — kept pending")
        return {"ok": True, "msg": "Link missing, kept pending", "http_status": 200}

    # 5. Mark paid + deliver
    mark_order_paid(code, ref, drive_link)
    deliver_text = (
        f"Đã nhận thanh toán *{amount:,}đ* cho đơn *#{code}*.\n"
        f"Sản phẩm: *{prod['name']}*\n\n"
        f"*Link tải:*\n{drive_link}\n\n"
        f"Cảm ơn anh/chị đã mua hàng.\n"
        f"Mọi vấn đề về sản phẩm, hãy gõ /lien\\_he.\n\n"
        f"_Mã giao dịch: {ref}_"
    )
    tg_send(chat_id, deliver_text)

    if ADMIN_CHAT_ID:
        tg_send(ADMIN_CHAT_ID,
                f"{source}: #{code} · {sku} · {amount:,}đ · ref:{ref}")

    log.info(f"{source}: Order {code} auto-delivered, ref={ref}")
    return {"ok": True, "msg": "Delivered", "http_status": 200}


# ============================================================
# BOOT
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    log.info(f"Starting bot server on port {port}")
    log.info(f"BASE_URL: {BASE_URL}")
    log.info(f"Telegram webhook should be set to: {BASE_URL}/telegram-webhook")
    if SEPAY_API_KEY:
        log.info(f"MODE: AUTOMATIC — Sepay webhook: {BASE_URL}/sepay-webhook")
    else:
        log.info("MODE: MANUAL — Sepay disabled. Admin xác nhận đơn bằng /confirm TXNxxx")
    app.run(host="0.0.0.0", port=port, debug=False)

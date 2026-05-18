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
    init_db, create_order, mark_order_paid, get_pending_order_by_code,
    get_order_by_chat, log_unmatched_payment, get_unmatched_payments,
    update_product_link, get_product_link
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

def tg_send(chat_id, text, reply_markup=None):
    """Gửi tin nhắn tới user qua Telegram Bot API."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{TG_API}/sendMessage", json=payload, timeout=10)
        if not r.ok:
            log.error(f"Telegram send failed: {r.status_code} {r.text}")
    except Exception as e:
        log.exception(f"Telegram send exception: {e}")


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
# BOT MESSAGE HANDLERS
# ============================================================

def handle_start(chat_id, first_name):
    text = (
        f"Xin chào *{first_name}*,\n\n"
        "Tôi là trợ lý bán hàng tự động của CEO *Tạ Quang Thuận — AI Thực Chiến*.\n\n"
        "Bộ sản phẩm hiện có:\n\n"
        "*Combo Full Pack* — 199.000đ (tiết kiệm 49k)\n"
        "  └ Trọn bộ Claude + OpenCode, 8 cấp độ\n\n"
        "*Claude AI Thực Chiến* — 99.000đ\n"
        "  └ Cho dân văn phòng, sinh viên, không cần biết code\n\n"
        "*OpenCode Thực Chiến* — 149.000đ\n"
        "  └ Cho developer, tech lead, Cho dân văn phòng, sinh viên, không cần biết code\n\n"
        "Chọn sản phẩm bên dưới hoặc gõ:\n"
        "/mua\\_combo — mua combo\n"
        "/mua\\_claude — mua Claude\n"
        "/mua\\_opencode — mua OpenCode\n"
        "/trang\\_thai — kiểm tra đơn\n"
        "/lien\\_he — gặp tác giả"
    )
    tg_send(chat_id, text, reply_markup=tg_keyboard())


def handle_mua(chat_id, sku):
    """Tạo đơn hàng mới + gửi QR thanh toán + hướng dẫn."""
    if sku not in PRODUCTS:
        tg_send(chat_id, "Sản phẩm không tồn tại. Gõ /start để xem menu.")
        return

    prod = PRODUCTS[sku]
    code = create_order(chat_id, sku, prod["price"])
    ck_content = f"MUA {code}"
    qr_url = build_vietqr_url(prod["price"], ck_content)

    # ETA tuỳ chế độ
    if SEPAY_API_KEY:
        eta = "⏱ Bot tự động gửi link tải trong 30 giây sau khi nhận tiền."
    else:
        eta = ("⏱ Tác giả xác nhận thủ công trong 30 phút "
               "(giờ làm việc 9:00–22:00 hàng ngày).")

    # Caption HTML (an toàn hơn Markdown vì underscore trong /lien_he không phá parser)
    caption = (
        f"<b>Đơn hàng #{code}</b> — {prod['price']:,}đ\n"
        f"Sản phẩm: <b>{prod['name']}</b>\n\n"
        f"<b>📱 Cách 1 — Quét QR (nhanh nhất):</b>\n"
        f"Mở app ngân hàng (VCB / MB / MoMo / ZaloPay…) → bấm Quét QR → quét ảnh trên.\n"
        f"App tự điền STK, số tiền và nội dung. Bạn chỉ xác nhận chuyển.\n\n"
        f"<b>✍️ Cách 2 — Chuyển thủ công:</b>\n"
        f"Ngân hàng: <b>{BANK_NAME}</b>\n"
        f"STK: <code>{BANK_ACCOUNT}</code>\n"
        f"Chủ TK: <b>{BANK_OWNER}</b>\n"
        f"Nội dung: <code>{ck_content}</code>\n\n"
        f"{eta}\n"
        f"<i>Cần hỗ trợ? Gõ /lien_he</i>"
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
        tg_send(chat_id, "Bạn chưa có đơn hàng. Gõ /start để xem sản phẩm.")
        return

    code, sku, amount, status, drive_link = order
    prod = PRODUCTS.get(sku, {"name": sku})

    if status == "paid":
        link = drive_link or get_product_link(sku) or "[Đang cập nhật — vui lòng liên hệ tác giả]"
        text = (
            f"*Đơn #{code}* — Đã thanh toán\n"
            f"Sản phẩm: {prod['name']}\n"
            f"Số tiền: {amount:,}đ\n\n"
            f"Link tải: {link}\n\n"
            f"Cảm ơn bạn đã mua hàng."
        )
    else:
        text = (
            f"*Đơn #{code}* — *Chưa thanh toán*\n"
            f"Sản phẩm: {prod['name']}\n"
            f"Số tiền: {amount:,}đ\n\n"
            f"Hệ thống chưa nhận được chuyển khoản.\n\n"
            f"Vui lòng kiểm tra:\n"
            f"1. Số tiền chuyển đúng *{amount:,}đ*\n"
            f"2. Nội dung CK đúng *MUA {code}*\n"
            f"3. Đợi thêm 1–2 phút (ngân hàng có thể delay)\n\n"
            f"Nếu đã chuyển đúng, gõ /lien\\_he."
        )
    tg_send(chat_id, text)


def handle_lien_he(chat_id):
    text = (
        "*Kênh liên hệ trực tiếp tác giả:*\n\n"
        "Email: thuanktqd.mba@gmail.com\n"
        "Giờ hỗ trợ: Chủ Nhật 9:00–12:00 (giờ Việt Nam)\n\n"
        "Khi nhắn, gửi kèm:\n"
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
        update_product_link(sku, url)
        tg_send(chat_id, f"Đã cập nhật link cho *{sku}*:\n{url}")

    elif cmd == "/confirm" and len(parts) >= 2:
        # Manual fallback: admin confirm thủ công khi Sepay lỗi/delay
        code = parts[1].upper()
        order = get_pending_order_by_code(code)
        if not order:
            tg_send(chat_id, f"Không tìm thấy đơn pending #{code}.")
            return
        _, customer_chat_id, sku, expected_amount, _ = order
        drive_link = get_product_link(sku) or "[Link chưa cập nhật]"
        mark_order_paid(code, f"MANUAL-{chat_id}", drive_link)

        prod = PRODUCTS.get(sku, {"name": sku})
        # Gửi cho khách
        tg_send(customer_chat_id,
                f"Đã nhận thanh toán *{expected_amount:,}đ* cho đơn *#{code}*.\n"
                f"Sản phẩm: *{prod['name']}*\n\n"
                f"*Link tải:*\n{drive_link}\n\n"
                f"Cảm ơn bạn đã mua hàng.\n"
                f"Mọi vấn đề về sản phẩm, gõ /lien\\_he.")
        # Báo admin
        tg_send(chat_id,
                f"Đã confirm đơn *#{code}* (manual) — {prod['name']} — {expected_amount:,}đ.\n"
                f"Đã gửi link cho khách (chat\\_id: `{customer_chat_id}`).")

    elif cmd == "/sale_stats":
        # Thống kê doanh số nhanh
        from db import conn as _conn
        with _conn() as c:
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
                  "/confirm", "/sale_stats")
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
                "Tôi chưa hiểu lệnh đó. Gõ /start để xem menu chính.")

    return jsonify({"ok": True})


@app.route("/sepay-webhook", methods=["POST"])
def sepay_webhook():
    """
    Nhận biến động ngân hàng từ Sepay.
    Sepay xác thực bằng header: Authorization: Apikey {key}
    """
    # 0. Nếu chưa có SEPAY_API_KEY → đang ở chế độ Manual (Hướng A)
    if not SEPAY_API_KEY:
        log.info("Sepay webhook called but SEPAY_API_KEY empty — manual mode, ignoring")
        return jsonify({"ok": True, "msg": "Manual mode, Sepay disabled"}), 200

    # 1. Verify API key
    auth = request.headers.get("Authorization", "")
    expected = f"Apikey {SEPAY_API_KEY}"
    if auth != expected:
        log.warning(f"Sepay webhook unauthorized: {auth[:20]}...")
        abort(401)

    data = request.get_json(silent=True) or {}
    log.info(f"Sepay webhook: {data.get('id')} amount={data.get('transferAmount')}")

    # 2. Only handle incoming transfers
    if data.get("transferType") != "in":
        return jsonify({"ok": True, "msg": "Not incoming, skip"})

    amount = int(data.get("transferAmount", 0))
    content = (data.get("content") or "").upper()
    ref = data.get("referenceCode", "")
    tx_id = data.get("id")

    # 3. Extract order code from content
    # Expected: "MUA TXNXXXX" or "MUATXNXXXX"
    order_code = None
    for token in content.replace(".", " ").replace(",", " ").split():
        if token.startswith("TXN") and len(token) >= 6:
            order_code = token
            break
        if token.startswith("MUA") and len(token) > 3:
            # MUATXN123 case
            candidate = token[3:]
            if candidate.startswith("TXN"):
                order_code = candidate
                break

    # 4. Match order
    if not order_code:
        log_unmatched_payment(tx_id, amount, content, ref)
        notify_admin_unmatched(amount, content, ref, reason="Không tìm thấy mã đơn")
        return jsonify({"ok": True, "msg": "No order code in content"})

    order = get_pending_order_by_code(order_code)
    if not order:
        log_unmatched_payment(tx_id, amount, content, ref)
        notify_admin_unmatched(amount, content, ref,
                               reason=f"Mã đơn {order_code} không tồn tại hoặc đã thanh toán")
        return jsonify({"ok": True, "msg": "Order not found or already paid"})

    code, chat_id, sku, expected_amount, status = order

    # 5. Verify amount
    if amount < expected_amount:
        # Underpaid — notify admin, don't deliver
        log_unmatched_payment(tx_id, amount, content, ref)
        notify_admin_unmatched(amount, content, ref,
                               reason=f"Thiếu tiền: nhận {amount}, cần {expected_amount} (đơn #{code})")
        tg_send(chat_id,
                f"Đã nhận {amount:,}đ cho đơn #{code} nhưng *thiếu {expected_amount-amount:,}đ*.\n"
                f"Vui lòng chuyển bù hoặc liên hệ tác giả.")
        return jsonify({"ok": True, "msg": "Underpaid"})

    # 6. Match successful — mark paid + deliver
    drive_link = get_product_link(sku) or "[Link chưa cập nhật — liên hệ tác giả]"
    mark_order_paid(code, ref, drive_link)

    prod = PRODUCTS.get(sku, {"name": sku})
    deliver_text = (
        f"Đã nhận thanh toán *{amount:,}đ* cho đơn *#{code}*.\n"
        f"Sản phẩm: *{prod['name']}*\n\n"
        f"*Link tải:*\n{drive_link}\n\n"
        f"Cảm ơn bạn đã mua hàng.\n"
        f"Mọi vấn đề về sản phẩm, gõ /lien\\_he.\n\n"
        f"_Mã giao dịch ngân hàng: {ref}_"
    )
    tg_send(chat_id, deliver_text)

    # Notify admin
    if ADMIN_CHAT_ID:
        tg_send(ADMIN_CHAT_ID,
                f"NEW SALE: #{code} · {sku} · {amount:,}đ · ref:{ref}")

    log.info(f"Order {code} delivered to chat {chat_id}, ref={ref}")
    return jsonify({"ok": True, "msg": "Delivered"})


@app.route("/vcb-email", methods=["POST"])
def vcb_email_webhook():
    """
    Nhận email biến động số dư VCB từ Google Apps Script.
    Apps Script chạy mỗi 1 phút, đọc email từ VCB Digibank, POST tới đây.

    Auth: header X-Auth-Token phải khớp env VCB_EMAIL_SECRET
    Body JSON:
    {
      "subject": "VCB Digibank: Biến động số dư...",
      "body": "Tài khoản: ...\nTăng: +199,000 VND\nNội dung: MUA TXNXXXXXX\n..."
    }
    """
    secret = os.environ.get("VCB_EMAIL_SECRET", "")
    if not secret:
        log.warning("VCB_EMAIL_SECRET not set, /vcb-email disabled")
        return jsonify({"ok": True, "msg": "Disabled"}), 200

    if request.headers.get("X-Auth-Token") != secret:
        log.warning(f"VCB email webhook unauthorized: {request.headers.get('X-Auth-Token', '')[:10]}...")
        abort(401)

    data = request.get_json(silent=True) or {}
    subject = (data.get("subject") or "").lower()
    body = data.get("body") or ""

    log.info(f"VCB email received: {subject[:60]}")

    # Chỉ xử lý email tiền VÀO (credit, biến động tăng)
    is_credit = any(kw in body.lower() for kw in [
        "tăng", "ghi có", "credit", "tien vao", "tiền vào", "phát sinh có",
    ])
    if not is_credit:
        return jsonify({"ok": True, "msg": "Not credit email, skip"}), 200

    # Extract số tiền: tìm pattern "+xxx,xxx VND" hoặc "Số tiền: xxx,xxx"
    amount = 0
    # Pattern 1: +199,000 VND
    m = re.search(r"\+\s*([\d.,]+)\s*VND", body)
    # Pattern 2: Số tiền: 199,000 / Số tiền: 199.000
    if not m:
        m = re.search(r"[Ss]ố tiền[:\s]*([\d.,]+)", body)
    # Pattern 3: Tăng: 199,000
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

    # Extract mã đơn TXN + 6-8 ký tự
    code_match = re.search(r"TXN[A-Z0-9]{4,10}", body.upper())
    ref_match = re.search(r"FT\d{10,15}", body.upper())
    ref = ref_match.group(0) if ref_match else f"VCB-EMAIL-{tx_id_safe()}"

    if not code_match:
        log_unmatched_payment(ref, amount, body[:200], ref)
        notify_admin_unmatched(amount, body[:150], ref, reason="Không tìm thấy mã TXN trong email VCB")
        return jsonify({"ok": True, "msg": "No order code"}), 200

    order_code = code_match.group(0)
    order = get_pending_order_by_code(order_code)
    if not order:
        log_unmatched_payment(ref, amount, body[:200], ref)
        notify_admin_unmatched(amount, body[:150], ref,
                               reason=f"Mã {order_code} không tồn tại hoặc đã thanh toán")
        return jsonify({"ok": True, "msg": "Order not found"}), 200

    code, chat_id, sku, expected_amount, status = order

    # Verify số tiền (cho phép sai số nhỏ ±100đ vì format)
    if abs(amount - expected_amount) > 100:
        if amount < expected_amount:
            log_unmatched_payment(ref, amount, body[:200], ref)
            notify_admin_unmatched(amount, body[:150], ref,
                                   reason=f"Thiếu tiền: nhận {amount}, cần {expected_amount} (đơn #{code})")
            tg_send(chat_id,
                    f"Đã nhận {amount:,}đ cho đơn #{code} nhưng *thiếu {expected_amount-amount:,}đ*.\n"
                    f"Vui lòng chuyển bù hoặc liên hệ tác giả.")
            return jsonify({"ok": True, "msg": "Underpaid"}), 200
        # Nếu thừa tiền (khách CK dư) → vẫn deliver, log thừa cho admin tự xử lý

    # Match thành công → mark paid + deliver
    drive_link = get_product_link(sku) or "[Link chưa cập nhật — liên hệ tác giả]"
    mark_order_paid(code, ref, drive_link)

    prod = PRODUCTS.get(sku, {"name": sku})
    deliver_text = (
        f"Đã nhận thanh toán *{amount:,}đ* cho đơn *#{code}*.\n"
        f"Sản phẩm: *{prod['name']}*\n\n"
        f"*Link tải:*\n{drive_link}\n\n"
        f"Cảm ơn bạn đã mua hàng.\n"
        f"_Mã giao dịch: {ref}_"
    )
    tg_send(chat_id, deliver_text)

    if ADMIN_CHAT_ID:
        tg_send(ADMIN_CHAT_ID, f"AUTO SALE (VCB email): #{code} · {sku} · {amount:,}đ")

    log.info(f"VCB-email: Order {code} auto-delivered, ref={ref}")
    return jsonify({"ok": True, "msg": "Delivered"}), 200


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

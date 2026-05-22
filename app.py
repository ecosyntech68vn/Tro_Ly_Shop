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
from flask import Flask, request, jsonify, abort, send_file
import requests

from config import (
    BOT_TOKEN, SEPAY_API_KEY, BANK_ACCOUNT, BANK_NAME, BANK_OWNER,
    ADMIN_CHAT_ID, PRODUCTS, BASE_URL
)
from db import (
    init_db, create_order, mark_order_paid, get_pending_order_by_code, get_order_status, expire_stale_orders, PENDING_TIMEOUT_MINUTES,
    get_order_by_chat, log_unmatched_payment, get_unmatched_payments,
    get_unmatched_payments_page, get_today_stats, get_pending_orders, get_recent_orders,
    update_product_link, get_product_link, conn as db_conn
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Startup validation
if not BOT_TOKEN:
    log.critical("BOT_TOKEN is EMPTY — bot will NOT send any messages!")
elif not TG_API.endswith("bot"):
    log.info(f"TG_API configured: …bot{str(BOT_TOKEN)[:10]}…{str(BOT_TOKEN)[-5:]}")
if not ADMIN_CHAT_ID:
    log.warning("ADMIN_CHAT_ID not set — admin notifications disabled")
if SEPAY_API_KEY:
    log.info("SEPAY: AUTOMATIC mode — payments are self-service")
else:
    log.info("SEPAY: MANUAL mode — admin must /confirm orders")

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


def _tg_call(method, payload):
    """Gọi Telegram Bot API với retry, trả về response hoặc None."""
    url = f"{TG_API}/{method}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.ok:
                return r
            log.error(f"TG {method} fail (attempt {attempt+1}): {r.status_code} {r.text[:200]}")
            if r.status_code == 400 and "parse_mode" in payload:
                payload.pop("parse_mode", None)
                continue
            if r.status_code == 401:
                log.critical(f"TG 401 Unauthorized — BOT_TOKEN may be WRONG! token={str(BOT_TOKEN)[:10]}…")
            if r.status_code in (429, 502, 503):
                _time.sleep(1 + attempt)
                continue
        except Exception as e:
            log.exception(f"TG {method} exception (attempt {attempt+1}): {e}")
            _time.sleep(1 + attempt)
    return None


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

    _tg_call("sendMessage", payload)


def tg_send_photo(chat_id, photo_url, caption=None):
    """Gửi ảnh (QR code) kèm caption HTML tới user."""
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "parse_mode": "HTML",
    }
    if caption:
        payload["caption"] = caption
    return _tg_call("sendPhoto", payload) is not None


def tg_keyboard():
    """Inline keyboard cho menu chính."""
    return {
        "inline_keyboard": [
            [{"text": "🛒 Combo Full Pack — 199.000đ", "callback_data": "mua_combo"}],
            [{"text": "🛒 Claude AI — 99.000đ", "callback_data": "mua_claude"}],
            [{"text": "🛒 OpenCode — 149.000đ", "callback_data": "mua_opencode"}],
            [{"text": "📋 Kiểm tra đơn", "callback_data": "trang_thai"},
             {"text": "📞 Liên hệ", "callback_data": "lien_he"}],
            [{"text": "❓ Hướng dẫn thanh toán", "callback_data": "huong_dan"}],
        ]
    }


def tg_after_order_keyboard():
    """Keyboard hiện sau khi khách đặt đơn."""
    return {
        "inline_keyboard": [
            [{"text": "📋 Kiểm tra trạng thái đơn", "callback_data": "trang_thai"}],
            [{"text": "📞 Liên hệ hỗ trợ", "callback_data": "lien_he"},
             {"text": "🏠 Menu chính", "callback_data": "ve_menu"}],
        ]
    }


def tg_admin_keyboard():
    """Inline keyboard cho menu admin."""
    return {
        "inline_keyboard": [
            [{"text": "📊 Thống kê hôm nay", "callback_data": "admin_today"},
             {"text": "💰 Sale stats", "callback_data": "admin_stats"}],
            [{"text": "⏳ Đơn chờ", "callback_data": "admin_pending_0"},
             {"text": "📋 Đơn gần đây", "callback_data": "admin_recent"}],
            [{"text": "⚠️ GD không khớp", "callback_data": "admin_unmatched_0"},
             {"text": "🔗 Set link", "callback_data": "admin_setlink"}],
            [{"text": "🧹 Dọn đơn hết hạn", "callback_data": "admin_expire"},
             {"text": "📖 Hướng dẫn", "callback_data": "admin_help"}],
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
        f"Xin chào *{first_name}* 👋\n\n"
        "Tôi là trợ lý bán hàng tự động của anh *Tạ Quang Thuận — AI Thực Chiến*.\n\n"
        "*Sản phẩm:*\n"
        "• 🎯 Combo Full Pack — *199.000đ* (tiết kiệm 49k)\n"
        "• 🤖 Claude AI Thực Chiến — *99.000đ*\n"
        "• 💻 OpenCode Thực Chiến — *149.000đ*\n\n"
        "Chọn sản phẩm bên dưới để đặt mua 👇"
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
        tg_send(chat_id,
                f"Đơn hàng #{code} — {prod['price']:,}đ\n\n"
                f"Chuyển khoản:\n"
                f"NH: {BANK_NAME}\n"
                f"STK: {BANK_ACCOUNT}\n"
                f"Chủ TK: {BANK_OWNER}\n"
                f"Số tiền: {prod['price']:,}đ\n"
                f"Nội dung: {ck_content}\n\n"
                f"(QR tạm thời lỗi, vui lòng chuyển thủ công theo thông tin trên)")

    tg_send(chat_id, "👉 Sau khi chuyển khoản, bấm *Kiểm tra đơn* để xem trạng thái:", reply_markup=tg_after_order_keyboard())
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
            f"*Đơn #{code}* — ✅ Đã thanh toán\n"
            f"Sản phẩm: {prod['name']}\n"
            f"Số tiền: {amount:,}đ\n\n"
            f"*Link tải:* {link}\n\n"
            f"Cảm ơn anh/chị đã mua hàng! Chúc anh chị thực hành tốt 🎉"
        )
        tg_send(chat_id, text)
    elif st == "expired":
        text = (
            f"*Đơn #{code}* — ⌛ Đã hết hạn\n"
            f"Sản phẩm: {prod['name']}\n"
            f"Đơn quá *{PENDING_TIMEOUT_MINUTES} phút* chưa nhận được thanh toán.\n\n"
            f"Vui lòng tạo đơn mới 👇"
        )
        tg_send(chat_id, text, reply_markup=tg_keyboard())
    else:  # pending
        text = (
            f"*Đơn #{code}* — ⏳ Chưa thanh toán\n"
            f"Sản phẩm: {prod['name']}\n"
            f"Số tiền: {amount:,}đ\n\n"
            f"Kiểm tra lại:\n"
            f"1️⃣ Số tiền đúng *{amount:,}đ*\n"
            f"2️⃣ Nội dung đúng *MUA {code}*\n"
            f"3️⃣ Đợi 1–2 phút (ngân hàng có thể delay)\n\n"
            f"Đã chuyển rồi? Bấm nút bên dưới để kiểm tra lại 👇"
        )
        tg_send(chat_id, text, reply_markup=tg_after_order_keyboard())


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

    if cmd == "/admin":
        tg_send(chat_id, "*Panel quản trị* — Chọn chức năng:", reply_markup=tg_admin_keyboard())
        return

    if cmd == "/unmatched":
        rows, total = get_unmatched_payments_page(0, 5)
        if not rows:
            tg_send(chat_id, "Không có giao dịch không khớp.")
            return
        msg = f"*Giao dịch không khớp* (trang 1/{max(1,(total-1)//5+1)} — {total} GD):\n\n"
        for tx_id, amount, content, ref, ts in rows:
            msg += f"`{ts}` — {amount:,}đ — {content}\n"
        kb = {
            "inline_keyboard": [
                [{"text": "Tiếp →", "callback_data": "admin_unmatched_1"}],
                [{"text": "◀ Về menu", "callback_data": "admin_back"}],
            ]
        } if total > 5 else {"inline_keyboard": [[{"text": "◀ Về menu", "callback_data": "admin_back"}]]}
        tg_send(chat_id, msg, reply_markup=kb)

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
        code = parts[1].upper().lstrip("#").strip()
        info = get_order_status(code)

        if info["status"] == "not_found":
            tg_send(chat_id,
                    f"⚠️ Không tìm thấy đơn *#{code}*.\n\n"
                    f"Kiểm tra lại mã đơn hoặc xem /unmatched.")
            return

        if info["status"] == "expired":
            mins = info.get("minutes_ago", 0)
            tg_send(chat_id,
                    f"⌛ Đơn *#{code}* đã hết hạn ({mins} phút trước).\n"
                    f"Hỏi khách tạo đơn mới rồi CK + /confirm lại.")
            return

        if info["status"] == "paid":
            tg_send(chat_id,
                    f"✅ Đơn *#{code}* ĐÃ thanh toán rồi (paid at: `{info.get('paid_at', '?')}`).")
            return

        customer_chat_id = info["chat_id"]
        sku = info["sku"]
        expected_amount = info["amount"]
        drive_link = resolve_drive_link(sku)
        if not drive_link:
            tg_send(chat_id,
                    f"⚠️ Chưa có link Drive cho *{sku}* — set link trước:\n"
                    f"`/set_link {sku} <url>`")
            return
        mark_order_paid(code, f"MANUAL-{chat_id}", drive_link)

        prod = PRODUCTS.get(sku, {"name": sku})
        tg_send(customer_chat_id,
                f"Đã nhận thanh toán *{expected_amount:,}đ* cho đơn *#{code}*.\n"
                f"Sản phẩm: *{prod['name']}*\n\n"
                f"*Link tải:*\n{drive_link}\n\n"
                f"Cảm ơn anh/chị đã mua hàng.")
        tg_send(chat_id,
                f"✅ Đã confirm đơn *#{code}* — {prod['name']} — {expected_amount:,}đ.\n"
                f"Đã gửi link cho khách.")

    elif cmd == "/expire_stale":
        cancelled = expire_stale_orders()
        if not cancelled:
            tg_send(chat_id, f"Không có đơn pending nào quá {PENDING_TIMEOUT_MINUTES} phút.")
        else:
            lines = [f"Đã mark `expired` {len(cancelled)} đơn:"]
            for c, _cid, sku in cancelled[:20]:
                lines.append(f"· `{c}` ({sku})")
            tg_send(chat_id, "\n".join(lines))

    elif cmd == "/sale_stats":
        send_sale_stats(chat_id)

    elif cmd == "/admin_help":
        msg = (
            "*Lệnh admin:*\n\n"
            "*Vận hành:*\n"
            "`/admin` — menu quản trị (có nút bấm)\n"
            "`/confirm TXNxxx` — confirm đơn thủ công\n"
            "`/expire_stale` — dọn đơn pending quá hạn\n"
            "`/unmatched` — GD không khớp (có phân trang)\n\n"
            "*Cấu hình:*\n"
            "`/set_link <sku> <url>` — cập nhật link Drive\n"
            "  SKU: `mua_combo`, `mua_claude`, `mua_opencode`\n\n"
            "*Báo cáo:*\n"
            "`/sale_stats` — doanh số theo SKU\n"
            "`/admin_today` — thống kê hôm nay"
        )
        tg_send(chat_id, msg)

    else:
        tg_send(chat_id, "Lệnh admin không hợp lệ. Gõ /admin\\_help hoặc /admin để mở menu.")


def send_sale_stats(chat_id):
    """Gửi thống kê doanh số chi tiết."""
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


ADMIN_STATE = {}

def handle_admin_callback(chat_id, data):
    """Xử lý callback từ menu admin."""
    if str(chat_id) != str(ADMIN_CHAT_ID):
        return

    if data == "admin_back":
        tg_send(chat_id, "*Panel quản trị* — Chọn chức năng:", reply_markup=tg_admin_keyboard())
        return

    if data == "admin_help":
        handle_admin(chat_id, "/admin_help")
        return

    if data == "admin_stats":
        send_sale_stats(chat_id)
        tg_send(chat_id, "Menu admin:", reply_markup=tg_admin_keyboard())
        return

    if data == "admin_expire":
        handle_admin(chat_id, "/expire_stale")
        tg_send(chat_id, "Menu admin:", reply_markup=tg_admin_keyboard())
        return

    if data == "admin_today":
        s = get_today_stats()
        msg = (
            f"*Thống kê hôm nay*\n\n"
            f"📦 Đơn hôm nay: {s['today_orders']}\n"
            f"💰 Doanh thu hôm nay: {s['today_revenue']:,}đ\n\n"
            f"*Tổng quan*\n"
            f"✅ Đã thanh toán: {s['total_paid']}\n"
            f"💵 Tổng doanh thu: {s['total_revenue']:,}đ\n"
            f"⏳ Đang chờ: {s['pending']}\n"
            f"⚠️ GD không khớp: {s['unmatched']}"
        )
        tg_send(chat_id, msg, reply_markup=tg_admin_keyboard())
        return

    if data == "admin_recent":
        orders = get_recent_orders(7)
        if not orders:
            tg_send(chat_id, "Không có đơn nào trong 7 ngày qua.", reply_markup=tg_admin_keyboard())
            return
        msg = "*Đơn hàng 7 ngày qua:*\n\n"
        for code, sku, amount, status, created, paid in orders:
            prod = PRODUCTS.get(sku, {}).get("name", sku)
            icon = "✅" if status == "paid" else "⏳" if status == "pending" else "⌛"
            msg += f"{icon} `{code}` — {prod} — {amount:,}đ — {status}\n"
        tg_send(chat_id, msg, reply_markup=tg_admin_keyboard())
        return

    if data == "admin_setlink":
        kb = {"inline_keyboard": []}
        for sku, prod in PRODUCTS.items():
            current = get_product_link(sku)
            label = f"{prod['name']} {'✅' if current else '❌'}"
            kb["inline_keyboard"].append([{"text": label, "callback_data": f"admin_link_{sku}"}])
        kb["inline_keyboard"].append([{"text": "◀ Về menu", "callback_data": "admin_back"}])
        tg_send(chat_id, "*Chọn sản phẩm để đặt link:*", reply_markup=kb)
        return

    if data.startswith("admin_link_"):
        sku = data.replace("admin_link_", "")
        prod = PRODUCTS.get(sku)
        if not prod:
            return
        current = get_product_link(sku)
        msg = f"*Set link cho:* {prod['name']} (`{sku}`)\n"
        if current:
            msg += f"Link hiện tại: {current}\n\n"
        msg += "Gõ link mới (bắt đầu bằng https://):"
        ADMIN_STATE[chat_id] = {"action": "setlink", "sku": sku}
        tg_send(chat_id, msg)
        return

    if data.startswith("admin_confirm_"):
        code = data.replace("admin_confirm_", "")
        handle_admin(chat_id, f"/confirm {code}")
        return

    if data.startswith("admin_pending_"):
        page = int(data.split("_")[2]) if data.count("_") >= 2 else 0
        rows, total = get_pending_orders(page, 5)
        if not rows:
            tg_send(chat_id, "Không có đơn chờ xử lý.", reply_markup=tg_admin_keyboard())
            return
        total_pages = max(1, (total - 1) // 5 + 1)
        msg = f"*Đơn chờ* (trang {page+1}/{total_pages} — {total} đơn):\n\n"
        kb = {"inline_keyboard": []}
        for code, cid, sku, amount, created in rows:
            prod = PRODUCTS.get(sku, {}).get("name", sku)
            msg += f"⏳ `{code}` — {prod} — {amount:,}đ\n"
            kb["inline_keyboard"].append([
                {"text": f"✅ Confirm {code}", "callback_data": f"admin_confirm_{code}"}
            ])
        nav = []
        if page > 0:
            nav.append({"text": "← Trước", "callback_data": f"admin_pending_{page-1}"})
        if page < total_pages - 1:
            nav.append({"text": "Tiếp →", "callback_data": f"admin_pending_{page+1}"})
        if nav:
            kb["inline_keyboard"].append(nav)
        kb["inline_keyboard"].append([{"text": "◀ Về menu", "callback_data": "admin_back"}])
        tg_send(chat_id, msg, reply_markup=kb)
        return

    if data.startswith("admin_unmatched_"):
        try:
            page = int(data.split("_")[2])
        except (IndexError, ValueError):
            page = 0
        rows, total = get_unmatched_payments_page(page, 5)
        if not rows:
            tg_send(chat_id, "Không có giao dịch không khớp.")
            return
        total_pages = max(1, (total - 1) // 5 + 1)
        msg = f"*GD không khớp* (trang {page+1}/{total_pages} — {total} GD):\n\n"
        for tx_id, amount, content, ref, ts in rows:
            msg += f"`{ts}` — {amount:,}đ — {content}\n"
        kb = {"inline_keyboard": []}
        nav = []
        if page > 0:
            nav.append({"text": "← Trước", "callback_data": f"admin_unmatched_{page-1}"})
        if page < total_pages - 1:
            nav.append({"text": "Tiếp →", "callback_data": f"admin_unmatched_{page+1}"})
        if nav:
            kb["inline_keyboard"].append(nav)
        kb["inline_keyboard"].append([{"text": "◀ Về menu", "callback_data": "admin_back"}])
        tg_send(chat_id, msg, reply_markup=kb)
        return


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "AI Thực Chiến Bot"})

@app.route("/download.apk")
def download_apk():
    return send_file("static/BankNotify.apk", mimetype="application/vnd.android.package-archive", as_attachment=True, download_name="BankNotify.apk")


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
        requests.post(f"{TG_API}/answerCallbackQuery",
                      json={"callback_query_id": cq["id"]}, timeout=5)

        if data.startswith("admin_"):
            handle_admin_callback(chat_id, data)
        elif data.startswith("mua_"):
            handle_mua(chat_id, data)
        elif data == "trang_thai":
            handle_trang_thai(chat_id)
        elif data == "lien_he":
            handle_lien_he(chat_id)
        elif data == "huong_dan":
            text = (
                "*Hướng dẫn thanh toán:*\n\n"
                "1️⃣ Chọn sản phẩm → bot gửi mã QR + thông tin chuyển khoản\n"
                "2️⃣ *Cách 1 — Quét QR:* Mở app ngân hàng → Quét mã QR → Xác nhận\n"
                "3️⃣ *Cách 2 — Chuyển thủ công:* Nhập STK + số tiền + nội dung như hướng dẫn\n"
                "4️⃣ Sau khi chuyển, bot tự động gửi link tải (hoặc admin xác nhận thủ công)\n\n"
                "📌 *Lưu ý:* Nội dung chuyển khoản PHẢI đúng mã đơn (VD: MUA TXNXXXX)\n"
                "⏱ Hệ thống tự động xử lý trong 30 giây sau khi nhận tiền."
            )
            tg_send(chat_id, text, reply_markup=tg_keyboard())
        elif data == "ve_menu":
            handle_start(chat_id, cq["message"]["chat"].get("first_name", "bạn"))
        return jsonify({"ok": True})

    # Regular message
    msg = update.get("message")
    if not msg:
        return jsonify({"ok": True})

    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    first_name = msg["from"].get("first_name", "bạn")

    # Admin state handling (set link flow)
    if str(chat_id) == str(ADMIN_CHAT_ID) and chat_id in ADMIN_STATE:
        state = ADMIN_STATE[chat_id]
        if state.get("action") == "setlink":
            sku = state["sku"]
            if not text.startswith(("https://", "http://")):
                tg_send(chat_id, "Link không hợp lệ. Gõ link bắt đầu bằng https://")
                return jsonify({"ok": True})
            update_product_link(sku, text)
            del ADMIN_STATE[chat_id]
            tg_send(chat_id, f"✅ Đã lưu link cho *{sku}*:\n{text}")
            return jsonify({"ok": True})

    # Admin commands
    admin_cmds = ("/unmatched", "/set_link", "/admin_help",
                  "/confirm", "/sale_stats", "/expire_stale",
                  "/admin", "/admin_today")
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


@app.route("/webhook/banknotify", methods=["POST"])
def banknotify_webhook():
    secret = os.environ.get("BANKNOTIFY_WEBHOOK_SECRET", "")
    if secret and request.headers.get("X-Webhook-Secret", "") != secret:
        log.warning("BankNotify webhook unauthorized")
        abort(401)

    data = request.get_json(silent=True) or {}
    event = data.get("event", "")
    if event != "transaction.new":
        return jsonify({"ok": True, "msg": "Not transaction.new, skip"}), 200

    status = (data.get("status") or "").upper()
    if status == "FAILED":
        return jsonify({"ok": True, "msg": "Failed tx, skip"}), 200

    amount_raw = data.get("amount", 0)
    try:
        amount = int(float(amount_raw))
    except (ValueError, TypeError):
        amount = 0
    if amount <= 0:
        return jsonify({"ok": True, "msg": "Invalid amount"}), 200

    content = data.get("content", "") or ""
    ref = data.get("reference_number", "") or ""
    if not ref:
        ref = f"BN-{data.get('id', tx_id_safe())}"

    log.info(f"BankNotify: {amount:,}đ · {content[:60]} · ref={ref} · status={status}")
    result = process_payment(amount, content, ref, source="BankNotify")
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

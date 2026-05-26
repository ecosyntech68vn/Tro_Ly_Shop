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
import io
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from flask import Flask, request, jsonify, abort, send_file
import requests

from config import (
    BOT_TOKEN, SEPAY_API_KEY, BANK_ACCOUNT, BANK_NAME, BANK_OWNER,
    ADMIN_CHAT_ID, PRODUCTS, BASE_URL,
    GEMINI_API_KEY, CLAUDE_API_KEY, OPENAI_API_KEY
)
from db import (
    init_db, create_order, mark_order_paid, get_pending_order_by_code, get_order_status, expire_stale_orders, PENDING_TIMEOUT_MINUTES,
    get_order_by_chat, log_unmatched_payment, get_unmatched_payments,
    get_unmatched_payments_page, get_today_stats, get_pending_orders, get_recent_orders,
    update_product_link, get_product_link, conn as db_conn
)
from agent_db import (
    AGENT_PLANS, create_subscription, get_subscription, can_send_message, increment_msg_count,
    save_onboarding_state, get_onboarding_state, clear_onboarding_state,
    save_shop_profile, get_shop_profile, is_onboarding_complete,
    build_agent_prompt, build_group_agent_prompt,
    save_chat, get_recent_conversation, get_agent_stats,
    get_industry_knowledge,
    register_group, activate_group, deactivate_group, get_owner_for_group,
    get_groups_for_owner, get_agent_group_stats, set_group_mode, get_group_mode,
    is_question_message,
    get_or_create_web_token, get_owner_by_web_token,
    import_products_from_csv, search_products, get_product_count, clear_products,
    save_correction, find_correction_match, count_corrections,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

app = Flask(__name__)
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
BOT_USERNAME = "TroLyAIThucChien_bot"
BOT_ID = None  # resolved at runtime from first my_chat_member

# Thread pool for non-blocking AI calls
AI_EXECUTOR = ThreadPoolExecutor(max_workers=32)
HISTORY_LIMIT = 10  # conversation memory depth

# Rating & Correction state
RATING_STATE = {}  # (chat_id, msg_id) -> {"question": str, "owner_chat_id": int}
PENDING_CORRECTION = {}  # owner_chat_id -> {"question": str}

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
            [{"text": "🆕 Copywriter Việt Pro — 449.000đ", "callback_data": "info_copywriter"}],
            [{"text": "🤖 Thuê AI Agent — từ 99k/tháng", "callback_data": "agent_info"},
             {"text": "📋 Kiểm tra đơn", "callback_data": "trang_thai"}],
            [{"text": "📞 Liên hệ", "callback_data": "lien_he"},
             {"text": "❓ Hướng dẫn", "callback_data": "huong_dan"}],
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

def tg_download_file(file_id):
    """Download a file from Telegram by file_id. Returns bytes or None."""
    r = requests.get(f"{TG_API}/getFile?file_id={file_id}", timeout=10)
    if not r.ok:
        return None
    fp = r.json().get("result", {}).get("file_path")
    if not fp:
        return None
    r2 = requests.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}", timeout=30)
    return r2.content if r2.ok else None


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
        "• 💻 OpenCode Thực Chiến — *149.000đ*\n"
        "• 🆕 Copywriter Việt Pro — *449.000đ* (bộ 20 file: instructions + 15 knowledge base — cần Claude Pro hoặc ChatGPT Plus)\n"
        "• 🆕 AI Agent — *thuê bao từ 99k/tháng* (trợ lý AI làm việc 24/7 trong Telegram)\n\n"
        "Chọn sản phẩm bên dưới để đặt mua 👇"
    )
    tg_send(chat_id, text, reply_markup=tg_keyboard())


# ============================================================
# AGENT HANDLERS
# ============================================================

AGENT_ONBOARD_STATE = {}


def handle_agent_info(chat_id):
    """Show AI Agent product info + pricing."""
    kb = {
        "inline_keyboard": [
            [{"text": "🤖 Basic — 99k/tháng", "callback_data": "agent_subscribe_basic"},
             {"text": "🚀 Pro — 199k/tháng", "callback_data": "agent_subscribe_pro"}],
            [{"text": "💼 Business — 499k/tháng", "callback_data": "agent_subscribe_business"}],
            [{"text": "🏠 Menu chính", "callback_data": "ve_menu"}],
        ]
    }
    tg_send(chat_id,
        "*🤖 AI Agent — Thuê trợ lý AI làm việc 24/7*\n\n"
        "Không phải file. Không phải video. Là một AI Agent thực sự "
        "sống trong Telegram, làm việc cho bạn ngay lập tức.\n\n"
        "*Các gói thuê bao:*\n"
        "• 🌱 *Basic* — 99.000đ/tháng — Gemini AI, 30 tin/ngày\n"
        "• 🚀 *Pro* — 199.000đ/tháng — Claude AI, 100 tin/ngày\n"
        "• 💼 *Business* — 499.000đ/tháng — GPT-4, 500 tin/ngày\n\n"
        "*Cách hoạt động:*\n"
        "1. Bạn chọn gói → thanh toán\n"
        "2. Bot hướng dẫn khai báo thông tin shop\n"
        "3. AI Agent bắt đầu làm việc cho bạn ngay trong Telegram này\n"
        "4. Gia hạn hàng tháng — ngừng trả là ngừng dùng\n\n"
        "Chọn gói bên dưới 👇",
        reply_markup=kb
    )


def handle_agent_subscribe(chat_id, plan_key):
    """Handle subscription purchase — reuse handle_mua flow."""
    plan = AGENT_PLANS.get(plan_key)
    if not plan:
        tg_send(chat_id, "Gói không tồn tại.")
        return
    # Use the existing mua flow with agent_ prefix for SKU matching
    # We handle it normally: create order → pay → on payment, activate subscription
    from config import PRODUCTS
    # Temporarily add to PRODUCTS for payment flow
    PRODUCTS[f"agent_{plan_key}"] = {"name": plan["name"], "price": plan["price"]}
    handle_mua(chat_id, f"agent_{plan_key}")


def handle_agent_dashboard(chat_id):
    """Show subscription status and settings."""
    sub = get_subscription(chat_id)
    if not sub:
        handle_agent_info(chat_id)
        return
    profile = get_shop_profile(chat_id)
    days_left = 0
    try:
        expires = datetime.fromisoformat(sub["expires_at"])
        days_left = max(0, (expires - datetime.utcnow()).days)
    except:
        pass
    plan_info = AGENT_PLANS.get(sub["plan"], {})
    status_icon = "✅" if sub["status"] == "active" else "❌"
    onboard_icon = "✅" if profile and profile.get("onboarding_done") else "⚠️ Chưa hoàn thành"
    shop_name = profile.get("shop_name", "Chưa cập nhật") if profile else "Chưa cập nhật"

    kb = {
        "inline_keyboard": [
            [{"text": "⚙️ Cài đặt shop", "callback_data": "agent_reset"}],
            [{"text": "✍️ Chat với Agent", "callback_data": "agent_chat_mode"}],
            [{"text": "🏠 Menu chính", "callback_data": "ve_menu"}],
        ]
    }
    tg_send(chat_id,
        f"*🤖 AI Agent Dashboard*\n\n"
        f"Trạng thái: {status_icon} {sub['status'].upper()}\n"
        f"Gói: {plan_info.get('name', sub['plan'])}\n"
        f"Model: {sub['model'].upper()}\n"
        f"Còn: {days_left} ngày\n"
        f"Tin nhắn hôm nay: {sub['msgs_today']}/{sub['daily_msgs']}\n"
        f"Shop: {shop_name}\n"
        f"Onboarding: {onboard_icon}\n\n"
        f"Chọn chức năng 👇",
        reply_markup=kb
    )


def handle_agent_reset(chat_id):
    """Reset onboarding — start over."""
    AGENT_ONBOARD_STATE[chat_id] = {"step": "shop_name", "data": {}}
    clear_onboarding_state(chat_id)
    tg_send(chat_id,
        "Bắt đầu cập nhật thông tin shop.\n\n"
        "*Bước 1/6 — Tên shop:*\n"
        "Gõ tên shop/cửa hàng của anh/chị:")
    AGENT_ONBOARD_STATE[chat_id] = {"step": "shop_name", "data": {}}
    save_onboarding_state(chat_id, "shop_name", {})


def start_onboarding(chat_id):
    """Start the onboarding flow after first subscription."""
    AGENT_ONBOARD_STATE[chat_id] = {"step": "shop_name", "data": {}}
    save_onboarding_state(chat_id, "shop_name", {})
    tg_send(chat_id,
        "🎉 *Chúc mừng! AI Agent đã sẵn sàng.*\n\n"
        "Trước khi bắt đầu, hãy cho tôi biết về shop của anh/chị nhé.\n\n"
        "*Bước 1/6 — Tên shop:*\n"
        "Gõ tên shop hoặc cửa hàng của anh/chị:")


def handle_onboarding_message(chat_id, text):
    """Process onboarding steps."""
    state = AGENT_ONBOARD_STATE.get(chat_id)
    if not state:
        return False
    step = state["step"]
    data = state["data"]

    steps = [
        ("shop_name", "Bước 2/6 — Ngành hàng:", None),
        ("industry", "Bước 3/6 — Giọng văn mong muốn:",
         [("Chuyên nghiệp", "chuyen-nghiep"),
          ("Thân thiện", "than-thien"),
          ("Hài hước trẻ trung", "hai-huoc")]),
        ("brand_voice", "Bước 4/6 — Sản phẩm chính:", None),
        ("product_info", "Bước 5/6 — Khách hàng mục tiêu:", None),
        ("target_customer", "Bước 6/6 — FAQ (câu hỏi thường gặp):", None),
    ]

    if step == "shop_name":
        data["shop_name"] = text.strip()
        next_step = "industry"
        tg_send(chat_id,
            "*Bước 2/6 — Ngành hàng:*\n"
            "Anh/chị bán ngành nào? Chọn bên dưới:",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "👗 Thời trang", "callback_data": "onboard_industry_thoi-trang"},
                     {"text": "🍜 F&B", "callback_data": "onboard_industry_f-b"}],
                    [{"text": "💄 Làm đẹp", "callback_data": "onboard_industry_lam-dep"},
                     {"text": "👶 Mẹ & Bé", "callback_data": "onboard_industry_me-be"}],
                    [{"text": "📱 Công nghệ", "callback_data": "onboard_industry_cong-nghe"},
                     {"text": "🛋 Nội thất", "callback_data": "onboard_industry_noi-that"}],
                    [{"text": "💊 Sức khoẻ", "callback_data": "onboard_industry_suc-khoe"},
                     {"text": "📚 Giáo dục", "callback_data": "onboard_industry_giao-duc"}],
                    [{"text": "✈️ Du lịch", "callback_data": "onboard_industry_du-lich"},
                     {"text": "🏢 Dịch vụ", "callback_data": "onboard_industry_dich-vu"}],
                ]
            }
        )
    else:
        # step == "faq" → finish
        data["faq"] = text.strip()
        finalize_onboarding(chat_id, data)
        return True

    AGENT_ONBOARD_STATE[chat_id] = {"step": next_step, "data": data}
    save_onboarding_state(chat_id, next_step, data)
    return True


def handle_onboard_callback(chat_id, data):
    """Handle inline callback for onboarding (industry selection)."""
    state = AGENT_ONBOARD_STATE.get(chat_id)
    if not state:
        return

    if data.startswith("onboard_industry_"):
        industry = data.replace("onboard_industry_", "")
        state["data"]["industry"] = industry
        state["step"] = "brand_voice"
        save_onboarding_state(chat_id, "brand_voice", state["data"])
        tg_send(chat_id,
            "*Bước 3/6 — Giọng văn:*\n"
            "Anh/chị muốn AI Agent nói chuyện với khách hàng như thế nào?",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "👔 Chuyên nghiệp", "callback_data": "onboard_voice_chuyen-nghiep"}],
                    [{"text": "😊 Thân thiện", "callback_data": "onboard_voice_than-thien"}],
                    [{"text": "😂 Hài hước trẻ trung", "callback_data": "onboard_voice_hai-huoc"}],
                ]
            }
        )
    elif data.startswith("onboard_voice_"):
        voice = data.replace("onboard_voice_", "")
        state["data"]["brand_voice"] = voice
        state["step"] = "product_info"
        save_onboarding_state(chat_id, "product_info", state["data"])
        tg_send(chat_id,
            "*Bước 4/6 — Sản phẩm chính:*\n"
            "Mô tả ngắn gọn sản phẩm anh/chị đang bán.\n"
            "Ví dụ: *Áo thun nam cotton, giá 199k-399k, 20 màu, chất liệu mềm mịn*")


def finalize_onboarding(chat_id, data):
    """Save all onboarding data and activate agent."""
    save_shop_profile(
        chat_id=chat_id,
        shop_name=data.get("shop_name", ""),
        industry=data.get("industry", ""),
        brand_voice=data.get("brand_voice", ""),
        product_info=data.get("product_info", ""),
        target_customer=data.get("target_customer", ""),
        faq=data.get("faq", ""),
    )
    clear_onboarding_state(chat_id)
    AGENT_ONBOARD_STATE.pop(chat_id, None)

    industry_names = {
        "thoi-trang": "Thời trang", "f-b": "F&B", "lam-dep": "Làm đẹp",
        "me-be": "Mẹ & Bé", "cong-nghe": "Công nghệ", "noi-that": "Nội thất",
        "suc-khoe": "Sức khoẻ", "giao-duc": "Giáo dục", "du-lich": "Du lịch",
        "dich-vu": "Dịch vụ"
    }
    tg_send(chat_id,
        f"✅ *Hoàn tất! AI Agent đã được trang bị đầy đủ thông tin.*\n\n"
        f"Shop: {data.get('shop_name')}\n"
        f"Ngành: {industry_names.get(data.get('industry', ''), data.get('industry', ''))}\n"
        f"Sản phẩm: {data.get('product_info', '')[:60]}...\n\n"
        f"Bây giờ anh/chị có thể chat với tôi như một trợ lý bán hàng!\n\n"
        f"*Ví dụ:*\n"
        f"• \"Viết giúp tôi bài Facebook bán sản phẩm\"\n"
        f"• \"Khách hỏi sp còn ko, tôi nên trả lời sao?\"\n"
        f"• \"Phân tích điểm mạnh yếu của shop tôi\"\n\n"
        f"*Gõ /agent để quay lại Dashboard*"
    )


def handle_agent_chat(chat_id, text):
    """Main chat handler for subscribed users."""
    can, reason = can_send_message(chat_id)
    if not can:
        sub = get_subscription(chat_id)
        if not sub:
            tg_send(chat_id,
                "Bạn chưa đăng ký AI Agent. Gõ /start để xem menu.",
                reply_markup={"inline_keyboard": [
                    [{"text": "🤖 Xem gói thuê", "callback_data": "agent_info"}]
                ]}
            )
        elif reason == "Hết hạn":
            tg_send(chat_id,
                "⏳ *Gói của bạn đã hết hạn.*\n"
                "Vui lòng gia hạn để tiếp tục sử dụng.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🔄 Gia hạn ngay", "callback_data": f"agent_subscribe_{sub['plan']}"}],
                        [{"text": "📋 Dashboard", "callback_data": "agent_dashboard"}],
                    ]
                }
            )
        else:
            tg_send(chat_id,
                f"⚠️ Bạn đã dùng hết {sub['daily_msgs']} tin nhắn hôm nay.\n"
                f"Hạn mức sẽ reset vào nửa đêm.\n"
                f"Nâng cấp gói để tăng hạn mức.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🚀 Nâng cấp", "callback_data": "agent_info"}],
                        [{"text": "📋 Dashboard", "callback_data": "agent_dashboard"}],
                    ]
                }
            )
        return True

    # Check onboarding
    if not is_onboarding_complete(chat_id):
        # Redirect to onboarding
        tg_send(chat_id, "⚠️ Vui lòng hoàn tất cài đặt shop trước khi chat với Agent.")
        start_onboarding(chat_id)
        return True

    # Build prompt with full context
    profile = get_shop_profile(chat_id)
    sub = get_subscription(chat_id)

    system_prompt, full_prompt = build_agent_prompt(chat_id, text)
    if not system_prompt:
        tg_send(chat_id, "⚠️ Chưa có thông tin shop. Vui lòng cài đặt trước.")
        return True

    model = sub.get("model", "gemini")

    # Show typing indicator
    requests.post(f"{TG_API}/sendChatAction", json={
        "chat_id": chat_id, "action": "typing"
    }, timeout=5)

    # Reserve message slot before async call
    increment_msg_count(chat_id)

    # Submit AI call to background thread — webhook returns immediately
    AI_EXECUTOR.submit(_execute_ai_response, chat_id, chat_id, "owner",
                       system_prompt, text, model)
    return True


def call_ai_model(model, system_prompt, user_message):
    """Call the selected AI model API."""
    import requests as rq

    if model == "gemini":
        key = GEMINI_API_KEY
        if not key:
            return "⚠️ Gemini chưa được cấu hình. Liên hệ admin."
        url = f"https://generativelanguage.googleapis.com/v1/models/gemini-2.0-flash:generateContent?key={key}"
        payload = {
            "contents": [{
                "parts": [{"text": f"{system_prompt}\n\nNgười dùng: {user_message}\n\nTrợ lý:"}]
            }],
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 2048,
            }
        }
        try:
            r = rq.post(url, json=payload, timeout=30)
            if r.ok:
                data = r.json()
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    return parts[0].get("text", "") if parts else ""
            log.error(f"Gemini error: {r.status_code} {r.text[:200]}")
            return ""
        except Exception as e:
            log.error(f"Gemini request failed: {e}")
            return ""

    elif model == "claude":
        key = CLAUDE_API_KEY
        if not key:
            return "⚠️ Claude chưa được cấu hình. Liên hệ admin."
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 2048,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_message}]
        }
        try:
            r = rq.post(url, json=payload, headers=headers, timeout=30)
            if r.ok:
                data = r.json()
                content = data.get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        return block.get("text", "")
            log.error(f"Claude error: {r.status_code} {r.text[:200]}")
            return ""
        except Exception as e:
            log.error(f"Claude request failed: {e}")
            return ""

    elif model == "gpt4":
        key = OPENAI_API_KEY
        if not key:
            return "⚠️ GPT-4 chưa được cấu hình. Liên hệ admin."
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            "max_tokens": 2048,
            "temperature": 0.7,
        }
        try:
            r = rq.post(url, json=payload, headers=headers, timeout=30)
            if r.ok:
                data = r.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
            log.error(f"GPT-4 error: {r.status_code} {r.text[:200]}")
            return ""
        except Exception as e:
            log.error(f"GPT-4 request failed: {e}")
            return ""

    return "⚠️ Model không hợp lệ."


from datetime import datetime


# ============================================================
# GROUP MODE
# ============================================================

def handle_my_chat_member(update):
    """Detect bot added to or removed from a group."""
    global BOT_ID
    mcm = update.get("my_chat_member")
    if not mcm:
        return
    chat = mcm.get("chat", {})
    chat_id = chat.get("id")
    if chat.get("type") not in ("group", "supergroup"):
        return
    new_status = mcm.get("new_chat_member", {}).get("status", "")
    from_user = mcm.get("from", {})
    adder_id = from_user.get("id")

    bot_info = mcm.get("new_chat_member", {}).get("user", {})
    if bot_info.get("id"):
        BOT_ID = bot_info["id"]

    if new_status in ("member", "administrator"):
        register_group(chat_id, adder_id)
        title = chat.get("title", "Group")
        tg_send(chat_id,
            f"👋 Cảm ơn đã thêm tôi vào *{title}*!\n\n"
            f"Tôi là trợ lý AI — sẽ hỗ trợ nhóm của bạn trả lời khách hàng.\n\n"
            f"*Để kích hoạt:* chủ shop gõ /claim tại group này.\n"
            f"Sau đó ai @tôi tôi sẽ trả lời.")
        log.info(f"Bot added to group {chat_id} ({title}) by {adder_id}")
    elif new_status in ("left", "kicked"):
        deactivate_group(chat_id)
        log.info(f"Bot removed from group {chat_id}")


def _send_with_rate(chat_id, text, owner_chat_id, question):
    """Send message with 👍👎 buttons. Returns message_id or None."""
    payload = {
        "chat_id": chat_id, "text": text,
        "parse_mode": "Markdown", "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "👍", "callback_data": "rate_ok"},
                {"text": "👎", "callback_data": "rate_bad"},
            ]]
        }
    }
    r = _tg_call("sendMessage", payload)
    if r and r.ok:
        msg_id = r.json().get("result", {}).get("message_id")
        if msg_id:
            RATING_STATE[(chat_id, msg_id)] = {"question": question, "owner_chat_id": owner_chat_id}
        return msg_id
    return None


def _execute_ai_response(chat_id, owner_chat_id, customer_id,
                         system_prompt, user_message, model):
    """Run AI model call + save chat + send response with rating. Runs in executor."""
    try:
        # Check if there's a saved correction matching this question
        match = find_correction_match(owner_chat_id, user_message)
        if match:
            _, corrected_answer = match
            save_chat(owner_chat_id, customer_id, "user", user_message, model)
            save_chat(owner_chat_id, customer_id, "agent", corrected_answer, model)
            _send_with_rate(chat_id, corrected_answer, owner_chat_id, user_message)
            return

        # Inject recent conversation history for context
        history = get_recent_conversation(owner_chat_id, customer_id, limit=HISTORY_LIMIT)
        if history:
            lines = []
            for h in history:
                label = "Khách" if h["role"] == "user" else "Shop"
                lines.append(f"{label}: {h['message']}")
            full_user_message = (
                "LỊCH SỬ HỘI THOẠI GẦN ĐÂY:\n" +
                "\n".join(lines) +
                f"\n\nTIN NHẮN MỚI:\n{user_message}"
            )
        else:
            full_user_message = user_message

        response_text = call_ai_model(model, system_prompt, full_user_message)
        if not response_text:
            tg_send(chat_id, "⚠️ Lỗi kết nối AI. Vui lòng thử lại sau.")
            return

        save_chat(owner_chat_id, customer_id, "user", user_message, model)
        save_chat(owner_chat_id, customer_id, "agent", response_text, model)
        _send_with_rate(chat_id, response_text, owner_chat_id, user_message)
    except Exception as e:
        log.exception(f"AI executor error: {e}")
        tg_send(chat_id, "❌ Có lỗi xảy ra. Vui lòng thử lại sau.")


def handle_group_message(chat_id, msg, text):
    """Respond in group. Behaviour depends on group mode:
    - mention: only when @bot or reply to bot
    - smart: auto-detect questions
    - auto: reply to everything
    """
    chat_type = msg.get("chat", {}).get("type", "")
    if chat_type not in ("group", "supergroup"):
        return False

    mode = get_group_mode(chat_id)
    should_respond = False

    # Always respond to @mention and reply-to-bot
    entities = msg.get("entities") or msg.get("caption_entities") or []
    for ent in entities:
        if ent.get("type") == "mention":
            m = text[ent["offset"]:ent["offset"] + ent["length"]]
            if m.lower() == f"@{BOT_USERNAME.lower()}":
                should_respond = True
                break

    reply = msg.get("reply_to_message")
    if reply and BOT_ID:
        ru = reply.get("from", {})
        if ru.get("id") == BOT_ID:
            should_respond = True

    # Mode-specific triggers
    if not should_respond:
        if mode == "auto":
            should_respond = True
        elif mode == "smart":
            should_respond = is_question_message(text)

    if not should_respond:
        return False

    owner_chat_id = get_owner_for_group(chat_id)
    if not owner_chat_id:
        tg_send(chat_id, "Group chưa có chủ. Chủ shop gõ /claim để đăng ký.")
        return True

    can, reason = can_send_message(owner_chat_id)
    if not can:
        tg_send(chat_id, "Xin lỗi, dịch vụ tạm ngưng. Vui lòng quay lại sau.")
        return True

    sender = msg.get("from", {})
    sender_id = sender.get("id", 0)
    sender_name = sender.get("first_name", "bạn")
    if sender.get("last_name"):
        sender_name += " " + sender["last_name"]

    clean = text.replace(f"@{BOT_USERNAME}", "").strip()
    if not clean:
        tg_send(chat_id, "Dạ em đây ạ. Có gì anh/chị cần hỗ trợ không?")
        return True

    requests.post(f"{TG_API}/sendChatAction", json={
        "chat_id": chat_id, "action": "typing"
    }, timeout=5)

    sub = get_subscription(owner_chat_id)
    model = sub.get("model", "gemini") if sub else "gemini"

    system_prompt, full_prompt = build_group_agent_prompt(owner_chat_id, clean, sender_name)
    if not system_prompt:
        tg_send(chat_id, "Shop chưa cập nhật thông tin. Vui lòng quay lại sau.")
        return True

    # Reserve message slot before async call
    increment_msg_count(owner_chat_id)

    # Submit AI call to background thread — per-sender memory
    AI_EXECUTOR.submit(_execute_ai_response, chat_id, owner_chat_id,
                       f"g{chat_id}_u{sender_id}", system_prompt, clean, model)
    return True


def handle_group_claim(chat_id, msg):
    """Activate an agent group — /claim in group."""
    sender_id = msg["from"]["id"]
    sub = get_subscription(sender_id)
    if not sub or sub["status"] != "active":
        tg_send(chat_id,
            "Bạn cần đăng ký AI Agent trước. Gõ /start để xem gói.",
            reply_markup={"inline_keyboard": [
                [{"text": "🤖 Xem gói thuê", "callback_data": "agent_info"}]
            ]})
        return
    activate_group(chat_id)
    tg_send(chat_id,
        "✅ *Đã kích hoạt!*\n\n"
        "Từ giờ khi ai @tôi, tôi sẽ trả lời với giọng văn của shop bạn.\n"
        "Gõ /leave nếu muốn tôi rời group.")
    tg_send(sender_id,
        f"✅ *AI Agent đã được kích hoạt cho một group!*\n"
        f"Tôi sẽ tự động trả lời khách hàng khi được @mention trong đó.")
    log.info(f"Group {chat_id} activated by {sender_id}")


def handle_mygroups(chat_id):
    """List all groups owned by a subscriber."""
    groups = get_groups_for_owner(chat_id)
    if not groups:
        tg_send(chat_id,
            "Bạn chưa thêm bot vào group nào.\n\n"
            "Cách thêm:\n"
            "1. Mở Telegram → Group của bạn\n"
            "2. Thêm thành viên → @TroLyAIThucChien_bot\n"
            "3. Vào group, gõ /claim để kích hoạt")
        return
    msg = "*📋 Danh sách Group:*\n\n"
    for g in groups:
        icon = "✅" if g["status"] == "active" else "⏳" if g["status"] == "pending" else "❌"
        label = "Đang hoạt động" if g["status"] == "active" else "Chờ kích hoạt" if g["status"] == "pending" else "Ngừng"
        msg += f"{icon} `{g['group_chat_id']}` — {label}\n"
    msg += "\nThêm bot vào group mới → gõ /claim trong group đó."
    tg_send(chat_id, msg)


def handle_group_mode(chat_id, msg, args):
    """Change group mode — /mode smart|auto|mention. Only owner can change."""
    sender_id = msg["from"]["id"]
    owner_id = get_owner_for_group(chat_id)
    if not owner_id or sender_id != owner_id:
        tg_send(chat_id, "Chỉ chủ shop mới có thể đổi chế độ.")
        return

    if not args or args[0] not in ("mention", "smart", "auto"):
        tg_send(chat_id,
            "Chế độ hiện tại: có thể đổi bằng /mode mention|smart|auto\n\n"
            "• *mention* — Chỉ trả lời khi @bot\n"
            "• *smart* — Tự nhận diện câu hỏi của khách\n"
            "• *auto* — Trả lời mọi tin nhắn")
        return

    mode = args[0]
    set_group_mode(chat_id, mode)
    labels = {"mention": "@mention là bot trả lời",
              "smart": "tự nhận diện câu hỏi",
              "auto": "trả lời tất cả"}
    tg_send(chat_id, f"✅ Đã chuyển sang chế độ *{mode}* — {labels.get(mode, '')}.")


def handle_webwidget(chat_id):
    """Generate web widget token and return embed code."""
    profile = get_shop_profile(chat_id)
    if not profile or not profile.get("onboarding_done"):
        tg_send(chat_id, "⚠️ Vui lòng hoàn tất cài đặt shop trước. Gõ /agent để vào Dashboard.")
        return

    token = get_or_create_web_token(chat_id)
    embed = (
        f"<script src=\"{BASE_URL}/widget.js\" data-token=\"{token}\"></script>"
    )
    tg_send(chat_id,
        "*🌐 Widget Chat cho Website*\n\n"
        "Dán đoạn mã sau vào file HTML của trang web bạn (trước thẻ </body>):\n\n"
        f"`{embed}`\n\n"
        "Sau đó, khách truy cập website sẽ thấy nút chat ở góc dưới phải.\n"
        "Họ chat → bot trả lời tự động với giọng văn shop bạn.\n\n"
        f"Token: `{token}`\n"
        "Giữ token này riêng tư. Nếu cần cấp lại, gõ /webwidget_reset.")


def handle_webwidget_reset(chat_id):
    """Reset web widget token."""
    profile = get_shop_profile(chat_id)
    if not profile:
        return
    get_or_create_web_token(chat_id)  # generates new
    tg_send(chat_id, "✅ Token widget đã được cấp lại. Gõ /webwidget để lấy mã nhúng mới.")


def handle_catalog(chat_id):
    """Show catalog status or accept CSV/Excel upload."""
    profile = get_shop_profile(chat_id)
    if not profile or not profile.get("onboarding_done"):
        tg_send(chat_id, "⚠️ Vui lòng hoàn tất cài đặt shop trước. Gõ /agent.")
        return
    count = get_product_count(chat_id)
    tg_send(chat_id,
        f"*📦 Danh mục sản phẩm*\n\n"
        f"Hiện có: *{count} sản phẩm*\n\n"
        f"Cách thêm sản phẩm:\n"
        f"• Gửi file *CSV* hoặc *Excel (.xlsx)* — bot tự động import\n"
        f"• Gõ /catalog_clear để xoá toàn bộ\n\n"
        f"*Hàng đầu tiên phải là header:*\n"
        f"`name, sku, price, stock, category, description`\n"
        f"_(có thể dịch: tên, mã, giá, tồn, danh mục, mô tả)_\n\n"
        f"*Ví dụ dòng dữ liệu:*\n"
        f"`Áo thun nam,AT001,199000,50,Thời trang,\"Áo cotton, 5 màu\"`\n\n"
        f"_Các cột lạ sẽ bỏ qua. Cột 'name' là bắt buộc._")


def handle_catalog_clear(chat_id):
    clear_products(chat_id)
    tg_send(chat_id, "✅ Đã xoá toàn bộ danh mục sản phẩm.")


def handle_catalog_file(chat_id, file_id, file_name):
    """Import products from uploaded CSV or Excel (.xlsx)."""
    if not file_name:
        tg_send(chat_id, "⚠️ File không có tên. Vui lòng gửi lại.")
        return

    raw = tg_download_file(file_id)
    if not raw:
        tg_send(chat_id, "⚠️ Không thể tải file. Thử lại sau.")
        return

    csv_text = None
    fname = file_name.lower()

    if fname.endswith(".csv"):
        try:
            csv_text = raw.decode("utf-8-sig")
        except:
            tg_send(chat_id, "⚠️ File CSV không đúng định dạng UTF-8.")
            return

    elif fname.endswith(".xlsx"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                tg_send(chat_id, "⚠️ File Excel không có dữ liệu.")
                return
            import csv, io as _io
            buf = _io.StringIO()
            w = csv.writer(buf)
            for row in rows:
                w.writerow([str(c) if c is not None else "" for c in row])
            csv_text = buf.getvalue()
        except ImportError:
            tg_send(chat_id, "⚠️ Bot chưa hỗ trợ Excel. Vui lòng gửi file CSV (UTF-8).")
            return
        except Exception as e:
            log.exception(f"Excel parse error: {e}")
            tg_send(chat_id, f"⚠️ Lỗi đọc Excel: {str(e)[:150]}")
            return
    else:
        tg_send(chat_id, "⚠️ Chỉ chấp nhận file CSV (.csv) hoặc Excel (.xlsx).")
        return

    try:
        count = import_products_from_csv(chat_id, csv_text)
        if count:
            tg_send(chat_id, f"✅ Đã import *{count} sản phẩm* thành công!")
        else:
            tg_send(chat_id, "⚠️ Không tìm thấy sản phẩm nào. Kiểm tra header (cần cột 'name').")
    except Exception as e:
        log.exception(f"Catalog import error: {e}")
        tg_send(chat_id, f"⚠️ Lỗi import: {str(e)[:200]}")


def handle_agent_admin(chat_id):
    """Admin stats for agent module — now includes group stats."""
    stats = get_agent_stats()
    g = get_agent_group_stats()
    msg = (
        "*🤖 AI Agent — Thống kê*\n\n"
        f"📊 Đang hoạt động: *{stats['total_active']}*\n"
        f"🌱 Basic: {stats['basic']}\n"
        f"🚀 Pro: {stats['pro']}\n"
        f"💼 Business: {stats['business']}\n\n"
        f"💬 Tin nhắn hôm nay: {stats['msgs_today']}\n"
        f"📝 Tổng chat: {stats['total_chats']}\n"
        f"⏳ Hết hạn: {stats['expired']}\n\n"
        f"*Group Mode:*\n"
        f"📋 Tổng group: {g['total']}\n"
        f"✅ Đang hoạt động: {g['active']}\n"
        f"⏳ Chờ kích hoạt: {g['pending']}"
    )
    tg_send(chat_id, msg)


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

        # Handle agent subscriptions
        if sku.startswith("agent_"):
            plan_key = sku.replace("agent_", "", 1) if sku.startswith("agent_agent_") else sku.replace("agent_", "")
            plan = AGENT_PLANS.get(plan_key, AGENT_PLANS.get("agent_basic"))
            mark_order_paid(code, f"MANUAL-{chat_id}", "agent")
            create_subscription(customer_chat_id, plan_key, plan["model"], plan["daily_msgs"])
            tg_send(customer_chat_id,
                    f"✅ Đã nhận thanh toán *{expected_amount:,}đ* cho đơn *#{code}*.\n"
                    f"Gói: *{plan['name']}*\n"
                    f"AI Agent của bạn đã sẵn sàng! Hãy cài đặt thông tin shop.")
            start_onboarding(customer_chat_id)
            tg_send(chat_id,
                    f"✅ Đã confirm đơn *#{code}* — {plan['name']} — {expected_amount:,}đ.\n"
                    f"Đã kích hoạt Agent + gửi onboarding cho khách.")
            return

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
            "  SKU: `mua_combo`, `mua_claude`, `mua_opencode`, `mua_copywriter`\n"
            "  Agent: `/agent_agent_stats` — xem thống kê AI Agent\n\n"
            "*Báo cáo:*\n"
            "`/sale_stats` — doanh số theo SKU\n"
            "`/admin_today` — thống kê hôm nay"
        )
        tg_send(chat_id, msg)

    elif cmd == "/agent_agent_stats":
        handle_agent_admin(chat_id)

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


@app.route("/widget.js")
def widget_js():
    return send_file("static/widget.js", mimetype="application/javascript")


@app.route("/api/chat", methods=["POST"])
def api_chat():
    """Public API for website chat widget."""
    data = request.get_json(silent=True) or {}
    token = data.get("token", "")
    message = data.get("message", "").strip()
    session = data.get("session", "default")

    if not token:
        return jsonify({"error": "missing_token"}), 400
    if not message:
        return jsonify({"error": "missing_message"}), 400

    owner_id = get_owner_by_web_token(token)
    if not owner_id:
        return jsonify({"error": "invalid_token"}), 403

    can, reason = can_send_message(owner_id)
    if not can:
        return jsonify({"error": "limit_exceeded", "reason": reason}), 429

    profile = get_shop_profile(owner_id)
    if not profile or not profile.get("onboarding_done"):
        return jsonify({"error": "shop_not_configured"}), 400

    system_prompt, _ = build_group_agent_prompt(owner_id, message, "Khách")
    if not system_prompt:
        return jsonify({"error": "shop_not_configured"}), 400

    sub = get_subscription(owner_id)
    model = sub.get("model", "gemini") if sub else "gemini"

    response_text = call_ai_model(model, system_prompt, message)
    if not response_text:
        return jsonify({"error": "ai_error"}), 500

    save_chat(owner_id, f"web_{session}", "user", message, model)
    save_chat(owner_id, f"web_{session}", "agent", response_text, model)
    increment_msg_count(owner_id)

    return jsonify({"reply": response_text, "session": session})


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

        # Rating buttons
        if data == "rate_ok":
            owner_id = cq["from"]["id"]
            msg_id = cq["message"]["message_id"]
            state = RATING_STATE.pop((chat_id, msg_id), None)
            if state and state["owner_chat_id"] == owner_id:
                tg_send(chat_id, "🙌 Cảm ơn anh! Em sẽ cố gắng hơn.")
            return jsonify({"ok": True})
        if data == "rate_bad":
            owner_id = cq["from"]["id"]
            msg_id = cq["message"]["message_id"]
            state = RATING_STATE.pop((chat_id, msg_id), None)
            if state and state["owner_chat_id"] == owner_id:
                PENDING_CORRECTION[owner_id] = {"question": state["question"]}
                tg_send(owner_id,
                    "Dạ, em đã ghi nhận. Anh vui lòng gõ câu trả lời đúng "
                    f"cho câu hỏi:\n\n*{state['question'][:200]}*\n\n"
                    "Em sẽ ghi nhớ và dùng câu này cho lần sau ạ 🙏")
            return jsonify({"ok": True})

        if data.startswith("admin_"):
            handle_admin_callback(chat_id, data)
        elif data == "agent_info":
            handle_agent_info(chat_id)
        elif data.startswith("agent_subscribe_"):
            handle_agent_subscribe(chat_id, data.replace("agent_subscribe_", ""))
        elif data == "agent_dashboard":
            handle_agent_dashboard(chat_id)
        elif data == "agent_reset":
            handle_agent_reset(chat_id)
        elif data == "info_copywriter":
            kb = {
                "inline_keyboard": [
                    [{"text": "🛒 Mua ngay — 449.000đ", "callback_data": "mua_copywriter"}],
                    [{"text": "🏠 Menu chính", "callback_data": "ve_menu"}],
                ]
            }
            tg_send(chat_id,
                "*Copywriter Việt Pro — Trợ lý Marketing AI*\n\n"
                "Bộ 20 file hướng dẫn để \"dạy\" Claude/ChatGPT viết content kiểu người Việt:\n\n"
                "📄 `instructions.md` — System Prompt: 10 module tư duy, giọng văn, cách xưng hô\n"
                "📂 `knowledge/` — 15 file kiến thức: tâm lý người mua + playbook từng nền tảng + 10 ngành hàng\n"
                "📋 `examples.md` — 15 prompt mẫu copy-paste dùng ngay\n"
                "📎 `industry-quick-reference.md` — Bảng tra nhanh\n"
                "📖 `setup-guide.md` — Hướng dẫn cài đặt A-Z\n\n"
                "*⚠️ Yêu cầu:* Cần Claude Pro hoặc ChatGPT Plus (~$20/tháng) để dùng. "
                "Đây là file text, không phải phần mềm AI.\n\n"
                "*Giá:* 449.000đ — thanh toán 1 lần, dùng vĩnh viễn.",
                reply_markup=kb
            )
        elif data.startswith("mua_"):
            handle_mua(chat_id, data)
        elif data.startswith("onboard_"):
            handle_onboard_callback(chat_id, data)
        elif data == "agent_chat_mode":
            sub = get_subscription(chat_id)
            if not sub or sub["status"] != "active":
                tg_send(chat_id, "Bạn chưa đăng ký gói nào. Xem /start để đăng ký.")
            elif not is_onboarding_complete(chat_id):
                start_onboarding(chat_id)
            else:
                tg_send(chat_id,
                    "*Chat với AI Agent*\n\n"
                    "Gõ bất cứ điều gì — tôi sẽ trả lời như trợ lý bán hàng của bạn.\n\n"
                    "*Gợi ý:*\n"
                    "• \"Viết bài Facebook bán sản phẩm chính\"\n"
                    "• \"Soạn tin nhắn trả lời khách hỏi giá\"\n"
                    "• \"Phân tích đối thủ cạnh tranh giúp tôi\"\n\n"
                    "Gõ /agent để về Dashboard bất cứ lúc nào.")
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

    # my_chat_member — bot added/removed from group
    if "my_chat_member" in update:
        handle_my_chat_member(update)
        return jsonify({"ok": True})

    # Migrate group — update group_chat_id
    msg = update.get("message")
    if not msg:
        return jsonify({"ok": True})

    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    text = msg.get("text", "").strip()
    first_name = msg["from"].get("first_name", "bạn")

    # ---- GROUP MESSAGES ----
    if chat.get("type") in ("group", "supergroup"):
        if text == "/claim":
            handle_group_claim(chat_id, msg)
        elif text.startswith("/mode"):
            parts = text.split()
            handle_group_mode(chat_id, msg, parts[1:] if len(parts) > 1 else [])
        elif text.startswith("/"):
            pass  # ignore other commands in group
        else:
            handle_group_message(chat_id, msg, text)
        return jsonify({"ok": True})

    # ---- ADMIN STATE (DM) ----
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

    # ---- ADMIN COMMANDS (DM) ----
    admin_cmds = ("/unmatched", "/set_link", "/admin_help",
                  "/confirm", "/sale_stats", "/expire_stale",
                  "/admin", "/admin_today", "/agent_agent_stats")
    if any(text.startswith(c) for c in admin_cmds):
        handle_admin(chat_id, text)
        return jsonify({"ok": True})

    # ---- USER COMMANDS (DM) ----
    if text == "/start":
        handle_start(chat_id, first_name)
    elif text == "/mua_combo":
        handle_mua(chat_id, "mua_combo")
    elif text == "/mua_claude":
        handle_mua(chat_id, "mua_claude")
    elif text == "/mua_opencode":
        handle_mua(chat_id, "mua_opencode")
    elif text == "/mua_copywriter":
        kb = {
            "inline_keyboard": [
                [{"text": "🛒 Mua ngay — 449.000đ", "callback_data": "mua_copywriter"}],
                [{"text": "🏠 Menu chính", "callback_data": "ve_menu"}],
            ]
        }
        tg_send(chat_id,
            "*Copywriter Việt Pro — Trợ lý Marketing AI*\n\n"
            "Bộ 20 file hướng dẫn để \"dạy\" Claude/ChatGPT viết content kiểu người Việt.\n\n"
            "⚠️ Cần Claude Pro hoặc ChatGPT Plus (~$20/tháng) để dùng.\n"
            "Đây là file text, không phải phần mềm AI.\n\n"
            "*Giá:* 449.000đ — thanh toán 1 lần.",
            reply_markup=kb
        )
    elif text in ("/agent", "/dashboard"):
        handle_agent_dashboard(chat_id)
    elif text == "/mygroups":
        handle_mygroups(chat_id)
    elif text == "/webwidget":
        handle_webwidget(chat_id)
    elif text == "/webwidget_reset":
        handle_webwidget_reset(chat_id)
    elif text == "/catalog":
        handle_catalog(chat_id)
    elif text == "/catalog_clear":
        handle_catalog_clear(chat_id)
    elif text == "/corrections":
        count = count_corrections(chat_id)
        if count:
            tg_send(chat_id, f"📚 Em đã ghi nhớ *{count} câu* đã được anh sửa.")
        else:
            tg_send(chat_id, "Chưa có câu sửa nào. Khi em trả lời chưa đúng, anh bấm 👎 để dạy em nhé!")
    elif text == "/trang_thai":
        handle_trang_thai(chat_id)
    elif text == "/lien_he":
        handle_lien_he(chat_id)
    else:
        # Handle file upload (CSV for product catalog)
        doc = msg.get("document")
        if doc:
            sub = get_subscription(chat_id)
            if sub and sub["status"] == "active" and is_onboarding_complete(chat_id):
                handle_catalog_file(chat_id, doc["file_id"], doc.get("file_name", ""))
            else:
                tg_send(chat_id, "Vui lòng đăng ký và cài đặt shop trước khi import sản phẩm.")
            return jsonify({"ok": True})
        # Pending correction from 👎 rating
        if chat_id in PENDING_CORRECTION:
            pc = PENDING_CORRECTION.pop(chat_id)
            save_correction(chat_id, pc["question"], text)
            count = count_corrections(chat_id)
            tg_send(chat_id,
                f"✅ Đã lưu! Lần sau gặp câu hỏi tương tự em sẽ dùng câu trả lời này.\n"
                f"📚 Em đang có *{count} câu* đã được anh sửa.")
            return jsonify({"ok": True})
        # Check if user is in agent onboarding
        if chat_id in AGENT_ONBOARD_STATE:
            handle_onboarding_message(chat_id, text)
        else:
            # Check if user has active agent subscription → handle as agent chat
            sub = get_subscription(chat_id)
            if sub and sub["status"] == "active":
                handle_agent_chat(chat_id, text)
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

    # Check if this is an agent subscription
    if sku.startswith("agent_"):
        # Extract plan key from SKU (e.g., "agent_agent_basic" → "agent_basic")
        plan_key = sku.replace("agent_", "", 1) if sku.startswith("agent_agent_") else sku.replace("agent_", "")
        plan = AGENT_PLANS.get(plan_key, AGENT_PLANS.get("agent_basic"))
        create_subscription(chat_id, plan_key, plan["model"], plan["daily_msgs"])
        tg_send(chat_id,
            f"✅ *Đã nhận thanh toán {amount:,}đ*\n"
            f"Gói: *{plan['name']}*\n"
            f"Hiệu lực: 30 ngày\n\n"
            f"AI Agent của bạn đã sẵn sàng! Hãy cài đặt thông tin shop để bắt đầu.")
        start_onboarding(chat_id)
    else:
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

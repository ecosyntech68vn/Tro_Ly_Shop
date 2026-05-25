"""
Cấu hình từ biến môi trường (env vars).
"""
import os
import sys

# ===== Telegram =====
# Ưu tiên env var, fallback về token đã biết để đề phòng Railway/Render env chưa cập nhật.
_BOT_TOKEN_ENV = os.environ.get("BOT_TOKEN", "")
if _BOT_TOKEN_ENV:
    BOT_TOKEN = _BOT_TOKEN_ENV
else:
    print("[config] WARNING: BOT_TOKEN env var is EMPTY — using fallback token!", file=sys.stderr)
    BOT_TOKEN = "8664729809:AAFGVBvefewYHShcQ30NWUKIoQ29vkUQ_2E"

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "558789316")

# ===== Sepay =====
# Nếu không có key → mode manual (admin tự /confirm).
# Có key → tự động gửi link khi Sepay báo giao dịch.
SEPAY_API_KEY = os.environ.get("SEPAY_API_KEY", "") or ""

# ===== Ngân hàng =====
# MB Bank — Sepay có API trực tiếp (không cần SMS forward).
BANK_ACCOUNT = os.environ.get("BANK_ACCOUNT", "3100181888868")
BANK_NAME = os.environ.get("BANK_NAME", "MB Bank")
BANK_OWNER = os.environ.get("BANK_OWNER", "TA QUANG THUAN")

# ===== Hosting =====
BASE_URL = os.environ.get("BASE_URL", "https://your-app.railway.app")

# ===== Sản phẩm =====
# Link Drive được lưu trong DB (cập nhật qua /set_link), giá cố định ở đây
PRODUCTS = {
    "mua_combo": {
        "name": "Combo Full Pack (Claude + OpenCode, 8 cấp)",
        "price": 199000,
    },
    "mua_claude": {
        "name": "Claude AI Thực Chiến (4 cấp)",
        "price": 99000,
    },
    "mua_opencode": {
        "name": "OpenCode Thực Chiến (4 cấp)",
        "price": 149000,
    },
    "mua_copywriter": {
        "name": "Copywriter Việt Pro — Bộ 20 file: instructions + 15 knowledge base (cần Claude Pro hoặc ChatGPT Plus để dùng)",
        "price": 449000,
    },
    # Agent subscription plans (handled via agent_db.AGENT_PLANS)
}

# Startup check — log trạng thái ngay khi import (gunicorn cũng chạy)
print(f"[config] BOT_TOKEN: {'✓' if BOT_TOKEN else '✗ MISSING'} ({BOT_TOKEN[:10]}...{BOT_TOKEN[-5:] if len(BOT_TOKEN) > 15 else ''})")
print(f"[config] SEPAY_API_KEY: {'✓' if SEPAY_API_KEY else '✗ MISSING (manual mode)'}")
print(f"[config] ADMIN_CHAT_ID: {'✓' if ADMIN_CHAT_ID else '✗ MISSING'}")
print(f"[config] BANK: {BANK_NAME} - {BANK_ACCOUNT} - {BANK_OWNER}")
print(f"[config] BASE_URL: {BASE_URL}")

# AI Agent — API keys (check tồn tại, không log giá trị)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or ""
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "") or ""
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "") or ""
print(f"[config] GEMINI_API_KEY: {'✓' if GEMINI_API_KEY else '✗'} | CLAUDE_API_KEY: {'✓' if CLAUDE_API_KEY else '✗'} | OPENAI_API_KEY: {'✓' if OPENAI_API_KEY else '✗'}")

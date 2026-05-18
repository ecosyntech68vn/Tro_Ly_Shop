"""
Cấu hình từ biến môi trường (env vars).
KHÔNG hardcode token/key vào file này.
"""
import os

# ===== Telegram =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # chat_id của anh để nhận thông báo

# ===== Sepay =====
SEPAY_API_KEY = os.environ.get("SEPAY_API_KEY", "")

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
}

# Validate
if __name__ == "__main__":
    print("BOT_TOKEN:", "✓" if BOT_TOKEN else "✗ MISSING")
    print("SEPAY_API_KEY:", "✓" if SEPAY_API_KEY else "✗ MISSING")
    print("ADMIN_CHAT_ID:", "✓" if ADMIN_CHAT_ID else "✗ MISSING")
    print("BANK_ACCOUNT:", BANK_ACCOUNT)
    print("BASE_URL:", BASE_URL)

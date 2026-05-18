# BOT TELEGRAM TỰ ĐỘNG GIAO HÀNG · 2 CHẾ ĐỘ

Hệ thống Python: bot Telegram nhận đơn → xác thực chuyển khoản → tự gửi link Drive cho khách.

**Tác giả:** Tạ Quang Thuận · AI Thực Chiến · 2026

---

## CHỌN 1 TRONG 2 CHẾ ĐỘ

| Chế độ | Phù hợp khi | Setup | Đọc file |
|---|---|---|---|
| **A · MANUAL** ⭐ Bắt đầu | Mở bán ngay hôm nay, 0–50 đơn/ngày, không phụ thuộc Sepay | 30 phút | [QUICK_START_MANUAL.md](./QUICK_START_MANUAL.md) |
| **B · AUTOMATIC** | Sau khi đã có khách ổn định, muốn tự động 100% qua Sepay | 1-2 giờ | [MSB_SETUP.md](./MSB_SETUP.md) |

**Khuyến nghị:** Start với A — mở bán hôm nay. Khi ổn định 1-2 tuần, upgrade lên B chỉ bằng cách thêm 1 env var, không sửa code.

---

## TỔNG QUAN HỆ THỐNG

### Luồng hoạt động

```
[1] Khách /mua_combo trong bot
        ↓
[2] Bot tạo mã đơn TXNXXXXXX, lưu DB, gửi STK + nội dung CK
        ↓
[3] Khách chuyển VCB 199.000đ, nội dung "MUA TXNXXXXXX"
        ↓
[4] VCB → Sepay → POST /sepay-webhook
        ↓
[5] Server xác thực: số tiền + mã đơn → tìm chat_id
        ↓
[6] Bot tự gửi link Google Drive cho khách (30s sau CK)
```

### Tính năng

- **Bot Telegram**: menu inline button, lệnh `/mua_combo`, `/trang_thai`, `/lien_he`
- **Sepay webhook**: nhận biến động VCB realtime, xác thực API key
- **Match tự động**: theo nội dung `MUA TXNXXXXXX` + số tiền
- **Underpaid detection**: cảnh báo nếu khách chuyển thiếu tiền
- **Admin commands**: `/unmatched` xem GD lệch, `/set_link` cập nhật link Drive
- **SQLite**: lưu pending orders + log GD không khớp

### Chi phí vận hành

| Thành phần | Free tier | Trả phí khi nào |
|---|---|---|
| Sepay | 100 GD/tháng miễn phí | Khi >100 GD/tháng → ~50–100k/tháng |
| Railway | $5 credit/tháng đủ chạy 24/7 | Khi vượt 500h compute |
| Telegram | Miễn phí vô hạn | Không có |
| **Tổng khởi điểm** | **~0–5 USD/tháng** | |

---

## BƯỚC 1 · CHUẨN BỊ TÀI KHOẢN

### 1.1 Telegram Bot Token

1. Mở Telegram → tìm `@BotFather`
2. Vì token cũ đã lộ, **PHẢI revoke**: gõ `/mybots` → chọn bot → `API Token` → `Revoke current token`
3. Lưu token mới (dạng `1234567890:ABC...`) — KHÔNG paste vào chat hoặc file public

### 1.2 Chat ID của anh (admin)

1. Trên Telegram, chat với `@userinfobot` → nó trả về số chat ID (vd `123456789`)
2. Lưu lại — sẽ dùng làm `ADMIN_CHAT_ID`

### 1.3 Đăng ký Sepay + Mở MB Bank

**KHUYẾN NGHỊ:** Dùng **MB Bank** (KHÔNG dùng Vietcombank).

**Lý do:** Sepay có API trực tiếp với MB Bank → biến động đẩy về bot trong 1–3 giây. Vietcombank không có API → phải dùng SMS forward (cần điện thoại Android riêng, phức tạp, dễ lỗi).

**Đọc file hướng dẫn riêng:** [MB_BANK_SETUP.md](./MB_BANK_SETUP.md) — cách mở MB Bank + đăng ký Sepay + lấy API key + cấu hình webhook (30–60 phút).

Sau khi hoàn thành, anh có:
- `BANK_ACCOUNT` = STK MB Bank
- `BANK_NAME` = `MB Bank`
- `SEPAY_API_KEY` = key Sepay vừa tạo
- Webhook URL Sepay đã trỏ về `{BASE_URL}/sepay-webhook`

### 1.4 Google Drive — chuẩn bị link sản phẩm

Tạo 3 thư mục Drive, set **"Anyone with the link → Viewer"**:

```
Drive/
├── Combo Full Pack/       → link Drive 1
├── Claude AI Thực Chiến/  → link Drive 2
└── OpenCode Thực Chiến/   → link Drive 3
```

Lưu 3 URL — sẽ nhập vào bot qua lệnh `/set_link` ở Bước 4.

---

## BƯỚC 2 · DEPLOY LÊN RAILWAY (KHUYẾN NGHỊ)

Railway tự động deploy từ GitHub, hỗ trợ Python sẵn, có HTTPS, $5/tháng đủ chạy 24/7.

### 2.1 Push code lên GitHub

```bash
cd bot_sepay/
git init
git add .
git commit -m "Initial bot setup"

# Tạo repo PRIVATE trên GitHub (không public vì có code thanh toán)
git remote add origin https://github.com/YOUR_USERNAME/bot-sepay.git
git push -u origin main
```

**Cảnh báo:** chắc chắn file `.env` (nếu có) đã trong `.gitignore`. Tuyệt đối KHÔNG push token lên GitHub.

### 2.2 Tạo project Railway

1. Vào https://railway.app → đăng nhập bằng GitHub
2. **New Project → Deploy from GitHub repo** → chọn `bot-sepay`
3. Railway tự nhận diện Python từ `requirements.txt` và `Procfile`

### 2.3 Cài Environment Variables

Trong Railway project → tab **Variables** → thêm:

```
BOT_TOKEN          = 1234567890:ABC... (token mới từ BotFather)
ADMIN_CHAT_ID      = 123456789 (chat ID của anh)
SEPAY_API_KEY      = sepay_key_xxx
BANK_ACCOUNT       = 3100181888868
BANK_NAME          = Vietcombank
BASE_URL           = https://YOUR-PROJECT.up.railway.app
```

`BASE_URL` lấy sau khi Railway deploy thành công (tab **Settings → Domains → Generate Domain**).

### 2.4 Verify deploy

Mở `https://YOUR-PROJECT.up.railway.app/` trong browser → phải thấy:
```json
{"status": "ok", "service": "AI Thực Chiến Bot"}
```

---

## BƯỚC 3 · KẾT NỐI WEBHOOK

### 3.1 Đăng ký Telegram webhook

Mở browser, vào URL này (thay `{TOKEN}` và `{BASE_URL}`):

```
https://api.telegram.org/bot{TOKEN}/setWebhook?url={BASE_URL}/telegram-webhook
```

Ví dụ:
```
https://api.telegram.org/bot1234567890:ABC.../setWebhook?url=https://my-bot.up.railway.app/telegram-webhook
```

Trả về `{"ok":true,"result":true,"description":"Webhook was set"}` → thành công.

### 3.2 Đặt Sepay webhook URL

Trên Sepay dashboard:
- **Webhook URL**: `https://YOUR-PROJECT.up.railway.app/sepay-webhook`
- **Method**: POST
- **Authentication**: Apikey (Sepay tự gửi header `Authorization: Apikey {key}`)

### 3.3 Test

1. Chat với bot trên Telegram → gõ `/start` → bot phải trả lời menu
2. Gõ `/mua_combo` → bot tạo đơn, trả mã `TXNXXXXXX`
3. Chuyển khoản test 1.000đ với nội dung `MUA TXNXXXXXX` (giá tiền sai sẽ thấy cảnh báo underpaid)
4. Hoặc dùng Sepay UI để gửi **test webhook** với amount=199000, content=`MUA {mã đơn của anh}`

---

## BƯỚC 4 · CÀI ĐẶT LINK DRIVE TRONG BOT

Sau khi bot online, từ chat của ANH (admin) gõ:

```
/set_link mua_combo https://drive.google.com/drive/folders/COMBO_FOLDER_ID
/set_link mua_claude https://drive.google.com/drive/folders/CLAUDE_FOLDER_ID
/set_link mua_opencode https://drive.google.com/drive/folders/OPENCODE_FOLDER_ID
```

Bot xác nhận từng cái. Từ giờ khi khách thanh toán, bot tự gửi đúng link.

### Lệnh admin đầy đủ

| Lệnh | Mục đích |
|---|---|
| `/confirm TXNxxx` | **Confirm thủ công** — dùng khi Sepay lỗi/delay hoặc khách CK qua VCB. Bot sẽ gửi link cho khách ngay. |
| `/unmatched` | Xem 10 GD không khớp gần nhất (sai số tiền / sai mã đơn) |
| `/set_link <sku> <url>` | Cập nhật link Drive cho từng sản phẩm |
| `/sale_stats` | Báo cáo doanh số: số đơn + doanh thu theo SKU |
| `/admin_help` | Xem danh sách lệnh admin |

---

## BƯỚC 5 · GO LIVE

### Checklist trước khi mở bán

- ☐ Bot phản hồi `/start` (test với SĐT khác)
- ☐ `/mua_combo` tạo được đơn mã `TXN...`
- ☐ `/trang_thai` xem được đơn
- ☐ 3 link Drive đã set qua `/set_link`
- ☐ Sepay đã link Vietcombank thành công
- ☐ Test 1 GD thật 1.000đ → bot báo underpaid (đúng)
- ☐ `ADMIN_CHAT_ID` đúng — nhận được noti khi có sale
- ☐ Link Drive set "Anyone with link → Viewer" (không cần đăng nhập Gmail)

### Sau khi mở bán

Quan sát 1 tuần đầu:
- `/unmatched` mỗi sáng — xem có GD nào không khớp không
- Theo dõi log Railway để bắt exception
- Backup `bot.db` định kỳ (Railway Volume hoặc download manual)

---

## KIẾN TRÚC FILE

```
bot_sepay/
├── app.py              # Flask app: webhook Telegram + Sepay
├── config.py           # Đọc env vars
├── db.py               # SQLite layer
├── requirements.txt    # Python deps
├── Procfile            # Railway start command
├── .env.example        # Mẫu env (chép thành .env khi dev local)
├── .gitignore          # KHÔNG commit .env, *.db
└── README.md           # File này
```

---

## BẢO MẬT — BẮT BUỘC LÀM

1. **Token = mật khẩu.** Không bao giờ commit `.env` lên Git. Không paste token vào chat/screenshot.
2. **GitHub repo PRIVATE** chứ không Public.
3. **Sepay API Key**: nếu bị lộ → vào Sepay regenerate ngay.
4. **Telegram bot bị lộ token**: `/revoke` trong BotFather → cập nhật env var Railway → redeploy.
5. **Database**: file `bot.db` chứa chat_id khách hàng — đối xử như dữ liệu cá nhân.
6. **Link Drive**: nếu rò rỉ → đổi link (tạo folder mới, di chuyển file, cập nhật `/set_link`).
7. **HTTPS only**: Railway tự cấp, đảm bảo BASE_URL bắt đầu bằng `https://`.

---

## TROUBLESHOOTING

### Bot không phản hồi
1. Kiểm tra log Railway → có exception không
2. Test `https://api.telegram.org/bot{TOKEN}/getWebhookInfo` → phải thấy URL đúng + `last_error_message: null`
3. Nếu `last_error_message` không null → đọc lỗi → fix
4. Restart Railway service

### Sepay webhook không trigger
1. Vào Sepay dashboard → tab **Webhook History** → xem GD vừa rồi có gửi không
2. Nếu Sepay không nhận biến động từ VCB → check link VCB đang active
3. Test bằng nút "Test Webhook" trong Sepay UI

### GD khớp nhưng bot không gửi link
1. Kiểm tra log Railway: search "Order ... delivered" hoặc error
2. `/unmatched` → có thể GD bị log vào unmatched (nội dung sai format)
3. Verify link Drive đã set: chat admin với bot, gõ `/admin_help`

### Khách kêu không nhận link
1. Hỏi mã đơn của khách
2. Vào log Railway: search mã đơn
3. Trường hợp:
   - GD chưa về: đợi 1-2 phút (VCB delay)
   - GD về nhưng nội dung sai: vào unmatched → resolve thủ công
   - Bot lỗi: gửi link Drive thủ công cho khách qua Telegram

---

## NÂNG CẤP TƯƠNG LAI

| Tính năng | Mức độ | Khi nào làm |
|---|---|---|
| Voucher / mã giảm giá | Dễ | Sau khi có 50 đơn |
| Affiliate (tracking referrer) | Trung bình | Sau khi có 200 đơn |
| Bot AI tư vấn (gắn Claude API) | Trung bình | Khi muốn upsell |
| Đa ngân hàng (MB, Tech, ACB) | Dễ | Khi VCB lỗi nhiều |
| Trang admin web | Trung bình | Khi muốn xem báo cáo bằng UI |
| Export Excel báo cáo doanh thu | Dễ | Cuối tháng |

---

*AI Thực Chiến · Bot Sepay v1.0 · 18/05/2026 · Tạ Quang Thuận*

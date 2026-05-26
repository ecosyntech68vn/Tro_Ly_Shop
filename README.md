# AI Agent Thuê Bao — Trợ lý AI 24/7 cho Shop Online

Hệ thống AI Agent bán hàng tự động qua Telegram + Website Widget. Bot tự động trả lời khách, nhận đơn hàng, đặt lịch hẹn — chủ shop chỉ cần ngồi chốt đơn.

**Tác giả:** Tạ Quang Thuận · AI Thực Chiến · 2026

---

## Tính năng

| Nhóm | Tính năng | Mô tả |
|---|---|---|
| **Core** | AI trả lời tự động | Gemini / Claude / GPT-4 tuỳ gói, context product catalog + learned corrections |
| | Group chat | Bot vào group, `/claim` → trả lời khách tự động. 3 chế độ: mention / smart / auto |
| | Smart Mode | Tự động detect câu hỏi (từ khoá, viết tắt, không dấu) — không cần @ |
| | Conversation memory | 10 tin nhắn gần nhất — khách không phải nói lại |
| **Bán hàng** | Web Widget | Nhúng `<script>` vào website → khách chat với AI Agent trực tiếp |
| | Product Catalog | Upload CSV/Excel → AI tự tra cứu khi khách hỏi |
| | Đặt hàng | AI hỏi thông tin → tạo đơn → notify chủ shop → Dashboard quản lý |
| | Đặt lịch hẹn | AI hỏi dịch vụ, ngày giờ → tạo lịch → Dashboard quản lý |
| **Dạy AI** | 👎 Sửa lỗi | Bấm 👍👎 trên mỗi câu trả lời. 👎 → gõ câu đúng → AI nhớ mãi |
| | Dashboard web | Xem hội thoại, sửa lỗi, catalog, đơn hàng, lịch hẹn — login bằng web token |
| **Vận hành** | Multi-language | AI tự detect tiếng Việt / Anh → trả lời đúng ngôn ngữ |
| | Sentiment detection | Khách tức giận → chuyển chủ shop, AI không trả lời |
| | Rate limiter | 5 lần/phút cho login, giới hạn tin nhắn theo gói |
| | Backup tự động | `backup.sh` → nén + gửi Telegram admin, retention 7 ngày |
| | Export CSV | Xuất hội thoại, sửa lỗi, sản phẩm từ Dashboard |
| | Broadcast | Gửi tin hàng loạt tới tất cả khách đã chat |
| | Admin web | `/admin` — tổng quan thuê bao, đơn hàng, GD không khớp |
| **P0-P3** | Thread-safe | ThreadSafeDict + DedupSet cho rating, corrections, dedup |
| | Health check | `GET /` → DB status, AI keys, uptime, version |
| | Graceful shutdown | SIGTERM → executor cleanup |
| | Security headers | nosniff, DENY frame, XSS block |

---

## Gói thuê bao

| Gói | Giá/tháng | AI Model | Tin nhắn/ngày | Agent |
|---|---|---|---|---|
| Basic | 99.000đ | Gemini | 30 | 1 |
| Pro | 199.000đ | Claude | 100 | 3 |
| Business | 499.000đ | GPT-4 | 500 | Không giới hạn |

---

## Kiến trúc

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│  Telegram     │────▶│  Flask App       │────▶│  SQLite / PG │
│  Webhook      │     │  (app.py)        │     │  (db.py)     │
└──────────────┘     │                  │     └──────────────┘
                     │  ThreadPool      │
┌──────────────┐     │  Executor(32)    │     ┌──────────────┐
│  Website      │────▶│  + Semaphore    │────▶│  AI APIs     │
│  Widget       │     │  (128)          │     │  Gemini      │
└──────────────┘     └──────────────────┘     │  Claude      │
                                              │  GPT-4       │
                     ┌──────────────────┐     └──────────────┘
                     │  agent_db.py     │
                     │  - subscriptions │     ┌──────────────┐
                     │  - profiles      │     │  backup.sh   │
                     │  - chat history  │────▶│  → Telegram  │
                     │  - products      │     └──────────────┘
                     │  - corrections   │
                     │  - orders        │
                     │  - appointments  │
                     └──────────────────┘
```

---

## Deploy

### Railway (khuyến nghị)

1. Fork / push code lên GitHub repo PRIVATE
2. Railway → New Project → Deploy from GitHub repo
3. Set Environment Variables (xem `.env.example`)
4. Set Telegram webhook:
   ```
   https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={BASE_URL}/telegram-webhook
   ```
5. Mở `{BASE_URL}/` → health check OK

### Docker

```bash
docker build -t ai-agent .
docker run -p 8000:8000 -e BOT_TOKEN=... -e GEMINI_API_KEY=... ai-agent
```

### Local dev

```bash
pip install -r requirements.txt
python app.py
```

---

## Cấu trúc file

```
├── app.py                 # Flask app (2819 dòng)
├── agent_db.py            # Database layer (~1068 dòng)
├── config.py              # Environment variables
├── db.py                  # SQLite/PostgreSQL connection
├── backup.sh              # Auto backup script
├── Dockerfile             # Container build
├── Procfile               # Railway start command
├── requirements.txt       # Python dependencies
├── templates/
│   ├── dashboard/         # Shop owner dashboard (7 files)
│   └── admin/             # Admin dashboard (4 files)
├── static/
│   ├── widget.js          # Website chat widget
│   └── BankNotify.apk     # Android app download
├── tests/
│   ├── conftest.py        # Pytest fixtures
│   ├── test_db.py         # DB CRUD tests (17 tests)
│   └── test_api.py        # API tests (7 tests)
└── .env.example           # Environment example
```

---

## Biến môi trường

| Biến | Bắt buộc | Mô tả |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram Bot Token từ @BotFather |
| `GEMINI_API_KEY` | ✅ (tối thiểu 1) | Google AI key cho Gemini |
| `CLAUDE_API_KEY` | ✋ | Anthropic key cho Claude |
| `OPENAI_API_KEY` | ✋ | OpenAI key cho GPT-4 |
| `BASE_URL` | ✅ | URL public sau khi deploy |
| `ADMIN_CHAT_ID` | ✅ | Telegram chat ID của admin |
| `ADMIN_PASSWORD` | ✅ | Mật khẩu cho `/admin` dashboard |
| `SEPAY_API_KEY` | ✋ | Sepay key (bỏ trống = manual mode) |
| `SECRET_KEY` | ✋ | Flask session key (tự sinh nếu trống) |

---

## Testing

```bash
pytest tests/ -v
# 24 tests passed in ~2s
```

---

## API Endpoints

| Method | Path | Mô tả |
|---|---|---|
| GET | `/` | Health check (DB, AI keys, uptime) |
| POST | `/telegram-webhook` | Telegram bot incoming |
| POST | `/api/chat` | Website widget chat (cần `token`) |
| GET | `/widget.js` | Widget JavaScript |
| POST | `/sepay-webhook` | Sepay payment notification |
| GET | `/cron/backup` | Railway Cronjob trigger backup |
| GET | `/dashboard/*` | Shop owner dashboard (cần login) |
| GET | `/admin/*` | Admin dashboard (cần password) |

---

## Bảo mật

- `.env` KHÔNG commit lên Git
- GitHub repo luôn PRIVATE
- Token Telegram = mật khẩu — revoke ngay nếu lộ
- API keys AI: Gemini / Claude / OpenAI — không log
- DB chứa chat_id khách hàng — xử lý như dữ liệu cá nhân
- Dashboard session 4h, rate limit 5 lần/phút login

---

*AI Agent Thuê Bao v3.0 · 26/05/2026 · Tạ Quang Thuận*

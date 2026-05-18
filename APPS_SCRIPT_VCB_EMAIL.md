# TỰ ĐỘNG HÓA VCB QUA EMAIL FORWARD

> **Mục tiêu:** Bot tự confirm đơn khi khách CK vào VCB, **không cần `/confirm` tay**.
> **Cách hoạt động:** VCB gửi email biến động số dư → Google Apps Script đọc email → POST tới bot → bot match đơn → giao link tự động.
> **Chi phí:** 0đ. Không Sepay, không Casso.
> **Thời gian setup:** 20–30 phút.

---

## NGUYÊN LÝ HOẠT ĐỘNG

```
[1] Khách CK 199.000đ vào VCB với nội dung MUA TXNABC123
        ↓
[2] VCB gửi email biến động số dư tới Gmail anh (thuanktqd.mba@gmail.com)
        ↓
[3] Google Apps Script chạy mỗi 1 phút, đọc email mới
        ↓
[4] Script POST nội dung email tới https://web-production-6a75.up.railway.app/vcb-email
        ↓
[5] Bot parse: amount=199000, code=TXNABC123 → match đơn → mark paid
        ↓
[6] Bot tự gửi link Google Drive cho khách
```

Khách nhận link trong **~30–60 giây** sau CK. Anh không phải làm gì.

---

## BƯỚC 1 · BẬT EMAIL BIẾN ĐỘNG SỐ DƯ TRÊN VCB (5 phút)

### 1.1 Mở app VCB Digibank

1. Đăng nhập VCB Digibank
2. Vào **Cài đặt** → **Quản lý dịch vụ** → **Đăng ký dịch vụ**
3. Chọn **Báo biến động số dư qua Email** (nếu chưa có)
4. Nhập email: `thuanktqd.mba@gmail.com`
5. Xác nhận OTP

### 1.2 Test

Chuyển 1.000đ vào TK của anh từ một TK khác (hoặc tự CK từ TK phụ). Sau ~1 phút, vào Gmail kiểm tra:
- Người gửi: `VCBDigibank@info.vietcombank.com.vn` (hoặc tương tự)
- Subject: "VCB Digibank: Biến động số dư..."
- Body có thông tin GD

→ Nếu nhận được email → Bước 1 OK.

---

## BƯỚC 2 · TẠO RANDOM SECRET (1 phút)

Tạo 1 chuỗi random để bot và Apps Script xác thực với nhau (chống ai gọi /vcb-email từ ngoài):

Mở https://www.random.org/strings/?num=1&len=32&digits=on&upperalpha=on&loweralpha=on → copy chuỗi 32 ký tự ra.

Ví dụ: `K7mP2nQ8xR9vS4tU6wY1zA3bC5dE7fG9`

→ Lưu chuỗi này (anh sẽ dùng ở Bước 3 và Bước 4).

---

## BƯỚC 3 · THÊM ENV `VCB_EMAIL_SECRET` VÀO RAILWAY (2 phút)

1. Vào Railway dashboard → project `worthy-presence` → service `web` → tab **Variables**
2. Click **+ New Variable**:
   - Tên: `VCB_EMAIL_SECRET`
   - Giá trị: chuỗi 32 ký tự vừa tạo ở Bước 2
3. Save → Railway tự redeploy 30 giây

---

## BƯỚC 4 · TẠO GOOGLE APPS SCRIPT (10 phút)

### 4.1 Mở Apps Script editor

1. Mở https://script.google.com (đăng nhập bằng Gmail `thuanktqd.mba@gmail.com`)
2. Click **New project** (góc trái)
3. Đặt tên: **VCB Email Forward to Bot**

### 4.2 Paste code dưới đây vào editor

```javascript
// ============================================================
// VCB Email → Bot Webhook Forwarder
// Tác giả: Tạ Quang Thuận · AI Thực Chiến · 2026
// ============================================================

// CẤU HÌNH — SỬA 2 DÒNG NÀY
const BOT_WEBHOOK_URL = 'https://web-production-6a75.up.railway.app/vcb-email';
const AUTH_TOKEN = 'PASTE_VCB_EMAIL_SECRET_HERE';  // chuỗi 32 ký tự ở Bước 2

// Filter Gmail: chỉ đọc email VCB chưa đọc, trong 24h gần đây
const GMAIL_QUERY = 'from:(vietcombank.com.vn) is:unread newer_than:1d';

function checkVCBEmails() {
  const threads = GmailApp.search(GMAIL_QUERY, 0, 20);
  if (threads.length === 0) {
    return;
  }

  let processed = 0;
  threads.forEach(thread => {
    thread.getMessages().forEach(message => {
      if (!message.isUnread()) return;

      const subject = message.getSubject();
      const body = message.getPlainBody();

      try {
        const response = UrlFetchApp.fetch(BOT_WEBHOOK_URL, {
          method: 'post',
          contentType: 'application/json',
          headers: { 'X-Auth-Token': AUTH_TOKEN },
          payload: JSON.stringify({
            subject: subject,
            body: body,
            received_at: message.getDate().toISOString()
          }),
          muteHttpExceptions: true
        });

        const status = response.getResponseCode();
        if (status === 200) {
          message.markRead();
          processed++;
          console.log(`✓ Forwarded: ${subject.substring(0, 60)}`);
        } else {
          console.error(`✗ Bot returned ${status}: ${response.getContentText().substring(0, 200)}`);
        }
      } catch (e) {
        console.error(`✗ Forward failed: ${e.toString()}`);
      }
    });
  });

  console.log(`Processed ${processed} VCB email(s).`);
}

// Manual run để test
function testRun() {
  checkVCBEmails();
}
```

### 4.3 Cấu hình

1. Đổi dòng `const AUTH_TOKEN = 'PASTE_VCB_EMAIL_SECRET_HERE';` → paste chuỗi secret ở Bước 2 vào.
2. Verify dòng `BOT_WEBHOOK_URL` đúng URL Railway của anh (mặc định trong code đã đúng).
3. Click biểu tượng **đĩa mềm (Save)** ở góc trên.

### 4.4 Authorize permissions

1. Trong dropdown menu chọn function **testRun** (góc trên cùng giữa)
2. Click **Run** ▶
3. Google sẽ hỏi cấp quyền lần đầu:
   - **Review permissions** → chọn account `thuanktqd.mba@gmail.com`
   - **Advanced** → **Go to VCB Email Forward (unsafe)** (an toàn, vì là script của anh)
   - **Allow** cho 2 quyền: đọc Gmail + gọi URL external
4. Script chạy thử, xem log: View → Logs (hoặc Ctrl+Enter)

### 4.5 Bật trigger tự động chạy mỗi phút

1. Click biểu tượng **đồng hồ ⏰** ở sidebar trái → **Triggers**
2. **+ Add Trigger** (góc dưới phải)
3. Cấu hình:
   - Function: `checkVCBEmails`
   - Event source: **Time-driven**
   - Type: **Minutes timer**
   - Interval: **Every minute**
4. Save → Google hỏi auth lần nữa → Allow

→ Script sẽ tự chạy mỗi 1 phút, đọc email VCB mới, forward tới bot.

---

## BƯỚC 5 · TEST END-TO-END (5 phút)

### 5.1 Tạo đơn test trên Telegram

Chat với `@TroLyAIThucChien_bot`:
```
/mua_combo
```
→ Bot trả mã `TXNXXXXXX` + QR + STK.

### 5.2 CK thật 1.000đ với nội dung sai số tiền

Anh CK 1.000đ vào VCB STK `0611001582739` với nội dung `MUA TXNXXXXXX`.

### 5.3 Đợi 1–2 phút

- VCB sẽ gửi email biến động số dư cho Gmail anh
- Apps Script chạy mỗi 1 phút → đọc email → POST tới bot
- Bot nhận → match đơn → thấy thiếu tiền → gửi tin nhắn "thiếu 198.000đ"

→ Nếu bot tự gửi cảnh báo trong vòng 2 phút **không cần anh `/confirm`** → **HỆ THỐNG TỰ ĐỘNG HÓA THÀNH CÔNG**.

### 5.4 CK đủ tiền

Anh CK bù 198.000đ với cùng nội dung `MUA TXNXXXXXX`.

→ Bot tự gửi link Drive trong 1-2 phút.

---

## TROUBLESHOOTING

### Script không chạy
1. Vào https://script.google.com → project → tab **Executions** (biểu tượng đồng hồ cát)
2. Xem có execution nào fail không, click vào để xem error
3. Nếu thấy "User has not enabled trigger" → Bước 4.5 chưa làm

### Bot không nhận webhook
1. Vào Railway → Deploy Logs → search "VCB email"
2. Nếu thấy `VCB email webhook unauthorized` → AUTH_TOKEN sai, kiểm tra lại 2 chỗ (env Railway + Apps Script)
3. Nếu thấy `VCB email received: ...` nhưng `cannot parse amount` → VCB đổi format email, gửi tôi nội dung email để fix regex

### VCB không gửi email
1. Vào VCB Digibank → Cài đặt → Quản lý dịch vụ → xem dịch vụ "Báo biến động qua Email" đã bật chưa
2. Kiểm tra Spam folder trong Gmail
3. Liên hệ tổng đài VCB 1900 545413 nhờ kích hoạt lại

### Script đọc cả email cũ (spam đơn)
- Script chỉ đọc email `is:unread` trong `newer_than:1d` → chỉ email mới chưa đọc
- Sau khi forward, script đánh dấu là đã đọc → không gửi lại

---

## NÂNG CẤP TƯƠNG LAI

| Tính năng | Cách làm |
|---|---|
| Multi-bank: tự động cho cả MB Bank, Tech, BIDV | Thêm filter từng bank vào Apps Script, parse format riêng từng NH |
| Phát hiện gian lận (CK trùng nội dung) | Bot check ref trong DB, từ chối duplicate |
| Notification qua Zalo/Lark cùng lúc | Bot gọi thêm Zalo OA API / Lark webhook |
| Dashboard doanh số realtime | Build trang web `/admin/dashboard` đọc DB |

---

## SO SÁNH VỚI CASSO/SEPAY

| Tiêu chí | Sepay/Casso | Tự build (cách này) |
|---|---|---|
| Phí | 84k–100k/tháng | 0đ vĩnh viễn |
| Setup | 30 phút | 20–30 phút |
| Tùy biến | Hạn chế (UI họ) | 100% (code anh) |
| Phụ thuộc | Phụ thuộc 3rd party | Tự chủ |
| Maintain | Họ lo | Anh tự maintain |
| Mở rộng | Theo họ | Vô hạn (AI, voucher, affiliate...) |

→ Đây là cách **đột phá** của anh. Không phải vì rẻ hơn — vì **làm chủ công nghệ**.

---

*AI Thực Chiến · VCB Email-to-Webhook · 18/05/2026 · Tạ Quang Thuận*

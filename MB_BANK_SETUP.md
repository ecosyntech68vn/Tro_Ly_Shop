# HƯỚNG DẪN MỞ MB BANK + KẾT NỐI SEPAY

**Tại sao chọn MB Bank?** Sepay có API trực tiếp với MB Bank → biến động số dư được đẩy về webhook trong 1–3 giây (không cần SMS forward, không cần điện thoại Android phụ).

**Tổng thời gian:** 30–60 phút (mở TK + setup Sepay).

---

## BƯỚC 1 · MỞ TÀI KHOẢN MB BANK ONLINE

### 1.1 Tải app MB Bank

- iOS: App Store → tìm "MB Bank"
- Android: Google Play → "MB Bank"

### 1.2 Đăng ký mới

1. Mở app → **Đăng ký** → **Khách hàng cá nhân**
2. Nhập SĐT chính chủ → nhận OTP
3. **eKYC online**:
   - Chụp 2 mặt CCCD gắn chip
   - Quay video xác thực khuôn mặt (4–5 giây)
   - Đợi 3–5 phút duyệt tự động
4. Đặt **mật khẩu đăng nhập** + **mật khẩu giao dịch**
5. Nhận STK ngay (16 số, vd `0123456789012345`)

### 1.3 Chọn số tài khoản đẹp (tùy chọn)

MB Bank cho chọn STK số đẹp miễn phí cho 1 số trong list. Lấy STK 10 số cho dễ nhớ (vd `0985438373` — trùng SĐT).

### 1.4 Kích hoạt nhận tiền

- Mặc định TK MB Bank cá nhân nhận tiền không giới hạn miễn phí.
- Không cần làm gì thêm.

---

## BƯỚC 2 · ĐĂNG KÝ SEPAY

### 2.1 Tạo tài khoản Sepay

1. Vào **https://sepay.vn** → **Bắt đầu miễn phí**
2. Đăng ký bằng email `thuanktqd.mba@gmail.com`
3. Xác nhận email
4. Chọn gói **MIỄN PHÍ — 50 GD/tháng**

### 2.2 Liên kết tài khoản MB Bank

1. Sepay dashboard → **Tài khoản** → **Thêm tài khoản**
2. Chọn **MB Bank**
3. Nhập:
   - Số TK MB Bank vừa mở
   - Tên chủ TK: `TA QUANG THUAN` (in hoa, không dấu)
4. Sepay sẽ:
   - Yêu cầu **cấp quyền API** qua MB Bank app
   - Anh vào MB Bank app → thông báo → **Approve** kết nối
5. Đợi 30 giây — Sepay confirm liên kết thành công

> **Lưu ý:** Liên kết API qua MB là chính thức, không phải SMS forward. Nhanh, ổn định, không cần thiết bị phụ.

### 2.3 Lấy API Key

1. Sepay dashboard → **Cài đặt** → **API & Webhook**
2. Tab **API Key** → **Tạo mới**
3. Đặt tên: `bot-aithucchien`
4. Copy key (dạng `sk_test_xxx` hoặc `sk_live_xxx`) — lưu vào password manager
5. Đây là `SEPAY_API_KEY` trong env vars

### 2.4 Cấu hình Webhook URL

1. Vẫn tab **API & Webhook** → **Webhook**
2. **Thêm webhook**:
   - URL: `https://YOUR-RAILWAY-APP.up.railway.app/sepay-webhook`
   - Event: `Tiền vào` (giao dịch IN)
   - Authentication: tự động (Sepay sẽ thêm header `Authorization: Apikey {key}`)
3. Save → Sepay test connection — phải thấy 200 OK

---

## BƯỚC 3 · CẬP NHẬT CONFIG BOT

### 3.1 Cập nhật env vars trên Railway

Vào Railway project → **Variables**:

```
BANK_ACCOUNT  = (STK MB Bank vừa mở)
BANK_NAME     = MB Bank
SEPAY_API_KEY = (key vừa tạo)
```

Save → Railway tự redeploy trong 30 giây.

### 3.2 Cập nhật file `GioiThieu_TacGia.html`

Tìm dòng (giữ VCB hoặc đổi sang MB):

```html
<p><strong>Bước 1.</strong> Chuyển khoản Vietcombank — số tài khoản <strong>0611001582739</strong> — chủ tài khoản <strong>TA QUANG THUAN</strong>.</p>
```

Đổi thành (nếu chuyển toàn bộ sang MB):

```html
<p><strong>Bước 1.</strong> Chuyển khoản <strong>MB Bank</strong> — số tài khoản <strong>3100181888868</strong> — chủ tài khoản <strong>TA QUANG THUAN</strong>.</p>
```

Hoặc giữ cả hai cho khách chọn:

```html
<p><strong>Bước 1.</strong> Chuyển khoản tới 1 trong 2 tài khoản:</p>
<ul>
  <li>MB Bank — STK <strong>3100181888868</strong> — TA QUANG THUAN <em>(khuyến nghị — tự động duyệt nhanh)</em></li>
  <li>Vietcombank — STK <strong>0611001582739</strong> — TA QUANG THUAN <em>(thủ công, đợi tác giả xác nhận)</em></li>
</ul>
```

---

## BƯỚC 4 · TEST TOÀN HỆ THỐNG

### Test 1 — Bot hoạt động

Telegram chat với `@Tro_Ly_Thuan_AI_bot`:
```
/start          ← bot phải trả menu
/mua_combo      ← bot tạo mã TXNXXXXXX, trả STK MB
```

### Test 2 — Sepay webhook

Trên Sepay dashboard → **API & Webhook** → Webhook đã add → nút **Gửi test**.

Server log Railway phải thấy:
```
Sepay webhook: 12345 amount=10000
Order TXNXXXXXX not found (test data) → log unmatched
```

### Test 3 — End-to-end với GD thật 1.000đ

1. Bot trả mã `TXN ABC123`
2. Anh CK 1.000đ vào MB Bank với nội dung `MUA TXN ABC123` (cố tình thiếu tiền để test)
3. Sepay nhận → bot phải trả "Đã nhận 1.000đ nhưng thiếu 198.000đ"
4. CK bù 198.000đ với cùng nội dung `MUA TXN ABC123`
5. Bot phải tự gửi link Drive trong 30 giây

### Test 4 — Fallback manual

Trường hợp Sepay sập/chậm:
```
Anh chat bot (admin chat): /confirm TXN ABC123
Bot gửi link Drive cho khách + báo anh đã xử lý
```

---

## BƯỚC 5 · MONITORING SAU GO-LIVE

### Hàng ngày
- `/sale_stats` — xem doanh số
- `/unmatched` — xem GD không khớp (nếu có)

### Hàng tuần
- Kiểm tra Sepay dashboard → tab **Lịch sử webhook** → tỷ lệ success 200 OK phải ≥99%
- Backup file `bot.db` (download từ Railway Volume hoặc qua shell)

### Khi vượt 50 GD/tháng
- Sepay tự cảnh báo + bắt đầu tính phí GD vượt
- Nâng gói Startup 84k/tháng cho 180 GD nếu cần

---

## TRƯỜNG HỢP MB BANK eKYC FAIL

Một số lý do thường gặp:
- CCCD không phải gắn chip → ra phòng giao dịch MB Bank gần nhất, mở TK 15 phút
- Vùng ánh sáng yếu khi quay video → quay lại nơi sáng đều
- Tên trên CCCD không khớp tên đăng ký → liên hệ CSKH MB 1900 545426

Nếu không mở được MB Bank: thử ngân hàng khác có API Sepay:
- **OCB**: app OCB Omni, eKYC tương tự
- **MSB**: app MSB mBank
- **ACB**: app ACB ONE

---

## CHI PHÍ TỔNG (ƯỚC TÍNH 100 ĐƠN/THÁNG)

| Khoản | Phí |
|---|---|
| MB Bank — duy trì TK cá nhân | 0đ |
| MB Bank — phí chuyển/nhận tiền | 0đ |
| Sepay — gói Startup (180 GD) | 84.000đ/tháng |
| Railway — host bot | ~120.000đ/tháng (5 USD) |
| **Tổng** | **~204.000đ/tháng** |

Với 100 đơn × 150k trung bình = **15 triệu/tháng doanh thu** → chi phí vận hành 1.4% — hoàn toàn chấp nhận được.

---

*AI Thực Chiến · MB Bank + Sepay Setup Guide · 18/05/2026 · Tạ Quang Thuận*

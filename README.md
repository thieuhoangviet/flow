# Flow2API

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.119.0-green.svg)](https://fastapi.tiangolo.com/)
[![Docker](https://img.shields.io/badge/docker-supported-blue.svg)](https://www.docker.com/)

**Một dịch vụ API tương thích OpenAI đầy đủ chức năng, cung cấp giao diện thống nhất cho Flow**

</div>

## ✨ Tính năng cốt lõi

- 🎨 **Text-to-Image (Văn bản thành hình ảnh)** / **Image-to-Image (Hình ảnh thành hình ảnh)**
- 🎬 **Text-to-Video (Văn bản thành video)** / **Image-to-Video (Hình ảnh thành video)**
- 🎞️ **Video khung hình đầu và cuối (First and last frame video)**
- 🔄 **Tự động làm mới AT/ST** - Tự động làm mới khi AT hết hạn, tự động cập nhật qua trình duyệt khi ST hết hạn (chế độ personal)
- 📊 **Hiển thị số dư** - Truy vấn và hiển thị VideoFX Credits theo thời gian thực
- 🚀 **Cân bằng tải** - Luân phiên nhiều Token và kiểm soát đồng thời
- 🌐 **Hỗ trợ Proxy** - Hỗ trợ proxy HTTP/SOCKS5
- 📱 **Giao diện quản lý Web** - Quản lý cấu hình và Token trực quan
- 🎨 **Tạo hình ảnh qua hội thoại liên tục**
- 🧩 **Tương thích Body Request chính thức của Gemini** - Hỗ trợ `generateContent` / `streamGenerateContent`, `systemInstruction`, `contents.parts.text/inlineData/fileData`
- ✅ **Đã kiểm chứng xuất ảnh với định dạng Gemini chính thức** - Đã xác minh bằng Token thật: `/models/{model}:generateContent` trả về chuẩn `candidates[].content.parts[].inlineData` của bản chính thức

## 🚀 Bắt đầu nhanh

### Yêu cầu tiên quyết

- Docker và Docker Compose (Khuyên dùng)
- Hoặc Python 3.8+

- Do Flow đã thêm captcha bổ sung, bạn có thể chọn tự giải mã bằng trình duyệt hoặc dùng dịch vụ bên thứ ba:
Đăng ký [YesCaptcha](https://yescaptcha.com/i/13Xd8K) và lấy API Key, điền vào mục `YesCaptcha API Key` trên trang cấu hình hệ thống.
- YesCaptcha hỗ trợ chuyển đổi `type` trên trang quản lý: `RecaptchaV3TaskProxyless`, `RecaptchaV3TaskProxylessM1`, `RecaptchaV3TaskProxylessM1S7`, `RecaptchaV3TaskProxylessM1S9`; S7/S9 bắt buộc nộp `minScore` là 0.7/0.9.
- Mặc định `docker-compose.yml` khuyên dùng với dịch vụ giải mã bên thứ ba (yescaptcha/capmonster/ezcaptcha/capsolver).
Nếu cần chạy giải mã bằng trình duyệt có giao diện (headed) trong Docker (chế độ browser/personal), vui lòng sử dụng `docker-compose.headed.yml` bên dưới.

- Tiện ích mở rộng trình duyệt tự động cập nhật ST: [Flow2API-Token-Updater](https://github.com/TheSmallHanCat/Flow2API-Token-Updater)

### Cách 1: Triển khai bằng Docker (Khuyên dùng)

#### Chế độ tiêu chuẩn (Không dùng proxy)

```bash
# Clone dự án
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# Khởi động dịch vụ
docker-compose up -d

# Xem log
docker-compose logs -f
```

> Lưu ý: Compose đã mặc định mount `./tmp:/app/tmp`. Nếu đặt thời gian hết hạn cache là `0`, có nghĩa là "không tự động xóa khi hết hạn"; nếu muốn giữ lại file cache sau khi tạo lại container, bạn cũng cần giữ lại mount `tmp` này.

#### Chế độ WARP (Dùng proxy)

```bash
# Khởi động với proxy WARP
docker-compose -f docker-compose.warp.yml up -d

# Xem log
docker-compose -f docker-compose.warp.yml logs -f
```

#### Chế độ giải mã trình duyệt có giao diện trên Docker (browser / personal)

> Dành cho trường hợp bạn cần màn hình ảo để dùng trình duyệt có giao diện giải mã trong container.
> Chế độ này mặc định khởi chạy `Xvfb + Fluxbox` để hiển thị trực quan trong container và thiết lập `ALLOW_DOCKER_HEADED_CAPTCHA=true`.
> Chỉ mở cổng ứng dụng, không cung cấp bất kỳ cổng kết nối remote desktop nào.
> Trình duyệt tích hợp `personal` hiện mặc định chạy chế độ có giao diện (headed); nếu cần tạm thời chuyển về không giao diện (headless), có thể thêm biến môi trường `PERSONAL_BROWSER_HEADLESS=true`.

```bash
# Khởi động chế độ có giao diện (Lần đầu nên thêm --build)
docker compose -f docker-compose.headed.yml up -d --build

# Xem log
docker compose -f docker-compose.headed.yml logs -f
```

- Cổng API: `8000`
- Sau khi vào trang quản trị, hãy thiết lập Phương thức giải captcha là `browser` hoặc `personal`

### Cách 2: Triển khai cục bộ

```bash
# Clone dự án
git clone https://github.com/TheSmallHanCat/flow2api.git
cd flow2api

# Tạo môi trường ảo (virtual environment)
python -m venv venv

# Kích hoạt môi trường ảo
# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

# Cài đặt thư viện phụ thuộc
pip install -r requirements.txt

# Khởi động dịch vụ
python main.py
```

### Truy cập lần đầu

Sau khi dịch vụ khởi động, truy cập trang quản trị tại: **http://localhost:8000**, sau lần đăng nhập đầu tiên, vui lòng đổi mật khẩu ngay lập tức!

- **Tên đăng nhập**: `admin`
- **Mật khẩu**: `admin`

## 📈 Endpoint Giám sát (Monitoring)

- `GET /health`: Kiểm tra tình trạng sức khỏe công khai, trả về dịch vụ còn sống không, số Token đang hoạt động, sắp hết hạn, đã hết hạn, số bị khóa 429...
- `GET /metrics`: Endpoint chỉ số Prometheus
- `GET /api/tokens`: Endpoint quản lý, trả về tình trạng Token như `at_expires`, `at_expired`, `at_expiring_within_1h`, `ban_reason`, `consecutive_error_count`

Prometheus có thể quét trực tiếp `/metrics`. Nếu triển khai trên Kubernetes, nên cấu hình chỉ quét trong cụm, và chặn quyền truy cập bên ngoài `/metrics` ở lớp Ingress/Gateway.

### Trang kiểm thử mô hình

Truy cập **http://localhost:8000/test** để mở trang kiểm thử mô hình tích hợp, hỗ trợ:

- Duyệt tất cả các mô hình có sẵn theo danh mục (Tạo ảnh, Text/Image to Video, Đa ảnh to Video, Phóng to video, v.v.)
- Nhập prompt để kiểm thử bằng một click, hiển thị tiến trình tạo kiểu stream
- Hỗ trợ tải ảnh lên cho Image-to-Image / Image-to-Video
- Xem trước trực tiếp ảnh/video sau khi tạo thành công

## 📋 Các Mô hình Hỗ trợ

### Tạo hình ảnh

| Tên Mô hình | Mô tả | Kích thước |
|---------|--------|--------|
| `gemini-3.0-pro-image-landscape` | Ảnh/Văn bản -> Ảnh | Ngang |
| `gemini-3.0-pro-image-portrait` | Ảnh/Văn bản -> Ảnh | Dọc |
| `gemini-3.0-pro-image-square` | Ảnh/Văn bản -> Ảnh | Vuông |
| `gemini-3.0-pro-image-four-three` | Ảnh/Văn bản -> Ảnh | Ngang 4:3 |
| `gemini-3.0-pro-image-three-four` | Ảnh/Văn bản -> Ảnh | Dọc 3:4 |
| `gemini-3.0-pro-image-landscape-2k` | Ảnh/Văn bản -> Ảnh (2K) | Ngang |
| `gemini-3.0-pro-image-portrait-2k` | Ảnh/Văn bản -> Ảnh (2K) | Dọc |
| `gemini-3.0-pro-image-square-2k` | Ảnh/Văn bản -> Ảnh (2K) | Vuông |
| `gemini-3.0-pro-image-four-three-2k` | Ảnh/Văn bản -> Ảnh (2K) | Ngang 4:3 |
| `gemini-3.0-pro-image-three-four-2k` | Ảnh/Văn bản -> Ảnh (2K) | Dọc 3:4 |
| `gemini-3.0-pro-image-landscape-4k` | Ảnh/Văn bản -> Ảnh (4K) | Ngang |
| `gemini-3.0-pro-image-portrait-4k` | Ảnh/Văn bản -> Ảnh (4K) | Dọc |
| `gemini-3.0-pro-image-square-4k` | Ảnh/Văn bản -> Ảnh (4K) | Vuông |
| `gemini-3.0-pro-image-four-three-4k` | Ảnh/Văn bản -> Ảnh (4K) | Ngang 4:3 |
| `gemini-3.0-pro-image-three-four-4k` | Ảnh/Văn bản -> Ảnh (4K) | Dọc 3:4 |
| `imagen-4.0-generate-preview-landscape` | Ảnh/Văn bản -> Ảnh | Ngang |
| `imagen-4.0-generate-preview-portrait` | Ảnh/Văn bản -> Ảnh | Dọc |
| `gemini-3.1-flash-image-landscape` | Ảnh/Văn bản -> Ảnh | Ngang |
| `gemini-3.1-flash-image-portrait` | Ảnh/Văn bản -> Ảnh | Dọc |
| `gemini-3.1-flash-image-square` | Ảnh/Văn bản -> Ảnh | Vuông |
| `gemini-3.1-flash-image-four-three` | Ảnh/Văn bản -> Ảnh | Ngang 4:3 |
| `gemini-3.1-flash-image-three-four` | Ảnh/Văn bản -> Ảnh | Dọc 3:4 |
| `gemini-3.1-flash-image-landscape-2k` | Ảnh/Văn bản -> Ảnh (2K) | Ngang |
| `gemini-3.1-flash-image-portrait-2k` | Ảnh/Văn bản -> Ảnh (2K) | Dọc |
| `gemini-3.1-flash-image-square-2k` | Ảnh/Văn bản -> Ảnh (2K) | Vuông |
| `gemini-3.1-flash-image-four-three-2k` | Ảnh/Văn bản -> Ảnh (2K) | Ngang 4:3 |
| `gemini-3.1-flash-image-three-four-2k` | Ảnh/Văn bản -> Ảnh (2K) | Dọc 3:4 |
| `gemini-3.1-flash-image-landscape-4k` | Ảnh/Văn bản -> Ảnh (4K) | Ngang |
| `gemini-3.1-flash-image-portrait-4k` | Ảnh/Văn bản -> Ảnh (4K) | Dọc |
| `gemini-3.1-flash-image-square-4k` | Ảnh/Văn bản -> Ảnh (4K) | Vuông |
| `gemini-3.1-flash-image-four-three-4k` | Ảnh/Văn bản -> Ảnh (4K) | Ngang 4:3 |
| `gemini-3.1-flash-image-three-four-4k` | Ảnh/Văn bản -> Ảnh (4K) | Dọc 3:4 |

### Tạo Video

#### Văn bản thành Video (T2V - Text to Video)
⚠️ **Không hỗ trợ tải ảnh lên**

| Tên Mô hình | Mô tả | Kích thước |
|---------|---------|--------|
| `veo_3_1_t2v_fast_portrait` | Văn bản -> Video | Dọc |
| `veo_3_1_t2v_fast_landscape` | Văn bản -> Video | Ngang |
| `veo_3_1_t2v_fast_portrait_ultra` | Văn bản -> Video | Dọc |
| `veo_3_1_t2v_fast_ultra` | Văn bản -> Video | Ngang |
| `veo_3_1_t2v_fast_portrait_ultra_relaxed` | Văn bản -> Video | Dọc |
| `veo_3_1_t2v_fast_ultra_relaxed` | Văn bản -> Video | Ngang |
| `veo_3_1_t2v_portrait` | Văn bản -> Video | Dọc |
| `veo_3_1_t2v_landscape` | Văn bản -> Video | Ngang |
| `veo_3_1_t2v_landscape_4s` | Văn bản -> Video 4s | Ngang |
| `veo_3_1_t2v_portrait_4s` | Văn bản -> Video 4s | Dọc |
| `veo_3_1_t2v_landscape_6s` | Văn bản -> Video 6s | Ngang |
| `veo_3_1_t2v_portrait_6s` | Văn bản -> Video 6s | Dọc |
| `veo_3_1_t2v_fast_landscape_4s` | Văn bản -> Video Fast 4s | Ngang |
| `veo_3_1_t2v_fast_portrait_4s` | Văn bản -> Video Fast 4s | Dọc |
| `veo_3_1_t2v_fast_landscape_6s` | Văn bản -> Video Fast 6s | Ngang |
| `veo_3_1_t2v_fast_portrait_6s` | Văn bản -> Video Fast 6s | Dọc |
| `veo_3_1_t2v_lite_portrait` | Văn bản -> Video Lite | Dọc |
| `veo_3_1_t2v_lite_landscape` | Văn bản -> Video Lite | Ngang |
| `veo_3_1_t2v_lite_4s_portrait` | Văn bản -> Video Lite 4s | Dọc |
| `veo_3_1_t2v_lite_4s_landscape` | Văn bản -> Video Lite 4s | Ngang |
| `veo_3_1_t2v_lite_6s_portrait` | Văn bản -> Video Lite 6s | Dọc |
| `veo_3_1_t2v_lite_6s_landscape` | Văn bản -> Video Lite 6s | Ngang |

#### Video Nội suy từ khung hình (I2V - Image to Video)
📸 **Hỗ trợ 1-2 ảnh: 1 ảnh làm khung hình đầu, 2 ảnh làm khung đầu/cuối**

> 💡 **Tự động cấu hình**: Hệ thống sẽ tự động chọn model_key tương ứng dựa trên số lượng ảnh
> - **Chế độ 1 khung hình** (1 ảnh): Dùng khung hình đầu để tạo video
> - **Chế độ 2 khung hình** (2 ảnh): Dùng khung đầu + khung cuối tạo video nội suy (transition)
> - `veo_3_1_i2v_lite_*` chỉ hỗ trợ **1 ảnh** khung đầu
> - `veo_3_1_interpolation_lite_*` chỉ hỗ trợ **2 ảnh** khung đầu và cuối

| Tên Mô hình | Mô tả | Kích thước |
|---------|---------|--------|
| `veo_3_1_i2v_s_fast_portrait_fl` | Ảnh -> Video | Dọc |
| `veo_3_1_i2v_s_fast_fl` | Ảnh -> Video | Ngang |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl` | Ảnh -> Video | Dọc |
| `veo_3_1_i2v_s_fast_ultra_fl` | Ảnh -> Video | Ngang |
| `veo_3_1_i2v_s_fast_portrait_ultra_relaxed` | Ảnh -> Video | Dọc |
| `veo_3_1_i2v_s_fast_ultra_relaxed` | Ảnh -> Video | Ngang |
| `veo_3_1_i2v_s_portrait` | Ảnh -> Video | Dọc |
| `veo_3_1_i2v_s_landscape` | Ảnh -> Video | Ngang |
| `veo_3_1_i2v_s_landscape_4s` | Ảnh -> Video 4s | Ngang |
| `veo_3_1_i2v_s_portrait_4s` | Ảnh -> Video 4s | Dọc |
| `veo_3_1_i2v_s_landscape_6s` | Ảnh -> Video 6s | Ngang |
| `veo_3_1_i2v_s_portrait_6s` | Ảnh -> Video 6s | Dọc |
| `veo_3_1_i2v_s_fast_landscape_4s_fl` | Ảnh -> Video Fast 4s | Ngang |
| `veo_3_1_i2v_s_fast_portrait_4s_fl` | Ảnh -> Video Fast 4s | Dọc |
| `veo_3_1_i2v_s_fast_landscape_6s_fl` | Ảnh -> Video Fast 6s | Ngang |
| `veo_3_1_i2v_s_fast_portrait_6s_fl` | Ảnh -> Video Fast 6s | Dọc |
| `veo_3_1_i2v_lite_portrait` | Ảnh -> Video Lite (Chỉ khung đầu) | Dọc |
| `veo_3_1_i2v_lite_landscape` | Ảnh -> Video Lite (Chỉ khung đầu) | Ngang |
| `veo_3_1_i2v_lite_4s_portrait` | Ảnh -> Video Lite 4s (Chỉ khung đầu) | Dọc |
| `veo_3_1_i2v_lite_4s_landscape` | Ảnh -> Video Lite 4s (Chỉ khung đầu) | Ngang |
| `veo_3_1_i2v_lite_6s_portrait` | Ảnh -> Video Lite 6s (Chỉ khung đầu) | Dọc |
| `veo_3_1_i2v_lite_6s_landscape` | Ảnh -> Video Lite 6s (Chỉ khung đầu) | Ngang |
| `veo_3_1_interpolation_lite_portrait` | Ảnh -> Video Lite (Nội suy 2 ảnh) | Dọc |
| `veo_3_1_interpolation_lite_landscape` | Ảnh -> Video Lite (Nội suy 2 ảnh) | Ngang |
| `veo_3_1_interpolation_lite_4s_portrait` | Ảnh -> Video Lite 4s (Nội suy 2 ảnh) | Dọc |
| `veo_3_1_interpolation_lite_4s_landscape` | Ảnh -> Video Lite 4s (Nội suy 2 ảnh) | Ngang |
| `veo_3_1_interpolation_lite_6s_portrait` | Ảnh -> Video Lite 6s (Nội suy 2 ảnh) | Dọc |
| `veo_3_1_interpolation_lite_6s_landscape` | Ảnh -> Video Lite 6s (Nội suy 2 ảnh) | Ngang |

#### Nhiều Ảnh thành Video (R2V - Reference Images to Video)
🖼️ **Hỗ trợ đa hình ảnh tham chiếu**

> **Cập nhật 06-03-2026**
>
> - Đã đồng bộ Body Request R2V bản mới nhất của máy chủ gốc
> - `textInput` đã được đổi sang `structuredPrompt.parts`
> - Đã thêm thuộc tính cao nhất `mediaGenerationContext.batchId`
> - Đã thêm `useV2ModelConfig: true`
> - Cả bản ngang và dọc R2V cùng chia sẻ format body mới này
> - Khóa mô hình gốc `videoModelKey` cho màn hình ngang đã chuyển thành định dạng `*_landscape`
> - Theo giao thức hiện tại, `referenceImages` hỗ trợ tối đa **3 ảnh**

| Tên Mô hình | Mô tả | Kích thước |
|---------|---------|--------|
| `veo_3_1_r2v_fast_portrait` | Ảnh -> Video | Dọc |
| `veo_3_1_r2v_fast_landscape` | Ảnh -> Video | Ngang |
| `veo_3_1_r2v_fast_portrait_ultra` | Ảnh -> Video | Dọc |
| `veo_3_1_r2v_fast_landscape_ultra` | Ảnh -> Video | Ngang |
| `veo_3_1_r2v_fast_portrait_ultra_relaxed` | Ảnh -> Video | Dọc |
| `veo_3_1_r2v_fast_landscape_ultra_relaxed` | Ảnh -> Video | Ngang |

#### Mô hình Phóng to Video (Upsample)

Các mô hình này không phải gọi trực tiếp key upsampler của hệ thống gốc, mà sẽ gọi các mô hình Veo 3.1 thông thường trước để tạo video, sau đó mới nộp lệnh xin phóng to 1080P/4K.

| Tên Mô hình | Mô tả | Đầu ra |
|---------|---------|--------|
| `veo_3_1_t2v_landscape_4k` | Phóng to Văn bản -> Video | 4K |
| `veo_3_1_t2v_portrait_4k` | Phóng to Văn bản -> Video | 4K |
| `veo_3_1_t2v_landscape_1080p` | Phóng to Văn bản -> Video | 1080P |
| `veo_3_1_t2v_portrait_1080p` | Phóng to Văn bản -> Video | 1080P |
| `veo_3_1_t2v_landscape_4s_4k` | Phóng to Văn bản -> Video 4s | 4K |
| `veo_3_1_t2v_portrait_4s_4k` | Phóng to Văn bản -> Video 4s | 4K |
| `veo_3_1_t2v_landscape_4s_1080p` | Phóng to Văn bản -> Video 4s | 1080P |
| `veo_3_1_t2v_portrait_4s_1080p` | Phóng to Văn bản -> Video 4s | 1080P |
| `veo_3_1_t2v_landscape_6s_4k` | Phóng to Văn bản -> Video 6s | 4K |
| `veo_3_1_t2v_portrait_6s_4k` | Phóng to Văn bản -> Video 6s | 4K |
| `veo_3_1_t2v_landscape_6s_1080p` | Phóng to Văn bản -> Video 6s | 1080P |
| `veo_3_1_t2v_portrait_6s_1080p` | Phóng to Văn bản -> Video 6s | 1080P |
| `veo_3_1_t2v_fast_portrait_4k` | Phóng to Văn bản -> Video | 4K |
| `veo_3_1_t2v_fast_4k` | Phóng to Văn bản -> Video | 4K |
| `veo_3_1_t2v_fast_portrait_ultra_4k` | Phóng to Văn bản -> Video | 4K |
| `veo_3_1_t2v_fast_ultra_4k` | Phóng to Văn bản -> Video | 4K |
| `veo_3_1_t2v_fast_portrait_1080p` | Phóng to Văn bản -> Video | 1080P |
| `veo_3_1_t2v_fast_1080p` | Phóng to Văn bản -> Video | 1080P |
| `veo_3_1_t2v_fast_portrait_ultra_1080p` | Phóng to Văn bản -> Video | 1080P |
| `veo_3_1_t2v_fast_ultra_1080p` | Phóng to Văn bản -> Video | 1080P |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_4k` | Phóng to Ảnh -> Video | 4K |
| `veo_3_1_i2v_s_fast_ultra_fl_4k` | Phóng to Ảnh -> Video | 4K |
| `veo_3_1_i2v_s_fast_portrait_ultra_fl_1080p` | Phóng to Ảnh -> Video | 1080P |
| `veo_3_1_i2v_s_fast_ultra_fl_1080p` | Phóng to Ảnh -> Video | 1080P |
| `veo_3_1_i2v_s_landscape_4k` | Phóng to Ảnh -> Video | 4K |
| `veo_3_1_i2v_s_portrait_4k` | Phóng to Ảnh -> Video | 4K |
| `veo_3_1_i2v_s_landscape_1080p` | Phóng to Ảnh -> Video | 1080P |
| `veo_3_1_i2v_s_portrait_1080p` | Phóng to Ảnh -> Video | 1080P |
| `veo_3_1_i2v_s_landscape_4s_4k` | Phóng to Ảnh -> Video 4s | 4K |
| `veo_3_1_i2v_s_portrait_4s_4k` | Phóng to Ảnh -> Video 4s | 4K |
| `veo_3_1_i2v_s_landscape_4s_1080p` | Phóng to Ảnh -> Video 4s | 1080P |
| `veo_3_1_i2v_s_portrait_4s_1080p` | Phóng to Ảnh -> Video 4s | 1080P |
| `veo_3_1_i2v_s_landscape_6s_4k` | Phóng to Ảnh -> Video 6s | 4K |
| `veo_3_1_i2v_s_portrait_6s_4k` | Phóng to Ảnh -> Video 6s | 4K |
| `veo_3_1_i2v_s_landscape_6s_1080p` | Phóng to Ảnh -> Video 6s | 1080P |
| `veo_3_1_i2v_s_portrait_6s_1080p` | Phóng to Ảnh -> Video 6s | 1080P |
| `veo_3_1_r2v_fast_portrait_ultra_4k` | Phóng to Nhiều Ảnh -> Video | 4K |
| `veo_3_1_r2v_fast_landscape_ultra_4k` | Phóng to Nhiều Ảnh -> Video | 4K |
| `veo_3_1_r2v_fast_portrait_ultra_1080p` | Phóng to Nhiều Ảnh -> Video | 1080P |
| `veo_3_1_r2v_fast_landscape_ultra_1080p` | Phóng to Nhiều Ảnh -> Video | 1080P |

## 📡 Ví dụ sử dụng API (Yêu cầu dạng Stream)

> Ngoài các ví dụ `OpenAI-compatible` bên dưới, dịch vụ còn hỗ trợ định dạng chính thức của Gemini:
> - `POST /v1beta/models/{model}:generateContent`
> - `POST /models/{model}:generateContent`
> - `POST /v1beta/models/{model}:streamGenerateContent`
> - `POST /models/{model}:streamGenerateContent`
>
> Định dạng Gemini chính thức hỗ trợ các cách chứng thực sau:
> - `Authorization: Bearer <api_key>`
> - `x-goog-api-key: <api_key>`
> - `?key=<api_key>`
>
> Body Request chính thức của Gemini hỗ trợ các thuộc tính:
> - `systemInstruction`
> - `contents[].parts[].text`
> - `contents[].parts[].inlineData`
> - `contents[].parts[].fileData.fileUri`
> - `generationConfig.responseModalities`
> - `generationConfig.imageConfig.aspectRatio`
> - `generationConfig.imageConfig.imageSize`

### Gọi generateContent chính thức của Gemini (Văn bản thành Ảnh)

> Đã kiểm chứng thực tế thành công bằng Token thật.
> Nếu cần trả về dạng stream, có thể đổi đường dẫn thành `:streamGenerateContent?alt=sse`.

```bash
curl -X POST "http://localhost:8000/models/gemini-3.1-flash-image:generateContent" \
  -H "x-goog-api-key: han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "systemInstruction": {
      "parts": [
        {
          "text": "Return an image only."
        }
      ]
    },
    "contents": [
      {
        "role": "user",
        "parts": [
          {
            "text": "Một quả táo đỏ trên bàn gỗ, ánh sáng studio, phông nền tối giản"
          }
        ]
      }
    ],
    "generationConfig": {
      "responseModalities": ["IMAGE"],
      "imageConfig": {
        "aspectRatio": "1:1",
        "imageSize": "1K"
      }
    }
  }'
```

### Văn bản thành Ảnh (Text-to-Image)

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": "Một con mèo dễ thương đang đùa nghịch trong khu vườn"
      }
    ],
    "stream": true
  }'
```

### Ảnh thành Ảnh (Image-to-Image)

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.1-flash-image-landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Biến hình ảnh này thành tranh màu nước"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_encoded_image>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Văn bản thành Video (Text-to-Video)

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_t2v_fast_landscape",
    "messages": [
      {
        "role": "user",
        "content": "Một chú mèo con đuổi theo con bướm trên đồng cỏ"
      }
    ],
    "stream": true
  }'
```

### Nội suy Video từ Ảnh đầu và cuối

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_i2v_s_fast_fl_landscape",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Tạo hoạt ảnh chuyển cảnh mượt mà từ hình ảnh đầu tiên sang hình ảnh thứ hai"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_ảnh_đầu>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64,<base64_ảnh_cuối>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

### Nhiều Ảnh tham chiếu thành Video

> `R2V` sẽ tự động đóng gói body request phiên bản mới nhất ở phía Server, bên gọi (Client) vẫn sử dụng chuẩn định dạng OpenAI.
> Máy chủ sẽ tự động map yêu cầu R2V ngang sang model gốc có hậu tố `*_landscape`.
> Hiện tại hỗ trợ tối đa **3 ảnh tham chiếu**.

```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer han1234" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "veo_3_1_r2v_fast_portrait",
    "messages": [
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "Dựa trên nhân vật và bối cảnh từ 3 ảnh tham chiếu, tạo một video màn hình dọc với góc máy quay mượt mà tiến về phía trước"
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<base64_ảnh_tham_chiếu_1>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<base64_ảnh_tham_chiếu_2>"
            }
          },
          {
            "type": "image_url",
            "image_url": {
              "url": "data:image/jpeg;base64/<base64_ảnh_tham_chiếu_3>"
            }
          }
        ]
      }
    ],
    "stream": true
  }'
```

---

## 📄 Giấy phép

Dự án này sử dụng giấy phép MIT. Xem chi tiết tại file [LICENSE](LICENSE).

---

## 🙏 Cảm ơn

- [PearNoDec](https://github.com/PearNoDec) đã cung cấp giải pháp vượt rào bằng YesCaptcha
- [raomaiping](https://github.com/raomaiping) đã cung cấp giải pháp giải mã tự động bằng trình duyệt không giao diện (headless)
Xin cảm ơn tất cả các đóng góp và sự ủng hộ của người dùng!

---

## 📞 Liên hệ

- Báo lỗi / Thắc mắc: [GitHub Issues](https://github.com/TheSmallHanCat/flow2api/issues)

---

**⭐ Nếu dự án này có ích với bạn, hãy cho chúng tôi một Star nhé!**

## Cập nhật gần đây

- `9f1d712` Đồng bộ logic giải mã personal, bao gồm dọn dẹp bộ nhớ, tham số trình duyệt và cấu hình giải mã.
- `da2ad06` Merge PR #133.
- `abd0c00` Sửa lỗi xung đột sau khi merge PR #133.
- `55431c9` Đồng bộ origin/main với PR #133.
- `4b7a0ad` Bổ sung Endpoint giám sát Prometheus và sức khỏe của Token.

## Lịch sử Star

[![Star History Chart](https://api.star-history.com/svg?repos=TheSmallHanCat/flow2api&type=date&legend=top-left)](https://www.star-history.com/#TheSmallHanCat/flow2api&type=date&legend=top-left)

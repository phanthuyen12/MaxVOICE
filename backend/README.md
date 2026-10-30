# MaxVOICE Container Backend API (FastAPI + Docker)

Bản **Headless Backend API** được tách/clone từ lõi xử lý của **MaxVOICE** để chạy độc lập trong **Docker Container** (hoặc trực tiếp bằng Python trên Linux/macOS/Server). Không phụ thuộc vào `PyQt6` hay giao diện màn hình, sẵn sàng để các hệ thống khác (Web, Mobile App, n8n, Make, Zapier, Microservices...) gọi HTTP REST API tạo video lồng tiếng & phụ đề tự động.

---

## 1. Kiến trúc thư mục `backend/`

Thư mục `backend/` hoàn toàn độc lập — bạn có thể chạy trực tiếp tại đây hoặc copy nguyên thư mục `backend/` sang bất kỳ VPS / Server / Git repo nào khác:

```
backend/
  Dockerfile              # Đóng gói container Linux (Python 3.11 + FFmpeg + espeak-ng + Fonts Noto)
  docker-compose.yml      # Chạy 1 lệnh với volume lưu trữ bền vững (/data & HuggingFace cache)
  requirements.txt        # Thư viện headless (FastAPI, Uvicorn, Faster-Whisper, VieNeu, Edge-TTS...)
  .env.example            # Biến môi trường mẫu
  run_server.py           # Entrypoint chạy trực tiếp ngoài Docker
  app/
    server.py             # FastAPI REST API + Swagger UI (/docs)
    config.py             # Cấu hình từ biến môi trường & JSON
    db.py                 # SQLite WAL lưu trạng thái jobs, clips, segments, webhook
    paths.py              # Quản lý đường dẫn uploads / outputs
    pipeline/
      worker.py           # Headless Worker Pool (threading + queue, không cần PyQt6)
      tasks.py            # Pipeline: Cắt clip -> Whisper -> Dịch đa ngữ -> TTS -> Ghép video & Burn Sub + Webhook
    services/             # Toàn bộ core xử lý giống hệt bản Desktop
      ffmpeg_service.py
      whisper_service.py
      translation_service.py
      local_tts_service.py
      resona_service.py
      srt_service.py
      text_splitter.py
      merge_service.py
```

---

## 2. Cách khởi chạy bằng Docker Container (Khuyên dùng)

```bash
cd backend
cp .env.example .env
docker compose up -d --build
```

Sau khi container chạy:
- **Swagger UI (Tài liệu & Test API trực tiếp trên trình duyệt)**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

### Chạy trực tiếp bằng Python (ngoài Docker, để dev/test nhanh)

```bash
cd backend
pip install fastapi "uvicorn[standard]" python-multipart edge-tts soundfile
python run_server.py
```

---

## 3. Hướng dẫn gọi API từ bên ngoài (Tạo Video 1 bước)

### Bước 1: Gửi yêu cầu tạo video (`POST /api/v1/videos/generate`)

Bạn có thể **upload trực tiếp file video** (`files`) HOẶC **truyền link URL video** (`video_urls` — container sẽ tự tải về), kèm chọn ngôn ngữ đích, giọng đọc, nhạc nền, chế độ phụ đề và `webhook_url`:

#### Ví dụ 1: Upload file video trực tiếp bằng `curl`
```bash
curl -X POST "http://localhost:8000/api/v1/videos/generate" \
  -F "files=@/duong/dan/video_goc.mp4" \
  -F "title=Video Review Công Nghệ" \
  -F "target_language=vi" \
  -F "voice_id=multi:vi-VN-NamMinh" \
  -F "sub_mode=bilingual" \
  -F "burn_subtitles=true"
```

#### Ví dụ 2: Truyền URL video để Backend tự tải về & xử lý + bắn Webhook khi xong
```bash
curl -X POST "http://localhost:8000/api/v1/videos/generate" \
  -F "video_urls=https://example.com/sample-video.mp4" \
  -F "target_language=vi" \
  -F "voice_id=multi:vi-VN-HoaiMy" \
  -F "sub_mode=target" \
  -F "burn_subtitles=true" \
  -F "webhook_url=https://your-system.com/api/webhook/maxvoice"
```

**Kết quả trả về ngay lập tức (`202 Accepted`):**
```json
{
  "status": "queued",
  "job_id": "8e35c4d2-9a1b-4f88-8c22-112233445566",
  "voice_id": "multi:vi-VN-HoaiMy",
  "target_language": "vi",
  "sub_mode": "target",
  "burn_subtitles": true,
  "urls": {
    "status": "/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566",
    "download_video": "/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566/download/video",
    "download_srt": "/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566/download/srt"
  }
}
```

---

### Bước 2: Kiểm tra tiến độ (`GET /api/v1/jobs/{job_id}`) hoặc nhận Webhook

```bash
curl "http://localhost:8000/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566"
```

Response trả về chi tiết phần trăm tiến độ (`progress.progress_percent` từ `0` → `100`), trạng thái từng đoạn (`segments`), nội dung phụ đề gốc & đã dịch, và khi `finalVideoStatus == "done"` thì sẵn sàng tải video!

Nếu bạn có truyền `webhook_url`, ngay khi ghép video xong, container sẽ tự động gọi `POST` tới `webhook_url` của bạn với JSON:
```json
{
  "event": "job.finished",
  "job_id": "8e35c4d2-9a1b-4f88-8c22-112233445566",
  "type": "video",
  "stage": "ready",
  "finalVideoStatus": "done",
  "error": "",
  "targetLanguage": "vi",
  "subMode": "target",
  "download_video_path": "/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566/download/video",
  "download_srt_path": "/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566/download/srt"
}
```

---

### Bước 3: Tải Video hoàn chỉnh (.mp4) & Phụ đề (.srt)

- **Tải file MP4 đã lồng tiếng:**
  ```bash
  curl -OJ "http://localhost:8000/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566/download/video"
  ```
- **Tải file phụ đề SRT (hỗ trợ chọn chế độ `target` / `bilingual` / `original` hoặc dịch sang ngôn ngữ bất kỳ ngay lúc tải):**
  ```bash
  # Phụ đề song ngữ
  curl -OJ "http://localhost:8000/api/v1/jobs/8e35c4d2-9a1b-4f88-8c22-112233445566/download/srt?mode=bilingual"
  ```

---

## 4. Danh sách các Endpoint chính

| Method | Endpoint | Mô tả |
|---|---|---|
| `GET` | `/health` | Kiểm tra trạng thái container, FFmpeg, hàng đợi worker |
| `GET` | `/api/v1/languages` | Danh sách ngôn ngữ đích (`vi`, `en`, `zh`, `ko`, `ja`, `ru`...) & chế độ phụ đề |
| `GET` | `/api/v1/voices` | Danh sách giọng đọc (28+ giọng Neural đa ngôn ngữ, giọng VieNeu, giọng nhân bản) |
| `POST` | `/api/v1/voices/clone` | Upload mẫu âm thanh 3–8s để nhân bản giọng đọc mới |
| `POST` | `/api/v1/voices/preview` | Nghe thử bất kỳ giọng đọc nào (trả về file `.wav`) |
| `POST` | `/api/v1/videos/generate` | **API 1 bước**: Gửi video (file hoặc URL) -> tự động chạy từ A-Z ra video lồng tiếng |
| `POST` | `/api/v1/audio/transcribe` | **Voice to Text 1 bước**: Gửi file audio/video -> nhận transcript + file `.srt` |
| `GET` | `/api/v1/jobs` | Danh sách toàn bộ jobs kèm phần trăm tiến độ |
| `GET` | `/api/v1/jobs/{job_id}` | Chi tiết tiến độ, danh sách clips, segments, phụ đề của 1 job |
| `POST` | `/api/v1/jobs/{job_id}/retranslate` | Dịch lại phụ đề của job sang ngôn ngữ / chế độ mới mà không cần chạy lại Whisper |
| `PUT` | `/api/v1/clips/{clip_id}/segments/{idx}` | Chỉnh sửa lời thoại (`edited_text`) của 1 đoạn cụ thể |
| `POST` | `/api/v1/clips/{clip_id}/segments/{idx}/tts` | Tạo lại giọng nói cho 1 đoạn cụ thể |
| `POST` | `/api/v1/jobs/{job_id}/merge` | Ghép lại video cuối cùng |
| `GET` | `/api/v1/jobs/{job_id}/download/video` | Tải file video MP4 hoàn chỉnh |
| `GET` | `/api/v1/jobs/{job_id}/download/srt` | Tải file phụ đề `.srt` |


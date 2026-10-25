# Voice To Text Studio — Desktop (PyQt6)

Bản desktop của app web gốc (`backend/` + `frontend/`), viết lại hoàn toàn bằng Python +
PyQt6: không còn Node.js, Redis, BullMQ hay server HTTP — mọi thứ chạy trong 1 process,
pipeline nền chạy trên 1 thread riêng (thay cho hàng đợi Redis/BullMQ).

Cùng pipeline với bản web, nhưng TTS mặc định dùng **VieNeu** (chạy local, miễn phí,
không cần API key) thay cho Resona — Resona vẫn chọn được trong Cài đặt cho ai đã có key:

```
chọn video → cắt đoạn ~60s → mỗi đoạn: trích audio → faster-whisper nhận diện
→ (nếu không phải tiếng Việt) dịch bằng Argos Translate chạy local → sửa transcript
→ chọn giọng (VieNeu local, hoặc Resona cloud) → tạo giọng nói theo từng câu phụ đề,
  căn đúng timeline → ghép giọng mới vào video, giữ nhạc nền gốc ở volume thấp
→ video hoàn chỉnh
```

Có thêm tab "Voice to Text" độc lập: chọn 1 file audio/video ngắn → ra transcript + SRT
ngay, không cắt đoạn, không cần TTS.

## Cấu trúc

```
desktop_pyqt6/
  run.py                  # entry point: python run.py
  build.spec              # PyInstaller spec để đóng gói thành 1 app/exe
  app/
    config.py             # cài đặt (API key, model, đường dẫn...), lưu ở ~/.voicetext_studio/config.json
    db.py                 # SQLite jobs/segments (thay better-sqlite3 của backend cũ)
    paths.py
    services/             # port 1:1 từ backend/src/services/*.js sang Python
      ffmpeg_service.py   # ffprobe/ffmpeg qua subprocess
      whisper_service.py  # faster-whisper gọi trực tiếp trong process (không subprocess nữa)
      translation_service.py  # argostranslate gọi trực tiếp trong process
      resona_service.py   # client HTTP cho Resona API (dùng `requests`)
      local_tts_service.py # VieNeu TTS local (free, offline), engine mặc định
      srt_service.py
      text_splitter.py
      merge_service.py
    pipeline/
      worker.py           # 1 QThread chạy hàng đợi task tuần tự (thay Redis + BullMQ)
      tasks.py             # port 1:1 từ backend/src/workers/*.js
    ui/
      main_window.py       # danh sách job + khung chi tiết
      video_job_widget.py  # job video: đoạn, transcript, chọn giọng, nhạc nền, ghép video
      audio_job_widget.py  # job "Voice to Text": transcript + SRT
      voice_picker_dialog.py
      settings_dialog.py
```

## Cài đặt (chạy từ source)

Yêu cầu hệ thống — giống bản web, cộng thêm `espeak` cho VieNeu:

- Python 3.10+ (đã test với 3.13)
- `ffmpeg` + `ffprobe` trên PATH (hoặc khai trong Cài đặt): `brew install ffmpeg`
- `espeak` (VieNeu dùng để phonemize văn bản):
  - macOS: `brew install espeak`
  - Ubuntu/Debian: `sudo apt install espeak-ng`
  - Windows: tải `.msi` từ [GitHub releases của espeak-ng](https://github.com/espeak-ng/espeak-ng/releases)
- `cmake` (chỉ cần lúc `pip install`, để build `sentencepiece` — dependency của
  `argostranslate` — vì chưa có wheel dựng sẵn cho Python 3.13):
  `brew install cmake` / `sudo apt install cmake` / `choco install cmake`
- Không cần Node.js, Redis, MongoDB, SQLite server — tất cả local, trong 1 process

```bash
cd desktop_pyqt6
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# vieneu cần 1 extra-index-url khác nhau theo OS (xem docs.vieneu.io/docs/getting-started/installation):
pip install -r requirements.txt \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/metal/   # macOS (Metal GPU)
# Windows (CPU):  --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/
# Linux:          không cần thêm --extra-index-url

python run.py
```

Lần đầu chạy sẽ tự tải model VieNeu (vài trăm MB, cần Internet 1 lần, sau đó chạy offline).
Vào **Cài đặt** để:
- Đổi engine TTS sang **Resona** nếu muốn (cần `RESONA_API_KEY`, tạo ở resona.live mục
  Cài đặt tài khoản, bản v2) — không có key/không đổi engine thì VieNeu (free, local) vẫn
  dùng được transcript/SRT và TTS bình thường.
- Chọn model Whisper (`small` là mặc định, nhẹ, đủ dùng CPU).
- Chọn ngôn ngữ nguồn (Anh, Hàn, Nhật, Trung, Pháp...) trong combo rồi nhấn **"Cài model
  dịch → Việt"** một lần cho mỗi ngôn ngữ cần (cần Internet); sau đó dịch phụ đề chạy hoàn
  toàn offline. Ngôn ngữ khác tiếng Anh sẽ tự dịch bắc cầu qua tiếng Anh (X → Anh → Việt) vì
  Argos Translate chỉ có cặp dịch trực tiếp cho tiếng Anh.

Dữ liệu (SQLite + file upload/output + giọng đã nhân bản) lưu ở `~/.voicetext_studio/` theo
mặc định, đổi được trong Cài đặt.

### Lưu ý kỹ thuật VieNeu (đã xử lý sẵn trong code)

`local_tts_service.py` tự đặt `HF_HUB_DISABLE_SYMLINKS=1` trước khi import `vieneu` — một số
bản `onnxruntime` mới chặn symlink chéo thư mục mà `huggingface_hub` dùng để cache model
(`External data path escapes model directory`), biến này buộc tải file thật thay vì symlink
nên không bị lỗi đó. Không cần tự set tay.

## Đóng gói app macOS để cài trên máy khách hàng

```bash
cd desktop_pyqt6
source .venv/bin/activate
pip install pyinstaller
brew install ffmpeg espeak-ng
pyinstaller build.spec
```

Kết quả: `dist/VoiceToTextStudio.app`. Nén app để chuyển máy:

```bash
ditto -c -k --sequesterRsrc --keepParent dist/VoiceToTextStudio.app VoiceToTextStudio-macOS.zip
```

Build phải chạy trên macOS và đúng kiến trúc máy khách: Apple Silicon (arm64) hoặc Intel (x86_64). Một bản build không tự chạy native trên cả hai kiến trúc. Khách hàng giải nén ZIP, kéo app vào Applications rồi mở. Vì app chưa được ký/notarize bằng Apple Developer ID, macOS có thể chặn lần mở đầu: Control-click app → Open → Open. Để cài đặt trơn tru cho khách hàng đại trà, cần ký Developer ID và notarize app.

### Đóng gói cho Windows

Phải build trực tiếp trên Windows (PyInstaller không tạo bản Windows từ macOS). Cài Python 3.11/3.12 và FFmpeg trên máy Windows build; sau đó trong PowerShell:

```powershell
cd desktop_pyqt6
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/
python -m pip install pyinstaller
pyinstaller build.spec
```

Nén toàn bộ `dist\VoiceToTextStudio` thành ZIP. Khách hàng giải nén đầy đủ thư mục rồi chạy `VoiceToTextStudio.exe` bên trong; không gửi riêng EXE vì đây là bản onedir. Build cần có `ffmpeg.exe` và `ffprobe.exe` trên PATH để nhúng chúng vào gói. Máy khách cũng cần cài espeak-ng cho VieNeu TTS. Model giọng/dịch tải khi sử dụng lần đầu.

Lưu ý khi đóng gói:

- `ffmpeg`/`ffprobe` được nhúng vào app khi build (lấy từ Homebrew trên máy build).
- `faster-whisper` và `vieneu` tự tải model từ Hugging Face vào cache lúc **chạy lần
  đầu** — máy người dùng cuối cần Internet ít nhất 1 lần, hoặc bạn tự đóng gói sẵn
  `~/.cache/huggingface` vào `datas` trong `build.spec`.
- `espeak-ng` (dependency hệ thống của VieNeu) chưa được nhúng: máy khách cần cài riêng bằng
  `brew install espeak-ng`, hoặc chọn Resona trong Cài đặt để dùng TTS cloud.
- Argos Translate cũng cần cài model dịch (nút trong Cài đặt) 1 lần có Internet; sau đó
  chạy offline.
- `build.spec` đã tự gọi `collect_all()` cho `faster_whisper`, `ctranslate2`, `vieneu`,
  `argostranslate`, `stanza`, `sentencepiece`, `tokenizers` để PyInstaller không bỏ sót file
  dữ liệu/native extension của các gói này.

## Khác biệt so với bản web (và lý do)

- **TTS mặc định là VieNeu (local, free), không phải Resona**: desktop app nhắm tới chạy
  offline, không cần ai cũng phải tự xin API key trả phí. Resona vẫn chọn được trong Cài
  đặt (`tts_backend`), code TTS tách thành `local_tts_service.py` (VieNeu) và
  `resona_service.py` (Resona) sau cùng một điểm rẽ trong `pipeline/tasks.py`.
- **Không còn Redis/BullMQ**: vì desktop app chỉ có 1 người dùng, không cần hàng đợi job
  chạy trên server riêng. Thay vào đó, `pipeline/worker.py` là 1 `QThread` chạy 1 hàng đợi
  task tuần tự trong process — cắt video, nhận diện giọng nói, tạo giọng TTS, ghép video
  chạy lần lượt, không chạy song song với nhau. Đơn giản hơn, dễ đóng gói thành 1 file duy
  nhất, đổi lại là chậm hơn bản web nếu cần xử lý nhiều job cùng lúc.
- **Không còn server HTTP/Express**: UI gọi trực tiếp các service Python (không qua
  `fetch()` + REST API) — nhanh hơn, không cần CORS/cổng mạng.
- **faster-whisper và Argos Translate chạy trực tiếp trong process** (trước đây backend
  Node gọi `python scripts/transcribe.py` / `scripts/translate.py` qua subprocess cho mỗi
  đoạn) — vì toàn bộ app giờ đã là Python, không cần tốn thời gian khởi động subprocess +
  load lại model mỗi lần; model được cache trong RAM sau lần transcribe đầu tiên.
- **SQLite qua `sqlite3` chuẩn của Python** thay cho `better-sqlite3` (Node) — cùng schema
  `jobs`/`segments`, không tương thích file `.sqlite3` cũ của backend Node (tạo mới từ đầu).

## Giới hạn hiện tại

- Pipeline chạy tuần tự trên 1 thread nền — một job dài (transcribe/TTS) sẽ làm job khác
  phải đợi, không chạy song song như bản web (BullMQ concurrency 2).
- Chưa có nút "hủy job đang xử lý giữa đường" — task đang chạy trong worker thread sẽ chạy
  hết rồi mới tới task tiếp theo trong hàng đợi.
- Đổi "Thư mục lưu dữ liệu" trong Cài đặt cần khởi động lại app để áp dụng (DB và model đã
  load không tự chuyển).

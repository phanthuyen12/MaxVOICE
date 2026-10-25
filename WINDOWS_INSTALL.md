# Hướng dẫn đóng gói và cài Voice To Text Studio trên Windows


## 1. Máy dùng để build

### Yêu cầu

- Windows 10 hoặc Windows 11 64-bit.
- Python 3.11 64-bit (khuyến nghị) và Python Launcher (`py`).
- Kết nối Internet trong lúc cài thư viện/tải model.
- FFmpeg có cả `ffmpeg.exe` và `ffprobe.exe` trên `PATH`.
- Đủ dung lượng trống cho Python packages, gói ứng dụng và model AI. Gói có thể khá lớn vì gồm PyTorch/Whisper và các thư viện native.


Tải Python 3.11 bản Windows 64-bit từ [python.org](https://www.python.org/downloads/windows/). Trong installer, chọn **Add python.exe to PATH**. Mở PowerShell mới và kiểm tra:

```powershell
py -3.11 --version
```

Nếu lệnh không chạy, cài lại Python Launcher hoặc kiểm tra Python đã được thêm vào PATH.

### Cài FFmpeg

Cài FFmpeg cho Windows và thêm thư mục `bin` chứa `ffmpeg.exe`/`ffprobe.exe` vào biến môi trường `PATH`. Mở PowerShell mới và kiểm tra:

```powershell
where.exe ffmpeg
where.exe ffprobe
ffmpeg -version
ffprobe -version
```

Hai lệnh `where.exe` phải tìm ra binary. `build.spec` lấy hai binary này từ PATH rồi đưa vào gói ứng dụng.

## 2. Chuẩn bị mã nguồn

Đưa thư mục project `desktop_pyqt6` lên máy Windows (ví dụ giải nén source ZIP hoặc clone repository). Mở PowerShell tại thư mục đó. Ví dụ:

```powershell
cd C:\Users\YourName\Desktop\TOOLVOICETOTECH\desktop_pyqt6
```

Xác nhận thư mục hiện tại có các file `run.py`, `build.spec`, `requirements.txt` và thư mục `app`:

```powershell
Get-ChildItem
```

Không sao chép virtual environment `.venv` từ Mac/Linux sang Windows. Tạo virtual environment mới trên Windows.

## 3. Cài dependencies và build

Chạy các lệnh sau trong PowerShell từ `desktop_pyqt6`:

```powershell
py -3.11 -m venv .venv
& .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/
python -m pip install pyinstaller
python -m PyInstaller --clean build.spec
```

Nếu PowerShell chặn activate script, có thể bỏ qua activate và gọi pip/python bằng đường dẫn đầy đủ:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements.txt --extra-index-url https://pnnbao97.github.io/llama-cpp-python-v0.3.16/cpu/
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --clean build.spec
```

Nếu cài `sentencepiece` thất bại do thiếu công cụ build, cài Visual Studio Build Tools với workload **Desktop development with C++**, rồi chạy lại lệnh cài requirements. Hãy xử lý lỗi cài đặt thành công trước khi đóng gói.

Kết quả dự kiến:

```text
desktop_pyqt6\dist\VoiceToTextStudio\VoiceToTextStudio.exe
```

Đây là kiểu đóng gói **onedir**: EXE cần các DLL, thư viện và file đi kèm trong cùng thư mục. Không di chuyển riêng EXE ra ngoài thư mục này.

## 4. Kiểm tra gói trước khi gửi

Trên máy build, chạy thử:

```powershell
.\dist\VoiceToTextStudio\VoiceToTextStudio.exe
```

Kiểm tra cửa sổ app mở được. Sau đó thử các chức năng cần bàn giao, đặc biệt là đọc file audio/video. Lần chạy đầu có thể mất thời gian tải model; cần Internet.

Đóng app, rồi tạo ZIP chứa **toàn bộ** thư mục `VoiceToTextStudio`:

```powershell
Compress-Archive -Path .\dist\VoiceToTextStudio -DestinationPath .\VoiceToTextStudio-Windows-x64.zip -Force
```

Gửi `VoiceToTextStudio-Windows-x64.zip` cho khách hàng. Đừng chỉ gửi `VoiceToTextStudio.exe`.

## 5. Cài trên máy khách hàng

1. Tải file `VoiceToTextStudio-Windows-x64.zip`.
2. Chuột phải file ZIP → **Extract All…** (Giải nén tất cả). Không chạy app trực tiếp từ bên trong ZIP.
3. Di chuyển thư mục `VoiceToTextStudio` đã giải nén đến nơi phù hợp, ví dụ `C:\Apps\VoiceToTextStudio` hoặc Desktop.
4. Mở thư mục đó và chạy `VoiceToTextStudio.exe`.
5. Nếu Windows SmartScreen hiện cảnh báo ứng dụng không xác định nhà phát hành, chỉ tiếp tục nếu khách nhận đúng file từ nguồn tin cậy: **More info** → **Run anyway**. Gói chưa được ký bằng chứng thư nhà phát hành Windows.

Không cần cài Node.js, Redis hay Python trên máy khách. Gói đã có runtime Python và FFmpeg/ffprobe. Phần mềm diệt virus/SmartScreen có thể kiểm tra gói lần đầu.

## 6. VieNeu, model và kết nối Internet

- TTS mặc định là VieNeu. VieNeu cần `espeak-ng` trên máy Windows khách hàng. Cài espeak-ng và đảm bảo executable của nó có trên PATH, rồi khởi động lại app. Nếu chưa cài, các chức năng khác có thể chạy nhưng TTS VieNeu có thể báo lỗi.
- App tải model VieNeu từ Hugging Face khi sử dụng lần đầu; máy cần Internet và có thể cần vài trăm MB dung lượng.
- Model dịch Argos không được cài sẵn. Vào **Cài đặt**, chọn ngôn ngữ rồi cài model dịch; cần Internet trong lần cài đầu. Sau đó có thể dịch offline.
- Whisper cũng tải model theo lựa chọn trong Cài đặt ở lần chạy đầu; dung lượng tùy model.
- Nếu không muốn dùng VieNeu, có thể cấu hình Resona trong app; tính năng này cần Internet và Resona API key hợp lệ.

## 7. Dữ liệu và cấu hình

Mặc định, cấu hình, database, file nguồn/kết quả và các giọng đã lưu nằm trong:

```text
%USERPROFILE%\.voicetext_studio\
```

Thư mục này không nằm trong thư mục app. Có thể thay app bằng cách giải nén bản mới, còn dữ liệu người dùng được giữ riêng. Không xóa thư mục dữ liệu nếu khách cần giữ lịch sử hoặc file đã xử lý. Nên sao lưu thư mục này trước khi chuyển máy hoặc gỡ dữ liệu.

## 8. Xử lý lỗi thường gặp

### Build báo không tìm thấy ffmpeg/ffprobe

Trên máy build chạy `where.exe ffmpeg` và `where.exe ffprobe`. Cài FFmpeg, thêm thư mục `bin` vào PATH, mở PowerShell mới rồi build lại.

### App mở lên rồi tắt hoặc báo thiếu DLL

- Giải nén lại ZIP đầy đủ; không chạy EXE riêng lẻ.
- Thử chạy EXE trong PowerShell để xem thông báo lỗi.
- Đảm bảo build bằng Python 64-bit và gửi đúng toàn bộ thư mục `dist\VoiceToTextStudio`.
- Một số máy Windows thiếu Microsoft Visual C++ Runtime; cài Microsoft Visual C++ Redistributable 2015–2022 (x64) từ trang Microsoft rồi thử lại.

### Bị Windows chặn

Kiểm tra tên file và nguồn gửi. Vì chưa ký số, SmartScreen có thể cảnh báo. Chỉ cho phép chạy nếu xác minh được gói đúng là bản mình gửi.

### VieNeu không tạo được giọng

Cài espeak-ng, đảm bảo chương trình có trên PATH, khởi động lại app và thử lại. Kiểm tra Internet nếu đây là lần đầu tải model.

### Không có tiếng hoặc không đọc được một định dạng media

Kiểm tra file đầu vào có hợp lệ và thử định dạng phổ biến như MP4/H.264 hoặc WAV/MP3. Nếu vẫn lỗi, mở phần Cài đặt để kiểm tra đường dẫn FFmpeg/ffprobe; bản build nhúng binary mà máy build tìm thấy tại thời điểm đóng gói.

## 9. Cập nhật ứng dụng

Build bản mới trên Windows theo quy trình ở trên, gửi ZIP mới và yêu cầu khách giải nén vào thư mục mới rồi chạy EXE mới. Dữ liệu ở `%USERPROFILE%\.voicetext_studio\` được giữ độc lập với app. Không xóa thư mục dữ liệu khi cập nhật.

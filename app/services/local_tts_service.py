import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request

import numpy as np

from .cpu_lock import heavy_inference_lock

# Must be set before the `vieneu` package (and the huggingface_hub it uses to
# download model weights) is imported: newer onnxruntime releases reject the
# cross-directory symlinks huggingface_hub's cache normally creates between
# `blobs/` and `snapshots/` ("External data path escapes model directory").
# Forcing real file copies instead of symlinks avoids that entirely.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

_tts = None
_config = None
_model_lock = threading.Lock()

# Built-in English & Multilingual voices (work alongside VieNeu local Vietnamese voices)
MULTILINGUAL_VOICES = [
    {
        "id": "multi:en-US-Ava",
        "name": "Ava (English US — Nữ tự nhiên)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nữ • Tự nhiên, rõ ràng (Review / Thuyết minh)",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-AvaMultilingualNeural",
        "macos_voice": "Samantha",
        "gender": "female",
        "pitch": 1.0,
        "demo": "Hi, I am Ava. This is my natural American English voice for your videos.",
    },
    {
        "id": "multi:en-US-Andrew",
        "name": "Andrew (English US — Nam trầm ấm)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nam • Trầm ấm, tin tức / Kể chuyện",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-AndrewMultilingualNeural",
        "macos_voice": "Alex",
        "gender": "male",
        "pitch": 0.86,
        "demo": "Hello, I am Andrew. This is my deep, warm American male voice for narration and storytelling.",
    },
    {
        "id": "multi:en-US-Jenny",
        "name": "Jenny (English US — Nữ trẻ trung)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nữ • Trẻ trung, năng động (Vlog / TikTok / Shorts)",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-JennyNeural",
        "macos_voice": "Victoria",
        "gender": "female",
        "pitch": 1.06,
        "demo": "Hey there! I'm Jenny, an energetic and friendly voice for your shorts and vlogs.",
    },
    {
        "id": "multi:en-US-Guy",
        "name": "Guy (English US — Nam chuẩn)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nam • Thuyết trình / Đào tạo",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-GuyNeural",
        "macos_voice": "Fred",
        "gender": "male",
        "pitch": 0.91,
        "demo": "Hello, I'm Guy. I provide a clear and professional voice for tutorials and presentations.",
    },
    {
        "id": "multi:en-US-Christopher",
        "name": "Christopher (English US — Nam điện ảnh)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nam • Giọng trầm điện ảnh, документальный / Review phim",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-ChristopherNeural",
        "macos_voice": "Alex",
        "gender": "male",
        "pitch": 0.84,
        "demo": "Welcome. I am Christopher, a cinematic male voice ideal for movie recaps and documentaries.",
    },
    {
        "id": "multi:en-US-Aria",
        "name": "Aria (English US — Nữ truyền cảm)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nữ • Truyền cảm, sắc nét (Quảng cáo / Podcast)",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-AriaNeural",
        "macos_voice": "Samantha",
        "gender": "female",
        "pitch": 1.02,
        "demo": "Hi, I'm Aria. My expressive voice is great for podcasts, commercials, and social media.",
    },
    {
        "id": "multi:en-US-Brian",
        "name": "Brian (English US — Nam trẻ trung / TikTok / Shorts)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nam • Hiện đại, năng động (Shorts / Review / Crypto)",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-BrianMultilingualNeural",
        "macos_voice": "Alex",
        "gender": "male",
        "pitch": 0.94,
        "demo": "Hey everyone, I'm Brian! A modern and energetic American voice for your shorts and reviews.",
    },
    {
        "id": "multi:en-US-Emma",
        "name": "Emma (English US — Nữ dịu dàng / Kể chuyện)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nữ • Ấm áp, cuốn hút (Audiobook / Storytelling)",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-EmmaMultilingualNeural",
        "macos_voice": "Samantha",
        "gender": "female",
        "pitch": 1.01,
        "demo": "Hello, I'm Emma. My warm and expressive voice is great for storytelling and audiobooks.",
    },
    {
        "id": "multi:en-US-Steffan",
        "name": "Steffan (English US — Nam bản tin / Phân tích)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nam • Chuyên gia, sắc bén (Tài chính / Công nghệ / Tin tức)",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-SteffanNeural",
        "macos_voice": "Fred",
        "gender": "male",
        "pitch": 0.89,
        "demo": "Good day, I am Steffan. A sharp, authoritative voice for news, finance, and tech analysis.",
    },
    {
        "id": "multi:en-US-Eric",
        "name": "Eric (English US — Nam trầm ấm / Documentary)",
        "subtitle": "🇺🇸 Tiếng Anh Mỹ • Nam • Điềm đạm, sâu lắng (Phim tài liệu / Lịch sử)",
        "lang": "en",
        "tl": "en",
        "edge_voice": "en-US-EricNeural",
        "macos_voice": "Alex",
        "gender": "male",
        "pitch": 0.87,
        "demo": "Hello, I am Eric. My calm and steady voice is tailored for documentaries and educational content.",
    },
    {
        "id": "multi:en-GB-Sonia",
        "name": "Sonia (English UK — Nữ Anh Quốc)",
        "subtitle": "🇬🇧 Tiếng Anh Anh (British) • Nữ • Sang trọng, chuẩn BBC",
        "lang": "en",
        "tl": "en-co.uk",
        "edge_voice": "en-GB-SoniaNeural",
        "macos_voice": "Kate",
        "gender": "female",
        "pitch": 1.0,
        "demo": "Hello, I am Sonia, speaking in a clear British English accent.",
    },
    {
        "id": "multi:en-GB-Ryan",
        "name": "Ryan (English UK — Nam Anh Quốc)",
        "subtitle": "🇬🇧 Tiếng Anh Anh (British) • Nam • Tài liệu / Kể chuyện",
        "lang": "en",
        "tl": "en-co.uk",
        "edge_voice": "en-GB-RyanNeural",
        "macos_voice": "Daniel",
        "gender": "male",
        "pitch": 0.89,
        "demo": "Good day, I am Ryan. This is a British male voice for documentaries and narration.",
    },
    {
        "id": "multi:vi-VN-HoaiMy",
        "name": "Hoài My (Tiếng Việt Neural — Nữ chuẩn phát thanh)",
        "subtitle": "🇻🇳 Tiếng Việt • Nữ • Giọng đọc mượt, chuẩn phát thanh",
        "lang": "vi",
        "tl": "vi",
        "edge_voice": "vi-VN-HoaiMyNeural",
        "macos_voice": "Linh",
        "gender": "female",
        "pitch": 1.0,
        "demo": "Xin chào, tôi là Hoài My. Đây là giọng đọc nữ tiếng Việt tự nhiên và truyền cảm.",
    },
    {
        "id": "multi:vi-VN-NamMinh",
        "name": "Nam Minh (Tiếng Việt Neural — Nam thuyết minh)",
        "subtitle": "🇻🇳 Tiếng Việt • Nam • Giọng đọc trầm ấm, thuyết minh phim",
        "lang": "vi",
        "tl": "vi",
        "edge_voice": "vi-VN-NamMinhNeural",
        "macos_voice": "Linh",
        "gender": "male",
        "pitch": 0.88,
        "demo": "Xin chào các bạn, tôi là Nam Minh. Đây là giọng đọc nam tiếng Việt mạnh mẽ, rõ ràng cho thuyết minh video.",
    },
    {
        "id": "multi:vi-VN-ThaoMy",
        "name": "Thảo My (Tiếng Việt Neural — Nữ trẻ trung / TikTok / Review)",
        "subtitle": "🇻🇳 Tiếng Việt • Nữ • Tươi sáng, năng động (Shorts / Review / Vlog)",
        "lang": "vi",
        "tl": "vi",
        "edge_voice": "vi-VN-HoaiMyNeural",
        "edge_pitch": "+5Hz",
        "macos_voice": "Linh",
        "gender": "female",
        "pitch": 1.05,
        "demo": "Xin chào mọi người! Mình là Thảo My, giọng đọc trẻ trung cho các video ngắn và review.",
    },
    {
        "id": "multi:vi-VN-MinhQuang",
        "name": "Minh Quang (Tiếng Việt Neural — Nam trầm điện ảnh)",
        "subtitle": "🇻🇳 Tiếng Việt • Nam • Giọng trầm dày, Tóm tắt phim / Tài liệu",
        "lang": "vi",
        "tl": "vi",
        "edge_voice": "vi-VN-NamMinhNeural",
        "edge_pitch": "-6Hz",
        "macos_voice": "Linh",
        "gender": "male",
        "pitch": 0.83,
        "demo": "Chào mừng các bạn. Tôi là Minh Quang, giọng nam trầm dành cho review phim và tài liệu.",
    },
    {
        "id": "multi:zh-CN-Xiaoxiao",
        "name": "Xiaoxiao (Tiếng Trung — Nữ tự nhiên)",
        "subtitle": "🇨🇳 Tiếng Trung Phổ thông (zh-CN) • Nữ • Tự nhiên, truyền cảm",
        "lang": "zh",
        "tl": "zh-CN",
        "edge_voice": "zh-CN-XiaoxiaoNeural",
        "macos_voice": "Tingting",
        "gender": "female",
        "pitch": 1.0,
        "demo": "你好，我是晓晓，这是用于视频配音的自然中文女声。",
    },
    {
        "id": "multi:zh-CN-Xiaoyi",
        "name": "Xiaoyi (Tiếng Trung — Nữ trẻ trung Douyin)",
        "subtitle": "🇨🇳 Tiếng Trung Phổ thông (zh-CN) • Nữ • Hoạt ngôn, Douyin / Shorts / Review",
        "lang": "zh",
        "tl": "zh-CN",
        "edge_voice": "zh-CN-XiaoyiNeural",
        "macos_voice": "Tingting",
        "gender": "female",
        "pitch": 1.04,
        "demo": "大家好，我是晓伊！声音活泼甜美，非常适合短视频和故事解说。",
    },
    {
        "id": "multi:zh-CN-Yunxi",
        "name": "Yunxi (Tiếng Trung — Nam review / kể chuyện)",
        "subtitle": "🇨🇳 Tiếng Trung Phổ thông (zh-CN) • Nam • Trẻ trung, Review phim / Kể chuyện",
        "lang": "zh",
        "tl": "zh-CN",
        "edge_voice": "zh-CN-YunxiNeural",
        "macos_voice": "Tingting",
        "gender": "male",
        "pitch": 0.92,
        "demo": "大家好，我是云希，这是用于电影解说和短视频的中文男声。",
    },
    {
        "id": "multi:zh-CN-Yunjian",
        "name": "Yunjian (Tiếng Trung — Nam trầm điện ảnh)",
        "subtitle": "🇨🇳 Tiếng Trung Phổ thông (zh-CN) • Nam • Trầm hùng, Thuyết minh phim / Hành động",
        "lang": "zh",
        "tl": "zh-CN",
        "edge_voice": "zh-CN-YunjianNeural",
        "macos_voice": "Tingting",
        "gender": "male",
        "pitch": 0.85,
        "demo": "大家好，我是云健，声音浑厚有力，适合纪录片与大片解说。",
    },
    {
        "id": "multi:zh-CN-Yunyang",
        "name": "Yunyang (Tiếng Trung — Nam phát thanh viên)",
        "subtitle": "🇨🇳 Tiếng Trung Phổ thông (zh-CN) • Nam • Chuẩn phát thanh, Tin tức / Kiến thức",
        "lang": "zh",
        "tl": "zh-CN",
        "edge_voice": "zh-CN-YunyangNeural",
        "macos_voice": "Tingting",
        "gender": "male",
        "pitch": 0.90,
        "demo": "您好，我是云扬，为您提供专业、清晰的中文新闻与知识播报。",
    },
    {
        "id": "multi:zh-TW-HsiaoChen",
        "name": "HsiaoChen (Tiếng Trung Đài Loan — Nữ dịu dàng)",
        "subtitle": "🇹🇼 Tiếng Trung (zh-TW) • Nữ • Nhẹ nhàng, cảm xúc",
        "lang": "zh",
        "tl": "zh-TW",
        "edge_voice": "zh-TW-HsiaoChenNeural",
        "macos_voice": "Meijia",
        "gender": "female",
        "pitch": 1.02,
        "demo": "你好，我是曉臻，這是溫柔自然的台灣中文女聲。",
    },
    {
        "id": "multi:zh-TW-YunJhe",
        "name": "YunJhe (Tiếng Trung Đài Loan — Nam chuẩn)",
        "subtitle": "🇹🇼 Tiếng Trung (zh-TW) • Nam • Trầm ấm, Thuyết minh / Tin tức",
        "lang": "zh",
        "tl": "zh-TW",
        "edge_voice": "zh-TW-YunJheNeural",
        "macos_voice": "Meijia",
        "gender": "male",
        "pitch": 0.90,
        "demo": "大家好，我是雲哲，這是自然清晰的台灣中文男聲。",
    },
    {
        "id": "multi:ko-KR-SunHi",
        "name": "SunHi (Tiếng Hàn — Nữ chuẩn phát thanh)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nữ • Rõ ràng, chuẩn phát thanh / Vlog",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "ko-KR-SunHiNeural",
        "macos_voice": "Yuna",
        "gender": "female",
        "pitch": 1.0,
        "demo": "안녕하세요, 선희입니다. 동영상 더빙을 위한 자연스러운 한국어 여성 음성입니다.",
    },
    {
        "id": "multi:ko-KR-InJoon",
        "name": "InJoon (Tiếng Hàn — Nam trầm ấm)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nam • Trầm ấm, Thuyết minh / Kể chuyện",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "ko-KR-InJoonNeural",
        "macos_voice": "Yuna",
        "gender": "male",
        "pitch": 0.90,
        "demo": "안녕하세요, 인준입니다. 영화 리뷰와 내레이션에 어울리는 한국어 남성 음성입니다.",
    },
    {
        "id": "multi:ko-KR-Hyunsu",
        "name": "Hyunsu (Tiếng Hàn — Nam hiện đại)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nam • Trẻ trung, Review / Tin tức / Shorts",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "ko-KR-HyunsuMultilingualNeural",
        "macos_voice": "Yuna",
        "gender": "male",
        "pitch": 0.88,
        "demo": "안녕하세요, 현수입니다. 깔끔하고 자연스러운 한국어 남성 목소리입니다.",
    },
    {
        "id": "multi:ko-KR-SeoHyeon",
        "name": "SeoHyeon (Tiếng Hàn — Nữ trẻ trung / Shorts / TikTok)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nữ • Tươi sáng, năng động, Vlog / K-Beauty / Review",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "ko-KR-SunHiNeural",
        "edge_pitch": "+5Hz",
        "macos_voice": "Yuna",
        "gender": "female",
        "pitch": 1.05,
        "demo": "안녕하세요 여러분! 서현입니다. 쇼츠와 브이로그에 잘 어울리는 밝고 경쾌한 목소리예요.",
    },
    {
        "id": "multi:ko-KR-MinHo",
        "name": "MinHo (Tiếng Hàn — Nam điện ảnh / K-Drama Recap)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nam • Giọng trầm sâu, Tóm tắt phim / Tài liệu",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "ko-KR-InJoonNeural",
        "edge_pitch": "-6Hz",
        "macos_voice": "Yuna",
        "gender": "male",
        "pitch": 0.83,
        "demo": "반갑습니다, 민호입니다. 영화 요약과 다큐멘터리에 적합한 중후한 남성 음성입니다.",
    },
    {
        "id": "multi:ko-KR-JiYeon",
        "name": "JiYeon (Tiếng Hàn — Nữ dịu dàng / Kể chuyện)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nữ • Nhẹ nhàng, truyền cảm, Audiobook / Radio",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "en-US-EmmaMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Yuna",
        "gender": "female",
        "pitch": 0.98,
        "demo": "안녕하세요, 지연입니다. 감성적인 이야기와 오디오북을 위한 부드러운 목소리입니다.",
    },
    {
        "id": "multi:ko-KR-TaeHyun",
        "name": "TaeHyun (Tiếng Hàn — Nam chuyên gia / Tài chính)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nam • Điềm tĩnh, sắc nét, Bản tin / Kinh tế / Crypto",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "en-US-BrianMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Yuna",
        "gender": "male",
        "pitch": 0.92,
        "demo": "안녕하십니까, 태현입니다. 뉴스 브리핑과 경제 분석에 어울리는 신뢰감 있는 목소리입니다.",
    },
    {
        "id": "multi:ko-KR-HaEun",
        "name": "HaEun (Tiếng Hàn — Nữ thanh lịch / Thuyết trình)",
        "subtitle": "🇰🇷 Tiếng Hàn (ko-KR) • Nữ • Chuyên nghiệp, rõ ràng, Đào tạo / Quảng cáo",
        "lang": "ko",
        "tl": "ko",
        "edge_voice": "en-US-AvaMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Yuna",
        "gender": "female",
        "pitch": 1.02,
        "demo": "안녕하세요, 하은입니다. 프레젠테이션과 광고 내레이션을 위한 또렷한 음성입니다.",
    },
    {
        "id": "multi:ja-JP-Nanami",
        "name": "Nanami (Tiếng Nhật — Nữ trong trẻo)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nữ • Tự nhiên, Anime / Vlog / Thuyết minh",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "ja-JP-NanamiNeural",
        "macos_voice": "Kyoko",
        "gender": "female",
        "pitch": 1.0,
        "demo": "こんにちは、七海です。動画ナレーション用の自然な日本語の女性音声です。",
    },
    {
        "id": "multi:ja-JP-Keita",
        "name": "Keita (Tiếng Nhật — Nam trầm ấm)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nam • Điềm đạm, Review / Kể chuyện / Tin tức",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "ja-JP-KeitaNeural",
        "macos_voice": "Otoya",
        "gender": "male",
        "pitch": 0.89,
        "demo": "こんにちは、圭太です。動画解説やナレーションに最適な日本語の男性音声です。",
    },
    {
        "id": "multi:ja-JP-Aoi",
        "name": "Aoi (Tiếng Nhật — Nữ trẻ trung / Anime / Shorts)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nữ • Hoạt bát, tươi sáng, TikTok / Shorts / Văn hóa Nhật",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "ja-JP-NanamiNeural",
        "edge_pitch": "+6Hz",
        "macos_voice": "Kyoko",
        "gender": "female",
        "pitch": 1.06,
        "demo": "みなさんこんにちは、葵です！ショート動画やアニメ解説にぴったりの明るい声です。",
    },
    {
        "id": "multi:ja-JP-Daichi",
        "name": "Daichi (Tiếng Nhật — Nam trầm điện ảnh / Documentary)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nam • Giọng nam trầm sâu, Phim tài liệu / Lịch sử",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "ja-JP-KeitaNeural",
        "edge_pitch": "-6Hz",
        "macos_voice": "Otoya",
        "gender": "male",
        "pitch": 0.83,
        "demo": "こんにちは、大地です。映画解説やドキュメンタリー向けの重厚な男性ボイスです。",
    },
    {
        "id": "multi:ja-JP-Mayu",
        "name": "Mayu (Tiếng Nhật — Nữ dịu dàng / Kể chuyện)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nữ • Nhẹ nhàng, truyền cảm, Audiobook / Radio",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "en-US-EmmaMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Kyoko",
        "gender": "female",
        "pitch": 0.97,
        "demo": "こんにちは、真由です。物語の朗読や落ち着いたナレーション向けの優しい声です。",
    },
    {
        "id": "multi:ja-JP-Naoki",
        "name": "Naoki (Tiếng Nhật — Nam bản tin / Chuyên gia)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nam • Chững chạc, rõ ràng, Kinh tế / Công nghệ / Tin tức",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "en-US-AndrewMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Otoya",
        "gender": "male",
        "pitch": 0.91,
        "demo": "こんにちは、直樹です。ビジネス解説やニュース番組に適した落ち着いた男性音声です。",
    },
    {
        "id": "multi:ja-JP-Shiori",
        "name": "Shiori (Tiếng Nhật — Nữ thanh lịch / Thuyết trình)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nữ • Sang trọng, chuẩn mực, Quảng cáo / Đào tạo",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "en-US-AvaMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Kyoko",
        "gender": "female",
        "pitch": 1.01,
        "demo": "こんにちは、栞です。プレゼンテーションやガイド音声に最適なクリアな声です。",
    },
    {
        "id": "multi:ja-JP-Takumi",
        "name": "Takumi (Tiếng Nhật — Nam năng động / Vlog / Review)",
        "subtitle": "🇯🇵 Tiếng Nhật (ja-JP) • Nam • Trẻ trung, hiện đại, Review công nghệ / Game",
        "lang": "ja",
        "tl": "ja",
        "edge_voice": "en-US-BrianMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Otoya",
        "gender": "male",
        "pitch": 0.95,
        "demo": "やあ、拓海です！ガジェット紹介やゲーム実況にぴったりのエネルギッシュな声です。",
    },
    {
        "id": "multi:ru-RU-Svetlana",
        "name": "Svetlana (Tiếng Nga — Nữ chuẩn phát thanh)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nữ • Rõ ràng, truyền cảm, chuẩn Moscow",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "ru-RU-SvetlanaNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Milena",
        "gender": "female",
        "pitch": 1.0,
        "demo": "Здравствуйте, меня зовут Светлана. Это естественный женский голос для озвучки ваших видео.",
    },
    {
        "id": "multi:ru-RU-Dmitry",
        "name": "Dmitry (Tiếng Nga — Nam trầm mạnh mẽ)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nam • Trầm ấm, Thuyết minh phim / Tài liệu",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "ru-RU-DmitryNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Yuri",
        "gender": "male",
        "pitch": 0.88,
        "demo": "Здравствуйте, я Дмитрий. Это глубокий мужской голос для профессиональной озвучки и обзоров.",
    },
    {
        "id": "multi:ru-RU-Aleksandr",
        "name": "Aleksandr (Tiếng Nga — Nam điện ảnh / Review phim)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nam • Trầm sâu, Điện ảnh / Kể chuyện kịch tính",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "de-DE-FlorianMultilingualNeural",
        "edge_pitch": "-2Hz",
        "macos_voice": "Yuri",
        "gender": "male",
        "pitch": 0.85,
        "demo": "Приветствую! Меня зовут Александр. Мой голос отлично подходит для кинообзоров и историй.",
    },
    {
        "id": "multi:ru-RU-Maksim",
        "name": "Maksim (Tiếng Nga — Nam trẻ trung / TikTok / Shorts)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nam • Năng động, hiện đại, Vlog / Shorts / Crypto",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "en-US-BrianMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Yuri",
        "gender": "male",
        "pitch": 0.94,
        "demo": "Всем привет! Я Максим. Молодой и энергичный голос для ваших роликов и блогов.",
    },
    {
        "id": "multi:ru-RU-Artem",
        "name": "Artem (Tiếng Nga — Nam điềm đạm / Chuyên gia)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nam • Chững chạc, Phân tích tài chính / Tin tức",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "fr-FR-RemyMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Yuri",
        "gender": "male",
        "pitch": 0.91,
        "demo": "Добрый день, я Артём. Спокойный и уверенный мужской голос для аналитики и обучающих видео.",
    },
    {
        "id": "multi:ru-RU-Nikolai",
        "name": "Nikolai (Tiếng Nga — Nam trầm hùng / Documentary)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nam • Giọng nam trầm dày, Phim tài liệu / Lịch sử",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "ru-RU-DmitryNeural",
        "edge_pitch": "-6Hz",
        "macos_voice": "Yuri",
        "gender": "male",
        "pitch": 0.83,
        "demo": "Здравствуйте, я Николай. Низкий и насыщенный тембр для документальных фильмов.",
    },
    {
        "id": "multi:ru-RU-Ivan",
        "name": "Ivan (Tiếng Nga — Nam ấm áp / Podcast)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nam • Tự nhiên, gần gũi, Podcast / Kể chuyện",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "it-IT-GiuseppeMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Yuri",
        "gender": "male",
        "pitch": 0.92,
        "demo": "Привет! Меня зовут Иван. Тёплый и дружелюбный голос для подкастов и рассказов.",
    },
    {
        "id": "multi:ru-RU-Anastasia",
        "name": "Anastasia (Tiếng Nga — Nữ trẻ trung / Vlog / Reels)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nữ • Trẻ trung, tươi sáng, TikTok / Shorts",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "en-US-EmmaMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Milena",
        "gender": "female",
        "pitch": 1.04,
        "demo": "Всем привет! Я Анастасия. Яркий и современный женский голос для коротких видео и блогов.",
    },
    {
        "id": "multi:ru-RU-Elena",
        "name": "Elena (Tiếng Nga — Nữ dịu dàng / Kể chuyện)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nữ • Ngọt ngào, truyền cảm, Audiobook / Tâm sự",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "de-DE-SeraphinaMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Milena",
        "gender": "female",
        "pitch": 1.01,
        "demo": "Здравствуйте, я Елена. Мягкий и выразительный голос для историй и аудиокниг.",
    },
    {
        "id": "multi:ru-RU-Daria",
        "name": "Daria (Tiếng Nga — Nữ sang trọng / Thuyết trình)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nữ • Thanh lịch, sắc nét, Quảng cáo / Đào tạo",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "fr-FR-VivienneMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Milena",
        "gender": "female",
        "pitch": 0.98,
        "demo": "Добрый день! Меня зовут Дарья. Элегантный голос для презентаций и коммерческих роликов.",
    },
    {
        "id": "multi:ru-RU-Ksenia",
        "name": "Ksenia (Tiếng Nga — Nữ hoạt ngôn / Review)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nữ • Trong trẻo, nhanh nhẹn, Review sản phẩm / Phim",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "ru-RU-SvetlanaNeural",
        "edge_pitch": "+5Hz",
        "macos_voice": "Milena",
        "gender": "female",
        "pitch": 1.05,
        "demo": "Привет! Я Ксения. Живой и звонкий голос для динамичных обзоров и рекламы.",
    },
    {
        "id": "multi:ru-RU-Olga",
        "name": "Olga (Tiếng Nga — Nữ trầm ấm / Tin tức)",
        "subtitle": "🇷🇺 Tiếng Nga (ru-RU) • Nữ • Điềm tĩnh, chuyên nghiệp, Tin tức / Tài liệu",
        "lang": "ru",
        "tl": "ru",
        "edge_voice": "pt-BR-ThalitaMultilingualNeural",
        "edge_pitch": "+0Hz",
        "macos_voice": "Milena",
        "gender": "female",
        "pitch": 0.96,
        "demo": "Здравствуйте, с вами Ольга. Спокойный и чёткий голос для информационных выпусков.",
    },
    {
        "id": "multi:fr-FR-Denise",
        "name": "Denise (Tiếng Pháp — Nữ)",
        "subtitle": "🇫🇷 Tiếng Pháp (fr-FR) • Nữ • Tự nhiên, chuẩn Paris",
        "lang": "fr",
        "tl": "fr",
        "edge_voice": "fr-FR-DeniseNeural",
        "macos_voice": "Thomas",
        "gender": "female",
        "pitch": 1.0,
        "demo": "Bonjour, je suis Denise. Voici une voix féminine naturelle en français pour vos vidéos.",
    },
    {
        "id": "multi:fr-FR-Henri",
        "name": "Henri (Tiếng Pháp — Nam)",
        "subtitle": "🇫🇷 Tiếng Pháp (fr-FR) • Nam • Trầm ấm, Thuyết minh",
        "lang": "fr",
        "tl": "fr",
        "edge_voice": "fr-FR-HenriNeural",
        "macos_voice": "Thomas",
        "gender": "male",
        "pitch": 0.90,
        "demo": "Bonjour, je suis Henri. Voici une voix masculine claire et chaleureuse en français.",
    },
    {
        "id": "multi:de-DE-Katja",
        "name": "Katja (Tiếng Đức — Nữ)",
        "subtitle": "🇩🇪 Tiếng Đức (de-DE) • Nữ • Tự nhiên, rõ ràng",
        "lang": "de",
        "tl": "de",
        "edge_voice": "de-DE-KatjaNeural",
        "macos_voice": "Anna",
        "gender": "female",
        "pitch": 1.0,
        "demo": "Hallo, ich bin Katja. Dies ist eine natürliche deutsche Frauenstimme für Ihre Videos.",
    },
    {
        "id": "multi:de-DE-Conrad",
        "name": "Conrad (Tiếng Đức — Nam)",
        "subtitle": "🇩🇪 Tiếng Đức (de-DE) • Nam • Trầm ấm, Chuyên nghiệp",
        "lang": "de",
        "tl": "de",
        "edge_voice": "de-DE-ConradNeural",
        "macos_voice": "Anna",
        "gender": "male",
        "pitch": 0.89,
        "demo": "Hallo, ich bin Conrad. Dies ist eine klare deutsche Männerstimme für Kommentare und Videos.",
    },
    {
        "id": "multi:es-ES-Elvira",
        "name": "Elvira (Tiếng Tây Ban Nha — Nữ)",
        "subtitle": "🇪🇸 Tiếng Tây Ban Nha (es-ES) • Nữ • Truyền cảm",
        "lang": "es",
        "tl": "es",
        "edge_voice": "es-ES-ElviraNeural",
        "macos_voice": "Monica",
        "gender": "female",
        "pitch": 1.0,
        "demo": "Hola, soy Elvira. Esta es una voz femenina natural en español para tus videos.",
    },
    {
        "id": "multi:es-ES-Alvaro",
        "name": "Alvaro (Tiếng Tây Ban Nha — Nam)",
        "subtitle": "🇪🇸 Tiếng Tây Ban Nha (es-ES) • Nam • Trầm ấm, Thuyết minh",
        "lang": "es",
        "tl": "es",
        "edge_voice": "es-ES-AlvaroNeural",
        "macos_voice": "Jorge",
        "gender": "male",
        "pitch": 0.90,
        "demo": "Hola, soy Álvaro. Esta es una voz masculina cálida y profesional en español.",
    },
    {
        "id": "multi:th-TH-Premwadee",
        "name": "Premwadee (Tiếng Thái — Nữ)",
        "subtitle": "🇹🇭 Tiếng Thái (th-TH) • Nữ • Tự nhiên",
        "lang": "th",
        "tl": "th",
        "edge_voice": "th-TH-PremwadeeNeural",
        "macos_voice": "Kanya",
        "gender": "female",
        "pitch": 1.0,
        "demo": "สวัสดีค่ะ ดิฉันเปรมวดี นี่คือเสียงพากย์ภาษาไทยที่เป็นธรรมชาติสำหรับวิดีโอของคุณ",
    },
    {
        "id": "multi:th-TH-Niwat",
        "name": "Niwat (Tiếng Thái — Nam)",
        "subtitle": "🇹🇭 Tiếng Thái (th-TH) • Nam • Rõ ràng, Thuyết minh",
        "lang": "th",
        "tl": "th",
        "edge_voice": "th-TH-NiwatNeural",
        "macos_voice": "Kanya",
        "gender": "male",
        "pitch": 0.90,
        "demo": "สวัสดีครับ ผมนิวัฒน์ นี่คือเสียงผู้ชายภาษาไทยที่ชัดเจนสำหรับการบรรยายวิดีโอ",
    },
]

_MULTILINGUAL_BY_ID = {v["id"]: v for v in MULTILINGUAL_VOICES}


def list_multilingual_voices():
    """Returns the built-in list of English & Multilingual voice dicts."""
    return list(MULTILINGUAL_VOICES)


def is_multilingual_voice(voice_id):
    return bool(voice_id and str(voice_id).startswith("multi:"))


def get_multilingual_voice(voice_id):
    return _MULTILINGUAL_BY_ID.get(str(voice_id or ""))


def voice_language(voice_id, default="vi"):
    """Returns the primary language code ('en', 'vi', 'zh', etc.) for a voice_id."""
    info = get_multilingual_voice(voice_id)
    if info:
        return info["lang"]
    return default


def demo_text_for_voice(voice_id):
    info = get_multilingual_voice(voice_id)
    if info and info.get("demo"):
        return info["demo"]
    return f"Xin chào, tôi là {voice_id}. Đây là giọng nói mẫu tiếng Việt rõ ràng và tự nhiên."


def init(config):
    """Remembers the app config so the model-loading singleton can re-register
    cloned voices the moment it's created. Cheap — doesn't load the model.
    """
    global _config
    _config = config


def _get_tts():
    global _tts
    if _tts is None:
        with _model_lock:
            if _tts is None:  # re-check: another thread may have loaded it first
                from pathlib import Path
                from vieneu import Vieneu

                hf_cache = Path(
                    os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
                ) / "hub" / "models--pnnbao-ump--VieNeu-TTS-v3-Turbo"
                prev_offline = os.environ.get("HF_HUB_OFFLINE")
                if hf_cache.exists() and not prev_offline:
                    os.environ["HF_HUB_OFFLINE"] = "1"
                try:
                    _tts = Vieneu()
                except Exception:
                    if prev_offline is None:
                        os.environ.pop("HF_HUB_OFFLINE", None)
                    _tts = Vieneu()

                if _config is not None:
                    registry = load_cloned_voices(_config)
                    for name, ref_audio_path in registry.items():
                        if os.path.exists(ref_audio_path):
                            _tts.add_voice(name, ref_audio_path)
    return _tts


_cached_preset_voices = None


def list_preset_voices():
    """Returns [(display_label, voice_name), ...] for the 25 built-in voices.
    Reads `voices_v3_turbo.json` directly when `_tts` has not been loaded yet,
    avoiding blocking the UI/HTTP server with heavy model initialization.
    """
    global _cached_preset_voices
    if _cached_preset_voices is not None:
        return list(_cached_preset_voices)

    if _tts is not None:
        _cached_preset_voices = _tts.list_preset_voices()
        return list(_cached_preset_voices)

    try:
        import importlib.util
        from pathlib import Path

        spec = importlib.util.find_spec("vieneu")
        if spec and spec.origin:
            assets_path = Path(spec.origin).parent / "assets" / "voices_v3_turbo.json"
            if assets_path.exists():
                data = json.loads(assets_path.read_text(encoding="utf-8"))
                presets = data.get("presets") or {}
                items = [
                    (v.get("description") or k, k, bool(v.get("featured", False)))
                    for k, v in presets.items()
                ]
                items.sort(key=lambda x: (not x[2], str(x[0])))
                _cached_preset_voices = [(desc, k) for desc, k, _ in items]
                return list(_cached_preset_voices)
    except Exception:
        pass

    return _get_tts().list_preset_voices()


def _clean_waveform(audio, sr=24000):
    """Cleans raw TTS waveform to prevent buzzing/clicks/padded silence:
    1. Converts to 1-D float32 numpy array and removes DC offset.
    2. Trims excessive leading/trailing dead air while preserving a natural 35ms pad.
    3. Applies a smooth 15ms cosine fade-in/out to eliminate clicks at chunk boundaries.
    4. Soft-clips peaks to [-0.98, 0.98].
    """
    arr = np.asarray(audio, dtype=np.float32).reshape(-1)
    if arr.size == 0:
        return np.zeros(int(sr * 0.1), dtype=np.float32)

    # Replace NaN/Inf if any
    arr = np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)
    # Remove DC offset
    arr = arr - float(np.mean(arr))

    # Trim leading/trailing silence (threshold ~ -42 dBFS)
    window = max(1, int(sr * 0.01))
    abs_arr = np.abs(arr)
    if abs_arr.size > window:
        kernel = np.ones(window, dtype=np.float32) / window
        envelope = np.convolve(abs_arr, kernel, mode="same")
    else:
        envelope = abs_arr

    active = np.where(envelope > 0.008)[0]
    if active.size > 0:
        pad = int(sr * 0.035)
        start_idx = max(0, int(active[0]) - pad)
        end_idx = min(arr.size, int(active[-1]) + pad)
        arr = arr[start_idx:end_idx]

    # Smooth 15ms cosine fade-in and fade-out
    fade_len = min(int(sr * 0.015), arr.size // 4)
    if fade_len > 4:
        fade_in = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fade_len, dtype=np.float32)))
        fade_out = fade_in[::-1]
        arr[:fade_len] *= fade_in
        arr[-fade_len:] *= fade_out

    # Normalize gently if overly quiet or hot, then clamp
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    if peak > 0.98:
        arr = arr * (0.95 / peak)
    elif 0.02 < peak < 0.35:
        arr = arr * min(2.2, 0.75 / peak)

    return np.clip(arr, -0.98, 0.98).astype(np.float32)


def _split_gtts_chunks(text, max_chars=175):
    """Splits text into sentence-friendly chunks under `max_chars` for HTTP TTS."""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    parts = re.split(r"(?<=[.!?;,。！？；，])\s+", cleaned)
    chunks = []
    current = ""
    for part in parts:
        if not part:
            continue
        candidate = f"{current} {part}".strip() if current else part
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current)
            if len(part) <= max_chars:
                current = part
            else:
                words = part.split(" ")
                sub = ""
                for w in words:
                    cand_w = f"{sub} {w}".strip() if sub else w
                    if len(cand_w) <= max_chars:
                        sub = cand_w
                    else:
                        if sub:
                            chunks.append(sub)
                        sub = w[:max_chars]
                current = sub
    if current:
        chunks.append(current)
    return chunks


def _synthesize_multilingual_to_wav(text, voice_info, output_path, speed=1.0, ffmpeg="ffmpeg"):
    """Synthesizes English or multilingual speech to `output_path` (WAV).
    Tries:
      1. `edge_tts` (if installed) for Microsoft Edge Neural voices
      2. Google Translate TTS HTTP endpoint (`translate.google.com/translate_tts`)
      3. macOS `say` command fallback (offline)
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    ffmpeg_bin = ffmpeg or (_config.ffmpeg_path if _config else "ffmpeg")

    with tempfile.TemporaryDirectory(prefix="maxvoice_mtts_") as tmp_dir:
        raw_audio_files = []
        used_edge = False

        # 1. Try edge_tts if available (real distinct Microsoft Edge Neural voices)
        try:
            import asyncio
            import edge_tts  # type: ignore

            mp3_path = os.path.join(tmp_dir, "edge.mp3")
            rate_pct = int(round((float(speed or 1.0) - 1.0) * 100))
            rate_str = f"{rate_pct:+d}%"
            edge_pitch = voice_info.get("edge_pitch") or "+0Hz"

            async def _run_edge():
                comm = edge_tts.Communicate(
                    text, voice_info["edge_voice"], rate=rate_str, pitch=edge_pitch
                )
                await comm.save(mp3_path)

            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_run_edge())
            finally:
                loop.close()
            if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 256:
                raw_audio_files.append(mp3_path)
                used_edge = True
        except Exception:
            raw_audio_files.clear()

        # 2. Offline fallback to macOS `say` (distinct voices: Samantha, Alex, Victoria, Fred, Daniel, Kate, Linh...)
        if not raw_audio_files and shutil.which("say"):
            aiff_path = os.path.join(tmp_dir, "say.aiff")
            macos_voice = voice_info.get("macos_voice") or "Samantha"
            res = subprocess.run(
                ["say", "-v", macos_voice, "-o", aiff_path, text],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0 and os.path.exists(aiff_path) and os.path.getsize(aiff_path) > 256:
                raw_audio_files.append(aiff_path)

        # 3. Fallback to Google Translate TTS HTTP endpoint
        if not raw_audio_files:
            chunks = _split_gtts_chunks(text)
            tl = voice_info.get("tl") or voice_info.get("lang") or "en"
            for idx, chunk in enumerate(chunks):
                q = urllib.parse.quote(chunk)
                url = (
                    f"https://translate.google.com/translate_tts"
                    f"?ie=UTF-8&client=tw-ob&tl={urllib.parse.quote(tl)}&q={q}"
                )
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = resp.read()
                    if data and len(data) > 128:
                        part_path = os.path.join(tmp_dir, f"gtts_{idx:03d}.mp3")
                        with open(part_path, "wb") as f:
                            f.write(data)
                        raw_audio_files.append(part_path)
                except Exception:
                    raw_audio_files.clear()
                    break

        if not raw_audio_files:
            raise RuntimeError("Không tạo được giọng đọc đa ngôn ngữ (kiểm tra kết nối mạng)")

        # Convert & concatenate to clean 24kHz mono WAV with silence trimming
        input_args = []
        for p in raw_audio_files:
            input_args += ["-i", p]

        pitch = float(voice_info.get("pitch") or 1.0)
        stream_refs = "".join(
            f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=24000:channel_layouts=mono[a{i}];"
            for i in range(len(raw_audio_files))
        )
        concat_inputs = "".join(f"[a{i}]" for i in range(len(raw_audio_files)))
        post_filters = [
            "silenceremove=start_periods=1:start_silence=0.04:start_threshold=-42dB:"
            "stop_periods=-1:stop_silence=0.18:stop_threshold=-42dB"
        ]
        # Only apply pitch shift when falling back to single-voice Google HTTP TTS
        if not used_edge and abs(pitch - 1.0) > 0.02:
            new_rate = int(round(24000 * pitch))
            tempo_comp = 1.0 / pitch
            post_filters.append(f"asetrate={new_rate},aresample=24000,atempo={tempo_comp:.4f}")
        if not used_edge and speed and abs(float(speed) - 1.0) > 0.02:
            post_filters.append(f"atempo={min(2.0, max(0.5, float(speed))):.4f}")

        filter_complex = (
            f"{stream_refs}{concat_inputs}concat=n={len(raw_audio_files)}:v=0:a=1,"
            f"{','.join(post_filters)}[aout]"
        )
        res = subprocess.run(
            [
                ffmpeg_bin, "-y",
                *input_args,
                "-filter_complex", filter_complex,
                "-map", "[aout]",
                "-ar", "24000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                output_path,
            ],
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg lỗi khi xử lý giọng đa ngôn ngữ: {res.stderr.strip()}")
    return output_path


def synthesize(text, voice=None, ref_audio=None, speed=1.0, seed=None):
    """Synthesizes `text` with either a preset `voice` name, a cloned
    `ref_audio` sample, or a `multi:...` English/Multilingual voice.
    Returns a clean float32 numpy array at `sample_rate()`.
    """
    if is_multilingual_voice(voice):
        import soundfile as sf

        voice_info = get_multilingual_voice(voice)
        if not voice_info:
            raise RuntimeError(f"Không tìm thấy giọng đa ngôn ngữ: {voice}")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_wav = tmp.name
        try:
            _synthesize_multilingual_to_wav(text, voice_info, tmp_wav, speed=speed)
            audio, sr = sf.read(tmp_wav, dtype="float32")
            return _clean_waveform(audio, sr=sr)
        finally:
            try:
                os.unlink(tmp_wav)
            except OSError:
                pass

    tts = _get_tts()
    kwargs = {"apply_watermark": False}
    if seed is not None:
        kwargs["seed"] = seed
    if speed and speed != 1.0:
        kwargs["speed"] = speed
    with heavy_inference_lock:
        try:
            raw_audio = tts.infer(text=text, voice=voice, ref_audio=ref_audio, **kwargs)
        except TypeError:
            kwargs.pop("apply_watermark", None)
            raw_audio = tts.infer(text=text, voice=voice, ref_audio=ref_audio, **kwargs)
    return _clean_waveform(raw_audio, sr=tts.sample_rate)


def save_wav(audio, output_path, sr=24000):
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    import soundfile as sf

    arr = np.asarray(audio, dtype=np.float32)
    sf.write(output_path, arr, sr, subtype="PCM_16")
    return output_path


def synthesize_to_file(text, output_path, voice=None, ref_audio=None, speed=1.0, ffmpeg="ffmpeg"):
    """Synthesizes `text` directly to `output_path` (.wav), supporting both
    VieNeu Vietnamese voices (without loading/unloading extra temp files) and
    built-in English/Multilingual voices (`multi:...`).
    """
    if is_multilingual_voice(voice):
        voice_info = get_multilingual_voice(voice)
        if not voice_info:
            raise RuntimeError(f"Không tìm thấy giọng đa ngôn ngữ: {voice}")
        _synthesize_multilingual_to_wav(text, voice_info, output_path, speed=speed, ffmpeg=ffmpeg)
        # Apply final waveform cleanup (DC offset, cosine fade, peak limit)
        import soundfile as sf

        audio, sr = sf.read(output_path, dtype="float32")
        cleaned = _clean_waveform(audio, sr=sr)
        sf.write(output_path, cleaned, sr, subtype="PCM_16")
        return output_path

    audio = synthesize(text, voice=voice, ref_audio=ref_audio, speed=speed)
    return save_wav(audio, output_path, sr=sample_rate())


def sample_rate():
    if _tts is not None:
        return _tts.sample_rate
    return 24000


# --- cloned voices registry -------------------------------------------------
# vieneu's own add_voice()/save_voices() persist into the installed package's
# assets dir, which isn't writable once this app is packaged (PyInstaller) and
# mixes user data with the install. Instead we keep our own small registry —
# {name: ref_audio_path} — under the app's storage dir, and re-register each
# cloned voice with add_voice() at the start of every session.

def _registry_path(config):
    return os.path.join(config.storage_dir, "vieneu_voices.json")


def load_cloned_voices(config):
    path = _registry_path(config)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def clone_voice(config, name, ref_audio_path, description="", gender=""):
    """Registers a new voice from a 3-8s reference audio sample and saves it
    to the local registry so it survives app restarts.
    """
    import uuid

    tts = _get_tts()
    voices_dir = os.path.join(config.storage_dir, "voices")
    os.makedirs(voices_dir, exist_ok=True)
    ext = os.path.splitext(ref_audio_path)[1] or ".wav"
    stored_path = os.path.join(voices_dir, f"{uuid.uuid4()}{ext}")
    shutil.copyfile(ref_audio_path, stored_path)

    tts.add_voice(name, stored_path, description=description, gender=gender)

    registry = load_cloned_voices(config)
    registry[name] = stored_path
    with open(_registry_path(config), "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    return name


def remove_cloned_voice(config, name):
    registry = load_cloned_voices(config)
    stored_path = registry.pop(name, None)
    with open(_registry_path(config), "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    if stored_path and os.path.exists(stored_path):
        try:
            os.unlink(stored_path)
        except OSError:
            pass
    try:
        _get_tts().remove_voice(name)
    except Exception:
        pass

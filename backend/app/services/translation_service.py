import re
import requests

COMMON_SOURCE_LANGUAGES = [
    ("English", "en"), ("Tiếng Việt / Vietnamese", "vi"),
    ("Hàn Quốc / Korean", "ko"), ("Nhật / Japanese", "ja"),
    ("Trung (giản thể) / Chinese", "zh"), ("Pháp / French", "fr"), ("Đức / German", "de"),
    ("Tây Ban Nha / Spanish", "es"), ("Nga / Russian", "ru"), ("Thái / Thai", "th"),
    ("Indonesia", "id"), ("Ý / Italian", "it"), ("Bồ Đào Nha / Portuguese", "pt"),
    ("Ả Rập / Arabic", "ar"), ("Hindi", "hi"), ("Thổ Nhĩ Kỳ / Turkish", "tr"),
    ("Ba Lan / Polish", "pl"), ("Hà Lan / Dutch", "nl"), ("Ukraina / Ukrainian", "uk"),
    ("Hy Lạp / Greek", "el"), ("Do Thái / Hebrew", "he"), ("Ba Tư / Persian", "fa"),
    ("Malaysia / Malay", "ms"), ("Thụy Điển / Swedish", "sv"), ("Séc / Czech", "cs"),
    ("Khác (nhập mã ISO-639-1)...", ""),
]

# Danh sách ngôn ngữ đích hỗ trợ cho Phụ đề (Sub), SRT và Lồng tiếng (Voice)
TARGET_LANGUAGES = [
    ("Tiếng Việt (Vietnamese)", "vi"),
    ("Tiếng Anh (English)", "en"),
    ("Giữ nguyên ngôn ngữ gốc (Original)", "original"),
    ("Tiếng Trung (Chinese)", "zh"),
    ("Tiếng Nhật (Japanese)", "ja"),
    ("Tiếng Hàn (Korean)", "ko"),
    ("Tiếng Pháp (French)", "fr"),
    ("Tiếng Đức (German)", "de"),
    ("Tiếng Tây Ban Nha (Spanish)", "es"),
    ("Tiếng Thái (Thai)", "th"),
    ("Tiếng Nga (Russian)", "ru"),
    ("Tiếng Indonesia (Indonesian)", "id"),
    ("Tiếng Bồ Đào Nha (Portuguese)", "pt"),
]

# Các chế độ hiển thị / xuất phụ đề (Sub & SRT)
SUBTITLE_MODES = [
    ("Phụ đề ngôn ngữ đích (Đã dịch)", "target"),
    ("Phụ đề Song ngữ (Đích + Gốc)", "bilingual"),
    ("Phụ đề ngôn ngữ gốc (Original)", "original"),
]

_TARGET_LABEL_BY_CODE = {code: label for label, code in TARGET_LANGUAGES}

_SENTENCE_END_RE = re.compile(r'[.!?…。！？]["\'\)\]]*\s*$')

# Từ nối tiếng Việt / tiếng Anh ưu tiên để ngắt câu tự nhiên khi chia lại câu dài về các mốc SRT
_NATURAL_BREAK_WORDS = {
    "nhưng", "và", "hoặc", "để", "vì", "nên", "nếu", "khi", "mà", "trong", "với", "tại", "bằng", "sau", "trước",
    "but", "and", "or", "so", "because", "if", "when", "while", "where", "which", "that", "to", "for", "with",
}


def _normalize_lang_code(code):
    code = str(code or "").strip().lower()
    if not code or code == "original":
        return code
    if code.startswith("zh"):
        return "zh"
    return code.split("-")[0].split("_")[0]


def is_same_language(lang_a, lang_b):
    """Returns True if `lang_a` and `lang_b` refer to the same base ISO-639-1 language."""
    a = _normalize_lang_code(lang_a)
    b = _normalize_lang_code(lang_b)
    return bool(a and b and a == b)


def language_label(code):
    norm = _normalize_lang_code(code)
    return _TARGET_LABEL_BY_CODE.get(norm, code or "Tiếng Việt")


def _preprocess_source_text(text, source_lang):
    """Chuẩn hóa chữ hoa cho tên riêng / nền tảng / thuật ngữ mà Whisper thường
    ghi chữ thường (như 'join our discord' -> 'join our Discord', 'ape trade' -> 'APE coin trade')
    để bộ dịch không hiểu nhầm thành danh từ thường ('mối bất hòa', 'loài vượn'...).
    """
    if not text:
        return text
    out = text
    if not source_lang or source_lang.startswith("en"):
        en_fixes = [
            (r"\b(join\s+(?:our|my|the)\s+)discord\b", r"\1Discord"),
            (r"\bon\s+discord\b", "on Discord"),
            (r"\bdiscord\s+(server|channel|group|community|link)\b", r"Discord \1"),
            (r"\btelegram\b", "Telegram"),
            (r"\byoutube\b", "YouTube"),
            (r"\btiktok\b", "TikTok"),
            (r"\bbybit\b", "Bybit"),
            (r"\bbinance\b", "Binance"),
            (r"\bape\s+trade\b", "APE coin trade"),
            (r"\bbuy\s+it\s+here\b", "Bybit here"),
        ]
        for pattern, repl in en_fixes:
            out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return out


def _polish_vietnamese_translation(text):
    """Chuẩn hóa các thuật ngữ chuyên ngành (Trading, Crypto, Tech, Social) thường bị dịch
    máy word-by-word thành từ ngô nghê ("vật kỷ niệm", "giao dịch bí mật", "mục nhập"...).
    """
    if not text:
        return text
    replacements = [
        (r"\bgiao dịch bí mật\b", "giao dịch tiền điện tử"),
        (r"\btiền tệ bí mật\b", "tiền điện tử"),
        (r"\bvật kỷ niệm\b", "token"),
        (r"\bmã thông báo\b", "token"),
        (r"\bđồng xu\b", "đồng coin"),
        (r"\b(\d+(?:[.,]\d+)?)\s+xu\b", r"\1 đồng coin"),
        (r"\b(?:tham gia\s+)?vào\s+mối\s+bất\s+hòa\s+của\s+chúng\s+tôi\b", "tham gia kênh Discord của chúng tôi"),
        (r"\bmối\s+bất\s+hòa\s+của\s+chúng\s+tôi\b", "kênh Discord của chúng tôi"),
        (r"\bviệc\s+buôn\s+bán\s+loài\s+vượn\b", "giao dịch đồng APE"),
        (r"\bbuôn\s+bán\s+loài\s+vượn\b", "giao dịch đồng APE"),
        (r"\bsự khác biệt giữa các mục nhập và các điểm dừng\b", "khoảng cách giữa điểm vào lệnh (entry) và điểm cắt lỗ (stop)"),
        (r"\bgiữa mục nhập và điểm dừng\b", "giữa điểm vào lệnh và điểm cắt lỗ"),
        (r"\bphần thưởng rủi ro\b", "tỷ lệ rủi ro/lợi nhuận (R:R)"),
        (r"\bnhà giao dịch ngày\b", "nhà giao dịch trong ngày (day trader)"),
        (r"\bthương nhân ngày\b", "nhà giao dịch trong ngày"),
        (r"\bquay số trong\b", "tối ưu chính xác"),
    ]
    out = text
    for pattern, repl in replacements:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", out).strip()


def _online_translate_single(text, source_lang, target_lang, timeout=12):
    """Dịch văn bản qua các cổng Neural Translation trực tuyến với cơ chế tự động
    chuyển đổi dự phòng (clients5.google.com -> googleapis -> MyMemory).
    """
    sl = "auto" if not source_lang or source_lang == "original" else source_lang
    tl = "zh-CN" if target_lang == "zh" else target_lang
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

    # 1. Cổng clients5.google.com (dict-chrome-ex) — chất lượng Neural cao, không bị lỗi 429
    try:
        resp = requests.get(
            "https://clients5.google.com/translate_a/t",
            params={"client": "dict-chrome-ex", "sl": sl, "tl": tl, "q": text},
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
                if isinstance(first, list) and first and isinstance(first[0], str):
                    return first[0].strip()
    except Exception:
        pass

    # 2. Cổng translate.googleapis.com (gtx)
    try:
        resp = requests.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text},
            headers=headers,
            timeout=timeout,
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list) and data and isinstance(data[0], list):
                parts = [seg[0] for seg in data[0] if isinstance(seg, list) and seg and seg[0]]
                joined = "".join(parts).strip()
                if joined:
                    return joined
    except Exception:
        pass

    # 3. Cổng dự phòng MyMemory Translated API
    src_pair = "en" if sl == "auto" else sl
    resp = requests.get(
        "https://api.mymemory.translated.net/get",
        params={"q": text, "langpair": f"{src_pair}|{tl}"},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    translated = (data.get("responseData") or {}).get("translatedText")
    if translated and isinstance(translated, str):
        return translated.strip()
    return text


def _split_translated_sentence_across_cues(translated_sentence, source_pieces):
    """Khi 1 câu dài bị cắt làm nhiều đoạn phụ đề (cues) liên tiếp, sau khi dịch
    cả câu hoàn chỉnh để giữ đúng ngữ cảnh/ngữ pháp, hàm này chia lại câu đã dịch
    về đúng số lượng `source_pieces` theo tỷ lệ độ dài và ưu tiên ngắt ở dấu phẩy
    hoặc từ nối tự nhiên.
    """
    n = len(source_pieces)
    if n <= 1:
        return [translated_sentence.strip()]

    text = re.sub(r"\s+", " ", translated_sentence or "").strip()
    if not text:
        return ["" for _ in source_pieces]

    # Đối với tiếng Trung/Nhật (không có dấu cách giữa các từ), chia theo tỷ lệ ký tự
    if " " not in text and len(text) >= n:
        weights = [max(1, len(str(p or "").strip())) for p in source_pieces]
        total_w = sum(weights)
        out = []
        pos = 0
        acc_w = 0
        for i in range(n - 1):
            acc_w += weights[i]
            target_idx = int(round(len(text) * (acc_w / total_w)))
            target_idx = max(pos + 1, min(len(text) - (n - 1 - i), target_idx))
            out.append(text[pos:target_idx].strip())
            pos = target_idx
        out.append(text[pos:].strip())
        return out

    words = text.split(" ")
    if len(words) < n:
        # Câu dịch quá ngắn
        padded = words + [""] * (n - len(words))
        return padded[:n]

    weights = [max(1, len(str(p or "").strip())) for p in source_pieces]
    total_w = sum(weights)

    out = []
    start_word = 0
    acc_w = 0
    total_words = len(words)

    for i in range(n - 1):
        acc_w += weights[i]
        ideal_end = int(round(total_words * (acc_w / total_w)))
        min_end = start_word + 1
        max_end = total_words - (n - 1 - i)
        ideal_end = max(min_end, min(max_end, ideal_end))

        # Tìm điểm ngắt tự nhiên nhất trong bán kính ±3 từ quanh ideal_end
        best_end = ideal_end
        best_score = -10
        search_lo = max(min_end, ideal_end - 3)
        search_hi = min(max_end, ideal_end + 3)
        for cand in range(search_lo, search_hi + 1):
            prev_word = words[cand - 1]
            next_word = re.sub(r'^["\'\(\[]+|["\'\)\],.;:!?]+$', "", words[cand]).lower()
            score = -abs(cand - ideal_end)
            if prev_word.endswith((",", ";", ":", "—", "-")):
                score += 6
            elif prev_word.endswith((".", "!", "?")):
                score += 8
            if next_word in _NATURAL_BREAK_WORDS:
                score += 4
            if score > best_score:
                best_score = score
                best_end = cand

        out.append(" ".join(words[start_word:best_end]).strip())
        start_word = best_end

    out.append(" ".join(words[start_word:]).strip())
    return out


def _translate_with_sentence_context(texts, source_lang, target_lang):
    """Dịch danh sách các câu phụ đề có ngữ cảnh (Context-Aware Sentence Stitching):
    Gộp các cue bị ngắt giữa chừng (chưa kết thúc câu bằng dấu chấm/hỏi/chấm than)
    thành câu hoàn chỉnh trước khi dịch, giúp tránh tuyệt đối lỗi dịch word-by-word
    hoặc đứt cụm từ (VD: 'full | time day trader', 'a hundred | dollar account').
    """
    if not texts:
        return []

    cleaned = [re.sub(r"\s+", " ", str(t or "")).strip() for t in texts]
    groups = []
    curr_group = []

    for idx, t in enumerate(cleaned):
        if not t:
            if curr_group:
                groups.append(curr_group)
                curr_group = []
            groups.append([idx])
            continue

        curr_group.append(idx)
        # Kết thúc nhóm khi gặp dấu kết câu (. ! ?) hoặc đã gộp đủ 4 cue (~15-18s)
        is_sentence_end = bool(_SENTENCE_END_RE.search(t))
        total_chars = sum(len(cleaned[i]) for i in curr_group)
        if is_sentence_end or len(curr_group) >= 4 or total_chars >= 280:
            groups.append(curr_group)
            curr_group = []

    if curr_group:
        groups.append(curr_group)

    # Dịch từng câu hoàn chỉnh (hoặc gộp nhiều câu hoàn chỉnh vào 1 batch ngăn cách bởi dòng mới)
    group_full_texts = []
    for g in groups:
        if len(g) == 1 and not cleaned[g[0]]:
            group_full_texts.append("")
        else:
            joined_sentence = " ".join(cleaned[i] for i in g if cleaned[i])
            group_full_texts.append(_preprocess_source_text(joined_sentence, source_lang))

    # Dịch các câu hoàn chỉnh theo batch 10 câu (mỗi câu 1 dòng) để vừa nhanh vừa có ngữ cảnh đoạn văn
    translated_groups = []
    batch_size = 10
    for b_start in range(0, len(group_full_texts), batch_size):
        batch = group_full_texts[b_start:b_start + batch_size]
        non_empty_indices = [i for i, s in enumerate(batch) if s]
        if not non_empty_indices:
            translated_groups.extend(["" for _ in batch])
            continue

        batch_out = ["" for _ in batch]
        # Gửi cả đoạn gồm các câu hoàn chỉnh cách nhau bởi "\n" để bộ dịch hiểu toàn bộ ngữ cảnh
        paragraph = "\n".join(batch[i] for i in non_empty_indices)
        try:
            translated_para = _online_translate_single(paragraph, source_lang, target_lang)
            lines = [ln.strip() for ln in translated_para.splitlines() if ln.strip()]
            if len(lines) == len(non_empty_indices):
                for idx_in_batch, line in zip(non_empty_indices, lines):
                    batch_out[idx_in_batch] = line
                translated_groups.extend(batch_out)
                continue
        except Exception:
            pass

        # Nếu số dòng trả về khác số câu gửi đi, dịch từng câu hoàn chỉnh một
        for idx_in_batch in non_empty_indices:
            batch_out[idx_in_batch] = _online_translate_single(
                batch[idx_in_batch], source_lang, target_lang
            )
        translated_groups.extend(batch_out)

    # Phân bổ câu đã dịch về lại đúng từng mốc cue ban đầu
    results = ["" for _ in texts]
    for g, trans_full in zip(groups, translated_groups):
        if target_lang == "vi":
            trans_full = _polish_vietnamese_translation(trans_full)
        if len(g) == 1:
            results[g[0]] = trans_full
        else:
            source_pieces = [cleaned[i] for i in g]
            split_pieces = _split_translated_sentence_across_cues(trans_full, source_pieces)
            for cue_idx, piece in zip(g, split_pieces):
                results[cue_idx] = piece

    return results


def translate_texts(texts, source_language, target_language="vi"):
    """Dịch danh sách câu phụ đề từ `source_language` sang `target_language` bất kỳ.
    Ưu tiên dịch Neural có ngữ cảnh câu hoàn chỉnh (`_translate_with_sentence_context`)
    để câu văn tự nhiên, chuẩn xác; chỉ dùng Argos Translate offline khi mất mạng hoàn toàn.
    """
    source = _normalize_lang_code(source_language)
    target = _normalize_lang_code(target_language)

    if not texts:
        return []
    if not target or target == "original" or (source and source == target):
        return [str(t or "") for t in texts]

    # 1. Ưu tiên dịch Neural trực tuyến theo ngữ cảnh câu hoàn chỉnh (chất lượng cao hơn hẳn Argos)
    try:
        return _translate_with_sentence_context(texts, source, target)
    except Exception as online_exc:
        # 2. Fallback sang Argos Translate (offline) nếu máy đang mất kết nối Internet
        try:
            translation = _get_translation(source, target) if source else None
        except Exception:
            translation = None

        if translation is not None:
            translated = []
            for text in texts:
                text = str(text or "")
                if not text.strip():
                    translated.append(text)
                    continue
                result = translation.translate(text)
                if target == "vi" and result:
                    result = _polish_vietnamese_translation(result)
                translated.append(result if result else text)
            return translated

        raise RuntimeError(
            f"Không thể dịch phụ đề ({source or 'auto'} → {target}). "
            f"Hãy kiểm tra kết nối mạng: {online_exc}"
        ) from online_exc


def translate_texts_to_vietnamese(texts, source_language):
    """Hàm tương thích ngược: dịch sang tiếng Việt."""
    return translate_texts(texts, source_language, target_language="vi")


def _get_translation(source, target="vi"):
    import argostranslate.translate as argos_translate

    installed_languages = argos_translate.get_installed_languages()
    from_language = next((lang for lang in installed_languages if lang.code == source), None)
    to_language = next((lang for lang in installed_languages if lang.code == target), None)
    if from_language is None or to_language is None:
        return None
    return from_language.get_translation(to_language)


def is_language_installed(source, target="vi"):
    if source == target:
        return True
    try:
        return _get_translation(source, target) is not None
    except Exception:
        return False


def _install_package(available, source, target, progress_cb):
    model = next((p for p in available if p.from_code == source and p.to_code == target), None)
    if model is None:
        raise RuntimeError(f"Không có model Argos Translate cho {source}->{target}")
    if progress_cb:
        progress_cb(f"Đang tải và cài model {source}->{target}...")
    package_path = model.download()
    import argostranslate.package

    argostranslate.package.install_from_path(package_path)


def install_translation_model(source, target="vi", progress_cb=None):
    """Cài đặt gói ngôn ngữ Argos Translate cho cặp `source -> target` (bắc cầu qua `en` nếu cần)."""
    import argostranslate.package

    source = _normalize_lang_code(source)
    target = _normalize_lang_code(target or "vi")
    if not source or source == target:
        raise RuntimeError("Ngôn ngữ nguồn và ngôn ngữ đích phải khác nhau")

    if progress_cb:
        progress_cb("Đang tải danh sách model dịch...")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()

    if source != "en" and not is_language_installed(source, "en"):
        _install_package(available, source, "en", progress_cb)
    if target != "en" and not is_language_installed("en", target):
        _install_package(available, "en", target, progress_cb)

    if progress_cb:
        via = " (bắc cầu qua tiếng Anh)" if (source != "en" and target != "en") else ""
        progress_cb(f"Đã cài xong model dịch {source} → {target}{via}")

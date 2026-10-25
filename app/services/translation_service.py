# Argos Translate's package repo is hub-and-spoke around English: there is
# no direct <lang>->vi pair for anything except English itself. Every other
# language (Korean, Japanese, Chinese, ...) needs two installed packages —
# <lang>->en and en->vi — and argostranslate.translate.get_translation()
# automatically composites a <lang>->en->vi path through them, so the
# calling code below never needs to chain the two hops itself.
COMMON_SOURCE_LANGUAGES = [
    ("English", "en"), ("Hàn Quốc / Korean", "ko"), ("Nhật / Japanese", "ja"),
    ("Trung (giản thể) / Chinese", "zh"), ("Pháp / French", "fr"), ("Đức / German", "de"),
    ("Tây Ban Nha / Spanish", "es"), ("Nga / Russian", "ru"), ("Thái / Thai", "th"),
    ("Indonesia", "id"), ("Ý / Italian", "it"), ("Bồ Đào Nha / Portuguese", "pt"),
    ("Ả Rập / Arabic", "ar"), ("Hindi", "hi"), ("Thổ Nhĩ Kỳ / Turkish", "tr"),
    ("Ba Lan / Polish", "pl"), ("Hà Lan / Dutch", "nl"), ("Ukraina / Ukrainian", "uk"),
    ("Hy Lạp / Greek", "el"), ("Do Thái / Hebrew", "he"), ("Ba Tư / Persian", "fa"),
    ("Malaysia / Malay", "ms"), ("Thụy Điển / Swedish", "sv"), ("Séc / Czech", "cs"),
    ("Khác (nhập mã ISO-639-1)...", ""),
]


def translate_texts_to_vietnamese(texts, source_language):
    """Translates a segment's subtitle cues to Vietnamese using the local
    Argos Translate model, in-process (the Node backend shelled out to
    scripts/translate.py; here we're already Python, so call the library
    directly).
    """
    source = str(source_language or "").split("-")[0].split("_")[0].lower()
    if not source or source == "vi":
        return list(texts)
    if not texts:
        return []

    translation = _get_translation(source, "vi")
    if translation is None:
        raise RuntimeError(
            f"Argos Translate chưa có model dịch {source}->vi (hoặc {source}->en->vi). "
            "Cài trong Cài đặt trước khi dịch phụ đề."
        )

    translated = []
    for text in texts:
        text = str(text or "")
        if not text.strip():
            translated.append(text)
            continue
        result = translation.translate(text)
        if not result:
            raise RuntimeError("Argos Translate trả về kết quả rỗng cho một câu phụ đề")
        translated.append(result)
    return translated


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
    return _get_translation(source, target) is not None


def _install_package(available, source, target, progress_cb):
    model = next((p for p in available if p.from_code == source and p.to_code == target), None)
    if model is None:
        raise RuntimeError(f"Không có model Argos Translate cho {source}->{target}")
    if progress_cb:
        progress_cb(f"Đang tải và cài model {source}->{target}...")
    package_path = model.download()
    import argostranslate.package

    argostranslate.package.install_from_path(package_path)


def install_translation_model(source, progress_cb=None):
    """Installs whatever Argos Translate package(s) are needed so that
    `source` can be translated to Vietnamese — a direct `source->vi` package
    if Argos has one (only English does), otherwise `source->en` plus
    `en->vi` so translation can pivot through English. Needs internet
    access once; after that, translation runs fully offline.
    """
    import argostranslate.package

    source = source.strip().lower()
    if source in ("", "vi"):
        raise RuntimeError("Chọn một ngôn ngữ nguồn khác tiếng Việt")

    if progress_cb:
        progress_cb("Đang tải danh sách model dịch...")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()

    if source != "en" and not is_language_installed(source, "en"):
        _install_package(available, source, "en", progress_cb)
    if not is_language_installed("en", "vi"):
        _install_package(available, "en", "vi", progress_cb)

    if progress_cb:
        via = " (bắc cầu qua tiếng Anh)" if source != "en" else ""
        progress_cb(f"Đã cài xong model dịch {source} → vi{via}")

import os
import time
from urllib.parse import urlparse

import requests


class ResonaError(Exception):
    def __init__(self, message, status=None, http_status=None):
        super().__init__(message)
        self.status = status
        self.http_status = http_status


class MissingApiKeyError(ResonaError):
    def __init__(self, message="RESONA_API_KEY chưa được cấu hình trong Cài đặt"):
        super().__init__(message, status="missing_api_key")


def _require_key(api_key):
    if not api_key:
        raise MissingApiKeyError()


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}"}


def _raise_for_response(resp):
    if resp.ok:
        return
    detail = None
    try:
        data = resp.json()
        detail = data.get("detail")
    except ValueError:
        pass
    if detail:
        raise ResonaError(
            detail.get("message", "Resona API error"),
            status=detail.get("status"),
            http_status=resp.status_code,
        )
    raise ResonaError(f"Resona API error: HTTP {resp.status_code}", http_status=resp.status_code)


def list_voices(api_base, api_key, params=None, timeout=120):
    _require_key(api_key)
    resp = requests.get(
        f"{api_base}/api/v2/voices", headers=_headers(api_key), params=params or {}, timeout=timeout
    )
    _raise_for_response(resp)
    return resp.json()


def text_to_speech(api_base, api_key, text, voice_id, language="vi", timeout=120):
    _require_key(api_key)
    resp = requests.post(
        f"{api_base}/api/v2/text-to-speech",
        headers=_headers(api_key),
        json={"text": text, "voice_id": voice_id, "language": language},
        timeout=timeout,
    )
    _raise_for_response(resp)
    return resp.json()


def get_text_to_speech_result(api_base, api_key, request_id, timeout=120):
    _require_key(api_key)
    resp = requests.get(
        f"{api_base}/api/v2/text-to-speech/{request_id}", headers=_headers(api_key), timeout=timeout
    )
    _raise_for_response(resp)
    return resp.json()


def poll_until_done(fetch_fn, interval_s=3, timeout_s=600):
    """Polls a text-to-speech result endpoint until it leaves "processing",
    or raises after timeout_s.
    """
    deadline = time.time() + timeout_s
    while True:
        result = fetch_fn()
        if result.get("status") != "processing":
            return result
        if time.time() > deadline:
            raise ResonaError("Timed out waiting for Resona TTS result", status="poll_timeout")
        time.sleep(interval_s)


def download_to_file(url, dest_path, timeout=60):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    resp = requests.get(url, stream=True, timeout=timeout)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
    return dest_path


def url_extension(url, default=".mp3"):
    ext = os.path.splitext(urlparse(url).path)[1]
    return ext or default

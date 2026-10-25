import re

_TIMESTAMP_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$")
_TIME_LINE_RE = re.compile(r"^(\S+) --> (\S+)$")


def _format_milliseconds(ms):
    total = max(0, round(ms))
    hours = total // 3600000
    minutes = (total % 3600000) // 60000
    seconds = (total % 60000) // 1000
    millis = total % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _format_timestamp(total_seconds):
    ms = max(0, round(total_seconds * 1000))
    return _format_milliseconds(ms)


def _parse_timestamp(value):
    match = _TIMESTAMP_RE.match(value)
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    h, m, s, ms = (int(g) for g in match.groups())
    return h * 3600000 + m * 60000 + s * 1000 + ms


def segments_to_srt(segments):
    """Builds an SRT file body from whisper-style segments: [{start, end, text}]."""
    blocks = []
    for i, seg in enumerate(segments):
        index = i + 1
        start = _format_timestamp(seg["start"])
        end = _format_timestamp(seg["end"])
        blocks.append(f"{index}\n{start} --> {end}\n{seg['text']}\n")
    return "\n".join(blocks)


def segments_to_plain_text(segments):
    text = " ".join(s["text"] for s in segments)
    return re.sub(r"\s+", " ", text).strip()


def parse_srt_cues(content):
    blocks = [b for b in re.split(r"\r?\n\s*\r?\n", content.strip()) if b]
    cues = []
    for block in blocks:
        lines = block.split("\n")
        lines = [line.rstrip("\r") for line in lines]
        time_line_index = next((i for i, line in enumerate(lines) if " --> " in line), -1)
        if time_line_index < 0:
            continue
        match = _TIME_LINE_RE.match(lines[time_line_index].strip())
        if not match:
            continue
        text = "\n".join(lines[time_line_index + 1:]).strip()
        if not text:
            continue
        cues.append({
            "start": _parse_timestamp(match.group(1)) / 1000,
            "end": _parse_timestamp(match.group(2)) / 1000,
            "text": text,
        })
    return cues


def combine_srt_files(parts):
    """Joins per-video-chunk SRTs into one timeline and renumbers every cue.

    `parts` is a list of {"content": str, "offsetSeconds": float}.
    """
    cue_index = 1
    cues = []
    for part in parts:
        content = part["content"]
        offset_ms = round(part.get("offsetSeconds", 0) * 1000)
        blocks = [b for b in re.split(r"\r?\n\s*\r?\n", content.strip()) if b]
        for block in blocks:
            lines = [line.rstrip("\r") for line in block.split("\n")]
            time_line_index = next((i for i, line in enumerate(lines) if " --> " in line), -1)
            if time_line_index < 0:
                continue
            match = _TIME_LINE_RE.match(lines[time_line_index].strip())
            if not match:
                continue
            lines[0] = str(cue_index)
            cue_index += 1
            start_ms = _parse_timestamp(match.group(1)) + offset_ms
            end_ms = _parse_timestamp(match.group(2)) + offset_ms
            lines[time_line_index] = f"{_format_milliseconds(start_ms)} --> {_format_milliseconds(end_ms)}"
            cues.append("\n".join(lines))
    return f"{chr(10).join(cues)}\n" if cues else ""

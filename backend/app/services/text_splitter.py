import re

# Resona charges "voice credits" ~= 1 per character after stripping whitespace,
# and rejects requests outside [50, 2000] credits (section_credit_limit_exceeded).
# A single transcript from a ~4-minute video segment easily blows past 2000, so
# long text must be split into smaller requests before calling text-to-speech.

_WHITESPACE_RE = re.compile(r"\s")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def text_credits(text):
    return len(_WHITESPACE_RE.sub("", text))


def _split_long_sentence(sentence, max_credits):
    words = sentence.split()
    chunks = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if current and text_credits(candidate) > max_credits:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def split_text_for_tts(text, max_credits=1800, min_credits=50):
    """Splits text into chunks that each fit Resona's per-request credit window,
    breaking at sentence boundaries where possible so the voice doesn't cut
    mid-sentence. Returns the chunks in order; each is safe to send as-is to
    POST /api/v2/text-to-speech.
    """
    trimmed = text.strip()
    if not trimmed:
        return []
    if text_credits(trimmed) <= max_credits:
        return [trimmed]

    sentences = []
    for s in _SENTENCE_SPLIT_RE.split(trimmed):
        if text_credits(s) > max_credits:
            sentences.extend(_split_long_sentence(s, max_credits))
        else:
            sentences.append(s)

    chunks = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}" if current else sentence
        if current and text_credits(candidate) > max_credits:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)

    # Merge a too-small trailing chunk into its neighbor so every request
    # clears Resona's 50-credit minimum.
    while len(chunks) > 1 and text_credits(chunks[-1]) < min_credits:
        last = chunks.pop()
        merged = f"{chunks[-1]} {last}"
        if text_credits(merged) <= 2000:
            chunks[-1] = merged
        else:
            chunks.append(last)  # can't merge without busting the hard cap — leave as-is
            break

    return chunks


def group_cues_for_tts(cues, min_credits=50, max_credits=1800, max_gap_sec=0.55):
    """Groups consecutive SRT cues ({start, end, text}) ONLY when they are
    adjacent in time (gap <= max_gap_sec) and under `min_credits`.

    IMPORTANT FIX: Never merge two cues separated by a silence gap > max_gap_sec!
    Previously, merging two cues separated by several seconds of silence caused
    the combined cue duration (`end - start`) to span the entire silence gap,
    which made `create_timed_audio_track` time-stretch (`atempo < 0.5`) the voice
    into a slow, buzzing, distorted drone ("rè rề").
    """
    groups = []
    current = None

    for cue in cues:
        cue_text = str(cue.get("text") or "").strip()
        if not cue_text:
            continue

        if current is None:
            current = {"start": cue["start"], "end": cue["end"], "text": cue_text}
            continue

        gap = float(cue["start"]) - float(current["end"])
        merged_text = f"{current['text']} {cue_text}"
        current_under_min = text_credits(current["text"]) < min_credits
        if gap <= max_gap_sec and current_under_min and text_credits(merged_text) <= max_credits:
            current = {"start": current["start"], "end": cue["end"], "text": merged_text}
        else:
            groups.append(current)
            current = {"start": cue["start"], "end": cue["end"], "text": cue_text}
    if current is not None:
        groups.append(current)

    # Fold trailing short group backwards ONLY if the gap to the previous group is small.
    while len(groups) > 1 and text_credits(groups[-1]["text"]) < min_credits:
        last = groups[-1]
        prev = groups[-2]
        gap = float(last["start"]) - float(prev["end"])
        merged_text = f"{prev['text']} {last['text']}"
        if gap <= max_gap_sec and text_credits(merged_text) <= max_credits:
            groups.pop()
            groups[-1] = {"start": prev["start"], "end": last["end"], "text": merged_text}
        else:
            break

    return groups

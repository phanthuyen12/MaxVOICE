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


def group_cues_for_tts(cues, min_credits=50, max_credits=1800):
    """Groups consecutive SRT cues ({start, end, text}) so each group's text
    clears Resona's 50-credit minimum before it's sent as its own TTS request.
    Individual subtitle cues (a few words, a few seconds) routinely fall under
    that floor on their own, which Resona rejects with section_credit_limit_exceeded.
    A merged group spans from its first cue's start to its last cue's end, so the
    caller can still place the resulting single audio take on the timeline.
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

        merged_text = f"{current['text']} {cue_text}"
        current_under_min = text_credits(current["text"]) < min_credits
        if current_under_min and text_credits(merged_text) <= max_credits:
            current = {"start": current["start"], "end": cue["end"], "text": merged_text}
        else:
            groups.append(current)
            current = {"start": cue["start"], "end": cue["end"], "text": cue_text}
    if current is not None:
        groups.append(current)

    # A trailing group can still end up under the floor (e.g. the last cue is
    # short and on its own) — fold it backwards into its neighbor.
    while len(groups) > 1 and text_credits(groups[-1]["text"]) < min_credits:
        last = groups.pop()
        prev = groups[-1]
        merged_text = f"{prev['text']} {last['text']}"
        if text_credits(merged_text) <= max_credits:
            groups[-1] = {"start": prev["start"], "end": last["end"], "text": merged_text}
        else:
            groups.append(last)  # can't merge without busting the hard cap — leave as-is
            break

    return groups

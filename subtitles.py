"""Sentence subtitles built from Whisper's word timings on the original audio."""

from dataclasses import dataclass
from functools import lru_cache
import os
import re
from typing import Callable, Iterable


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


SENTENCE_END = re.compile(r'''[.!?؟…。]+["'»”’\)\]\}]*$''')


def sentence_cues(segments: Iterable) -> Iterable[SubtitleCue]:
    """Keep sentences across model segments; split unpunctuated speech at pauses.

    Long sentences are also split at word boundaries for readable subtitles.
    All boundaries use word timings, never a proportion of the text length.
    """
    parts = []
    start = end = 0.0
    for segment in segments:
        if not segment.words and segment.text.strip():
            raise ValueError("زمان‌بندی واژه‌ها دریافت نشد؛ تولید زیرنویس جمله‌ای ممکن نیست.")
        for word in segment.words or []:
            if not word.word.strip():
                continue
            if parts and (
                word.start - end >= 1.0
                or word.end - start > 12.0
                or len("".join(parts)) + len(word.word) > 140
            ):
                yield SubtitleCue(start, end, "".join(parts).strip())
                parts = []
            if not parts:
                start = word.start
            parts.append(word.word)
            end = word.end
            if SENTENCE_END.search(word.word.strip()):
                yield SubtitleCue(start, end, "".join(parts).strip())
                parts = []
    if parts:
        yield SubtitleCue(start, end, "".join(parts).strip())


def format_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def render_srt(cues: Iterable[SubtitleCue]) -> str:
    blocks = []
    previous_end = 0
    for cue in cues:
        text = re.sub(r"\s+", " ", cue.text).strip()
        start = max(previous_end, round(cue.start * 1000), 0)
        end = round(cue.end * 1000)
        if not text or end <= start:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n"
            f"{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n\n"
        )
        previous_end = end
    return "".join(blocks)


@lru_cache(maxsize=1)
def get_whisper_model():
    # TXT conversion remains usable without the optional subtitle dependencies.
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "برای خروجی SRT در ویندوز، ابتدا setup_srt.bat و سپس run_srt.bat را اجرا کنید. "
            "در لینوکس و مک، وابستگی‌ها را با دستور "
            "python3.13 -m pip install -r requirements-srt.txt نصب کنید."
        ) from exc
    return WhisperModel(
        os.environ.get("WHISPER_MODEL", "small"),
        device="cpu",
        compute_type="int8",
        download_root=os.environ.get("WHISPER_DOWNLOAD_ROOT"),
    )


def transcribe_srt(
    audio_path: str,
    language: str = "fa-IR",
    on_progress: Callable[[float], None] | None = None,
) -> str:
    model = get_whisper_model()
    segments, info = model.transcribe(
        audio_path,
        language=language.split("-")[0],
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        append_punctuations="\"'.。,，!！?？:：”)]}、،؛؟…»",
    )

    def track_segments():
        for segment in segments:
            if on_progress and info.duration > 0:
                on_progress(min(1.0, max(0.0, segment.end / info.duration)))
            yield segment

    output = render_srt(sentence_cues(track_segments()))
    if not output:
        raise ValueError("گفتار قابل تشخیصی برای ساخت زیرنویس در فایل پیدا نشد.")
    return output

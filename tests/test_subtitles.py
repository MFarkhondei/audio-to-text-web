import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import subtitles
from subtitles import SubtitleCue, render_srt, sentence_cues, transcribe_srt


def segment(*words):
    timed_words = [SimpleNamespace(start=start, end=end, word=text) for start, end, text in words]
    return SimpleNamespace(
        start=words[0][0], end=words[-1][1],
        text="".join(word.word for word in timed_words), words=timed_words,
    )


class SentenceSubtitleTests(unittest.TestCase):
    def test_persian_sentences_use_their_first_and_last_word_times(self):
        cues = list(sentence_cues([segment(
            (1.2, 1.7, " سلام"), (1.8, 2.4, " دنیا."),
            (3.0, 3.6, " حالتان"), (3.7, 4.2, " چطور است؟"),
        )]))
        self.assertEqual(cues, [
            SubtitleCue(1.2, 2.4, "سلام دنیا."),
            SubtitleCue(3.0, 4.2, "حالتان چطور است؟"),
        ])
        self.assertEqual(render_srt(cues),
            "1\n00:00:01,200 --> 00:00:02,400\nسلام دنیا.\n\n"
            "2\n00:00:03,000 --> 00:00:04,200\nحالتان چطور است؟\n\n")

    def test_sentence_continues_across_model_segments(self):
        cues = list(sentence_cues([
            segment((29.0, 29.8, " جمله")),
            segment((30.1, 31.2, " ادامه دارد.")),
        ]))
        self.assertEqual(cues, [SubtitleCue(29.0, 31.2, "جمله ادامه دارد.")])

    def test_unpunctuated_speech_splits_at_a_pause_and_keeps_the_last_words(self):
        cues = list(sentence_cues([segment(
            (0.5, 1.5, " بخش اول"), (3.0, 4.0, " بخش دوم"),
        )]))
        self.assertEqual(cues, [SubtitleCue(0.5, 1.5, "بخش اول"), SubtitleCue(3, 4, "بخش دوم")])

    def test_closing_quotes_and_arabic_question_mark_end_sentences(self):
        cues = list(sentence_cues([segment(
            (1, 2, " «سلام!»"), (2.1, 3, " كيف حالك؟"),
        )]))
        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[1].text, "كيف حالك؟")

    def test_long_speech_is_split_at_word_boundaries_without_losing_words(self):
        words = [(i, i + 0.8, f" word{i}") for i in range(30)]
        cues = list(sentence_cues([segment(*words)]))
        self.assertGreater(len(cues), 1)
        self.assertTrue(all(cue.end - cue.start <= 12 for cue in cues))
        self.assertEqual(" ".join(cue.text for cue in cues).split(), [f"word{i}" for i in range(30)])

    def test_missing_word_times_are_reported_instead_of_fabricated(self):
        with self.assertRaisesRegex(ValueError, "زمان‌بندی واژه‌ها"):
            list(sentence_cues([SimpleNamespace(start=0, end=10, text="دو جمله. جمله دوم.", words=None)]))

    def test_timestamp_rounding_carries_milliseconds_into_minutes_and_hours(self):
        self.assertEqual(render_srt([SubtitleCue(59.9996, 3600.001, "Test.")]),
                         "1\n00:01:00,000 --> 01:00:00,001\nTest.\n\n")

    def test_invalid_cues_are_skipped_and_output_has_no_overlaps_or_blank_text_lines(self):
        output = render_srt([
            SubtitleCue(-1, 1, "First.\n\nSentence."),
            SubtitleCue(0.5, 2, "Second."),
            SubtitleCue(2, 2, "Zero duration."),
            SubtitleCue(3, 4, "  "),
        ])
        self.assertEqual(output,
            "1\n00:00:00,000 --> 00:00:01,000\nFirst. Sentence.\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\nSecond.\n\n")

    def test_transcription_requests_word_times_and_preserves_original_offsets(self):
        model = Mock()
        model.transcribe.return_value = (
            iter([segment((35.2, 36.7, " سلام.")), segment((70.1, 72, " خداحافظ."))]),
            SimpleNamespace(duration=80),
        )
        progress = []
        with patch.object(subtitles, "get_whisper_model", return_value=model):
            result = transcribe_srt("input.mp3", "fa-IR", on_progress=progress.append)
        self.assertIn("00:00:35,200 --> 00:00:36,700", result)
        self.assertIn("00:01:10,100 --> 00:01:12,000", result)
        options = model.transcribe.call_args.kwargs
        self.assertEqual(model.transcribe.call_args.args, ("input.mp3",))
        self.assertEqual(options["language"], "fa")
        self.assertTrue(options["word_timestamps"])
        self.assertTrue(options["vad_filter"])
        self.assertEqual(progress, [36.7 / 80, 72 / 80])

    def test_silent_audio_reports_no_recognized_speech(self):
        model = Mock()
        model.transcribe.return_value = (iter([]), SimpleNamespace(duration=10))
        with patch.object(subtitles, "get_whisper_model", return_value=model):
            with self.assertRaisesRegex(ValueError, "گفتار قابل تشخیصی"):
                transcribe_srt("silent.wav")

    def test_missing_dependency_has_installation_instructions(self):
        subtitles.get_whisper_model.cache_clear()
        self.addCleanup(subtitles.get_whisper_model.cache_clear)
        with patch.dict("sys.modules", {"faster_whisper": None}):
            with self.assertRaisesRegex(RuntimeError, "requirements-srt.txt"):
                subtitles.get_whisper_model()


if __name__ == "__main__":
    unittest.main()

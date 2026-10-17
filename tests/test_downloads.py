import io
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote
from uuid import uuid4

import app as audio_app


class DownloadLinks(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("class") == "transcript-download":
            self.links.append((attrs["href"], attrs["download"]))


class TranscriptDownloadTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(storage.cleanup)
        for patcher in (
            patch.object(audio_app, "UPLOAD_FOLDER", storage.name),
            patch.dict(audio_app.app.config, TESTING=True),
            patch.object(audio_app.shutil, "which", side_effect=lambda tool: f"{tool}.exe"),
            patch.object(audio_app, "split_audio", side_effect=lambda path, directory: [path]),
            patch.object(
                audio_app, "transcribe_segments",
                side_effect=lambda segments, language, **kwargs: Path(segments[0]).read_text(encoding="utf-8"),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = audio_app.app.test_client()

    def upload(self, files, **fields):
        response = self.client.post("/", data={
            "output_format": "txt",
            "audio": [(io.BytesIO(text.encode("utf-8")), name) for name, text in files],
            **fields,
        })
        self.addCleanup(response.close)
        return response

    def download(self, url):
        response = self.client.get(url)
        self.addCleanup(response.close)
        return response

    def test_multiple_files_have_independent_named_downloads(self):
        response = self.upload([
            ("جلسه اول.mp3", "متن جلسه اول"),
            ("second.wav", "Second transcript"),
        ], language="en-US")
        self.assertEqual(response.status_code, 200)
        links = DownloadLinks(response.get_data(as_text=True)).links
        self.assertEqual([name for _, name in links], ["جلسه اول.txt", "second.txt"])
        for (url, filename), expected in zip(links, ["متن جلسه اول", "Second transcript"]):
            result = self.download(url)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_data(as_text=True), expected)
            self.assertIn("attachment", result.headers["Content-Disposition"])
            self.assertIn(filename, unquote(result.headers["Content-Disposition"]))
        self.assertEqual(audio_app.transcribe_segments.call_args.args[1], "en-US")
        self.assertEqual(self.client.get("/progress").json["percent"], 100)

    def test_duplicate_names_and_later_uploads_do_not_overwrite_results(self):
        first = self.upload([("same.mp3", "first"), ("same.mp3", "second")])
        first_links = DownloadLinks(first.get_data(as_text=True)).links
        second = self.upload([("same.wav", "third"), ("same.mp3", "fourth")])
        second_links = DownloadLinks(second.get_data(as_text=True)).links
        links = first_links + second_links
        self.assertEqual(len({url for url, _ in links}), 4)
        self.assertEqual([name for _, name in links], ["same.txt"] * 4)
        self.assertEqual(
            [self.download(url).get_data(as_text=True) for url, _ in links],
            ["first", "second", "third", "fourth"],
        )

    def test_single_file_still_downloads_directly(self):
        response = self.upload([("voice.m4a", "فقط متن همین فایل")])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "فقط متن همین فایل")
        self.assertIn("voice.txt", response.headers["Content-Disposition"])
        self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_drive_file_still_downloads_directly(self):
        def save_drive_file(file_id, destination):
            Path(destination).write_text("متن فایل درایو", encoding="utf-8")
            return True

        with patch.object(audio_app, "download_drive_file", side_effect=save_drive_file):
            response = self.upload([], drive_url="https://drive.google.com/file/d/example_id/view")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "متن فایل درایو")
        self.assertIn("example_id.txt", response.headers["Content-Disposition"])

    def test_uploaded_paths_are_reduced_to_a_filename(self):
        response = self.upload([("../../escaped.wav", "first"), (r"C:\\fakepath\\voice.mp3", "second")])
        links = DownloadLinks(response.get_data(as_text=True)).links
        self.assertEqual([name for _, name in links], ["escaped.txt", "voice.txt"])
        self.assertEqual([self.download(url).get_data(as_text=True) for url, _ in links], ["first", "second"])
        self.assertFalse((Path(audio_app.UPLOAD_FOLDER) / "escaped.wav").exists())

    def test_missing_downloads_and_path_traversal_return_not_found(self):
        result_id = uuid4()
        self.assertEqual(self.download(f"/download/{result_id}/missing.txt").status_code, 404)
        self.assertEqual(self.download(f"/download/{result_id}/../../app.py").status_code, 404)

    def test_empty_upload_redirects_with_a_message(self):
        response = self.upload([])
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("لطفاً یک یا چند فایل", page)

    def test_processing_failure_redirects_with_an_error(self):
        with patch.object(audio_app, "split_audio", side_effect=RuntimeError("test failure")):
            response = self.upload([("broken.wav", "audio")])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/progress").json["status"], "خطا")
        self.assertEqual(list(Path(audio_app.UPLOAD_FOLDER).iterdir()), [])

    def test_multiple_srt_downloads_are_independent_and_report_overall_progress(self):
        updates = []

        def make_subtitles(path, language, on_progress):
            for fraction in (0.5, 1.0):
                on_progress(fraction)
                updates.append(audio_app.progress_data["percent"])
            text = Path(path).read_text(encoding="utf-8")
            return f"1\n00:00:01,200 --> 00:00:03,400\n{text}\n\n"

        with patch.object(audio_app, "transcribe_srt", side_effect=make_subtitles):
            response = self.upload([
                ("جلسه.mp3", "جمله اول."), ("جلسه.wav", "جمله دوم؟"),
            ], output_format="srt")
        self.assertEqual(response.status_code, 200)
        links = DownloadLinks(response.get_data(as_text=True)).links
        self.assertEqual([name for _, name in links], ["جلسه.srt", "جلسه.srt"])
        self.assertNotEqual(links[0][0], links[1][0])
        for (url, filename), text in zip(links, ["جمله اول.", "جمله دوم؟"]):
            result = self.download(url)
            self.assertEqual(result.get_data(as_text=True), f"1\n00:00:01,200 --> 00:00:03,400\n{text}\n\n")
            self.assertIn(filename, unquote(result.headers["Content-Disposition"]))
        self.assertEqual(updates, [25, 50, 75, 99])
        self.assertEqual(self.client.get("/progress").json["percent"], 100)
        audio_app.split_audio.assert_not_called()
        audio_app.transcribe_segments.assert_not_called()

    def test_txt_is_the_default_when_no_format_is_submitted(self):
        response = self.client.post("/", data={"audio": (io.BytesIO("سلام.".encode("utf-8")), "voice.mp3")})
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "سلام.")
        self.assertIn("voice.txt", response.headers["Content-Disposition"])

    def test_srt_downloads_directly_when_explicitly_selected(self):
        srt = "1\n00:00:02,000 --> 00:00:04,000\nسلام.\n\n"
        with patch.object(audio_app, "transcribe_srt", return_value=srt):
            response = self.upload([("voice.mp3", "audio")], output_format="srt")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), srt)
        self.assertIn("voice.srt", response.headers["Content-Disposition"])

    def test_invalid_format_is_rejected(self):
        response = self.upload([("voice.wav", "audio")], output_format="../other")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(Path(audio_app.UPLOAD_FOLDER).iterdir()), [])

    def test_missing_ffmpeg_shows_installation_guidance_and_stops_processing(self):
        with patch.object(audio_app.shutil, "which", return_value=None), patch.object(audio_app.platform, "system", return_value="Windows"):
            response = self.upload([("voice.mp3", "audio")])
        self.assertEqual(response.status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("ffmpeg و ffprobe نصب نیست", page)
        self.assertIn("winget install --id Gyan.FFmpeg -e", page)
        self.assertIn("PATH", page)
        self.assertIn("برنامه را دوباره اجرا کنید", page)
        self.assertEqual(self.client.get("/progress").json["status"], "خطا")
        audio_app.split_audio.assert_not_called()
        audio_app.transcribe_segments.assert_not_called()
        self.assertEqual(list(Path(audio_app.UPLOAD_FOLDER).iterdir()), [])

    def test_missing_ffprobe_is_reported_even_when_ffmpeg_exists(self):
        with patch.object(audio_app.shutil, "which", side_effect=lambda tool: "ffmpeg.exe" if tool == "ffmpeg" else None):
            response = self.upload([("voice.mp3", "audio")])
        self.assertEqual(response.status_code, 302)
        self.assertIn("ffprobe نصب نیست", self.client.get("/").get_data(as_text=True))
        audio_app.split_audio.assert_not_called()

    def test_srt_does_not_require_system_ffmpeg(self):
        srt = "1\n00:00:01,000 --> 00:00:02,000\nسلام.\n\n"
        with patch.object(audio_app.shutil, "which", return_value=None), patch.object(audio_app, "transcribe_srt", return_value=srt):
            response = self.upload([("voice.mp3", "audio")], output_format="srt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), srt)

    def test_old_install_route_cannot_install_software(self):
        with patch.object(audio_app.subprocess, "run") as run:
            response = self.client.post("/install-ffmpeg")
        self.assertEqual(response.status_code, 404)
        run.assert_not_called()

    def test_srt_model_error_is_shown_and_no_empty_output_is_saved(self):
        with patch.object(audio_app, "transcribe_srt", side_effect=RuntimeError("model unavailable")):
            response = self.upload([("voice.wav", "audio")], output_format="srt")
        self.assertEqual(response.status_code, 302)
        self.assertIn("model unavailable", self.client.get("/").get_data(as_text=True))
        self.assertEqual(list(Path(audio_app.UPLOAD_FOLDER).iterdir()), [])


if __name__ == "__main__":
    unittest.main()

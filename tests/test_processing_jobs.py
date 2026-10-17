import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import app
import processing_jobs as jobs


class ProcessingJobTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(storage.cleanup)
        self.root = Path(storage.name)
        for patcher in (
            patch.object(app, "UPLOAD_FOLDER", storage.name),
            patch.dict(app.app.config, TESTING=True),
            patch.dict(jobs._jobs, {}, clear=True),
            patch.object(app.shutil, "which", side_effect=lambda tool: tool),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = app.app.test_client()

    def upload(self, job_id, **fields):
        return self.client.post(f"/jobs/{job_id}", data={
            "audio": [(io.BytesIO(b"first"), "same.mp3"), (io.BytesIO(b"second"), "same.wav")],
            **fields,
        })

    def test_new_route_returns_independent_named_downloads_and_default_txt(self):
        job_id = uuid4()

        def inline_worker(job, spec):
            self.assertEqual(spec["output_format"], "txt")
            (job.directory / "request.json").write_text(json.dumps(spec), encoding="utf-8")
            jobs.run_worker(job.directory, app.process_files, app.download_drive_file)

        with patch.object(jobs, "launch_job", side_effect=inline_worker), \
             patch.object(app, "split_audio", side_effect=lambda path, directory: [path]), \
             patch.object(app, "transcribe_segments", side_effect=lambda paths, language, **kwargs: Path(paths[0]).read_text()):
            response = self.upload(job_id)
        self.assertEqual(response.status_code, 202)
        state = self.client.get(f"/jobs/{job_id}").json
        self.assertEqual(state["state"], "completed")
        self.assertEqual(state["percent"], 100)
        self.assertEqual([result["filename"] for result in state["results"]], ["same.txt", "same.txt"])
        for result, expected in zip(state["results"], ["first", "second"]):
            download = self.client.get(result["url"])
            self.assertEqual(download.get_data(as_text=True), expected)
            self.assertIn("attachment", download.headers["Content-Disposition"])
            download.close()
        self.assertFalse((self.root / "jobs" / str(job_id) / "inputs").exists())

    def test_stop_before_upload_prevents_late_request_from_starting(self):
        job_id = uuid4()
        stopped = self.client.post(f"/jobs/{job_id}/cancel")
        self.assertEqual(stopped.json["state"], "cancelled")
        with patch.object(jobs, "launch_job") as launch:
            response = self.upload(job_id)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["state"], "cancelled")
        launch.assert_not_called()
        self.assertFalse((self.root / "jobs" / str(job_id)).exists())

    def test_stop_between_upload_and_launch_cleans_inputs_and_prevents_spawn(self):
        job_id = uuid4()
        original_launch = jobs.launch_job

        def stop_before_launch(job, spec):
            self.client.post(f"/jobs/{job_id}/cancel")
            original_launch(job, spec)

        with patch.object(jobs, "launch_job", side_effect=stop_before_launch), patch.object(jobs.subprocess, "Popen") as spawn:
            response = self.upload(job_id)
        self.assertEqual(response.json["state"], "cancelled")
        spawn.assert_not_called()
        self.assertFalse((self.root / "jobs" / str(job_id)).exists())

    def test_job_progress_is_isolated_from_other_jobs_and_legacy_progress(self):
        first, second = uuid4(), uuid4()
        for job_id, percent in [(first, 20), (second, 70)]:
            job = jobs.begin_job(job_id, self.root)
            jobs.write_status(job.directory, {"state": "processing", "percent": percent, "status": "processing", "results": []})
        app.progress_data["percent"] = 100
        self.assertEqual(self.client.get(f"/jobs/{first}").json["percent"], 20)
        self.assertEqual(self.client.get(f"/jobs/{second}").json["percent"], 70)
        self.client.post(f"/jobs/{first}/cancel")
        self.assertEqual(self.client.get(f"/jobs/{second}").json["state"], "processing")

    def test_missing_tools_returns_json_guidance_without_launching(self):
        with patch.object(app.shutil, "which", return_value=None), patch.object(jobs, "launch_job") as launch:
            response = self.upload(uuid4())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["state"], "error")
        self.assertIn("ffmpeg", response.json["status"])
        self.assertIn("PATH", response.json["status"])
        launch.assert_not_called()

    def test_cancel_failure_is_not_reported_as_a_success(self):
        with patch.object(jobs, "cancel_job", side_effect=RuntimeError("cannot stop")):
            response = self.client.post(f"/jobs/{uuid4()}/cancel")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json["state"], "processing")

    def test_worker_error_removes_partial_outputs(self):
        job_id = uuid4()
        job = jobs.begin_job(job_id, self.root)
        (job.directory / "request.json").write_text(json.dumps({
            "files": [], "drive_id": None, "language": "fa-IR", "output_format": "srt",
        }), encoding="utf-8")

        def fail_after_output(*args, **kwargs):
            output = job.directory / str(uuid4())
            output.mkdir()
            (output / "partial.srt").write_text("partial")
            raise RuntimeError("model error")

        jobs.run_worker(job.directory, fail_after_output, Mock())
        state = self.client.get(f"/jobs/{job_id}").json
        self.assertEqual(state["state"], "error")
        self.assertIn("model error", state["status"])
        self.assertEqual(state["results"], [])
        self.assertFalse(any(path.is_dir() for path in job.directory.iterdir()))

    def test_each_file_is_downloadable_while_next_file_is_processing(self):
        for output_format in ("txt", "srt"):
            with self.subTest(output_format=output_format):
                job_id = uuid4()
                calls = []

                def transcribe(path, language, on_progress=None):
                    state = self.client.get(f"/jobs/{job_id}").json
                    self.assertEqual(state["state"], "processing")
                    self.assertEqual(len(state["results"]), len(calls))
                    if calls:
                        download = self.client.get(state["results"][0]["url"])
                        self.assertEqual(download.get_data(as_text=True), "first")
                        download.close()
                    calls.append(path)
                    on_progress(0.5)
                    self.assertEqual(len(self.client.get(f"/jobs/{job_id}").json["results"]), len(calls) - 1)
                    return Path(path).read_text()

                def inline_worker(job, spec):
                    (job.directory / "request.json").write_text(json.dumps(spec), encoding="utf-8")
                    jobs.run_worker(job.directory, app.process_files, Mock())

                with patch.object(jobs, "launch_job", side_effect=inline_worker), \
                     patch.object(app, "split_audio", side_effect=lambda path, directory: [path]), \
                     patch.object(app, "transcribe_srt", side_effect=transcribe), \
                     patch.object(app, "transcribe_segments", side_effect=lambda paths, language, **kw: transcribe(paths[0], language, **kw)):
                    self.upload(job_id, output_format=output_format)
                state = self.client.get(f"/jobs/{job_id}").json
                self.assertEqual(state["state"], "completed")
                self.assertEqual(len(state["results"]), 2)
                self.assertEqual(len(calls), 2)

    def test_later_file_error_preserves_completed_file(self):
        job_id = uuid4()

        def inline_worker(job, spec):
            (job.directory / "request.json").write_text(json.dumps(spec), encoding="utf-8")
            jobs.run_worker(job.directory, app.process_files, Mock())

        with patch.object(jobs, "launch_job", side_effect=inline_worker), \
             patch.object(app, "transcribe_srt", side_effect=["complete subtitle", RuntimeError("second failed")]):
            self.upload(job_id, output_format="srt")
        state = self.client.get(f"/jobs/{job_id}").json
        self.assertEqual(state["state"], "error")
        self.assertIn("second failed", state["status"])
        self.assertEqual(len(state["results"]), 1)
        download = self.client.get(state["results"][0]["url"])
        self.assertEqual(download.get_data(as_text=True), "complete subtitle")
        download.close()

    def test_stop_preserves_files_published_even_during_process_termination(self):
        job_id = uuid4()
        job = jobs.begin_job(job_id, self.root)
        results = []

        def publish_file():
            result_id = uuid4()
            output = job.directory / str(result_id)
            output.mkdir()
            (output / "complete.txt").write_text("complete")
            results.append({"result_id": str(result_id), "filename": "complete.txt"})
            jobs.write_status(job.directory, {"state": "processing", "percent": 50, "status": "processing", "results": results})

        publish_file()
        unfinished = job.directory / str(uuid4())
        unfinished.mkdir()
        (unfinished / "partial.txt").write_text("unfinished")
        job.process = Mock()
        job.process.pid = 12345
        job.process.poll.return_value = None
        job.process.wait.side_effect = lambda **kwargs: publish_file()
        with patch.object(jobs.subprocess, "run", return_value=Mock(returncode=0)), \
             patch.object(jobs.os, "killpg", create=True):
            stopped = self.client.post(f"/jobs/{job_id}/cancel").json
        self.assertEqual(stopped["state"], "cancelled")
        self.assertEqual(len(stopped["results"]), 2)
        jobs.cleanup_cancelled_upload(job)
        self.assertFalse(unfinished.exists())
        self.assertFalse((job.directory / "inputs").exists())
        for result in stopped["results"]:
            download = self.client.get(result["url"])
            self.assertEqual(download.get_data(as_text=True), "complete")
            download.close()
        self.assertEqual(self.client.get(f"/jobs/{job_id}").json["state"], "cancelled")

    def test_cancelled_or_incomplete_jobs_do_not_offer_downloads(self):
        job_id, result_id = uuid4(), uuid4()
        job = jobs.begin_job(job_id, self.root)
        (job.directory / str(result_id)).mkdir()
        (job.directory / str(result_id) / "partial.txt").write_text("partial")
        jobs.write_status(job.directory, {"state": "processing", "percent": 40, "status": "processing", "results": []})
        url = f"/jobs/{job_id}/download/{result_id}/partial.txt"
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.post(f"/jobs/{job_id}/cancel")
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_stop_kills_real_worker_and_its_child_process(self):
        job_id = uuid4()
        job = jobs.begin_job(job_id, self.root)
        ready = job.directory / "child.pid"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        program = (
            "import subprocess, sys, time; from pathlib import Path; "
            f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags={flags}); "
            f"Path({str(ready)!r}).write_text(str(child.pid)); time.sleep(60)"
        )
        options = {"creationflags": flags} if os.name == "nt" else {"start_new_session": True}
        process = subprocess.Popen([sys.executable, "-c", program], **options)
        job.process = process
        job.state = {"state": "processing", "percent": 30, "status": "processing", "results": []}
        self.addCleanup(lambda: jobs.cancel_job(job_id, self.root) if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "worker did not create a child")
        child_pid = int(ready.read_text())
        stopped = self.client.post(f"/jobs/{job_id}/cancel")
        self.assertEqual(stopped.json["state"], "cancelled")
        self.assertIsNotNone(process.poll())
        self.assertFalse(job.directory.exists())
        if os.name == "nt":
            output = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                                    capture_output=True, text=True, creationflags=flags, check=True).stdout
            rows = list(csv.reader(output.splitlines()))
            self.assertFalse(any(len(row) > 1 and row[1] == str(child_pid) for row in rows), "child process survived stop")


if __name__ == "__main__":
    unittest.main()

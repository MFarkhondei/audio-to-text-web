"""Run each conversion in an isolated process so it can be stopped safely."""

from dataclasses import dataclass, field
import atexit
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Lock, Thread


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    directory: Path
    state: dict = field(default_factory=lambda: {
        "state": "uploading", "percent": 0, "status": "در حال دریافت فایل‌ها...", "results": [],
    })
    process: subprocess.Popen | None = None
    cancelled: bool = False
    lock: Lock = field(default_factory=Lock)


_jobs = {}
_jobs_lock = Lock()


def _record(job_id, root):
    with _jobs_lock:
        return _jobs.setdefault(str(job_id), Job(Path(root) / "jobs" / str(job_id)))


def begin_job(job_id, root):
    job = _record(job_id, root)
    with job.lock:
        if job.cancelled:
            raise JobCancelled()
        if job.directory.exists():
            raise ValueError("این درخواست قبلاً ثبت شده است.")
        (job.directory / "inputs").mkdir(parents=True)
    return job


def check_cancelled(job):
    with job.lock:
        if job.cancelled:
            raise JobCancelled()


def launch_job(job, spec):
    with job.lock:
        if job.cancelled:
            raise JobCancelled()
        (job.directory / "request.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        job.state = {"state": "processing", "percent": 0, "status": "شروع پردازش...", "results": []}
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.append(str(Path(__file__).with_name("app.py")))
        command.extend(["--process-job", str(job.directory)])
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with (job.directory / "worker.log").open("wb") as log:
            job.process = subprocess.Popen(command, stdout=log, stderr=log, **options)
        Thread(target=_reap_job, args=(job, job.process), daemon=True).start()


def fail_job(job, message):
    with job.lock:
        if not job.cancelled:
            job.state = {"state": "error", "percent": 0, "status": message, "results": []}
    shutil.rmtree(job.directory / "inputs", ignore_errors=True)


def _snapshot(job):
    if not job.cancelled:
        try:
            job.state = json.loads((job.directory / "status.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if job.process is not None and job.process.poll() is not None:
            if job.state["state"] == "processing":
                job.state = {**job.state, "state": "error", "status": "پردازش به‌طور غیرمنتظره متوقف شد."}
                write_status(job.directory, job.state)
                cleanup_unpublished(job.directory, job.state["results"])
            job.process = None
    return dict(job.state)


def job_status(job_id):
    with _jobs_lock:
        job = _jobs.get(str(job_id))
    if job is None:
        return None
    with job.lock:
        return _snapshot(job)


def _reap_job(job, process):
    process.wait()
    with job.lock:
        if job.process is process:
            _snapshot(job)


def cancel_job(job_id, root):
    # Remember a stop even if it arrives before the upload request.
    job = _record(job_id, root)
    with job.lock:
        state = _snapshot(job)
        if state["state"] in {"completed", "error", "cancelled"}:
            return state
        process = job.process
        if process is not None and process.poll() is None:
            if os.name == "nt":
                taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
                result = subprocess.run(
                    [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if result.returncode and process.poll() is None:
                    raise RuntimeError("توقف پردازش انجام نشد؛ دوباره تلاش کنید.")
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    if process.poll() is None:
                        process.kill()
            process.wait(timeout=5)
        job.process = None
        # The worker may have finished another file while its process was stopping.
        state = _snapshot(job)
        job.cancelled = True
        job.state = {"state": "cancelled", "percent": state["percent"], "status": "پردازش متوقف شد.", "results": state.get("results", [])}
        # During upload, the request handler removes inputs after its last write.
        if job.state["results"] or process is not None:
            if job.state["results"]:
                write_status(job.directory, job.state)
                cleanup_unpublished(job.directory, job.state["results"])
            else:
                shutil.rmtree(job.directory, ignore_errors=True)
        return dict(job.state)


def cleanup_cancelled_upload(job):
    with job.lock:
        if job.cancelled and job.process is None and not job.state["results"]:
            shutil.rmtree(job.directory, ignore_errors=True)


def stop_running_jobs():
    with _jobs_lock:
        running = list(_jobs.items())
    for job_id, job in running:
        if job.process is not None and job.process.poll() is None:
            try:
                cancel_job(job_id, job.directory.parent.parent)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass


atexit.register(stop_running_jobs)


def write_status(directory, state):
    path = Path(directory) / "status.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def cleanup_unpublished(directory, results):
    published = {result["result_id"] for result in results}
    for path in Path(directory).iterdir():
        if path.is_dir() and path.name not in published:
            shutil.rmtree(path, ignore_errors=True)


def run_worker(directory, process_files, download_drive_file):
    directory = Path(directory)
    results = []
    percent = 0
    status = "شروع پردازش..."
    try:
        spec = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        filepaths = spec["files"]
        if spec["drive_id"]:
            filepath = str(directory / "inputs" / "drive.audio")
            if not download_drive_file(spec["drive_id"], filepath):
                raise ValueError("دانلود از گوگل درایو ناموفق بود. مطمئن شوید فایل عمومی (Anyone with the link) باشد.")
            filepaths = [(filepath, f"{spec['drive_id']}.audio")]

        def report(value, text):
            nonlocal percent, status
            percent, status = value, text
            write_status(directory, {"state": "processing", "percent": percent, "status": status, "results": results})

        def publish(output):
            results.append({"filename": output["filename"], "result_id": str(output["result_id"])})
            report(percent, status)

        process_files(filepaths, spec["language"], spec["output_format"], report, str(directory), on_result=publish)
        write_status(directory, {"state": "completed", "percent": 100, "status": "تمام شد", "results": results})
    except Exception as exc:
        write_status(directory, {"state": "error", "percent": percent, "status": f"خطا در پردازش: {exc}", "results": results})
        cleanup_unpublished(directory, results)
    finally:
        shutil.rmtree(directory / "inputs", ignore_errors=True)

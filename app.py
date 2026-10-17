from flask import Flask, render_template, request, send_file, send_from_directory, flash, redirect, url_for, jsonify, abort
import os
import tempfile
import subprocess
import re
import requests
import shutil
import platform
import sys
import json
from pathlib import Path
from uuid import uuid4
import speech_recognition as sr
from subtitles import transcribe_srt
import processing_jobs as jobs

app = Flask(__name__)
app.secret_key = "audio-to-text-secret"
UPLOAD_FOLDER = "/tmp/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

progress_data = {"percent": 0, "status": "idle", "transcript": ""}

def require_ffmpeg() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if not missing:
        return
    message = f"{' و '.join(missing)} نصب نیست یا در PATH پیدا نشد. "
    if platform.system() == "Windows":
        message += "برای نصب، در PowerShell دستور winget install --id Gyan.FFmpeg -e را اجرا کنید. "
    else:
        message += "بستهٔ FFmpeg مناسب سیستم‌عامل خود را نصب کنید. "
    message += (
        "اگر قبلاً نصب شده است، پوشهٔ bin شامل ffmpeg و ffprobe را به PATH اضافه کنید. "
        "سپس پنجرهٔ اجرای برنامه را ببندید و برنامه را دوباره اجرا کنید."
    )
    raise RuntimeError(message)

def get_duration(audio_path: str) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())

def split_audio(audio_path: str, segment_dir: str, segment_time: int = 30) -> list[str]:
    Path(segment_dir).mkdir(parents=True, exist_ok=True)
    out_pattern = os.path.join(segment_dir, "seg_%03d.wav")
    cmd = [
        "ffmpeg", "-i", audio_path, "-f", "segment", "-segment_time", str(segment_time),
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", out_pattern, "-y"
    ]
    subprocess.run(cmd, capture_output=True, check=True)
    segments = sorted(Path(segment_dir).glob("seg_*.wav"))
    return [str(s) for s in segments]

def transcribe_segments(segments: list[str], language: str = "fa-IR", on_progress=None) -> str:
    r = sr.Recognizer()
    full_text = []
    total = len(segments)
    for i, seg in enumerate(segments):
        try:
            with sr.AudioFile(seg) as source:
                audio = r.record(source)
                text = r.recognize_google(audio, language=language)
                full_text.append(text.strip())
        except sr.UnknownValueError:
            pass
        except Exception:
            pass
        if on_progress:
            on_progress((i + 1) / total)
    return " ".join(full_text)

def extract_drive_file_id(url: str) -> str | None:
    patterns = [
        r"/file/d/([a-zA-Z0-9_-]+)",
        r"id=([a-zA-Z0-9_-]+)",
        r"/d/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def download_drive_file(file_id: str, dest_path: str) -> bool:
    url = f"https://drive.google.com/uc?export=download&id={file_id}"
    try:
        response = requests.get(url, stream=True, timeout=60)
        response.raise_for_status()
        if "confirm=" in response.url or b"download_warning" in response.content[:1000]:
            for key, value in response.cookies.items():
                if key.startswith("download_warning"):
                    url = f"https://drive.google.com/uc?export=download&confirm={value}&id={file_id}"
                    response = requests.get(url, stream=True, timeout=60)
                    break
        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception:
        return False

def process_files(filepaths, language, output_format, report, output_root=None, on_result=None):
    if output_format == "txt":
        require_ffmpeg()
    results = []
    total_files = len(filepaths)
    for idx, (filepath, filename) in enumerate(filepaths):
        status = f"در حال پردازش فایل {idx+1} از {total_files}"
        report(int(idx / total_files * 100), status)

        def update_progress(fraction):
            report(min(99, int((idx + fraction) / total_files * 100)), status)

        if output_format == "srt":
            report(int(idx / total_files * 100),
                   f"در حال آماده‌سازی مدل زیرنویس برای فایل {idx+1} از {total_files} (بار اول ممکن است دانلود مدل زمان ببرد)")
            transcript = transcribe_srt(filepath, language, on_progress=update_progress)
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                segments = split_audio(filepath, tmpdir)
                transcript = transcribe_segments(segments, language, on_progress=update_progress)
        result_id = uuid4()
        result_dir = os.path.join(output_root or UPLOAD_FOLDER, str(result_id))
        os.makedirs(result_dir)
        output_name = f"{Path(filename).stem or 'transcript'}.{output_format}"
        out_path = os.path.join(result_dir, output_name)
        with open(out_path, "w", encoding="utf-8", newline="\n") as output:
            output.write(transcript)
        results.append({"filename": output_name, "result_id": result_id, "path": out_path, "transcript": transcript})
        if on_result:
            on_result(results[-1])
    return results


def job_response(job_id, state):
    if state is None:
        return jsonify({"state": "error", "status": "درخواست پردازش پیدا نشد."}), 404
    state["results"] = [{
        "filename": result["filename"],
        "url": url_for("download_job_output", job_id=job_id, result_id=result["result_id"], filename=result["filename"]),
    } for result in state.get("results", [])]
    return jsonify(state)


@app.route("/jobs/<uuid:job_id>", methods=["POST"])
def start_job(job_id):
    job = None
    try:
        job = jobs.begin_job(job_id, UPLOAD_FOLDER)
        output_format = request.form.get("output_format", "txt")
        if output_format not in {"txt", "srt"}:
            raise ValueError("فرمت خروجی معتبر نیست.")
        language = request.form.get("language", "fa-IR")
        drive_url = request.form.get("drive_url", "").strip()
        drive_id = extract_drive_file_id(drive_url) if drive_url else None
        if drive_url and not drive_id:
            raise ValueError("لینک گوگل درایو معتبر نیست")
        jobs.check_cancelled(job)
        filepaths = []
        if not drive_id:
            for uploaded_file in request.files.getlist("audio"):
                jobs.check_cancelled(job)
                if uploaded_file.filename:
                    filename = uploaded_file.filename.replace("\\", "/").rsplit("/", 1)[-1]
                    filepath = str(job.directory / "inputs" / f"{len(filepaths)}.audio")
                    uploaded_file.save(filepath)
                    filepaths.append((filepath, filename))
        jobs.check_cancelled(job)
        if not filepaths and not drive_id:
            raise ValueError("لطفاً یک یا چند فایل آپلود کنید یا لینک گوگل درایو وارد کنید")
        if output_format == "txt":
            require_ffmpeg()
        jobs.launch_job(job, {"files": filepaths, "drive_id": drive_id, "language": language, "output_format": output_format})
        return job_response(job_id, jobs.job_status(job_id)), 202
    except jobs.JobCancelled:
        return job_response(job_id, jobs.job_status(job_id)), 409
    except Exception as exc:
        if job is None:
            return jsonify({"state": "error", "status": str(exc)}), 409
        jobs.fail_job(job, f"خطا در پردازش: {exc}")
        return job_response(job_id, jobs.job_status(job_id)), 400
    finally:
        if job is not None:
            jobs.cleanup_cancelled_upload(job)


@app.route("/jobs/<uuid:job_id>", methods=["GET"])
def processing_status(job_id):
    return job_response(job_id, jobs.job_status(job_id))


@app.route("/jobs/<uuid:job_id>/cancel", methods=["POST"])
def stop_job(job_id):
    try:
        return job_response(job_id, jobs.cancel_job(job_id, UPLOAD_FOLDER))
    except Exception:
        return jsonify({"state": "processing", "status": "توقف پردازش انجام نشد؛ دوباره تلاش کنید."}), 500


@app.route("/jobs/<uuid:job_id>/download/<uuid:result_id>/<filename>")
def download_job_output(job_id, result_id, filename):
    directory = Path(UPLOAD_FOLDER) / "jobs" / str(job_id)
    try:
        state = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        abort(404)
    if not any(result["result_id"] == str(result_id) and result["filename"] == filename
               for result in state.get("results", [])):
        abort(404)
    return send_from_directory(str(directory / str(result_id)), filename, as_attachment=True)

@app.route("/progress")
def progress():
    return progress_data

@app.route("/download/<uuid:result_id>/<filename>")
def download_transcript(result_id, filename):
    return send_from_directory(
        os.path.join(UPLOAD_FOLDER, str(result_id)), filename, as_attachment=True
    )

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        progress_data["percent"] = 0
        progress_data["status"] = "شروع پردازش..."
        progress_data["transcript"] = ""

        language = request.form.get("language", "fa-IR")
        output_format = request.form.get("output_format", "txt")
        if output_format not in {"srt", "txt"}:
            flash("فرمت خروجی معتبر نیست.")
            return redirect(request.url)
        drive_url = request.form.get("drive_url", "").strip()
        uploaded_files = request.files.getlist("audio")

        try:
            with tempfile.TemporaryDirectory(dir=UPLOAD_FOLDER) as upload_dir:
                filepaths = []
                if drive_url:
                    file_id = extract_drive_file_id(drive_url)
                    if not file_id:
                        flash("لینک گوگل درایو معتبر نیست")
                        return redirect(request.url)
                    filepath = os.path.join(upload_dir, "drive.audio")
                    if not download_drive_file(file_id, filepath):
                        flash("دانلود از گوگل درایو ناموفق بود. مطمئن شوید فایل عمومی (Anyone with the link) باشد.")
                        return redirect(request.url)
                    filepaths.append((filepath, f"{file_id}.audio"))
                else:
                    for uploaded_file in uploaded_files:
                        if uploaded_file.filename:
                            filename = uploaded_file.filename.replace("\\", "/").rsplit("/", 1)[-1]
                            filepath = os.path.join(upload_dir, f"{len(filepaths)}.audio")
                            uploaded_file.save(filepath)
                            filepaths.append((filepath, filename))

                if not filepaths:
                    flash("لطفاً یک یا چند فایل آپلود کنید یا لینک گوگل درایو وارد کنید")
                    return redirect(request.url)

                def report(percent, status):
                    progress_data.update(percent=percent, status=status)

                outputs = process_files(filepaths, language, output_format, report)
                results = [{"filename": output["filename"], "url": url_for(
                    "download_transcript", result_id=output["result_id"], filename=output["filename"]
                )} for output in outputs]
                progress_data["transcript"] = "\n\n".join(
                    f"=== فایل {idx+1}: {filename} ===\n{output['transcript']}"
                    for idx, ((_, filename), output) in enumerate(zip(filepaths, outputs))
                )
                progress_data["percent"] = 100
                progress_data["status"] = "تمام شد"

                if len(results) == 1:
                    return send_file(outputs[0]["path"], as_attachment=True, download_name=outputs[0]["filename"])
                return render_template("index.html", results=results)
        except Exception as e:
            progress_data["status"] = "خطا"
            flash(f"خطا در پردازش: {str(e)}")
            return redirect(request.url)

    return render_template("index.html")

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--process-job":
        jobs.run_worker(sys.argv[2], process_files, download_drive_file)
    else:
        app.run(host="0.0.0.0", port=5000, debug=True)

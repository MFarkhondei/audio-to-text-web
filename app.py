from flask import Flask, render_template, request, send_file, flash, redirect, url_for
import os
import tempfile
import subprocess
import re
import requests
from pathlib import Path
import speech_recognition as sr

app = Flask(__name__)
app.secret_key = "audio-to-text-secret"
UPLOAD_FOLDER = "/tmp/uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

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

def transcribe_segments(segments: list[str], language: str = "fa-IR") -> str:
    r = sr.Recognizer()
    full_text = []
    for seg in segments:
        try:
            with sr.AudioFile(seg) as source:
                audio = r.record(source)
                text = r.recognize_google(audio, language=language)
                full_text.append(text.strip())
        except sr.UnknownValueError:
            continue
        except Exception:
            continue
    return " ".join(full_text)

def extract_drive_file_id(url: str) -> str | None:
    """Extract Google Drive file ID from various URL formats."""
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
    """Download a publicly shared Google Drive file."""
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

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        language = request.form.get("language", "fa-IR")
        drive_url = request.form.get("drive_url", "").strip()
        uploaded_file = request.files.get("audio")

        filepath = None

        if drive_url:
            file_id = extract_drive_file_id(drive_url)
            if not file_id:
                flash("لینک گوگل درایو معتبر نیست")
                return redirect(request.url)
            filepath = os.path.join(UPLOAD_FOLDER, f"{file_id}.audio")
            if not download_drive_file(file_id, filepath):
                flash("دانلود از گوگل درایو ناموفق بود. مطمئن شوید فایل عمومی (Anyone with the link) باشد.")
                return redirect(request.url)
        elif uploaded_file and uploaded_file.filename:
            filepath = os.path.join(UPLOAD_FOLDER, uploaded_file.filename)
            uploaded_file.save(filepath)
        else:
            flash("لطفاً یک فایل آپلود کنید یا لینک گوگل درایو وارد کنید")
            return redirect(request.url)

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                segments = split_audio(filepath, tmpdir)
                transcript = transcribe_segments(segments, language)

            out_path = os.path.join(UPLOAD_FOLDER, "transcript.txt")
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(transcript)

            return send_file(out_path, as_attachment=True, download_name="transcript.txt")
        except Exception as e:
            flash(f"خطا در پردازش: {str(e)}")
            return redirect(request.url)

    return render_template("index.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

from flask import Flask, render_template, request, send_file, flash, redirect, url_for
import os
import tempfile
import subprocess
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

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        if "audio" not in request.files:
            flash("فایلی انتخاب نشده")
            return redirect(request.url)
        file = request.files["audio"]
        if file.filename == "":
            flash("فایلی انتخاب نشده")
            return redirect(request.url)
        if file:
            language = request.form.get("language", "fa-IR")
            filename = file.filename
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)

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

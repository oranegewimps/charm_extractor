"""charm_extractor をブラウザから使うためのローカル専用Webサーバー。

起動: start.bat をダブルクリック（または python app.py）
"""

from __future__ import annotations

import csv
import logging
import os
import threading
import time
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

import charm_extractor as ce

HOST = "127.0.0.1"  # このPCからのみアクセス可能
PORT = 5000
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"

app = Flask(__name__)
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


class JobLogHandler(logging.Handler):
    def __init__(self, job: dict):
        super().__init__()
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        self.job["logs"].append(f"{time.strftime('%H:%M:%S')} {record.getMessage()}")


def running_job() -> dict | None:
    return next((j for j in jobs.values() if j["state"] == "running"), None)


def process(job: dict, video: Path, api_key: str) -> None:
    handler = JobLogHandler(job)
    ce.logger.addHandler(handler)
    try:
        entries = ce.run(video, job["output"], api_key, frames_dir=job["frames_dir"])
        with job["output"].open(encoding="utf-8-sig", newline="") as f:
            job["rows"] = list(csv.reader(f))
        job["flagged"] = [number for number, e in ce.numbered(entries) if e.flagged]
        if job["frames_dir"]:
            job["frames"] = sorted(p.stem for p in job["frames_dir"].glob("*.png"))
        job["state"] = "done"
    except Exception as e:  # 画面に理由を出すため、すべての例外を拾う
        job["logs"].append(f"エラー: {type(e).__name__}: {e}")
        job["state"] = "error"
    finally:
        ce.logger.removeHandler(handler)
        video.unlink(missing_ok=True)


@app.get("/")
def index():
    return INDEX_HTML.replace("__KEY_SET__", "true" if os.environ.get("GEMINI_API_KEY") else "false")


@app.post("/upload")
def upload():
    file = request.files.get("video")
    if not file or not file.filename:
        return jsonify(error="動画ファイルを選んでください。"), 400
    api_key = request.form.get("api_key", "").strip() or os.environ.get("GEMINI_API_KEY")
    problem = ce.check_environment(api_key)
    if problem:
        return jsonify(error=problem), 400

    with jobs_lock:
        if running_job():
            return jsonify(error="別の動画を処理中です。終わってから実行してください。"), 409
        job_id = uuid.uuid4().hex[:12]
        UPLOAD_DIR.mkdir(exist_ok=True)
        original = Path(file.filename).name
        stem = Path(original).stem
        video_out_dir = OUTPUT_DIR / stem  # 動画ごとのフォルダ
        video_out_dir.mkdir(parents=True, exist_ok=True)
        video = UPLOAD_DIR / f"{job_id}{Path(original).suffix or '.mp4'}"
        job = {
            "state": "running",
            "logs": [f"{time.strftime('%H:%M:%S')} 受け付けました: {original}"],
            "rows": None,
            "flagged": [],
            "frames": [],
            "output": video_out_dir / f"{stem}_{time.strftime('%Y%m%d_%H%M%S')}.csv",
            "frames_dir": video_out_dir / "flagged" if request.form.get("save_frames") == "1" else None,
        }
        jobs[job_id] = job

    file.save(video)
    threading.Thread(target=process, args=(job, video, api_key), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/status/<job_id>")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="ジョブが見つかりません。"), 404
    return jsonify(state=job["state"], logs=job["logs"], rows=job["rows"], flagged=job["flagged"],
                   frames=job["frames"], filename=job["output"].name)


@app.get("/download/<job_id>")
def download(job_id: str):
    job = jobs.get(job_id)
    if not job or job["state"] != "done":
        return "CSVはまだありません。", 404
    return send_file(job["output"], as_attachment=True, download_name=job["output"].name, mimetype="text/csv")


@app.get("/frame/<job_id>/<number>")
def frame(job_id: str, number: str):
    job = jobs.get(job_id)
    if not job or number not in job["frames"]:
        abort(404)
    return send_file(job["frames_dir"] / f"{number}.png", mimetype="image/png")


INDEX_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>護石CSVメーカー</title>
<style>
  :root { --bg:#f6f5f2; --card:#fff; --text:#222; --muted:#666; --line:#ddd; --accent:#b5562a; --ok:#2e7d32; --ng:#c62828; --warn:#fff3cd; --flag:#fde2e2; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#1c1b19; --card:#262522; --text:#eee; --muted:#aaa; --line:#444; --accent:#e0875a; --ok:#81c784; --ng:#ef9a9a; --warn:#4a3f1a; --flag:#4a2424; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:"Yu Gothic UI","Meiryo",sans-serif; }
  main { max-width:1100px; margin:0 auto; padding:24px 16px 48px; }
  h1 { font-size:1.5rem; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:20px; margin-bottom:16px; }
  #drop { border:2px dashed var(--line); border-radius:10px; padding:36px 16px; text-align:center; cursor:pointer; }
  #drop.over { border-color:var(--accent); }
  #drop strong { display:block; font-size:1.1rem; margin-bottom:6px; }
  #fileName { margin-top:10px; color:var(--accent); font-weight:bold; word-break:break-all; }
  label { display:block; margin:16px 0 6px; font-weight:bold; }
  input[type=password] { width:100%; padding:10px; border:1px solid var(--line); border-radius:6px; background:var(--bg); color:var(--text); }
  .hint { color:var(--muted); font-size:.85rem; margin-top:4px; }
  button, .btn { display:inline-block; margin-top:16px; padding:12px 28px; border:0; border-radius:8px; background:var(--accent); color:#fff; font-size:1rem; font-weight:bold; cursor:pointer; text-decoration:none; }
  button:disabled { opacity:.5; cursor:default; }
  progress { width:100%; height:14px; margin-top:12px; }
  #log { background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:10px; height:200px; overflow:auto; font-family:Consolas,monospace; font-size:.85rem; white-space:pre-wrap; margin:0; }
  #status { font-weight:bold; margin-bottom:8px; }
  .ok { color:var(--ok); } .ng { color:var(--ng); }
  .tableWrap { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:.9rem; margin-top:12px; }
  th, td { border:1px solid var(--line); padding:6px 8px; text-align:left; white-space:nowrap; }
  th { background:var(--bg); }
  tr.dup td { background:var(--warn); }
  tr.flag td { background:var(--flag); }
  tr.dup.flag td { background:linear-gradient(var(--warn), var(--flag)); }
  .check { display:flex; align-items:center; gap:8px; margin-top:16px; font-weight:normal; }
  td a { color:var(--accent); }
  .hidden { display:none; }
</style>
</head>
<body>
<main>
  <h1>護石CSVメーカー</h1>
  <p class="sub">護石の装備詳細画面を録画した動画を選んで「解析開始」を押すと、CSVを作成します。</p>

  <div class="card">
    <div id="drop">
      <strong>ここに動画をドラッグ＆ドロップ</strong>
      <span>またはクリックしてファイルを選択</span>
      <div id="fileName"></div>
    </div>
    <input type="file" id="file" accept="video/*" class="hidden">

    <div id="keyBox" class="hidden">
      <label for="key">Gemini APIキー</label>
      <input type="password" id="key" placeholder="AIza..." autocomplete="off">
      <div class="hint">このPC内のサーバーに渡すだけで、ファイルには保存しません。毎回入力したくない場合は README の手順で環境変数に設定してください。</div>
    </div>

    <label class="check"><input type="checkbox" id="saveFrames" checked> 要確認の行の画面を画像で保存する</label>

    <button id="start" disabled>解析開始</button>
    <progress id="upProg" class="hidden" max="100" value="0"></progress>
  </div>

  <div class="card hidden" id="progressCard">
    <div id="status"></div>
    <pre id="log"></pre>
  </div>

  <div class="card hidden" id="resultCard">
    <a class="btn" id="download" href="#">CSVをダウンロード</a>
    <div class="hint">赤い行は要確認（備考に理由があります）、黄色の行は「重複の可能性あり」です。「画像」から該当する画面を開けます。</div>
    <div class="tableWrap"><table id="table"></table></div>
  </div>
</main>
<script>
const KEY_SET = __KEY_SET__;
const $ = id => document.getElementById(id);
let selected = null;
if (!KEY_SET) $("keyBox").classList.remove("hidden");

function choose(f) {
  selected = f;
  $("fileName").textContent = f ? f.name + "（" + (f.size / 1048576).toFixed(1) + " MB）" : "";
  $("start").disabled = !f;
}
$("drop").onclick = () => $("file").click();
$("file").onchange = e => choose(e.target.files[0]);
$("drop").ondragover = e => { e.preventDefault(); $("drop").classList.add("over"); };
$("drop").ondragleave = () => $("drop").classList.remove("over");
$("drop").ondrop = e => { e.preventDefault(); $("drop").classList.remove("over"); choose(e.dataTransfer.files[0]); };

function setStatus(text, cls) { $("status").textContent = text; $("status").className = cls || ""; }

$("start").onclick = () => {
  if (!KEY_SET && !$("key").value.trim()) { alert("Gemini APIキーを入力してください。"); return; }
  const form = new FormData();
  form.append("video", selected);
  form.append("api_key", $("key").value);
  form.append("save_frames", $("saveFrames").checked ? "1" : "0");
  $("start").disabled = true;
  $("resultCard").classList.add("hidden");
  $("progressCard").classList.remove("hidden");
  $("log").textContent = "";
  setStatus("動画を読み込み中...");
  $("upProg").classList.remove("hidden");

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/upload");
  xhr.upload.onprogress = e => { if (e.lengthComputable) $("upProg").value = e.loaded / e.total * 100; };
  xhr.onload = () => {
    $("upProg").classList.add("hidden");
    let res = {};
    try { res = JSON.parse(xhr.responseText); } catch (_) {}
    if (xhr.status !== 200) { setStatus(res.error || "エラーが発生しました。", "ng"); $("start").disabled = false; return; }
    setStatus("解析中...（動画の長さによっては数分〜数十分かかります。このページは閉じないでください）");
    poll(res.job_id);
  };
  xhr.onerror = () => { setStatus("サーバーに接続できません。start.bat の黒い画面が開いているか確認してください。", "ng"); $("start").disabled = false; };
  xhr.send(form);
};

function poll(jobId) {
  fetch("/status/" + jobId).then(r => r.json()).then(s => {
    $("log").textContent = s.logs.join("\\n");
    $("log").scrollTop = $("log").scrollHeight;
    if (s.state === "running") { setTimeout(() => poll(jobId), 2000); return; }
    $("start").disabled = false;
    if (s.state === "error") { setStatus("失敗しました。下のログを確認してください。もう一度実行すると、解析済みの部分は飛ばして続きから処理します。", "ng"); return; }
    setStatus("完了しました：" + (s.rows.length - 1) + "件", "ok");
    $("download").href = "/download/" + jobId;
    showTable(s.rows, new Set(s.flagged), new Set(s.frames), jobId);
    $("resultCard").classList.remove("hidden");
  }).catch(() => setTimeout(() => poll(jobId), 3000));
}

const DUP_COL = 14;
function showTable(rows, flagged, frames, jobId) {
  const table = $("table");
  table.innerHTML = "";
  rows.forEach((row, i) => {
    const tr = document.createElement("tr");
    if (i > 0) {
      if (row[DUP_COL]) tr.classList.add("dup");
      if (flagged.has(row[0])) tr.classList.add("flag");
    }
    row.forEach(v => { const c = document.createElement(i === 0 ? "th" : "td"); c.textContent = v; tr.appendChild(c); });
    const c = document.createElement(i === 0 ? "th" : "td");
    if (i === 0) c.textContent = "画像";
    else if (frames.has(row[0])) {
      const a = document.createElement("a");
      a.href = "/frame/" + jobId + "/" + row[0];
      a.target = "_blank";
      a.textContent = "開く";
      c.appendChild(a);
    }
    tr.appendChild(c);
    table.appendChild(tr);
  });
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    threading.Timer(1.5, webbrowser.open, args=(f"http://{HOST}:{PORT}/",)).start()
    print(f"護石CSVメーカーを起動しました: http://{HOST}:{PORT}/")
    print("終了するときはこの画面を閉じてください。")
    app.run(host=HOST, port=PORT, threaded=True)

"""Small paste-a-link web UI, served from the standard library.

Same job queue as the Telegram bot: submit a link, poll the job, download the
finished file. No framework, no build step — one page and three endpoints.
"""

import json
import mimetypes
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config
from .jobs import DONE, Job, manager

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vidgrab</title>
<style>
  :root { color-scheme: light dark; --bg:#0f1115; --fg:#e8eaed; --mut:#9aa3af;
          --card:#171a21; --line:#272b34; --acc:#5b9dff; }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f6f7f9; --fg:#14171c; --mut:#5b6472; --card:#fff;
            --line:#e2e5ea; --acc:#2563eb; }
  }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5
         ui-sans-serif, system-ui, -apple-system, sans-serif;
         display:flex; justify-content:center; padding:6vh 16px; }
  main { width:100%; max-width:560px; }
  h1 { font-size:20px; margin:0 0 4px; letter-spacing:-.02em; }
  p.sub { color:var(--mut); margin:0 0 20px; font-size:14px; }
  form, .job { background:var(--card); border:1px solid var(--line);
               border-radius:12px; padding:16px; margin-bottom:12px; }
  input, select, button { font:inherit; width:100%; padding:10px 12px;
    border-radius:8px; border:1px solid var(--line); background:transparent;
    color:var(--fg); }
  .row { display:flex; gap:8px; margin-top:10px; }
  .row select { flex:1; }
  button { background:var(--acc); color:#fff; border:none; font-weight:600;
           cursor:pointer; flex:0 0 auto; width:auto; padding:10px 18px; }
  button:disabled { opacity:.5; cursor:default; }
  .job h3 { margin:0 0 4px; font-size:15px; }
  .job small { color:var(--mut); word-break:break-all; }
  .bar { height:6px; background:var(--line); border-radius:99px;
         overflow:hidden; margin:10px 0 6px; }
  .bar i { display:block; height:100%; background:var(--acc); width:0;
           transition:width .3s; }
  a.dl { display:inline-block; margin-top:8px; color:var(--acc);
         font-weight:600; text-decoration:none; }
  .err { color:#ef4444; }
</style>
<main>
  <h1>vidgrab</h1>
  <p class="sub">Paste a link. Best quality that fits, stepping down until it does.</p>
  <form id="f">
    <input id="url" name="url" placeholder="https://..." autocomplete="off" required>
    <div class="row">
      <select id="quality">
        <option value="">Best available</option>
        __OPTIONS__
        <option value="audio">Audio only</option>
      </select>
      <button id="go" type="submit">Grab</button>
    </div>
  </form>
  <div id="jobs"></div>
</main>
<script>
const token = new URLSearchParams(location.search).get("t") || "";
const jobsEl = document.getElementById("jobs");
document.getElementById("f").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = document.getElementById("url").value.trim();
  if (!url) return;
  const quality = document.getElementById("quality").value;
  const res = await fetch("api/submit" + (token ? "?t=" + encodeURIComponent(token) : ""), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ url, quality }),
  });
  if (!res.ok) { alert("Rejected: " + (await res.text())); return; }
  const { id } = await res.json();
  document.getElementById("url").value = "";
  track(id, url);
});
function track(id, url) {
  const el = document.createElement("div");
  el.className = "job";
  el.innerHTML = `<h3>Queued</h3><small></small>
    <div class="bar"><i></i></div><div class="meta"></div>`;
  el.querySelector("small").textContent = url;
  jobsEl.prepend(el);
  const tick = async () => {
    const res = await fetch("api/job/" + id + (token ? "?t=" + encodeURIComponent(token) : ""));
    if (!res.ok) return;
    const j = await res.json();
    el.querySelector("h3").textContent = j.title || j.stage;
    el.querySelector(".bar i").style.width = (j.percent || 0) + "%";
    const meta = el.querySelector(".meta");
    if (j.status === "done") {
      meta.innerHTML = `${j.quality} · ${j.size_human} · ${j.duration}
        <br><a class="dl" href="file/${id}${token ? "?t=" + encodeURIComponent(token) : ""}"
        download>Download ${j.filename}</a>`;
      return;
    }
    if (j.status === "failed") {
      meta.innerHTML = `<span class="err"></span>`;
      meta.querySelector(".err").textContent = j.error;
      return;
    }
    meta.textContent = j.stage;
    setTimeout(tick, 1500);
  };
  tick();
}
</script>
"""


def _page() -> bytes:
    options = "\n".join(
        f'<option value="{h}">{h}p or lower</option>' for h in config.QUALITY_LADDER
    )
    options += f'\n<option value="fit">Fit {config.MAX_UPLOAD_MB}MB (Telegram)</option>'
    return PAGE.replace("__OPTIONS__", options).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "vidgrab"

    def log_message(self, fmt, *args):  # quieter than the default access log
        print(f"[vidgrab-web] {self.address_string()} {fmt % args}")

    # --- helpers -----------------------------------------------------------
    def _authorised(self) -> bool:
        if not config.WEB_TOKEN:
            return True
        query = urllib.parse.urlparse(self.path).query
        supplied = urllib.parse.parse_qs(query).get("t", [""])[0]
        return (supplied or self.headers.get("X-Token", "")) == config.WEB_TOKEN

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload).encode(), "application/json")

    # --- routes ------------------------------------------------------------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path == "/healthz":
            self._send(200, b"ok", "text/plain")
            return
        if not self._authorised():
            self._send(401, b"unauthorised", "text/plain")
            return
        if path == "/":
            self._send(200, _page(), "text/html; charset=utf-8")
            return
        if path.startswith("/api/job/"):
            job = manager.get(path.rsplit("/", 1)[-1])
            if not job:
                self._json(404, {"error": "unknown job"})
                return
            self._json(200, job.snapshot())
            return
        if path.startswith("/file/"):
            self._serve_file(path.rsplit("/", 1)[-1])
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if not self._authorised():
            self._send(401, b"unauthorised", "text/plain")
            return
        if path != "/api/submit":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {
                k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()
            }
        url = (payload.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            self._send(400, b"that is not an http(s) link", "text/plain")
            return

        quality = str(payload.get("quality") or "")
        job = Job(
            url=url,
            source="web",
            max_bytes=(
                config.MAX_UPLOAD_BYTES if quality == "fit" else config.MAX_WEB_BYTES
            ),
            max_height=int(quality) if quality.isdigit() else None,
            audio_only=quality == "audio",
        )
        manager.submit(job)
        self._json(200, {"id": job.id})

    def _serve_file(self, job_id: str) -> None:
        job = manager.get(job_id)
        if not job or job.status != DONE or not job.result:
            self._send(404, b"not ready", "text/plain")
            return
        path = job.result.path
        if not os.path.exists(path):
            self._send(410, b"file expired", "text/plain")
            return
        name = os.path.basename(path)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{urllib.parse.quote(name)}"',
        )
        self.end_headers()
        with open(path, "rb") as fh:
            while chunk := fh.read(1024 * 256):
                self.wfile.write(chunk)


def serve() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    guard = "token-protected" if config.WEB_TOKEN else "OPEN — set VIDGRAB_WEB_TOKEN"
    print(f"[vidgrab] web UI on :{config.PORT} ({guard})")
    server.serve_forever()

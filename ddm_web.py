#!/usr/bin/env python3
"""DDM Database Viewer — standalone web interface (zero coupling with ddm/).

Usage:
  python3 ddm_web.py                  # default: http://0.0.0.0:8080
  python3 ddm_web.py --port 9000      # custom port
  python3 ddm_web.py --db /path/to/ddm.db

Dependencies: Python 3 stdlib only (sqlite3 + http.server + json).
Does NOT import from ddm/ — reads the SQLite database directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# HTML template (single-page app, vanilla JS)
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DDM Database Viewer</title>
<style>
:root {
  --bg: #f5f5f7; --panel: #fff; --text: #18181b; --muted: #71717a;
  --line: #e4e4e7; --green: #16a34a; --red: #dc2626; --yellow: #ca8a04;
  --cyan: #0891b2; --blue: #2563eb;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--text); min-height: 100vh; }
header { background: var(--panel); border-bottom: 1px solid var(--line);
         padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; }
header h1 { font-size: 18px; font-weight: 700; }
header span { font-size: 12px; color: var(--muted); }
main { padding: 20px 24px; max-width: 1400px; margin: 0 auto; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
         gap: 12px; margin-bottom: 24px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
        padding: 14px 16px; }
.stat .label { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; }
.stat .value { font-size: 28px; font-weight: 700; margin-top: 2px; }
.tabs { display: flex; gap: 0; margin-bottom: -1px; }
.tab { padding: 8px 20px; border: 1px solid var(--line); border-bottom: none;
       border-radius: 8px 8px 0 0; cursor: pointer; background: var(--bg);
       font-size: 13px; font-weight: 600; color: var(--muted); }
.tab.active { background: var(--panel); color: var(--text); border-bottom: 1px solid var(--panel); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 0 10px 10px 10px;
         padding: 16px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; padding: 8px 10px; border-bottom: 2px solid var(--line);
     font-weight: 600; color: var(--muted); white-space: nowrap; }
td { padding: 7px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }
tr:hover td { background: #fafafa; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 99px;
         font-size: 11px; font-weight: 650; }
.badge-PENDING { background: #fef9c3; color: var(--yellow); }
.badge-SUBMITTED { background: #cffafe; color: var(--cyan); }
.badge-RELEASED { background: #dcfce7; color: var(--green); }
.badge-FAILED { background: #fee2e2; color: var(--red); }
.hash { font-family: monospace; font-size: 11px; color: var(--muted); }
.empty { text-align: center; padding: 40px; color: var(--muted); }
.filter { margin-bottom: 12px; display: flex; gap: 8px; align-items: center; }
.filter select, .filter input { padding: 4px 10px; border: 1px solid var(--line);
       border-radius: 6px; font: inherit; font-size: 13px; }
.filter label { font-size: 12px; color: var(--muted); font-weight: 600; }
.spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid var(--line);
           border-top-color: var(--blue); border-radius: 50%; animation: spin .6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>DDM Database Viewer</h1>
  <span id="db-path"></span>
</header>
<main>
  <div class="stats" id="stats"></div>
  <div class="tabs">
    <div class="tab active" data-tab="batches">Batches</div>
    <div class="tab" data-tab="files">Files</div>
    <div class="tab" data-tab="events">Events</div>
  </div>
  <div class="panel" id="content"><div class="empty">Loading...</div></div>
</main>
<script>
const API = '/api';

async function fetchJSON(path) {
  const r = await fetch(API + path);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

function badge(status) {
  return `<span class="badge badge-${status}">${status}</span>`;
}

function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function fmtSize(n) {
  if (!n) return '-';
  for (const u of ['B','KB','MB','GB']) { if (n < 1024) return n + ' ' + u; n = Math.round(n/1024); }
  return n + ' TB';
}

function fmtHash(h) {
  return h ? h.substring(0, 16) + '...' : '-';
}

// ---- Stats ----
async function loadStats() {
  const s = await fetchJSON('/stats');
  document.getElementById('stats').innerHTML = [
    { label: 'Batches', value: s.batch_count },
    { label: 'Files', value: s.file_count },
    { label: 'SUBMITTED', value: s.submitted || 0 },
    { label: 'RELEASED', value: s.released || 0 },
    { label: 'FAILED', value: s.failed || 0 },
    { label: 'DB Size', value: s.db_size },
  ].map(x => `<div class="stat"><div class="label">${x.label}</div><div class="value">${x.value}</div></div>`).join('');
}

// ---- Batches Table ----
async function loadBatches() {
  const data = await fetchJSON('/batches');
  if (!data.length) {
    document.getElementById('content').innerHTML = '<div class="empty">No batches yet</div>';
    return;
  }
  const rows = data.map(b => `<tr>
    <td>${b.batch_uuid.substring(0, 8)}</td>
    <td>${badge(b.status)}</td>
    <td><b>${b.module}</b></td>
    <td>${b.tag}</td>
    <td>${b.username}</td>
    <td>${b.version || '-'}</td>
    <td>${b.summary || '-'}</td>
    <td>${fmtTime(b.created_at)}</td>
  </tr>`).join('');
  document.getElementById('content').innerHTML = `<table>
    <thead><tr><th>UUID</th><th>Status</th><th>Module</th><th>Tag</th><th>User</th><th>Version</th><th>Summary</th><th>Time</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ---- Files Table ----
async function loadFiles() {
  const data = await fetchJSON('/files');
  if (!data.length) {
    document.getElementById('content').innerHTML = '<div class="empty">No files yet</div>';
    return;
  }
  const rows = data.map(f => `<tr>
    <td>${f.batch_uuid.substring(0, 8)}</td>
    <td>${badge(f.status)}</td>
    <td>${Path(f.source_path).name}</td>
    <td class="hash" title="${f.blake3_hash || ''}">${fmtHash(f.blake3_hash)}</td>
    <td>${fmtSize(f.file_size)}</td>
    <td>${fmtTime(f.created_at)}</td>
  </tr>`).join('');
  document.getElementById('content').innerHTML = `<table>
    <thead><tr><th>Batch</th><th>Status</th><th>File</th><th>BLAKE3</th><th>Size</th><th>Time</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function Path(p) { return { name: (p || '').split('/').pop() || p }; }

// ---- Events Table ----
async function loadEvents() {
  const data = await fetchJSON('/events');
  if (!data.length) {
    document.getElementById('content').innerHTML = '<div class="empty">No events yet</div>';
    return;
  }
  const rows = data.map(e => `<tr>
    <td>${e.batch_uuid.substring(0, 8)}</td>
    <td><b>${e.event_type}</b></td>
    <td>${e.message || '-'}</td>
    <td>${fmtTime(e.created_at)}</td>
  </tr>`).join('');
  document.getElementById('content').innerHTML = `<table>
    <thead><tr><th>Batch</th><th>Event</th><th>Message</th><th>Time</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

// ---- Tab switching ----
const loaders = { batches: loadBatches, files: loadFiles, events: loadEvents };
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    loaders[t.dataset.tab]();
  });
});

// ---- Init ----
(async () => {
  document.getElementById('db-path').textContent = location.host;
  await loadStats();
  loadBatches();
})();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# API handler
# ---------------------------------------------------------------------------

class APIHandler(BaseHTTPRequestHandler):
    db_path: str = "repository/ddm.db"

    def log_message(self, format, *args):
        pass  # suppress access log

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self, sql: str, params=()):
        """Run SELECT and return list of dicts."""
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def do_GET(self):
        parsed = urlparse(self.path)

        # Serve the main page
        if parsed.path == "/" or parsed.path == "/index.html":
            body = PAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # JSON API endpoints
        try:
            if parsed.path == "/api/stats":
                db = sqlite3.connect(self.db_path)
                try:
                    batch_count = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
                    file_count = db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                    submitted = db.execute(
                        "SELECT COUNT(*) FROM batches WHERE status='SUBMITTED'").fetchone()[0]
                    released = db.execute(
                        "SELECT COUNT(*) FROM batches WHERE status='RELEASED'").fetchone()[0]
                    failed = db.execute(
                        "SELECT COUNT(*) FROM batches WHERE status='FAILED'").fetchone()[0]
                finally:
                    db.close()
                db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
                for unit in ("B", "KB", "MB"):
                    if db_size < 1024:
                        break
                    db_size //= 1024
                self._json({
                    "batch_count": batch_count,
                    "file_count": file_count,
                    "submitted": submitted,
                    "released": released,
                    "failed": failed,
                    "db_size": f"{db_size} {unit}",
                })

            elif parsed.path == "/api/batches":
                rows = self._query(
                    "SELECT * FROM batches ORDER BY created_at DESC LIMIT 200")
                self._json(rows)

            elif parsed.path == "/api/files":
                rows = self._query("""
                    SELECT f.*, b.status FROM files f
                    JOIN batches b ON f.batch_uuid = b.batch_uuid
                    ORDER BY f.created_at DESC LIMIT 500
                """)
                self._json(rows)

            elif parsed.path == "/api/events":
                rows = self._query("""
                    SELECT * FROM events ORDER BY created_at DESC LIMIT 500
                """)
                self._json(rows)

            else:
                self._json({"error": "not found"}, 404)

        except Exception as e:
            self._json({"error": str(e)}, 500)


def main():
    parser = argparse.ArgumentParser(description="DDM Database Viewer")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--db", default="repository/ddm.db", help="Path to ddm.db")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address (default: 0.0.0.0)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[WARN] Database not found: {db_path} (will show empty data)")

    APIHandler.db_path = str(db_path.resolve())

    server = HTTPServer((args.host, args.port), APIHandler)
    print(f"  DDM Database Viewer")
    print(f"  http://{args.host}:{args.port}")
    print(f"  DB:   {APIHandler.db_path}")
    print(f"  Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        server.server_close()


if __name__ == "__main__":
    main()

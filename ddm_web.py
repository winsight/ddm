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

# ======================================================================
# HTML + CSS + JS (single-page app, vanilla)
# ======================================================================

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
  --cyan: #0891b2; --blue: #2563eb; --purple: #9333ea;
  --badge-pending: #fef9c3; --badge-submitted: #cffafe;
  --badge-released: #dcfce7; --badge-failed: #fee2e2; --badge-superseded: #f3f4f6;
  --badge-pending-text: #a16207; --badge-submitted-text: #0e7490;
  --badge-released-text: #15803d; --badge-failed-text: #b91c1c;
  --badge-superseded-text: #9ca3af;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font: 13px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: var(--bg); color: var(--text); min-height: 100vh; }
header { background: var(--panel); border-bottom: 1px solid var(--line);
         padding: 12px 24px; display: flex; align-items: center; justify-content: space-between; }
header h1 { font-size: 18px; font-weight: 700; }
header span { font-size: 12px; color: var(--muted); }
main { padding: 16px 24px; max-width: 1500px; margin: 0 auto; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
         gap: 10px; margin-bottom: 16px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
        padding: 10px 14px; cursor: pointer; transition: box-shadow .15s; }
.stat:hover { box-shadow: 0 4px 12px rgba(0,0,0,.06); }
.stat .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .4px; }
.stat .value { font-size: 24px; font-weight: 700; margin-top: 2px; }
.stat.active { border-color: var(--blue); background: #eff6ff; }

.filter-bar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
.filter-bar select, .filter-bar input { padding: 5px 10px; border: 1px solid var(--line);
       border-radius: 6px; font: inherit; background: var(--panel); }
.filter-bar select:focus, .filter-bar input:focus { outline: none; border-color: var(--blue); }
.filter-bar label { font-size: 12px; color: var(--muted); font-weight: 600; margin-left: 2px; }
.filter-bar .sep { width: 1px; background: var(--line); height: 24px; align-self: center; }
.filter-bar .chip { padding: 4px 10px; border-radius: 99px; font-size: 11px; font-weight: 600;
       cursor: pointer; border: 1px solid var(--line); background: var(--panel); }
.filter-bar .chip.active { background: var(--blue); color: #fff; border-color: var(--blue); }
.filter-bar .btn { padding: 5px 14px; border-radius: 6px; font: inherit; font-size: 12px; font-weight: 600;
       cursor: pointer; border: 1px solid var(--line); background: var(--panel); }
.filter-bar .btn:hover { background: #f4f4f5; }

.tabs { display: flex; gap: 0; margin-bottom: -1px; }
.tab { padding: 7px 18px; border: 1px solid var(--line); border-bottom: none;
       border-radius: 7px 7px 0 0; cursor: pointer; background: var(--bg);
       font-size: 12px; font-weight: 600; color: var(--muted); }
.tab.active { background: var(--panel); color: var(--text); border-bottom: 1px solid var(--panel); }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 0 8px 8px 8px;
         padding: 14px; overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; padding: 7px 8px; border-bottom: 2px solid var(--line);
     font-weight: 600; color: var(--muted); white-space: nowrap; cursor: pointer; user-select: none; }
th:hover { color: var(--text); }
td { padding: 6px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }
tr:hover td { background: #fafafa; }
.badge { display: inline-block; padding: 1px 7px; border-radius: 99px;
         font-size: 10px; font-weight: 650; }
.badge-PENDING { background: var(--badge-pending); color: var(--badge-pending-text); }
.badge-SUBMITTED { background: var(--badge-submitted); color: var(--badge-submitted-text); }
.badge-RELEASED { background: var(--badge-released); color: var(--badge-released-text); }
.badge-FAILED { background: var(--badge-failed); color: var(--badge-failed-text); }
.badge-SUPERSEDED { background: var(--badge-superseded); color: var(--badge-superseded-text); }
.hash { font-family: monospace; font-size: 10px; color: var(--muted); }
.empty { text-align: center; padding: 40px; color: var(--muted); }
.fab { position: fixed; right: 20px; z-index: 99; width: 36px; height: 36px;
       border: 1px solid var(--line); border-radius: 8px; background: var(--panel);
       cursor: pointer; display: flex; align-items: center; justify-content: center;
       color: var(--muted); font-size: 16px; transition: all .15s; box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.fab:hover { color: var(--text); border-color: #a1a1aa; box-shadow: 0 4px 12px rgba(0,0,0,.1); }
.fab-top { bottom: 64px; }
.fab-bot { bottom: 20px; }
.row-count { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
.pager { display: flex; justify-content: center; align-items: center; gap: 12px;
         padding: 14px 0 4px; }
.pager-btn { cursor: pointer; padding: 6px 12px; border: 1px solid var(--line);
             border-radius: 6px; font-size: 13px; color: var(--text);
             background: var(--panel); transition: background .15s; user-select: none; }
.pager-btn:hover { background: #f4f4f5; }
.pager-info { font-size: 13px; color: var(--muted); font-weight: 600; }
.warn { color: var(--yellow); font-size: 11px; margin: 4px 0; }
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
    <div class="tab" data-tab="overview">Overview</div>
  </div>

  <div class="panel">
    <div class="filter-bar" id="filters"></div>
    <div class="row-count" id="row-count"></div>
    <div id="content"><div class="empty">Loading...</div></div>
  </div>
</main>
<script>
const API = '/api';
let currentTab = 'batches';
let filters = { tag: '', module: '', status: '', time: '' };
let allTags = [], allModules = [];
let currentPage = 1, totalPages = 1, perPage = 50;

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
  return d.toLocaleString('sv-SE', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function fmtSize(n) {
  if (!n || n === 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let s = n;
  for (const u of units) { if (s < 1024) return u === 'B' ? s + ' B' : s.toFixed(1) + ' ' + u; s /= 1024; }
  return s.toFixed(1) + ' TB';
}

function fmtHash(h) { return h ? h.substring(0, 12) + '...' : '-'; }

function buildQuery(f, page) {
  const parts = [];
  for (const [k, v] of Object.entries(f)) {
    if (v) parts.push(k + '=' + encodeURIComponent(v));
  }
  if (page > 1) { parts.push('page=' + page); parts.push('per_page=' + perPage); }
  else if (page) { parts.push('per_page=' + perPage); }
  return parts.length ? '?' + parts.join('&') : '';
}

function renderFilters() {
  const bar = document.getElementById('filters');
  let html = '<label>Tag</label><select id="f-tag" onchange="applyFilter(\'tag\',this.value)">';
  html += '<option value="">All</option>';
  for (const t of allTags) html += '<option value="'+t+'"'+(filters.tag===t?' selected':'')+'>'+t+'</option>';
  html += '</select><span class="sep"></span>';
  html += '<label>Module</label><select id="f-module" onchange="applyFilter(\'module\',this.value)">';
  html += '<option value="">All</option>';
  for (const m of allModules) html += '<option value="'+m+'"'+(filters.module===m?' selected':'')+'>'+m+'</option>';
  html += '</select><span class="sep"></span>';
  html += '<label>Status</label>';
  for (const s of ['','PENDING','SUBMITTED','RELEASED','FAILED']) {
    const label = s || 'All';
    html += '<span class="chip'+(filters.status===s?' active':'')+'" onclick="applyFilter(\'status\',\''+s+'\')">'+label+'</span>';
  }
  html += '<span class="sep"></span>';
  html += '<label>Time</label><select id="f-time" onchange="applyFilter(\'time\',this.value)">';
  html += '<option value="">All time</option><option value="1h"'+(filters.time==='1h'?' selected':'')+'>Last hour</option>';
  html += '<option value="24h"'+(filters.time==='24h'?' selected':'')+'>Last 24h</option>';
  html += '<option value="7d"'+(filters.time==='7d'?' selected':'')+'>Last 7 days</option>';
  html += '<option value="30d"'+(filters.time==='30d'?' selected':'')+'>Last 30 days</option>';
  html += '</select>';
  html += '<span class="sep"></span>';
  html += '<button class="btn" onclick="clearFilters()">Clear</button>';
  html += '<button class="btn" onclick="refreshTab()" title="Refresh">↻</button>';
  bar.innerHTML = html;
}

function applyFilter(key, value) {
  filters[key] = value;
  renderFilters();
  loadTab(currentTab);
}

function clearFilters() {
  filters = { tag: '', module: '', status: '', time: '', sort: '' };
  renderFilters();
  loadTab(currentTab);
}

function refreshTab() { currentPage = 1; loadTab(currentTab); }

function pageNav() {
  if (totalPages <= 1) return '';
  const prevCls = currentPage <= 1 ? 'pointer-events:none;opacity:.3' : '';
  const nextCls = currentPage >= totalPages ? 'pointer-events:none;opacity:.3' : '';
  return '<div class="pager">'
    + '<span class="pager-btn" style="'+prevCls+'" onclick="goPage('+(currentPage-1)+')">◀</span>'
    + '<span class="pager-info">' + currentPage + ' / ' + totalPages + '</span>'
    + '<span class="pager-btn" style="'+nextCls+'" onclick="goPage('+(currentPage+1)+')">▶</span>'
    + '</div>';
}
function goPage(p) { if (p >= 1 && p <= totalPages) { currentPage = p; loaders[currentTab](); } }

// ---- Stats ----
async function loadStats() {
  const s = await fetchJSON('/stats'+buildQuery(filters));
  document.getElementById('stats').innerHTML = [
    { label: 'Batches', value: s.batch_count },
    { label: 'SUBMITTED', value: s.submitted },
    { label: 'RELEASED', value: s.released },
    { label: 'FAILED', value: s.failed },
    { label: 'Tags', value: s.tag_count },
    { label: 'Modules', value: s.module_count },
  ].map(x => `<div class="stat"><div class="label">${x.label}</div><div class="value">${x.value}</div></div>`).join('');
}

// ---- Batches Table ----
async function loadBatches() {
  const data = await fetchJSON('/batches'+buildQuery(filters,currentPage));
  totalPages = data.total_pages || 1;
  document.getElementById('row-count').innerHTML = data.total + ' batches'
    + (totalPages > 1 ? ' — page ' + currentPage + '/' + totalPages : '');
  if (!data.rows || !data.rows.length) {
    document.getElementById('content').innerHTML = '<div class="empty">No batches found</div>'; return;
  }
  const groups = [];
  let last = '';
  for (const b of data.rows) {
    const key = (b.module||'') + '/' + (b.tag||'');
    if (key !== last) { groups.push([]); last = key; }
    groups[groups.length-1].push(b);
  }
  const colors = ['rgba(59,130,246,0.04)', 'rgba(139,92,246,0.04)', 'rgba(16,185,129,0.04)', 'rgba(245,158,11,0.04)'];
  const rows = groups.map((g, gi) => {
    const bg = colors[gi % colors.length];
    return g.map((b, fi) => {
      const topBorder = fi === 0 ? 'border-top: 2px solid var(--line);' : '';
      return `<tr style="background:${bg};${topBorder}">
        <td>${b.batch_uuid.substring(0,8)}</td>
        <td>${badge(b.status)}</td>
        <td><b>${b.module||'-'}</b></td>
        <td>${b.tag||'-'}</td>
        <td>${b.username||'-'}</td>
        <td>${b.version||'-'}</td>
        <td>${(b.summary||'-').substring(0,40)}</td>
        <td>${fmtTime(b.created_at)}</td>
      </tr>`;
    }).join('');
  }).join('');
  document.getElementById('content').innerHTML = `<table>
    <thead><tr><th>UUID</th><th>Status</th><th>Module</th><th>Tag</th><th>User</th><th>Version</th><th>Summary</th><th>Time</th></tr></thead>
    <tbody>${rows}</tbody></table>` + pageNav();
}

// ---- Files Table (grouped by batch with visual separation) ----
async function loadFiles() {
  const data = await fetchJSON('/files'+buildQuery(filters,currentPage));
  totalPages = data.total_pages || 1;
  document.getElementById('row-count').innerHTML = data.total + ' files'
    + (totalPages > 1 ? ' — page ' + currentPage + '/' + totalPages : '');
  if (!data.rows || !data.rows.length) {
    document.getElementById('content').innerHTML = '<div class="empty">No files found</div>'; return;
  }
  // Group by batch_uuid
  const groups = [];
  let last = '';
  for (const f of data.rows) {
    if (f.batch_uuid !== last) { groups.push([]); last = f.batch_uuid; }
    groups[groups.length-1].push(f);
  }
  const colors = ['rgba(59,130,246,0.04)', 'rgba(139,92,246,0.04)', 'rgba(16,185,129,0.04)', 'rgba(245,158,11,0.04)'];
  const rows = groups.map((g, gi) => {
    const bg = colors[gi % colors.length];
    return g.map((f, fi) => {
      const fname = (f.source_path||'').split('/').pop() || '-';
      const topBorder = fi === 0 ? 'border-top: 2px solid var(--line);' : '';
      const groupBg = fi === 0 ? `background:${bg};` : `background:${bg};`;
      return `<tr style="${groupBg}${topBorder}">
        <td>${fi === 0 ? '<b>'+f.batch_uuid.substring(0,8)+'</b>' : ''}</td>
        <td>${badge(f.status)}</td>
        <td><b>${fname}</b></td>
        <td class="hash" title="${f.blake3_hash||''}">${fmtHash(f.blake3_hash)}</td>
        <td>${fmtSize(f.file_size)}</td>
        <td>${fmtSize(f.source_size)}</td>
        <td>${fmtTime(f.created_at)}</td>
      </tr>`;
    }).join('');
  }).join('');
  document.getElementById('content').innerHTML = `<table>
    <thead><tr><th>Batch</th><th>Status</th><th>File</th><th>BLAKE3</th><th>Size</th><th>Source</th><th>Time</th></tr></thead>
    <tbody>${rows}</tbody></table>` + pageNav();
}

// ---- Events Table ----
async function loadEvents() {
  const data = await fetchJSON('/events'+buildQuery(filters,currentPage));
  totalPages = data.total_pages || 1;
  document.getElementById('row-count').innerHTML = data.total + ' events'
    + (totalPages > 1 ? ' — page ' + currentPage + '/' + totalPages : '');
  if (!data.rows || !data.rows.length) {
    document.getElementById('content').innerHTML = '<div class="empty">No events found</div>'; return;
  }
  const groups = [];
  let last = '';
  for (const e of data.rows) {
    if (e.batch_uuid !== last) { groups.push([]); last = e.batch_uuid; }
    groups[groups.length-1].push(e);
  }
  const colors = ['rgba(59,130,246,0.04)', 'rgba(139,92,246,0.04)', 'rgba(16,185,129,0.04)', 'rgba(245,158,11,0.04)'];
  const rows = groups.map((g, gi) => {
    const bg = colors[gi % colors.length];
    return g.map((e, fi) => {
      const topBorder = fi === 0 ? 'border-top: 2px solid var(--line);' : '';
      return `<tr style="background:${bg};${topBorder}">
        <td>${fi === 0 ? '<b>'+e.batch_uuid.substring(0,8)+'</b>' : ''}</td>
        <td><b>${e.event_type}</b></td>
        <td>${e.message||'-'}</td>
        <td>${fmtTime(e.created_at)}</td>
      </tr>`;
    }).join('');
  }).join('');
  document.getElementById('content').innerHTML = `<table>
    <thead><tr><th>Batch</th><th>Event</th><th>Detail</th><th>Time</th></tr></thead>
    <tbody>${rows}</tbody></table>` + pageNav();
}

// ---- Overview (tag × module matrix) ----
async function loadOverview() {
  const data = await fetchJSON('/overview'+buildQuery(filters));
  if (!data.tags || !data.tags.length) {
    document.getElementById('row-count').textContent = '';
    document.getElementById('content').innerHTML = '<div class="empty">No data yet</div>'; return;
  }
  document.getElementById('row-count').textContent = data.modules.length + ' modules × ' + data.tags.length + ' tags';
  // Build matrix: rows=modules, cols=tags
  const ths = '<th>Module</th>' + data.tags.map(t => '<th>'+t+'</th>').join('');
  const trs = data.modules.map(mod => {
    const tds = data.tags.map(tag => {
      const cell = data.matrix[mod]?.[tag];
      if (!cell) return '<td style="color:var(--muted)">-</td>';
      let cls = '';
      if (cell.status === 'RELEASED') cls = 'color:var(--green);font-weight:650';
      else if (cell.status === 'SUBMITTED') cls = 'color:var(--cyan);font-weight:650';
      else if (cell.status === 'FAILED') cls = 'color:var(--red)';
      const ver = cell.version ? ' v'+cell.version : '';
      return '<td style="'+cls+'">'+cell.status+ver+'</td>';
    }).join('');
    return '<tr><td><b>'+mod+'</b></td>'+tds+'</tr>';
  }).join('');
  document.getElementById('content').innerHTML = `<table>
    <thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;
}

const loaders = { batches: loadBatches, files: loadFiles, events: loadEvents, overview: loadOverview };
const overviewTab = { batches: false, files: false, events: false, overview: true };
const noPager = { overview: true };

function switchTab(tab, pushHash) {
  currentTab = tab;
  currentPage = 1;
  if (pushHash !== false && location.hash.substring(1) !== tab) {
    history.pushState(null, '', '#' + tab);
  }
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelector('.tab[data-tab="'+tab+'"]').classList.add('active');
  loaders[tab]();
  document.getElementById('filters').style.display = overviewTab[tab] ? 'none' : '';
}

function loadTab(tab) { switchTab(tab, true); }

document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});
window.addEventListener('popstate', () => {
  const tab = location.hash.substring(1) || 'batches';
  if (loaders[tab] && tab !== currentTab) switchTab(tab, false);
});

(async () => {
  document.getElementById('db-path').textContent = location.host;
  // Load filter options
  try {
    const s = await fetchJSON('/stats');
    allTags = s.all_tags || [];
    allModules = s.all_modules || [];
  } catch(e) { console.error(e); }
  renderFilters();
  loadStats();
  const startTab = location.hash.substring(1) || 'batches';
  switchTab(startTab, false);
})();
</script>
<div class="fab fab-top" onclick="window.scrollTo({top:0,behavior:'smooth'})" title="页首">▲</div>
<div class="fab fab-bot" onclick="window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'})" title="页尾">▼</div>
</body>
</html>"""

# ======================================================================
# API handler
# ======================================================================

class APIHandler(BaseHTTPRequestHandler):
    db_path: str = "repository/ddm.db"

    def log_message(self, format, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _query(self, sql: str, params=()):
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()

    def _parse_filters(self, parsed) -> dict:
        """Parse filter + pagination params from query string."""
        qs = parse_qs(parsed.query)
        f = {}
        for key in ("tag", "module", "status", "time"):
            val = qs.get(key, [""])[0]
            if val:
                f[key] = val
        f["page"] = int(qs.get("page", ["1"])[0])
        f["per_page"] = int(qs.get("per_page", ["50"])[0])
        return f

    def _paginate(self, sql, params, filters):
        """Execute paginated query, return {total, total_pages, rows}."""
        page = filters.get("page", 1)
        per_page = filters.get("per_page", 50)
        offset = (page - 1) * per_page

        # Count total
        count_sql = f"SELECT COUNT(*) FROM ({sql})"
        db = sqlite3.connect(self.db_path)
        total = db.execute(count_sql, params).fetchone()[0]
        db.close()

        total_pages = max(1, (total + per_page - 1) // per_page)
        rows = self._query(f"{sql} LIMIT {per_page} OFFSET {offset}", params)
        return {"total": total, "total_pages": total_pages, "rows": rows}

    def _build_where(self, filters: dict, table_alias: str = "") -> tuple:
        """Build WHERE clause from filters. Returns (clause, params)."""
        prefix = table_alias + "." if table_alias else ""
        clauses = []
        params = []
        if filters.get("tag"):
            clauses.append(prefix + "tag = ?")
            params.append(filters["tag"])
        if filters.get("module"):
            clauses.append(prefix + "module = ?")
            params.append(filters["module"])
        if filters.get("status"):
            clauses.append(prefix + "status = ?")
            params.append(filters["status"])
        if filters.get("time"):
            val = filters["time"]
            unit = val[-1]
            amount = int(val[:-1])
            if unit == "h":
                secs = amount * 3600
            elif unit == "d":
                secs = amount * 86400
            else:
                secs = 0
            if secs > 0:
                clauses.append(prefix + "created_at > ?")
                params.append(time.time() - secs)
        where = " AND ".join(clauses) if clauses else "1=1"
        return where, params

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            body = PAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            if parsed.path == "/api/stats":
                db = sqlite3.connect(self.db_path)
                try:
                    filters = self._parse_filters(parsed)
                    wb, pb = self._build_where(filters)
                    batch_count = db.execute(f"SELECT COUNT(*) FROM batches WHERE {wb}", pb).fetchone()[0]
                    file_count = db.execute(f"SELECT COUNT(*) FROM files WHERE {wb}", pb).fetchone()[0]
                    submitted = db.execute(f"SELECT COUNT(*) FROM batches WHERE {wb} AND status='SUBMITTED'", pb).fetchone()[0]
                    released = db.execute(f"SELECT COUNT(*) FROM batches WHERE {wb} AND status='RELEASED'", pb).fetchone()[0]
                    failed = db.execute(f"SELECT COUNT(*) FROM batches WHERE {wb} AND status='FAILED'", pb).fetchone()[0]
                    all_tags = [r[0] for r in db.execute("SELECT DISTINCT tag FROM batches ORDER BY tag").fetchall()]
                    all_modules = [r[0] for r in db.execute("SELECT DISTINCT module FROM batches ORDER BY module").fetchall()]
                finally:
                    db.close()
                self._json({
                    "batch_count": batch_count,
                    "file_count": file_count,
                    "submitted": submitted,
                    "released": released,
                    "failed": failed,
                    "tag_count": len(all_tags),
                    "module_count": len(all_modules),
                    "all_tags": all_tags,
                    "all_modules": all_modules,
                })

            elif parsed.path == "/api/batches":
                filters = self._parse_filters(parsed)
                where, params = self._build_where(filters)
                result = self._paginate(
                    f"SELECT * FROM batches WHERE {where} ORDER BY created_at DESC",
                    params, filters)
                self._json(result)

            elif parsed.path == "/api/files":
                filters = self._parse_filters(parsed)
                where, params = self._build_where(filters, "b")
                result = self._paginate(f"""
                    SELECT f.*, b.status, b.tag, b.module FROM files f
                    JOIN batches b ON f.batch_uuid = b.batch_uuid
                    WHERE {where} ORDER BY f.created_at DESC
                """, params, filters)
                self._json(result)

            elif parsed.path == "/api/events":
                filters = self._parse_filters(parsed)
                where, params = self._build_where(filters, "b")
                result = self._paginate(f"""
                    SELECT e.*, b.tag, b.module FROM events e
                    JOIN batches b ON e.batch_uuid = b.batch_uuid
                    WHERE {where} ORDER BY e.created_at DESC
                """, params, filters)
                self._json(result)

            elif parsed.path == "/api/overview":
                filters = self._parse_filters(parsed)
                # Get all tags and modules
                tags_list = [r["tag"] for r in self._query("SELECT DISTINCT tag FROM batches ORDER BY tag") if r.get("tag")]
                modules_list = [r["module"] for r in self._query("SELECT DISTINCT module FROM batches ORDER BY module") if r.get("module")]

                # Get latest batch per (module, tag)
                all_rows = self._query("""
                    SELECT module, tag, status, version FROM batches
                    WHERE (module, tag, created_at) IN (
                        SELECT module, tag, MAX(created_at)
                        FROM batches GROUP BY module, tag
                    )
                """)

                matrix = {}
                for r in all_rows:
                    mod = r["module"]
                    tg = r["tag"]
                    if mod not in matrix:
                        matrix[mod] = {}
                    matrix[mod][tg] = {"status": r["status"], "version": r["version"]}

                self._json({"tags": tags_list, "modules": modules_list, "matrix": matrix})

            else:
                self._json({"error": "not found"}, 404)

        except Exception as e:
            self._json({"error": str(e)}, 500)


def main():
    parser = argparse.ArgumentParser(description="DDM Database Viewer")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--db", default="repository/ddm.db", help="Path to ddm.db")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address")
    args = parser.parse_args()

    APIHandler.db_path = str(Path(args.db).resolve())
    server = HTTPServer((args.host, args.port), APIHandler)
    print(f"  DDM Database Viewer")
    print(f"  http://{args.host}:{args.port}")
    print(f"  DB: {APIHandler.db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        server.server_close()


if __name__ == "__main__":
    main()

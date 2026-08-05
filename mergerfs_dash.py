#!/usr/bin/env python3
"""
mergerfs-dash — a single-file, zero-dependency web dashboard for mergerfs pools.

It answers: "how is my data distributed across the branches of my pool?"

  * Instant stats (no scan needed): per-branch total/used/free via statvfs.
  * Deep stats (background scan, cached to disk): which branch each top-level
    share's bytes live on, file-type breakdown, per-branch file/dir counts,
    and the largest files in the pool.

Scans happen ONCE on first run and thereafter only when you press the
"Rescan" button in the web UI. Results are cached in a JSON file, so
restarting the app does not re-scan.

Everything is read-only. No symlinks are followed.

----------------------------------------------------------------------------
Usage
----------------------------------------------------------------------------

On the host (auto-detects branches from the mergerfs mount):

    python3 mergerfs_dash.py --mount /storage

Explicit branches, comma-separated or as a glob like in a mergerfs fstab
(quote it so your shell doesn't expand it; this is also how Docker works):

    python3 mergerfs_dash.py --branches /mnt/disk1,/mnt/disk2,/mnt/disk3
    python3 mergerfs_dash.py --branches '/mnt/disk*'

With NO configuration at all (typical in Docker): the app discovers branches
itself — every filesystem mounted under DISCOVERY_ROOT is treated as a
branch. Map your disk parent dir read-only and you're done:

    docker run -d -p 8282:8282 -v /mnt:/branches:ro \
        -v mergerfs-dash-data:/data ghcr.io/<you>/mergerfs-dash:latest

Config can also come from environment variables (this is how Docker works):

    MOUNT          mergerfs mount point used for auto-detection
    BRANCHES       comma-separated list or glob of branch paths
                   (takes priority over MOUNT and discovery)
    DISCOVERY_ROOT where to look for mounted branches when nothing else is
                   configured (default /branches)
    PORT           listen port              (default 8282)
    CREATE_POLICY  mergerfs create policy for the "next write" panel (read
                   from the mergerfs mount automatically in host mode)
    HOST           listen address           (default 0.0.0.0)
    CACHE_PATH     where to store scan JSON (default: alongside this script,
                   /data/mergerfs_dash_cache.json in the container)

systemd unit for running on the host (Docker users don't need this):

    # /etc/systemd/system/mergerfs-dash.service
    [Unit]
    Description=mergerfs-dash
    After=network-online.target

    [Service]
    ExecStart=/usr/bin/python3 /opt/mergerfs-dash/mergerfs_dash.py --mount /storage
    Restart=on-failure
    DynamicUser=no          # run as a user that can READ all branches

    [Install]
    WantedBy=multi-user.target
"""

import argparse
import glob
import heapq
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_VERSION = "1.0.3"
DEFAULT_PORT = 8282
TOP_N_SHARES = 12        # rows in the "share distribution" chart
TOP_N_LARGEST = 10       # rows in the "largest files" list

# File-type buckets used by the scanner. Anything not listed -> "other".
_EXT_TO_CATEGORY = {}
for _cat, _exts in {
    "video": ["mkv", "mp4", "avi", "mov", "wmv", "flv", "webm", "m4v",
              "mpg", "mpeg", "ts", "m2ts", "vob", "3gp"],
    "audio": ["mp3", "flac", "wav", "aac", "ogg", "opus", "m4a", "wma", "aiff"],
    "images": ["jpg", "jpeg", "png", "gif", "bmp", "webp", "tiff", "tif",
               "svg", "raw", "heic", "heif"],
    "docs": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt",
             "md", "odt", "epub", "mobi", "cbz", "cbr"],
    "archives": ["zip", "tar", "gz", "bz2", "xz", "7z", "rar", "iso"],
}.items():
    for _e in _exts:
        _EXT_TO_CATEGORY[_e] = _cat

CATEGORIES = ["video", "audio", "images", "docs", "archives", "other"]


def safe_text(s):
    """Make a possibly surrogate-escaped filename safe for JSON output."""
    return s.encode("utf-8", "replace").decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Branch discovery
# ---------------------------------------------------------------------------

def detect_from_xattr(mount):
    """Ask mergerfs for its branch list via the runtime xattr API."""
    raw = os.getxattr(mount, "user.mergerfs.srcmounts")
    branches = [b for b in raw.decode().strip().split(":") if b]
    version = None
    try:
        version = os.getxattr(mount, "user.mergerfs.version").decode().strip()
    except OSError:
        pass
    return branches, version


def detect_from_proc_mounts():
    """Last-resort fallback: find any fuse.mergerfs entry in /proc/mounts."""
    with open("/proc/mounts") as fh:
        for line in fh:
            fields = line.split(" ")
            if len(fields) >= 3 and fields[2].startswith("fuse.mergerfs"):
                source = fields[0].replace("\\040", " ")
                mount = fields[1].replace("\\040", " ")
                branches = []
                for chunk in source.split(":"):
                    if any(c in chunk for c in "*?["):
                        branches.extend(sorted(glob.glob(chunk)))
                    elif os.path.isdir(chunk):
                        branches.append(chunk)
                return branches, mount
    return None, None


def discover_mounted_branches(root, max_depth=4):
    """Docker-friendly auto-discovery: every directory under `root` that is
    its own filesystem (a mount point) is treated as a branch.

    A mount is detected by st_dev differing from its parent directory.
    Same-device directories are descended into, so nested layouts like
    /branches/disks/disk1 work the same as flat ones. Mounts are not
    descended into further. Note: only the mounts visible at process start
    are seen (Docker mount propagation), so adding a disk later means
    restarting the container.
    """
    found = []
    try:
        root_dev = os.stat(root).st_dev
    except OSError:
        return found
    stack = [(root, root_dev, 0)]
    while stack:
        current, parent_dev, depth = stack.pop()
        if depth >= max_depth:
            continue
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    dev = os.stat(entry.path, follow_symlinks=False).st_dev
                except OSError:
                    continue
                if dev != parent_dev:
                    found.append(entry.path)          # a mounted filesystem
                else:
                    stack.append((entry.path, dev, depth + 1))  # plain dir
    return sorted(found)


def branch_display_names(paths):
    """Short, unique display names: basenames, unless they collide."""
    base = [os.path.basename(p.rstrip("/")) or p for p in paths]
    return base if len(set(base)) == len(base) else paths


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class State:
    def __init__(self, branch_paths, cache_path, version=None,
                 create_policy=None, policy_assumed=True):
        self.branch_paths = branch_paths
        self.branch_names = branch_display_names(branch_paths)
        self.cache_path = cache_path
        self.version = version
        self.create_policy = create_policy
        self.policy_assumed = policy_assumed
        self.lock = threading.Lock()
        self.scanning = False
        self.scan_error = None
        self.files_seen = 0          # live progress counter during a scan
        self.scan_data = None        # last completed scan (dict) or None

    def load_cache(self):
        try:
            with open(self.cache_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return False
        # If the branch config changed since that scan, the cached numbers
        # belong to a different pool layout — discard and let a fresh scan
        # run instead of showing stale data.
        if data.get("branch_order") != list(self.branch_names):
            return False
        self.scan_data = data
        return True

    def save_cache(self):
        tmp = self.cache_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump(self.scan_data, fh)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            self.scan_error = f"could not write cache: {exc}"


def scan_branch(root, state):
    """Walk one branch. Returns the per-branch aggregate dict."""
    data = {"files": 0, "dirs": 0, "bytes": 0, "errors": 0, "shares": {}}
    largest = []  # small heap of (size, path) for this branch
    categories = {c: {"bytes": 0, "files": 0} for c in CATEGORIES}

    # Iterative depth-first walk. `top` is the top-level (share) name the
    # current directory belongs to; None means "we are at the branch root".
    stack = [(root, None)]
    while stack:
        dirpath, top = stack.pop()
        try:
            entries = os.scandir(dirpath)
        except OSError:
            data["errors"] += 1
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        data["dirs"] += 1
                        stack.append((entry.path,
                                      top if top is not None else entry.name))
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat(follow_symlinks=False).st_size
                        share = top if top is not None else "(root)"
                        agg = data["shares"].setdefault(safe_text(share),
                                                        {"bytes": 0, "files": 0})
                        agg["bytes"] += size
                        agg["files"] += 1
                        data["files"] += 1
                        data["bytes"] += size
                        state.files_seen += 1

                        ext = os.path.splitext(entry.name)[1][1:].lower()
                        cat = _EXT_TO_CATEGORY.get(ext, "other")
                        categories[cat]["bytes"] += size
                        categories[cat]["files"] += 1

                        item = (size, safe_text(entry.path))
                        if len(largest) < TOP_N_LARGEST:
                            heapq.heappush(largest, item)
                        elif size > largest[0][0]:
                            heapq.heapreplace(largest, item)
                except OSError:
                    data["errors"] += 1

    data["categories"] = categories
    data["largest"] = [{"path": p, "size": s}
                       for s, p in sorted(largest, reverse=True)]
    return data


def run_scan(state):
    """Scan every branch, then merge and cache the results."""
    state.scanning = True
    state.scan_error = None
    state.files_seen = 0
    started = time.time()
    try:
        branches = {}
        pool_cats = {c: {"bytes": 0, "files": 0} for c in CATEGORIES}
        pool_largest = []

        for name, path in zip(state.branch_names, state.branch_paths):
            result = scan_branch(path, state)

            # Merge per-branch categories into pool-wide totals.
            for cat, vals in result.pop("categories").items():
                pool_cats[cat]["bytes"] += vals["bytes"]
                pool_cats[cat]["files"] += vals["files"]

            for item in result.pop("largest"):
                pool_largest.append({"branch": name, **item})

            branches[name] = result

        pool_largest.sort(key=lambda i: i["size"], reverse=True)
        state.scan_data = {
            "scanned_at": time.time(),
            "duration_s": round(time.time() - started, 2),
            "branch_order": list(state.branch_names),
            "branches": branches,
            "categories": pool_cats,
            "largest": pool_largest[:TOP_N_LARGEST],
        }
        state.save_cache()
    except Exception as exc:  # a scan must never take the server down
        state.scan_error = f"scan failed: {exc}"
    finally:
        state.scanning = False


# ---------------------------------------------------------------------------
# Live capacity stats (cheap: re-read on every request, no scanning needed)
# ---------------------------------------------------------------------------

def statvfs_info(path):
    st = os.statvfs(path)
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    inodes_total = st.f_files
    inodes_free = st.f_favail
    return {
        "total": total,
        "free": free,
        "used": total - free,
        "inodes_total": inodes_total,
        "inodes_free": inodes_free,
        "inodes_used": (inodes_total - inodes_free) if inodes_total else None,
    }


def predict_next_write(branches, policy, assumed):
    """Estimate which branch receives the next create, per mergerfs policy.

    Only 'which branch wins' resolutions are modelled; mergerfs details like
    minfreespace and existing-path filtering are intentionally ignored, so
    this is a good-faith estimate for the common policies.
    """
    if policy:
        policy = policy.lower()
    else:
        policy = "mfs"   # mergerfs' spirit-default family; flagged assumed
    ok = [b for b in branches if b.get("ok") and b["total"] > 0]
    if not ok:
        return None

    if policy in ("mfs", "epmfs"):
        target = max(ok, key=lambda b: b["free"])
        return {"mode": "branch", "policy": policy, "assumed": assumed,
                "branch": target["name"], "free": target["free"],
                "reason": "most free space"}
    if policy == "lus":
        target = min(ok, key=lambda b: b["used"])
        return {"mode": "branch", "policy": policy, "assumed": assumed,
                "branch": target["name"], "free": target["free"],
                "reason": "least used space"}
    if policy in ("lfs", "eplus"):
        target = min(ok, key=lambda b: b["free"])
        return {"mode": "branch", "policy": policy, "assumed": assumed,
                "branch": target["name"], "free": target["free"],
                "reason": "least free space"}
    if policy in ("ff", "eoff", "pfrd"):
        return {"mode": "branch", "policy": policy, "assumed": assumed,
                "branch": ok[0]["name"], "free": ok[0]["free"],
                "reason": "first eligible branch"}
    if policy in ("rand", "erand"):
        return {"mode": "text", "policy": policy, "assumed": assumed,
                "text": "a randomly chosen branch"}
    if policy in ("all", "eall"):
        return {"mode": "text", "policy": policy, "assumed": assumed,
                "text": "ALL branches (one copy each)"}
    if policy in ("newest",):
        return {"mode": "text", "policy": policy, "assumed": assumed,
                "text": "the branch holding the newest parent dir"}
    return {"mode": "text", "policy": policy, "assumed": assumed,
            "text": f"policy-dependent ({policy})"}


def build_stats(state):
    branches = []
    totals = {"total": 0, "free": 0}
    for name, path in zip(state.branch_names, state.branch_paths):
        entry = {"name": name, "path": path}
        try:
            info = statvfs_info(path)
            entry["ok"] = True
            entry.update(info)
            totals["total"] += info["total"]
            totals["free"] += info["free"]
        except OSError:
            entry["ok"] = False
        branches.append(entry)
    totals["used"] = totals["total"] - totals["free"]

    return {
        "app_version": APP_VERSION,
        "mergerfs_version": state.version,
        "now": time.time(),
        "pool": {"branches": branches,
                 "next_write": predict_next_write(branches, state.create_policy,
                                                  state.policy_assumed),
                 **totals},
        "scan": {
            "scanning": state.scanning,
            "files_seen": state.files_seen,
            "error": state.scan_error,
            "data": state.scan_data,
        },
    }


# ---------------------------------------------------------------------------
# Web UI (single page, no external assets: charts are drawn with inline SVG)
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mergerfs-dash</title>
<style>
:root{
  --bg:#0e1116; --panel:#161b22; --border:#262c36;
  --text:#e6e9ef; --dim:#8b95a5;
  --good:#3fca7b; --warn:#f0b429; --bad:#f7724f; --accent:#4f8ef7;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg); color:var(--text);
  font:14px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  padding:24px; max-width:1200px; margin:0 auto;
}
header{display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:20px}
h1{font-size:20px}
.chip{background:var(--panel); border:1px solid var(--border); border-radius:999px;
      padding:3px 10px; font-size:12px; color:var(--dim)}
.spacer{flex:1}
button{background:var(--accent); border:0; color:#fff; padding:8px 16px;
       border-radius:8px; cursor:pointer; font-weight:600; font-size:13px}
button:disabled{opacity:.45; cursor:default}
.cards{display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
       gap:14px; margin-bottom:14px}
.card,.panel{background:var(--panel); border:1px solid var(--border);
             border-radius:12px; padding:16px}
.card .l{color:var(--dim); font-size:11px; text-transform:uppercase; letter-spacing:.07em}
.card .v{font-size:22px; font-weight:700; margin-top:4px}
.grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(360px,1fr)); gap:14px}
.wide{grid-column:1/-1}
.panel h2{font-size:12px; text-transform:uppercase; letter-spacing:.07em;
          color:var(--dim); margin-bottom:14px}
.fillbar{height:8px; background:#0b0e13; border-radius:5px; overflow:hidden}
.fillbar > span{display:block; height:100%; border-radius:5px}
.marginrows > div{margin:10px 0}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
.muted{color:var(--dim)}
.small{font-size:12px}
.good{color:var(--good)} .warn{color:var(--warn)} .bad{color:var(--bad)}
/* doughnut legend */
.donutwrap{display:flex; align-items:center; gap:18px; flex-wrap:wrap}
.donutholder{position:relative; width:170px; height:170px; flex:none}
.donutcenter{position:absolute; inset:0; display:flex; flex-direction:column;
             align-items:center; justify-content:center; text-align:center}
.donutcenter .big{font-size:18px; font-weight:700}
.legend{flex:1; min-width:160px}
.legend .row{display:flex; align-items:center; gap:8px; margin:6px 0; font-size:13px}
.dot{width:10px; height:10px; border-radius:3px; flex:none}
/* stacked share bars */
.sharerow{margin:10px 0}
.sharerow .top{display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px}
.stackbar{display:flex; height:10px; border-radius:5px; overflow:hidden; background:#0b0e13}
.stackbar > span{display:block; height:100%}
/* category bars */
.catrow{display:grid; grid-template-columns:90px 1fr 90px; gap:10px;
        align-items:center; margin:9px 0; font-size:13px}
.catrow .val{text-align:right}
/* table */
table{width:100%; border-collapse:collapse; font-size:13px}
th,td{padding:7px 10px; border-bottom:1px solid var(--border); text-align:left}
th{color:var(--dim); font-weight:600; white-space:nowrap}
td.num,th.num{text-align:right; font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
/* largest files list */
ol.largest{padding-left:22px; font-size:13px}
ol.largest li{margin:7px 0}
ol.largest .fp{color:var(--dim); font-size:11px; word-break:break-all}
.banner{background:#3a2517; border:1px solid #7c521e; color:#ffd9a0;
        border-radius:10px; padding:12px 16px; margin-bottom:14px}
.spin{display:inline-block; width:12px; height:12px; border:2px solid var(--dim);
      border-top-color:transparent; border-radius:50%; animation:rot 0.8s linear infinite;
      vertical-align:-2px; margin-right:6px}
@keyframes rot{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<header>
  <h1>mergerfs&#8209;dash</h1>
  <span class="chip" id="verchip">mergerfs …</span>
  <span class="chip" id="scanchip">…</span>
  <span class="spacer"></span>
  <span class="muted small" id="updated"></span>
  <button id="rescan" onclick="rescan()">Rescan</button>
</header>

<div id="banner" class="banner" style="display:none"></div>

<div class="cards">
  <div class="card"><div class="l">Pool total</div><div class="v" id="c_total">—</div></div>
  <div class="card"><div class="l">Used</div><div class="v" id="c_used">—</div></div>
  <div class="card"><div class="l">Free</div><div class="v" id="c_free">—</div></div>
  <div class="card"><div class="l">Branches</div><div class="v" id="c_branches">—</div></div>
</div>

<div class="grid">

  <div class="panel">
    <h2>Used space per branch</h2>
    <div class="donutwrap">
      <div class="donutholder">
        <svg id="donut" viewBox="0 0 42 42" width="170" height="170"></svg>
        <div class="donutcenter">
          <div class="big" id="donut_used"></div>
          <div class="muted small">used</div>
        </div>
      </div>
      <div class="legend" id="donut_legend"></div>
    </div>
  </div>

  <div class="panel">
    <h2>Balance</h2>
    <div id="balance" class="marginrows"></div>
  </div>

  <div class="panel">
    <h2>Next write goes to</h2>
    <div id="nextwrite"></div>
  </div>

  <div class="panel wide">
    <h2>Where each share lives (scan data)</h2>
    <div id="shares"></div>
  </div>

  <div class="panel">
    <h2>By file type (scan data)</h2>
    <div id="cats"></div>
  </div>

  <div class="panel">
    <h2>Largest files</h2>
    <div id="largest"></div>
  </div>

  <div class="panel wide">
    <h2>Branches</h2>
    <div style="overflow-x:auto">
    <table id="branchtable">
      <thead><tr>
        <th>Branch</th><th class="num">Total</th><th class="num">Used</th>
        <th class="num">Free</th><th class="num">Fill</th><th class="num">Files</th>
        <th class="num">Dirs</th><th class="num">Unreadable</th>
        <th class="num">Inodes free</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

</div>

<script>
const PALETTE = ["#4f8ef7","#f7724f","#3fca7b","#c95bf0","#f0b429",
                 "#3fc9cf","#ef4f91","#a3e048","#8b7cf6","#f6a03c",
                 "#5ad1a5","#d9604f"];
const TOPN = 12;

function fmtBytes(n){
  if(n===null||n===undefined||isNaN(n)) return "—";
  const u=["B","KiB","MiB","GiB","TiB","PiB"]; let i=0;
  while(n>=1024 && i<u.length-1){ n/=1024; i++; }
  return (i===0 ? n : n.toFixed(1)) + " " + u[i];
}
function fmtNum(n){ return (n===null||n===undefined) ? "—" : n.toLocaleString(); }
function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function fillColor(pct){ return pct<0.75 ? "var(--good)" : pct<0.9 ? "var(--warn)" : "var(--bad)"; }
function ago(ts){
  if(!ts) return "never";
  const s = Math.max(0, (Date.now()/1000) - ts);
  if(s<60) return "just now";
  if(s<3600) return Math.floor(s/60)+" min ago";
  if(s<86400) return Math.floor(s/3600)+" h ago";
  return Math.floor(s/86400)+" d ago";
}

function branchColor(i){ return PALETTE[i % PALETTE.length]; }

function render(s){
  // ---- header cards -------------------------------------------------
  const pool = s.pool;
  document.getElementById("c_total").textContent = fmtBytes(pool.total);
  document.getElementById("c_used").textContent  = fmtBytes(pool.used);
  document.getElementById("c_free").textContent  = fmtBytes(pool.free);
  document.getElementById("c_branches").textContent = pool.branches.length;
  document.getElementById("verchip").textContent =
      "mergerfs " + (s.mergerfs_version || "version n/a") + " · app v" + s.app_version;
  document.getElementById("updated").textContent =
      "updated " + new Date(s.now*1000).toLocaleTimeString() +
      " · auto-refresh 30s";

  // ---- scanning chip / button ---------------------------------------
  const chip = document.getElementById("scanchip");
  const btn  = document.getElementById("rescan");
  if(s.scan.scanning){
    chip.innerHTML = '<span class="spin"></span>scanning… ' +
                     fmtNum(s.scan.files_seen) + " files";
    btn.disabled = true;
  } else {
    const d = s.scan.data;
    chip.textContent = "scanned " + (d ? ago(d.scanned_at) +
        " · took " + d.duration_s + "s" : "never");
    btn.disabled = false;
  }

  // ---- error banner ---------------------------------------------------
  const banner = document.getElementById("banner");
  if(s.scan.error){
    banner.style.display = "block";
    banner.textContent = s.scan.error;
  } else {
    banner.style.display = "none";
  }

  renderDonut(pool);
  renderBalance(pool);
  renderNextWrite(pool);
  renderBranchTable(s);

  const d = s.scan.data;
  renderShares(d, pool);
  renderCats(d);
  renderLargest(d);
}

function renderDonut(pool){
  const svg  = document.getElementById("donut");
  const leg  = document.getElementById("donut_legend");
  const okBranches = pool.branches.filter(b => b.ok);
  const totalUsed = okBranches.reduce((a,b) => a + b.used, 0);
  document.getElementById("donut_used").textContent = fmtBytes(totalUsed);
  if(!totalUsed){ svg.innerHTML=""; leg.innerHTML='<span class="muted">no data</span>'; return; }

  let start = 0, circles = "", legend = "";
  okBranches.forEach((b,i) => {
    const pct  = b.used/totalUsed*100;
    const col  = branchColor(i);
    // pathLength=100 lets us use plain percentages for the arc segments
    circles += '<circle r="15.9155" cx="21" cy="21" fill="none" stroke="'+col+
      '" stroke-width="6" pathLength="100" stroke-dasharray="'+pct+' '+(100-pct)+
      '" stroke-dashoffset="'+(25-start)+'"></circle>';
    legend += '<div class="row"><span class="dot" style="background:'+col+'"></span>'+
      '<span style="flex:1">'+esc(b.name)+'</span><span class="muted">'+
      fmtBytes(b.used)+' · '+pct.toFixed(1)+'%</span></div>';
    start += pct;
  });
  svg.innerHTML = circles;
  leg.innerHTML = legend;
}

function renderBalance(pool){
  const el = document.getElementById("balance");
  const ok = pool.branches.filter(b => b.ok && b.total > 0);
  if(!ok.length){ el.innerHTML = '<span class="muted">no data</span>'; return; }

  const fills = ok.map(b => b.used / b.total);
  const spread = Math.max(...fills) - Math.min(...fills);

  let msg, cls;
  if(ok.length === 1){ msg = "Only one branch — nothing to balance."; cls = "muted"; }
  else if(spread < 0.02){ msg = "Nicely balanced."; cls = "good"; }
  else if(spread < 0.10){ msg = "Slightly uneven, probably fine."; cls = "warn"; }
  else { msg = "Lopsided — consider running mergerfs.balance."; cls = "bad"; }

  let html = '<div class="'+cls+'" style="margin-bottom:6px">'+
             'Fill spread: '+(spread*100).toFixed(1)+'% — '+msg+'</div>';
  ok.forEach((b,i) => {
    const pct = b.used/b.total;
    html += '<div><div class="small" style="display:flex;justify-content:space-between">'+
      '<span>'+esc(b.name)+'</span><span class="muted">'+(pct*100).toFixed(1)+'%</span></div>'+
      '<div class="fillbar"><span style="width:'+(pct*100)+'%;background:'+
      fillColor(pct)+'"></span></div></div>';
  });
  el.innerHTML = html;
}

function renderNextWrite(pool){
  const el = document.getElementById("nextwrite");
  const nw = pool.next_write;
  if(!nw){ el.innerHTML = '<span class="muted">no data</span>'; return; }
  let body;
  if(nw.mode === "branch"){
    const idx = pool.branches.findIndex(b => b.name === nw.branch);
    const col = branchColor(Math.max(idx, 0));
    body = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'+
      '<span class="dot" style="background:'+col+';width:12px;height:12px"></span>'+
      '<span style="font-size:20px;font-weight:700">'+esc(nw.branch)+'</span></div>'+
      '<div class="muted small">'+esc(nw.reason)+' · '+fmtBytes(nw.free)+' free</div>';
  } else {
    body = '<div style="font-size:15px">'+esc(nw.text)+'</div>';
  }
  body += '<div class="muted small" style="margin-top:10px">policy: '+
    '<span class="mono">'+esc(nw.policy)+'</span>'+
    (nw.assumed ? ' <span class="warn">(assumed — set CREATE_POLICY to match your mergerfs config)</span>' : '')+
    '<br>estimate only — mergerfs also applies minfreespace and '+
    'existing-path rules</div>';
  el.innerHTML = body;
}

function renderShares(d, pool){
  const el = document.getElementById("shares");
  if(!d){
    el.innerHTML = '<span class="muted">No scan data yet — press Rescan '+
                   'or wait for the first scan.</span>';
    return;
  }
  // share name -> { per-branch bytes, total }
  const order = d.branch_order;
  const shares = {};
  order.forEach(bn => {
    const br = d.branches[bn];
    if(!br) return;
    Object.entries(br.shares).forEach(([share, v]) => {
      if(!shares[share]) shares[share] = {total:0, per:{}};
      shares[share].per[bn] = v.bytes;
      shares[share].total += v.bytes;
    });
  });
  let rows = Object.entries(shares).sort((a,b) => b[1].total - a[1].total);
  let restTotal = 0;
  if(rows.length > TOPN){
    restTotal = rows.slice(TOPN).reduce((a,r) => a + r[1].total, 0);
    rows = rows.slice(0, TOPN);
  }
  const maxTotal = rows.length ? rows[0][1].total : 1;

  let html = "";
  rows.forEach(([name, info]) => {
    let segs = "";
    order.forEach((bn, i) => {
      const bytes = info.per[bn] || 0;
      if(!bytes) return;
      const w = (bytes / info.total * 100);
      segs += '<span style="width:'+w+'%;background:'+branchColor(i)+
              '" title="'+esc(bn)+': '+fmtBytes(bytes)+'"></span>';
    });
    html += '<div class="sharerow"><div class="top"><span>'+esc(name)+
      '</span><span class="muted">'+fmtBytes(info.total)+'</span></div>'+
      '<div class="stackbar" style="width:'+(info.total/maxTotal*100)+
      '%;min-width:2px">'+segs+'</div></div>';
  });
  if(restTotal > 0){
    html += '<div class="small muted" style="margin-top:8px">+ '+
            fmtBytes(restTotal)+' in smaller shares</div>';
  }
  // colour key across all rows
  html += '<div class="small muted" style="margin-top:10px;display:flex;gap:14px;flex-wrap:wrap">';
  order.forEach((bn,i) => {
    html += '<span><span class="dot" style="display:inline-block;vertical-align:-1px;'+
            'background:'+branchColor(i)+';margin-right:5px"></span>'+esc(bn)+'</span>';
  });
  html += '</div>';
  el.innerHTML = html;
}

function renderCats(d){
  const el = document.getElementById("cats");
  if(!d){ el.innerHTML = '<span class="muted">No scan data yet.</span>'; return; }
  const entries = Object.entries(d.categories).filter(([,v]) => v.bytes > 0)
                        .sort((a,b) => b[1].bytes - a[1].bytes);
  const max = entries.length ? entries[0][1].bytes : 1;
  let html = "";
  entries.forEach(([cat, v]) => {
    html += '<div class="catrow"><span>'+esc(cat)+'</span>'+
      '<div class="fillbar"><span style="width:'+(v.bytes/max*100)+
      '%;background:var(--accent)"></span></div>'+
      '<span class="val muted">'+fmtBytes(v.bytes)+'</span></div>';
  });
  el.innerHTML = html || '<span class="muted">No files seen.</span>';
}

function renderLargest(d){
  const el = document.getElementById("largest");
  if(!d){ el.innerHTML = '<span class="muted">No scan data yet.</span>'; return; }
  if(!d.largest.length){ el.innerHTML = '<span class="muted">No files seen.</span>'; return; }
  let html = '<ol class="largest">';
  d.largest.forEach(f => {
    const base = f.path.split("/").pop();
    html += '<li><span class="mono">'+esc(base)+'</span> '+
      '<span class="muted">· '+fmtBytes(f.size)+' · '+esc(f.branch)+'</span>'+
      '<div class="fp mono">'+esc(f.path)+'</div></li>';
  });
  html += '</ol>';
  el.innerHTML = html;
}

function renderBranchTable(s){
  const tbody = document.querySelector("#branchtable tbody");
  const scanD = s.scan.data;
  let html = "";
  s.pool.branches.forEach(b => {
    let cells;
    if(!b.ok){
      cells = '<td colspan="8" class="bad">unreachable</td>';
    } else {
      const pct = b.used / b.total;
      let files = "—", dirs = "—", unreadable = "—";
      if(scanD && scanD.branches[b.name]){
        files = fmtNum(scanD.branches[b.name].files);
        dirs  = fmtNum(scanD.branches[b.name].dirs);
        const errs = scanD.branches[b.name].errors || 0;
        unreadable = errs ? '<span style="color:var(--bad)">'+errs+'</span>' : "0";
      }
      cells = '<td class="num">'+fmtBytes(b.total)+'</td>'+
              '<td class="num">'+fmtBytes(b.used)+'</td>'+
              '<td class="num">'+fmtBytes(b.free)+'</td>'+
              '<td class="num" style="color:'+fillColor(pct)+'">'+
                  (pct*100).toFixed(1)+'%</td>'+
              '<td class="num">'+files+'</td>'+
              '<td class="num">'+dirs+'</td>'+
              '<td class="num">'+unreadable+'</td>'+
              '<td class="num">'+fmtNum(b.inodes_free)+'</td>';
    }
    html += '<tr><td><div>'+esc(b.name)+'</div>'+
            '<div class="small muted mono">'+esc(b.path)+'</div></td>'+
            cells+'</tr>';
  });
  tbody.innerHTML = html;
}

// -------------------------------------------------------------------------
let pollTimer = null;
async function load(){
  try{
    const r = await fetch("/api/stats");
    render(await r.json());
  }catch(e){ /* server busy/restarting; next tick will retry */ }
  // while scanning, poll faster so the progress counter feels alive
  const scanning = document.getElementById("rescan").disabled;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(load, scanning ? 2000 : 30000);
}
async function rescan(){
  document.getElementById("rescan").disabled = true;
  try{ await fetch("/api/rescan", {method:"POST"}); }catch(e){}
  clearTimeout(pollTimer);
  pollTimer = setTimeout(load, 500);
}
load();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"mergerfs-dash/{APP_VERSION}"
    state = None  # set by main()

    def log_message(self, *args):  # keep logs clean; poll every 30s is noise
        pass

    def _respond(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload, code=200):
        self._respond(code, json.dumps(payload), "application/json")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._respond(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/stats":
            self._json(build_stats(self.state))
        elif path == "/healthz":
            self._json({"ok": True})
        else:
            self._respond(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/rescan":
            if self.state.scanning:
                self._json({"ok": False, "message": "scan already running"})
            else:
                threading.Thread(target=run_scan, args=(self.state,),
                                 daemon=True).start()
                self._json({"ok": True, "scanning": True})
        else:
            self._respond(404, "not found", "text/plain")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="Single-file web dashboard for mergerfs pools.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  %(prog)s --mount /storage\n"
               "  %(prog)s --branches /mnt/disk1,/mnt/disk2 --port 9000\n")
    p.add_argument("--mount", default=os.environ.get("MOUNT"),
                   help="mergerfs mount point (branches auto-detected via xattr)")
    p.add_argument("--branches", default=os.environ.get("BRANCHES"),
                   help="comma-separated branch paths or glob patterns like "
                        "'/mnt/disk*' (takes priority over --mount)")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("PORT", DEFAULT_PORT)),
                   help=f"listen port (default {DEFAULT_PORT})")
    p.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"),
                   help="listen address (default 0.0.0.0)")
    default_cache = os.environ.get("CACHE_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "mergerfs_dash_cache.json")
    p.add_argument("--cache", default=default_cache,
                   help="location of the scan cache (default: next to this script)")
    p.add_argument("--discover-root",
                   default=os.environ.get("DISCOVERY_ROOT", "/branches"),
                   help="where to look for mounted branches when nothing else "
                        "is configured (default /branches; map your disk "
                        "parent dir there in Docker)")
    p.add_argument("--policy", default=os.environ.get("CREATE_POLICY"),
                   help="mergerfs create policy (mfs, epmfs, lfs, ff, rand, "
                        "...), used for the 'next write' panel; read from the "
                        "mergerfs mount automatically in host mode")
    return p.parse_args(argv)


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)  # visible startup logs
    except AttributeError:
        pass
    args = parse_args(argv if argv is not None else sys.argv[1:])

    version = None
    mergerfs_mount = None
    if args.branches:
        # Entries may be shell-style globs (e.g. "/branches/disk*"), just
        # like in a mergerfs fstab. Expand them here.
        branches = []
        for raw in args.branches.split(","):
            raw = raw.strip()
            if not raw:
                continue
            if any(c in raw for c in "*?["):
                matches = sorted(g for g in glob.glob(raw) if os.path.isdir(g))
                if not matches:
                    print(f"warning: pattern '{raw}' matched no directories; "
                          f"skipping", file=sys.stderr)
                branches.extend(matches)
            else:
                branches.append(raw)
    elif args.mount:
        mergerfs_mount = args.mount
        try:
            branches, version = detect_from_xattr(args.mount)
        except OSError as exc:
            sys.exit(f"error: could not read mergerfs xattrs on {args.mount}: {exc}\n"
                     f"Is '{args.mount}' a mergerfs mount? "
                     f"(check with: getfattr -n user.mergerfs.srcmounts {args.mount})")
        if not branches:
            sys.exit(f"error: mergerfs on {args.mount} reported no branches.")
    else:
        try:
            branches, mount = detect_from_proc_mounts()
        except OSError:  # no /proc/mounts (e.g. macOS)
            branches, mount = None, None
        if branches:
            mergerfs_mount = mount
            print(f"auto-detected mergerfs mount at {mount}")
            try:
                _, version = detect_from_xattr(mount)
            except OSError:
                pass
        else:
            branches = discover_mounted_branches(args.discover_root)
            if branches:
                print(f"auto-discovered {len(branches)} mounted branch(es) "
                      f"under {args.discover_root}")
            else:
                sys.exit(
                    f"error: no branches configured, no mergerfs mount found, "
                    f"and no mounted filesystems under '{args.discover_root}'.\n"
                    f"Use --mount /path/to/mergerfs, --branches a,b,c, or map "
                    f"your disks under {args.discover_root}.")

    branches = list(dict.fromkeys(branches))  # dedupe, keep order
    missing = [b for b in branches if not os.path.isdir(b)]
    for b in missing:
        print(f"warning: branch '{b}' does not exist or is not a directory; skipping",
              file=sys.stderr)
    branches = [b for b in branches if b not in missing]
    if not branches:
        sys.exit("error: no usable branches found.")

    # Create-policy resolution: explicit flag/env wins; otherwise read the
    # live value from mergerfs (host mode only); else assume most-free-space.
    create_policy = args.policy
    policy_assumed = not bool(args.policy)
    if not create_policy and mergerfs_mount:
        try:
            create_policy = os.getxattr(
                mergerfs_mount, "user.mergerfs.create").decode().strip() or None
            policy_assumed = create_policy is None
        except OSError:
            create_policy = None
            policy_assumed = True

    state = State(branches, args.cache, version=version,
                  create_policy=create_policy, policy_assumed=policy_assumed)
    had_cache = state.load_cache()
    if had_cache:
        cache_note = "loaded, scanned " + time.ctime(state.scan_data["scanned_at"])
    elif os.path.exists(args.cache):
        cache_note = "branch layout changed — discarded, will rescan"
    else:
        cache_note = "none yet"

    print(f"mergerfs-dash v{APP_VERSION}")
    if version:
        print(f"mergerfs version : {version}")
    print(f"branches ({len(branches)}):")
    for name, path in zip(state.branch_names, branches):
        print(f"  {name}: {path}")
    print(f"cache            : {args.cache} ({cache_note})")
    print(f"listening on     : http://{args.host}:{args.port}")

    if not had_cache:
        print("no usable cache — starting background scan "
              "(the page works immediately from live capacity data)")
        threading.Thread(target=run_scan, args=(state,), daemon=True).start()

    Handler.state = state
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()


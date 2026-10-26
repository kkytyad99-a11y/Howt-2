# web_hosting_panel.py - COMPLETE FIXED VERSION v4.0
# ✅ Randar suspend fix - Memory + Logs optimized
# ✅ Ring buffer system (no memory leak)
# ✅ 5MB log rotation
# ✅ 30s frontend refresh (CPU kam)

from __future__ import annotations
import io, json, os, secrets, shutil, signal, subprocess, sys, threading, time, zipfile, gc
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from flask import Flask, render_template_string, request, jsonify, send_file
from flask_cors import CORS

# ============================================================
# 🔑 CONFIGURATION
# ============================================================
PORT = int(os.environ.get("PORT", 5000))
MAX_BOTS = 20
MAX_FILE_SIZE = 500 * 1024 * 1024
MAX_STORAGE_MB = 4500
MAX_LOG_SIZE_MB = 5          # ✅ 5MB pe rotate (was 50MB)
LOG_RETENTION_DAYS = 1        # ✅ 1 din (was 2)
MAX_RAM_LOG_LINES = 100       # ✅ Sirf 100 lines RAM mein (was 500)
LOG_CACHE_TTL = 5             # ✅ 5 sec cache for logs API

# ============================================================
# 📁 DIRECTORY SETUP
# ============================================================
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
BOTS_DIR = STORAGE_DIR / "bots"
LOGS_DIR = STORAGE_DIR / "logs"
BACKUP_DIR = STORAGE_DIR / "backups"
TEMP_DIR = STORAGE_DIR / "temp"

for d in [STORAGE_DIR, BOTS_DIR, LOGS_DIR, BACKUP_DIR, TEMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

DB_FILE = STORAGE_DIR / "db.json"
RUNNING_BOTS: Dict[str, Dict[str, Any]] = {}
START_TIME = time.time()
BOT_RUNNING = True
MEMORY_LIMIT_MB = 600

# ✅ Log cache (API spam rokne ke liye)
_log_cache: Dict[str, Dict[str, Any]] = {}

# ============================================================
# 💾 DATABASE FUNCTIONS
# ============================================================
_db_lock = threading.RLock()

def load_db() -> Dict[str, Any]:
    with _db_lock:
        if not DB_FILE.exists():
            return {"bots": {}, "users": {}, "admins": [], "total_uploads": 0}
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"bots": {}, "users": {}, "admins": [], "total_uploads": 0}

def save_db(data: Dict[str, Any]) -> None:
    with _db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)

# ============================================================
# 🛠 UTILITY FUNCTIONS
# ============================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_bot(bot_id: str) -> Optional[Dict[str, Any]]:
    db = load_db()
    return db["bots"].get(bot_id)

def save_bot(bot_data: Dict[str, Any]) -> None:
    db = load_db()
    db["bots"][bot_data["_id"]] = bot_data
    save_db(db)

def delete_bot(bot_id: str) -> None:
    db = load_db()
    db["bots"].pop(bot_id, None)
    save_db(db)

def format_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

def get_storage_usage() -> Dict[str, Any]:
    total_size = 0
    file_count = 0
    for root, dirs, files in os.walk(STORAGE_DIR):
        for f in files:
            try:
                file_path = Path(root) / f
                total_size += file_path.stat().st_size
                file_count += 1
            except:
                pass
    return {
        "total_mb": total_size / 1024 / 1024,
        "file_count": file_count,
        "total_gb": total_size / 1024 / 1024 / 1024
    }

def check_storage_limit() -> bool:
    usage = get_storage_usage()
    if usage["total_mb"] > MAX_STORAGE_MB:
        return False
    return True

# ✅ FIXED: Sirf 5MB se bada log delete karo
def rotate_log_if_needed(log_file: Path) -> None:
    """Agar log 5MB se bada ho jaye toh truncate kar do"""
    try:
        if log_file.exists():
            size_mb = log_file.stat().st_size / 1024 / 1024
            if size_mb > MAX_LOG_SIZE_MB:
                # Purana content hata do, sirf last 100KB rakho
                with open(log_file, "rb") as f:
                    f.seek(-100 * 1024, 2)  # Last 100KB
                    tail = f.read()
                with open(log_file, "wb") as f:
                    f.write(b"[LOG ROTATED - Old content removed]\n")
                    f.write(tail)
                print(f"🔄 Log rotated: {log_file.name} (was {size_mb:.1f}MB)")
    except Exception as e:
        print(f"Log rotation error: {e}")

def aggressive_cleanup():
    print("🧹 Running aggressive cleanup...")
    try:
        for log_file in LOGS_DIR.iterdir():
            if log_file.is_file():
                # ✅ 5MB se bada turant delete
                if log_file.stat().st_size > (MAX_LOG_SIZE_MB * 1024 * 1024):
                    log_file.unlink(missing_ok=True)
                    continue
                age = time.time() - log_file.stat().st_mtime
                if age > (LOG_RETENTION_DAYS * 86400):
                    log_file.unlink(missing_ok=True)
    except:
        pass
    
    try:
        for f in TEMP_DIR.iterdir():
            if f.is_file():
                age = time.time() - f.stat().st_mtime
                if age > 1800:  # 30 min
                    f.unlink(missing_ok=True)
    except:
        pass
    
    try:
        db = load_db()
        valid_ids = set(db["bots"].keys())
        for bot_dir in BOTS_DIR.iterdir():
            if bot_dir.is_dir() and bot_dir.name not in valid_ids:
                shutil.rmtree(bot_dir, ignore_errors=True)
    except:
        pass
    
    # ✅ Download zips bhi clean karo
    try:
        for f in STORAGE_DIR.glob("*_download.zip"):
            age = time.time() - f.stat().st_mtime
            if age > 600:  # 10 min
                f.unlink(missing_ok=True)
    except:
        pass
    
    gc.collect()

def get_system_stats() -> Dict[str, Any]:
    try:
        import psutil
        process = psutil.Process()
        memory = process.memory_info()
        return {
            "memory_used": memory.rss / 1024 / 1024,
            "memory_percent": process.memory_percent(),
            "cpu_percent": process.cpu_percent(),
            "threads": process.num_threads(),
            "running_bots": sum(1 for b in RUNNING_BOTS.values() if b.get("proc") and b["proc"].poll() is None),
            "total_bots": len(RUNNING_BOTS),
        }
    except:
        return {"memory_used": 0, "memory_percent": 0, "cpu_percent": 0, "running_bots": 0, "total_bots": 0}

# ============================================================
# 🔍 FILE DETECTION
# ============================================================
def detect_entry_file(bot_dir: Path) -> Optional[str]:
    if (bot_dir / "bot.py").exists():
        return "bot.py"
    for fname in ["new0.py", "new6.py", "main.py", "app.py", "index.py", "run.py", "server.py", "start.py"]:
        if (bot_dir / fname).exists():
            return fname
    for f in bot_dir.rglob("bot.py"):
        if "__pycache__" not in str(f):
            return str(f.relative_to(bot_dir))
    for f in bot_dir.iterdir():
        if f.suffix == '.py' and f.is_file():
            return f.name
    if (bot_dir / "index.js").exists():
        return "index.js"
    if (bot_dir / "server.js").exists():
        return "server.js"
    if (bot_dir / "app.js").exists():
        return "app.js"
    for f in bot_dir.iterdir():
        if f.suffix == '.js' and f.is_file():
            return f.name
    for f in bot_dir.iterdir():
        if f.suffix == '.sh' and f.is_file():
            return f.name
    return None

def get_file_type(filename: str) -> str:
    if filename.endswith('.py'):
        return 'python'
    elif filename.endswith('.js'):
        return 'node'
    elif filename.endswith('.sh'):
        return 'shell'
    else:
        return 'unknown'

# ============================================================
# 🚀 BOT RUNNER - FIXED (Memory optimized)
# ============================================================
def run_bot_process(bot_id: str, bot_dir: Path, entry_file: str) -> Dict[str, Any]:
    if len(RUNNING_BOTS) >= MAX_BOTS:
        return {"ok": False, "error": f"Max {MAX_BOTS} bots allowed"}
    
    if not check_storage_limit():
        aggressive_cleanup()
        if not check_storage_limit():
            return {"ok": False, "error": "Storage full!"}
    
    try:
        file_type = get_file_type(entry_file)
        if file_type == 'python':
            cmd = [sys.executable, "-u", entry_file]
        elif file_type == 'node':
            cmd = ["node", entry_file]
        elif file_type == 'shell':
            cmd = ["bash", entry_file]
        else:
            return {"ok": False, "error": f"Unsupported file: {entry_file}"}
        
        env = os.environ.copy()
        env["HOME"] = str(bot_dir)
        env["TMPDIR"] = str(bot_dir / "tmp")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        
        (bot_dir / "tmp").mkdir(parents=True, exist_ok=True)
        (bot_dir / "logs").mkdir(parents=True, exist_ok=True)
        
        log_file = LOGS_DIR / f"{bot_id}.log"
        
        # ✅ Rotate agar 5MB se bada ho
        rotate_log_if_needed(log_file)
        
        # ✅ CRITICAL FIX: stdout DIRECTLY file mein, RAM mein nahi!
        log_handle = open(log_file, "a", encoding="utf-8", buffering=1)
        
        proc = subprocess.Popen(
            cmd,
            cwd=str(bot_dir),
            env=env,
            stdout=log_handle,        # ✅ Direct file, no pipe!
            stderr=subprocess.STDOUT,  # ✅ stderr bhi file mein
            text=True,
            preexec_fn=os.setsid if os.name == "posix" else None,
            bufsize=1,
        )
        
        info = {
            "proc": proc,
            "started": time.time() * 1000,
            "log": deque(maxlen=MAX_RAM_LOG_LINES),  # ✅ Ring buffer (no leak)
            "dir": str(bot_dir),
            "log_file": str(log_file),
            "log_handle": log_handle,  # ✅ File handle (close karna hai)
            "manual_stop": False,
            "restart_count": 0,
            "last_restart": time.time(),
            "last_activity": time.time(),
        }
        RUNNING_BOTS[bot_id] = info
        
        # ✅ Sirf watch thread (read_logs thread hata diya - ab jarurat nahi)
        threading.Thread(target=_watch_process, args=(bot_id, proc), daemon=True).start()
        
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ❌ REMOVED: _read_logs() - ab stdout directly file mein ja raha hai
# Isse RAM 0 ho gayi aur CPU bhi bacha

def _watch_process(bot_id: str, proc: subprocess.Popen) -> None:
    try:
        proc.wait()
        # ✅ File handle band karo
        info = RUNNING_BOTS.get(bot_id)
        if info and info.get("log_handle"):
            try:
                info["log_handle"].close()
            except:
                pass
        
        if bot_id in RUNNING_BOTS:
            info = RUNNING_BOTS.get(bot_id)
            if info and not info.get("manual_stop", False):
                b = get_bot(bot_id)
                if b and b.get("auto_restart", True):
                    time.sleep(5)
                    if BOT_RUNNING and len(RUNNING_BOTS) < MAX_BOTS:
                        if check_storage_limit():
                            result = run_bot_process(bot_id, Path(b["dir"]), b.get("entry_file", "bot.py"))
                            if result.get("ok"):
                                print(f"🔄 Auto-restarted bot: {b.get('name', bot_id)}")
    except:
        pass

def stop_bot_process(bot_id: str, manual: bool = True) -> Dict[str, Any]:
    info = RUNNING_BOTS.get(bot_id)
    if not info:
        return {"ok": False, "error": "Bot not running"}
    
    info["manual_stop"] = manual
    proc = info["proc"]
    
    try:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except:
                proc.terminate()
        else:
            proc.terminate()
        
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except:
                    proc.kill()
            else:
                proc.kill()
        
        # ✅ File handle close karo
        if info.get("log_handle"):
            try:
                info["log_handle"].close()
            except:
                pass
        
        RUNNING_BOTS.pop(bot_id, None)
        b = get_bot(bot_id)
        if b:
            b["status"] = "stopped"
            save_bot(b)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def get_bot_status(bot_id: str) -> Dict[str, Any]:
    info = RUNNING_BOTS.get(bot_id)
    running = info and info["proc"].poll() is None
    return {
        "running": running,
        "pid": info["proc"].pid if running else None,
        "uptime": int(time.time() * 1000 - info["started"]) if running else 0,
        "restart_count": info.get("restart_count", 0) if info else 0,
        "memory": 0,
    }

# ============================================================
# 🔄 KEEP-ALIVE - FIXED (less frequent cleanup)
# ============================================================
def keep_alive_system():
    global BOT_RUNNING
    cleanup_counter = 0
    while BOT_RUNNING:
        try:
            for bot_id, info in list(RUNNING_BOTS.items()):
                try:
                    proc = info.get("proc")
                    if proc and proc.poll() is not None:
                        b = get_bot(bot_id)
                        if b and b.get("auto_restart", True):
                            if not info.get("manual_stop", False):
                                if check_storage_limit():
                                    stop_bot_process(bot_id, manual=False)
                                    time.sleep(1)
                                    run_bot_process(bot_id, Path(b["dir"]), b.get("entry_file", "bot.py"))
                except:
                    pass
            
            cleanup_counter += 1
            if cleanup_counter >= 30:  # ✅ Har 60 min (was 30 min)
                cleanup_counter = 0
                usage = get_storage_usage()
                if usage["total_mb"] > MAX_STORAGE_MB * 0.8:
                    aggressive_cleanup()
                # ✅ 5MB logs check
                for log_file in LOGS_DIR.glob("*.log"):
                    rotate_log_if_needed(log_file)
        except:
            pass
        time.sleep(120)

# ============================================================
# 🌐 HTML TEMPLATE - FIXED (30s refresh instead of 10s)
# ============================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🚀 Hosting Panel</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: #0a0e1a;
            color: #fff;
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        
        .header {
            background: #111827;
            border-radius: 20px;
            padding: 25px 35px;
            margin-bottom: 25px;
            border: 1px solid #1a2a3a;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 15px;
        }
        .header h1 {
            font-size: 24px;
            font-weight: 700;
            background: linear-gradient(135deg, #f7971e, #ffd200);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .header .subtitle { color: #6a7a8a; font-size: 14px; }
        .stats {
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }
        .stat-item {
            background: rgba(255,255,255,0.03);
            padding: 10px 18px;
            border-radius: 12px;
            border: 1px solid #1a2a3a;
            text-align: center;
            min-width: 80px;
        }
        .stat-item .num { font-size: 20px; font-weight: 700; color: #ffd200; }
        .stat-item .label { font-size: 11px; color: #6a7a8a; }
        
        .upload-box {
            background: #111827;
            border-radius: 20px;
            padding: 40px;
            margin-bottom: 25px;
            border: 2px dashed #1a2a3a;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s;
        }
        .upload-box:hover { border-color: #f7971e; background: rgba(247,151,30,0.05); }
        .upload-box .icon { font-size: 50px; margin-bottom: 10px; }
        .upload-box h3 { font-size: 20px; font-weight: 600; margin-bottom: 8px; }
        .upload-box p { color: #6a7a8a; }
        .upload-box input[type="file"] { display: none; }
        .upload-progress { display: none; margin-top: 20px; }
        .progress-bar {
            width: 100%;
            height: 6px;
            background: rgba(255,255,255,0.05);
            border-radius: 3px;
            overflow: hidden;
        }
        .progress-bar .fill {
            height: 100%;
            background: linear-gradient(90deg, #f7971e, #ffd200);
            width: 0%;
            transition: width 0.3s;
        }
        
        .section-title {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        .section-title h2 { font-size: 20px; font-weight: 600; }
        .section-title .count { color: #6a7a8a; font-size: 14px; }
        
        .bots-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
            gap: 18px;
        }
        .bot-card {
            background: #111827;
            border-radius: 16px;
            padding: 20px;
            border: 1px solid #1a2a3a;
            transition: all 0.3s;
        }
        .bot-card:hover { transform: translateY(-4px); border-color: rgba(247,151,30,0.3); }
        .bot-card .bot-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .bot-card .bot-name { font-size: 16px; font-weight: 600; }
        .bot-card .bot-status {
            padding: 3px 12px;
            border-radius: 20px;
            font-size: 11px;
            font-weight: 500;
        }
        .bot-status.running { background: rgba(0,200,83,0.2); color: #00c853; border: 1px solid rgba(0,200,83,0.2); }
        .bot-status.stopped { background: rgba(255,23,68,0.2); color: #ff1744; border: 1px solid rgba(255,23,68,0.2); }
        .bot-card .bot-info {
            color: #6a7a8a;
            font-size: 13px;
            margin: 6px 0;
        }
        .bot-card .bot-actions {
            display: flex;
            gap: 6px;
            margin-top: 12px;
            flex-wrap: wrap;
        }
        .bot-card .btn {
            padding: 6px 14px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 500;
            transition: all 0.3s;
            color: #fff;
        }
        .btn-start { background: #00c853; }
        .btn-start:hover { background: #00e676; }
        .btn-stop { background: #ff1744; }
        .btn-stop:hover { background: #ff5252; }
        .btn-restart { background: #ff9100; }
        .btn-restart:hover { background: #ffab40; }
        .btn-logs { background: #2979ff; }
        .btn-logs:hover { background: #448aff; }
        .btn-download { background: #00bfa5; }
        .btn-download:hover { background: #00e5d9; }
        .btn-delete { background: #d50000; }
        .btn-delete:hover { background: #ff1744; }
        
        .modal {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.8);
            backdrop-filter: blur(10px);
            z-index: 1000;
            align-items: center;
            justify-content: center;
        }
        .modal.show { display: flex; }
        .modal-content {
            background: #111827;
            border-radius: 20px;
            padding: 30px;
            max-width: 800px;
            width: 92%;
            max-height: 80vh;
            overflow-y: auto;
            border: 1px solid #1a2a3a;
        }
        .modal-content .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 12px;
            border-bottom: 1px solid #1a2a3a;
        }
        .modal-content .modal-header h3 { color: #ffd200; }
        .modal-content .modal-close {
            background: none;
            border: none;
            color: #6a7a8a;
            font-size: 24px;
            cursor: pointer;
        }
        .modal-content .modal-close:hover { color: #ff1744; }
        .modal-content pre {
            background: rgba(0,0,0,0.3);
            padding: 15px;
            border-radius: 10px;
            font-size: 12px;
            max-height: 400px;
            overflow-y: auto;
            color: #00ff41;
            font-family: monospace;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        
        .toast-container {
            position: fixed;
            bottom: 20px;
            right: 20px;
            z-index: 2000;
        }
        .toast {
            padding: 12px 24px;
            border-radius: 12px;
            color: #fff;
            font-weight: 500;
            animation: slideIn 0.3s ease;
            margin-top: 8px;
            display: flex;
            align-items: center;
            gap: 10px;
            box-shadow: 0 8px 30px rgba(0,0,0,0.3);
        }
        .toast.success { background: rgba(0,200,83,0.9); }
        .toast.error { background: rgba(255,23,68,0.9); }
        .toast.info { background: rgba(41,121,255,0.9); }
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #6a7a8a;
        }
        .empty-state .icon { font-size: 60px; margin-bottom: 15px; }
        .empty-state h3 { color: #fff; margin-bottom: 8px; }
        
        @media (max-width: 768px) {
            .header { flex-direction: column; text-align: center; padding: 20px; }
            .stats { justify-content: center; }
            .stat-item { min-width: 70px; padding: 8px 14px; }
            .stat-item .num { font-size: 16px; }
            .upload-box { padding: 25px 20px; }
            .bots-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>🚀 Hosting Panel Pro</h1>
                <div class="subtitle">Manage your bots easily</div>
            </div>
            <div class="stats" id="stats">
                <div class="stat-item">
                    <div class="num" id="stat-running">0</div>
                    <div class="label">Running</div>
                </div>
                <div class="stat-item">
                    <div class="num" id="stat-total">0</div>
                    <div class="label">Total Bots</div>
                </div>
                <div class="stat-item">
                    <div class="num" id="stat-storage">0 MB</div>
                    <div class="label">Storage</div>
                </div>
            </div>
        </div>
        
        <div class="upload-box" id="uploadBox">
            <div class="icon">📤</div>
            <h3>Upload Your Bot</h3>
            <p>Click or drag & drop <strong>.zip</strong> file</p>
            <p style="font-size:12px;color:#4a5a6a;margin-top:8px;">Max: 500MB | .py, .js, .sh supported</p>
            <input type="file" id="fileInput" accept=".zip">
            <div class="upload-progress" id="uploadProgress">
                <div style="display:flex;justify-content:space-between;font-size:13px;color:#6a7a8a;margin-bottom:6px;">
                    <span>Uploading...</span>
                    <span id="uploadPercent">0%</span>
                </div>
                <div class="progress-bar"><div class="fill" id="progressFill"></div></div>
            </div>
        </div>
        
        <div class="section-title">
            <h2><i class="fas fa-robot" style="color:#ffd200;"></i> Your Bots</h2>
            <span class="count" id="botCount">0 bots</span>
        </div>
        
        <div id="botsContainer">
            <div class="empty-state">
                <div class="icon">🤖</div>
                <h3>No Bots Yet</h3>
                <p>Upload your first bot to get started!</p>
            </div>
        </div>
    </div>
    
    <div class="modal" id="logsModal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="logsTitle">📋 Logs</h3>
                <button class="modal-close" onclick="closeLogs()">✕</button>
            </div>
            <pre id="logsContent">Loading...</pre>
        </div>
    </div>
    
    <div class="toast-container" id="toastContainer"></div>
    
    <script>
        let bots = [];
        let refreshInterval = null;
        
        async function api(method, endpoint, data = null) {
            const options = {
                method: method,
                headers: { 'Content-Type': 'application/json' }
            };
            if (data) options.body = JSON.stringify(data);
            
            try {
                const res = await fetch('/api/' + endpoint, options);
                
                if (res.status === 404) {
                    throw new Error('API endpoint not found: /api/' + endpoint);
                }
                
                if (!res.ok) {
                    const text = await res.text();
                    throw new Error('HTTP ' + res.status + ': ' + text.substring(0, 50));
                }
                
                return await res.json();
            } catch (e) {
                console.error('API Error:', e);
                return null;
            }
        }
        
        async function loadBots() {
            try {
                const data = await api('GET', 'bots');
                if (data && data.bots !== undefined) {
                    bots = data.bots;
                    renderBots();
                    updateStats();
                }
            } catch (e) {
                console.error('Load bots error:', e);
            }
        }
        
        function renderBots() {
            const container = document.getElementById('botsContainer');
            document.getElementById('botCount').textContent = bots.length + ' bots';
            
            if (bots.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <div class="icon">🤖</div>
                        <h3>No Bots Yet</h3>
                        <p>Upload your first bot to get started!</p>
                    </div>
                `;
                return;
            }
            
            let html = '<div class="bots-grid">';
            for (const bot of bots) {
                const statusClass = bot.running ? 'running' : 'stopped';
                const statusText = bot.running ? '🟢 Running' : '⚪ Stopped';
                const uptime = bot.running ? Math.floor(bot.uptime / 1000) + 's' : '—';
                
                html += `
                    <div class="bot-card">
                        <div class="bot-header">
                            <span class="bot-name">🤖 ${escapeHtml(bot.name)}</span>
                            <span class="bot-status ${statusClass}">${statusText}</span>
                        </div>
                        <div class="bot-info">📁 ${escapeHtml(bot.entry_file)}</div>
                        <div class="bot-info">📦 ${bot.size} 🔄 ${bot.restart_count} ⏱ ${uptime}</div>
                        <div class="bot-actions">
                            ${bot.running ? 
                                `<button class="btn btn-stop" onclick="stopBot('${bot.id}')">⏹ Stop</button>` :
                                `<button class="btn btn-start" onclick="startBot('${bot.id}')">▶️ Start</button>`
                            }
                            <button class="btn btn-restart" onclick="restartBot('${bot.id}')">🔄 Restart</button>
                            <button class="btn btn-logs" onclick="viewLogs('${bot.id}')">📋 Logs</button>
                            <button class="btn btn-download" onclick="downloadBot('${bot.id}')">📦 Download</button>
                            <button class="btn btn-delete" onclick="deleteBot('${bot.id}')">🗑 Delete</button>
                        </div>
                    </div>
                `;
            }
            html += '</div>';
            container.innerHTML = html;
        }
        
        function updateStats() {
            const running = bots.filter(b => b.running).length;
            document.getElementById('stat-running').textContent = running;
            document.getElementById('stat-total').textContent = bots.length;
            
            api('GET', 'stats').then(data => {
                if (data && data.storage_mb !== undefined) {
                    document.getElementById('stat-storage').textContent = data.storage_mb.toFixed(1) + ' MB';
                }
            });
        }
        
        document.getElementById('uploadBox').addEventListener('click', () => {
            document.getElementById('fileInput').click();
        });
        
        document.getElementById('uploadBox').addEventListener('dragover', (e) => {
            e.preventDefault();
            document.getElementById('uploadBox').style.borderColor = '#f7971e';
        });
        
        document.getElementById('uploadBox').addEventListener('dragleave', (e) => {
            e.preventDefault();
            document.getElementById('uploadBox').style.borderColor = '#1a2a3a';
        });
        
        document.getElementById('uploadBox').addEventListener('drop', (e) => {
            e.preventDefault();
            document.getElementById('uploadBox').style.borderColor = '#1a2a3a';
            if (e.dataTransfer.files.length) {
                handleUpload(e.dataTransfer.files[0]);
            }
        });
        
        document.getElementById('fileInput').addEventListener('change', (e) => {
            if (e.target.files.length) {
                handleUpload(e.target.files[0]);
            }
        });
        
        function handleUpload(file) {
            if (!file.name.endsWith('.zip')) {
                showToast('❌ Only .zip files allowed!', 'error');
                return;
            }
            if (file.size > 500 * 1024 * 1024) {
                showToast('❌ File too large! Max 500MB', 'error');
                return;
            }
            
            const progress = document.getElementById('uploadProgress');
            progress.style.display = 'block';
            
            const formData = new FormData();
            formData.append('file', file);
            
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/upload');
            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    const percent = Math.round((e.loaded / e.total) * 100);
                    document.getElementById('uploadPercent').textContent = percent + '%';
                    document.getElementById('progressFill').style.width = percent + '%';
                }
            };
            xhr.onload = () => {
                progress.style.display = 'none';
                document.getElementById('progressFill').style.width = '0%';
                document.getElementById('uploadPercent').textContent = '0%';
                document.getElementById('fileInput').value = '';
                
                try {
                    const data = JSON.parse(xhr.responseText);
                    if (data.ok) {
                        showToast('✅ Bot uploaded successfully!', 'success');
                        loadBots();
                    } else {
                        showToast('❌ ' + data.error, 'error');
                    }
                } catch (e) {
                    showToast('❌ Upload failed: ' + e.message, 'error');
                }
            };
            xhr.onerror = () => {
                progress.style.display = 'none';
                showToast('❌ Upload error!', 'error');
            };
            xhr.send(formData);
        }
        
        async function startBot(id) {
            const data = await api('POST', 'start/' + id);
            if (data && data.ok) { showToast('✅ Bot started!', 'success'); loadBots(); }
            else { showToast('❌ ' + (data ? data.error : 'Failed'), 'error'); }
        }
        
        async function stopBot(id) {
            const data = await api('POST', 'stop/' + id);
            if (data && data.ok) { showToast('✅ Bot stopped!', 'success'); loadBots(); }
            else { showToast('❌ ' + (data ? data.error : 'Failed'), 'error'); }
        }
        
        async function restartBot(id) {
            const data = await api('POST', 'restart/' + id);
            if (data && data.ok) { showToast('✅ Bot restarted!', 'success'); loadBots(); }
            else { showToast('❌ ' + (data ? data.error : 'Failed'), 'error'); }
        }
        
        async function deleteBot(id) {
            if (!confirm('⚠️ Delete this bot?')) return;
            const data = await api('DELETE', 'bot/' + id);
            if (data && data.ok) { showToast('✅ Bot deleted!', 'success'); loadBots(); }
            else { showToast('❌ ' + (data ? data.error : 'Failed'), 'error'); }
        }
        
        function downloadBot(id) {
            window.open('/api/download/' + id, '_blank');
        }
        
        async function viewLogs(id) {
            const data = await api('GET', 'logs/' + id);
            if (data && data.logs !== undefined) {
                document.getElementById('logsTitle').textContent = '📋 Logs - ' + data.name;
                document.getElementById('logsContent').textContent = data.logs || '(No logs)';
                document.getElementById('logsModal').classList.add('show');
            } else {
                showToast('❌ Failed to load logs', 'error');
            }
        }
        
        function closeLogs() {
            document.getElementById('logsModal').classList.remove('show');
        }
        
        document.getElementById('logsModal').addEventListener('click', (e) => {
            if (e.target === document.getElementById('logsModal')) closeLogs();
        });
        
        function showToast(message, type = 'info') {
            const container = document.getElementById('toastContainer');
            const toast = document.createElement('div');
            toast.className = 'toast ' + type;
            const icons = { success: '✅', error: '❌', info: 'ℹ️' };
            toast.innerHTML = `<span>${icons[type] || 'ℹ️'}</span><span>${message}</span>`;
            container.appendChild(toast);
            setTimeout(() => {
                toast.style.opacity = '0';
                toast.style.transform = 'translateX(100px)';
                setTimeout(() => toast.remove(), 300);
            }, 4000);
        }
        
        function escapeHtml(text) {
            if (!text) return '';
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        loadBots();
        // ✅ FIXED: 30 seconds (was 10s) - CPU 3x kam
        refreshInterval = setInterval(loadBots, 30000);
    </script>
</body>
</html>
"""

# ============================================================
# 🌐 FLASK APP
# ============================================================
app = Flask(__name__)
CORS(app)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/health')
def health():
    stats = get_system_stats()
    usage = get_storage_usage()
    return jsonify({
        "status": "alive",
        "timestamp": now_iso(),
        "uptime": int(time.time() - START_TIME),
        "bots_running": stats['running_bots'],
        "total_bots": stats['total_bots'],
        "max_bots": MAX_BOTS,
        "memory_mb": stats["memory_used"],
        "storage_mb": usage["total_mb"],
    })

@app.route('/api/bots', methods=['GET'])
def api_bots():
    db = load_db()
    result = []
    for bot_id, b in db["bots"].items():
        status = get_bot_status(bot_id)
        result.append({
            "id": bot_id,
            "name": b.get("name", "Unknown"),
            "entry_file": b.get("entry_file", "?"),
            "size": format_size(b.get("size", 0)),
            "running": status["running"],
            "uptime": status["uptime"],
            "restart_count": status["restart_count"],
            "has_accs": b.get("has_accs", False),
        })
    return jsonify({"bots": result})

@app.route('/api/stats', methods=['GET'])
def api_stats():
    stats = get_system_stats()
    usage = get_storage_usage()
    db = load_db()
    return jsonify({
        "running": stats["running_bots"],
        "total": len(db["bots"]),
        "storage_mb": usage["total_mb"],
        "storage_max": MAX_STORAGE_MB,
        "memory_mb": stats["memory_used"],
        "cpu": stats["cpu_percent"],
    })

@app.route('/api/upload', methods=['POST'])
def api_upload():
    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "No file uploaded"})
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"ok": False, "error": "No file selected"})
    
    if not file.filename.lower().endswith('.zip'):
        return jsonify({"ok": False, "error": "Only .zip files allowed"})
    
    if len(RUNNING_BOTS) >= MAX_BOTS:
        return jsonify({"ok": False, "error": f"Max {MAX_BOTS} bots reached"})
    
    if not check_storage_limit():
        aggressive_cleanup()
        if not check_storage_limit():
            return jsonify({"ok": False, "error": "Storage full! Delete some bots."})
    
    content = file.read()
    if len(content) > MAX_FILE_SIZE:
        return jsonify({"ok": False, "error": f"File too large (max {format_size(MAX_FILE_SIZE)})"})
    
    bot_id = secrets.token_hex(8)
    bot_dir = BOTS_DIR / bot_id
    bot_dir.mkdir(parents=True, exist_ok=True)
    
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            zf.extractall(bot_dir)
    except Exception as e:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": f"Invalid ZIP: {str(e)}"})
    
    accs_file = bot_dir / "accs.json"
    has_accs = accs_file.exists()
    if not has_accs:
        for f in bot_dir.rglob("accs.json"):
            if f.is_file():
                shutil.copy(f, bot_dir / "accs.json")
                has_accs = True
                break
    
    entry_file = detect_entry_file(bot_dir)
    if not entry_file:
        shutil.rmtree(bot_dir, ignore_errors=True)
        return jsonify({"ok": False, "error": "No supported file found"})
    
    bot_data = {
        "_id": bot_id,
        "name": Path(file.filename).stem[:30],
        "dir": str(bot_dir),
        "entry_file": entry_file,
        "uploaded": now_iso(),
        "status": "stopped",
        "auto_restart": True,
        "file_type": get_file_type(entry_file),
        "has_accs": has_accs,
        "size": len(content),
    }
    save_bot(bot_data)
    
    result = run_bot_process(bot_id, bot_dir, entry_file)
    if result.get("ok"):
        bot_data["status"] = "running"
        save_bot(bot_data)
    
    return jsonify({
        "ok": True,
        "bot_id": bot_id,
        "name": bot_data["name"],
        "running": bot_data["status"] == "running"
    })

@app.route('/api/start/<bot_id>', methods=['POST'])
def api_start(bot_id):
    b = get_bot(bot_id)
    if not b:
        return jsonify({"ok": False, "error": "Bot not found"})
    
    if len(RUNNING_BOTS) >= MAX_BOTS:
        return jsonify({"ok": False, "error": f"Max {MAX_BOTS} bots reached"})
    
    result = run_bot_process(bot_id, Path(b["dir"]), b["entry_file"])
    if result.get("ok"):
        b["status"] = "running"
        save_bot(b)
    return jsonify(result)

@app.route('/api/stop/<bot_id>', methods=['POST'])
def api_stop(bot_id):
    result = stop_bot_process(bot_id)
    return jsonify(result)

@app.route('/api/restart/<bot_id>', methods=['POST'])
def api_restart(bot_id):
    b = get_bot(bot_id)
    if not b:
        return jsonify({"ok": False, "error": "Bot not found"})
    
    stop_bot_process(bot_id)
    time.sleep(0.5)
    result = run_bot_process(bot_id, Path(b["dir"]), b["entry_file"])
    if result.get("ok"):
        b["status"] = "running"
        save_bot(b)
    return jsonify(result)

@app.route('/api/bot/<bot_id>', methods=['DELETE'])
def api_delete(bot_id):
    stop_bot_process(bot_id)
    b = get_bot(bot_id)
    if b:
        shutil.rmtree(Path(b["dir"]), ignore_errors=True)
        delete_bot(bot_id)
    # ✅ Log file bhi delete karo
    log_file = LOGS_DIR / f"{bot_id}.log"
    if log_file.exists():
        log_file.unlink(missing_ok=True)
    aggressive_cleanup()
    return jsonify({"ok": True})

@app.route('/api/download/<bot_id>')
def api_download(bot_id):
    b = get_bot(bot_id)
    if not b:
        return jsonify({"ok": False, "error": "Bot not found"})
    
    bot_dir = Path(b["dir"])
    if not bot_dir.exists():
        return jsonify({"ok": False, "error": "Bot files not found"})
    
    zip_path = STORAGE_DIR / f"{bot_id}_download.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in bot_dir.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(bot_dir))
    
    return send_file(zip_path, as_attachment=True, download_name=f"{b['name']}.zip")

# ✅ FIXED: Logs API with CACHE (5 sec) + tail only (no full read)
@app.route('/api/logs/<bot_id>')
def api_logs(bot_id):
    b = get_bot(bot_id)
    
    # ✅ Cache check
    now = time.time()
    cached = _log_cache.get(bot_id)
    if cached and (now - cached["time"]) < LOG_CACHE_TTL:
        return jsonify({
            "logs": cached["logs"],
            "name": b.get("name", "Bot") if b else "Bot"
        })
    
    log_text = ""
    log_file = LOGS_DIR / f"{bot_id}.log"
    
    if log_file.exists():
        try:
            size = log_file.stat().st_size
            # ✅ Sirf last 30KB padho (poora file nahi)
            read_size = min(size, 30 * 1024)
            with open(log_file, "rb") as f:
                if size > read_size:
                    f.seek(size - read_size)
                data = f.read()
            log_text = data.decode("utf-8", errors="ignore")
        except Exception as e:
            log_text = f"(Error reading logs: {e})"
    
    if not log_text:
        log_text = "(No logs yet)"
    
    # ✅ Cache mein save
    _log_cache[bot_id] = {"logs": log_text, "time": now}
    
    return jsonify({
        "logs": log_text,
        "name": b.get("name", "Bot") if b else "Bot"
    })

# ============================================================
# 🚀 MAIN
# ============================================================
def main():
    global BOT_RUNNING
    
    print("="*70)
    print("🌐 WEB HOSTING PANEL - v4.0 (Randor Fix)")
    print("="*70)
    print(f"📁 Storage: {STORAGE_DIR}")
    print(f"🌐 Port: {PORT}")
    print(f"🤖 Max Bots: {MAX_BOTS}")
    print(f"💾 Log Rotation: {MAX_LOG_SIZE_MB}MB")
    print(f"🧠 RAM Log Buffer: {MAX_RAM_LOG_LINES} lines")
    print("="*70)
    
    aggressive_cleanup()
    
    threading.Thread(target=keep_alive_system, daemon=True).start()
    print("✅ Keep-Alive system started")
    
    db = load_db()
    for b in db["bots"].values():
        if b.get("status") == "running" and len(RUNNING_BOTS) < MAX_BOTS:
            try:
                run_bot_process(b["_id"], Path(b["dir"]), b["entry_file"])
                print(f"✅ Auto-started: {b['name']}")
            except Exception as e:
                print(f"❌ Auto-start error: {e}")
    
    print("="*70)
    print(f"✅ Panel running at: http://localhost:{PORT}")
    print("="*70)
    
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

if __name__ == "__main__":
    main()
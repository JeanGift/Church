#!/usr/bin/env python3
"""
app.py - Tomorrow PWA backend + Admin UI (GitHub-backed DB optional)

Environment variables (optional):
  GITHUB_TOKEN    - your GitHub personal access token (starts with ghp_...)
  GITHUB_REPO     - "username/repo"
  GITHUB_BRANCH   - branch to use (default: "main")
  GITHUB_DB_PATH  - path in repo for DB file (default: "data/db.json")
  FLASK_SECRET    - Flask session secret (recommended to set)
  PORT            - port for gunicorn or for local run (default: 4001)
  APP_URL         - public URL used by keep-alive (e.g. https://example.com)
  KEEPALIVE_ENABLED - "1" to enable keep-alive (default "1")
  KEEPALIVE_INTERVAL - seconds between pings (default 1200 = 20min)
  UPTIME_MONITORS - comma-separated extra URLs to ping (optional)
  RUN_KEEPALIVE_IN_WORKERS - "1" to allow thread to start in each worker (default "1")
  PULSE_SECRET - optional secret required in X-PULSE-TOKEN or ?token= for /pulse_receiver
"""
import os
import json
import base64
import datetime
import uuid
import threading
import time
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash

# ------------------ GitHub DB helpers ------------------

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
REPO = os.getenv("GITHUB_REPO")
BRANCH = os.getenv("GITHUB_BRANCH", "main")
DB_PATH = os.getenv("GITHUB_DB_PATH", "data/db.json")

ROOT = Path(__file__).parent.resolve()
LOCAL_DATA_FILE = ROOT / "data.json"

# stored in memory while process runs (sha returned by GitHub) - kept for visibility but not required
_GITHUB_SHA = None

def load_from_github():
    """Returns (data_dict, sha) or (None, None) if failed."""
    if not GITHUB_TOKEN or not REPO:
        return None, None
    url = f"https://api.github.com/repos/{REPO}/contents/{DB_PATH}?ref={BRANCH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
    except Exception:
        return None, None
    if r.status_code == 200:
        payload = r.json()
        content = payload.get("content", "")
        if payload.get("encoding") == "base64" and content:
            try:
                decoded = base64.b64decode(content).decode("utf-8")
                data = json.loads(decoded)
                return data, payload.get("sha")
            except Exception:
                return None, None
    return None, None

def save_to_github(data, sha=None):
    """
    Saves data to the repo path. Returns (success_bool, new_sha_or_None, status_code, response_text).
    """
    if not GITHUB_TOKEN or not REPO:
        return False, None, None, "no token/repo configured"
    url = f"https://api.github.com/repos/{REPO}/contents/{DB_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}
    content_b64 = base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")
    payload = {"message": "Update database (auto)", "content": content_b64, "branch": BRANCH}
    if sha:
        payload["sha"] = sha
    try:
        r = requests.put(url, headers=headers, json=payload, timeout=15)
    except Exception as e:
        return False, None, None, str(e)
    if r.status_code in (200, 201):
        try:
            new_sha = r.json().get("content", {}).get("sha")
            return True, new_sha, r.status_code, r.text
        except Exception:
            return True, None, r.status_code, r.text
    else:
        return False, None, r.status_code, r.text

# ------------------ Flask + app setup ------------------

app = Flask(__name__, static_folder=str(ROOT))
app.secret_key = os.getenv("FLASK_SECRET", "CHANGE-THIS-TO-A-SECURE-RANDOM-STRING")

DEFAULT_DATA = {
    "admins": [],
    "staff": [],
    "members": [],
    "attendance": {},
    "events": [],
    "summons": [],       # announcements
    "bible": [],
    "resources": [],
    "donations": [],
    "contributions": [], # tithes / offerings / pledges (new collection)
    "prayers": [],
    "pulses": []         # external pulses received via /pulse_receiver
}

START_TIME = datetime.datetime.utcnow()
app.config.setdefault("KEEPALIVE_RUNNING", False)
app.config.setdefault("KEEPALIVE_STARTED", False)
app.config.setdefault("LAST_PINGS", {})
app.config.setdefault("GITHUB_SHA", None)

# Optional PULSE_SECRET to require X-PULSE-TOKEN or ?token= on /pulse_receiver
PULSE_SECRET = os.getenv("PULSE_SECRET")

def now():
    return datetime.datetime.utcnow().isoformat() + "Z"

# ------------------ load/save data (GitHub-aware) ------------------

def load_data():
    """Loads data from GitHub if configured, otherwise from local file. Ensures DEFAULT_DATA keys exist and returns dict."""
    global _GITHUB_SHA
    # Try GitHub first
    if GITHUB_TOKEN and REPO:
        db, sha = load_from_github()
        if isinstance(db, dict):
            _GITHUB_SHA = sha
            for k, v in DEFAULT_DATA.items():
                if k not in db:
                    db[k] = v
            # local cache (best-effort)
            try:
                with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(db, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            app.config["GITHUB_SHA"] = _GITHUB_SHA
            return db
    # Fallback to local file
    if not LOCAL_DATA_FILE.exists():
        try:
            with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_DATA.copy(), f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    try:
        with open(LOCAL_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = DEFAULT_DATA.copy()
        try:
            with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
    for k, v in DEFAULT_DATA.items():
        if k not in data:
            data[k] = v
    return data

def save_data(data):
    """
    Saves data either to GitHub (if configured) or to local file.
    Strategy:
      - If GitHub configured, attempt up to 3 times: fetch latest SHA, PUT with that SHA.
      - On success: update local cache and app.config["GITHUB_SHA"], return True.
      - If all GitHub attempts fail: write local cache (fallback) and return True (so app continues),
        but app logs/can detect that GitHub writes failed via /api/health github_sha field (None or stale).
    """
    global _GITHUB_SHA
    # If GitHub configured, attempt robust save
    if GITHUB_TOKEN and REPO:
        attempts = 3
        last_status = None
        for attempt in range(attempts):
            # fetch latest file+sha just before writing
            latest_db, latest_sha = load_from_github()
            # Note: latest_db may be None if GitHub GET failed; latest_sha may be None if file doesn't exist yet.
            success, new_sha, status_code, resp_text = save_to_github(data, sha=latest_sha)
            last_status = (success, new_sha, status_code, resp_text)
            if success:
                _GITHUB_SHA = new_sha
                app.config["GITHUB_SHA"] = _GITHUB_SHA
                # update local cache (best-effort)
                try:
                    with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
                return True
            # if conflict or validation error, retry shortly
            if status_code in (409, 422):
                time.sleep(0.2)
                continue
            # other failures: break retry loop
            break

        # If we reach here, GitHub writes failed after retries.
        # Persist to local cache so app keeps working; surface the last status in logs via return False (or True fallback)
        try:
            with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            # keep app.config["GITHUB_SHA"] as whatever last known (may be None)
            # You can inspect last_status to see why it failed in logs (status_code / resp_text)
            return True
        except Exception:
            return False
    else:
        # Pure local mode
        try:
            with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False

# ------------------ decorators ------------------

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin_id"):
            return jsonify({"ok": False, "error": "Admin login required"}), 403
        return fn(*args, **kwargs)
    return wrapper

def staff_or_admin_allowed(check_perm_name=None):
    def outer(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if session.get("admin_id"):
                return fn(*args, **kwargs)
            sid = session.get("staff_id")
            if not sid:
                return jsonify({"ok": False, "error": "Login required"}), 403
            data = load_data()
            staff = next((s for s in data.get("staff", []) if s["id"] == sid), None)
            if not staff:
                return jsonify({"ok": False, "error": "Staff not found"}), 403
            if check_perm_name and not staff.get("perms", {}).get(check_perm_name):
                return jsonify({"ok": False, "error": "Permission denied"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return outer

# ------------------ static pages ------------------

@app.route("/")
def home():
    return send_from_directory(str(ROOT), "index.html")

@app.route("/prayers")
def prayers_page():
    return send_from_directory(str(ROOT), "prayers.html")

@app.route("/admin")
def admin_page():
    return send_from_directory(str(ROOT), "admin.html")

# explicit finance route so /finance works (serves finance.html)
@app.route("/finance")
def finance_page():
    return send_from_directory(str(ROOT), "finance.html")

@app.route("/staff")
def staff_page():
    file = ROOT / "staff.html"
    if file.exists():
        return send_from_directory(str(ROOT), "staff.html")
    return "Staff page not created. Add staff.html in project root.", 404

@app.route("/<path:p>")
def static_files(p):
    file = ROOT / p
    if file.exists() and file.is_file():
        return send_from_directory(str(ROOT), p)
    return "Not Found", 404

@app.route("/manifest.json")
def manifest():
    return send_from_directory(str(ROOT), "manifest.json")

@app.route("/sw.js")
def sw():
    return send_from_directory(str(ROOT), "sw.js")

# ------------------ public APIs ------------------

@app.route("/api/public")
def api_public():
    d = load_data()
    public_staff = [{"id": s["id"], "name": s["name"], "role": s.get("role",""), "contact": s.get("contact","")} for s in d.get("staff", [])]
    # Return contributions so finance.html can consume them
    return jsonify({
        "members": d["members"],
        "attendance": d["attendance"],
        "events": d["events"],
        "summons": d["summons"],
        "bible": d["bible"],
        "resources": d["resources"],
        "donations": d["donations"],
        "contributions": d["contributions"],
        "staff": public_staff
    })

# ------------------ prayers (public) ------------------

@app.route("/api/prayers", methods=["GET", "POST"])
def api_prayers():
    data = load_data()
    if request.method == "POST":
        j = request.get_json() or {}
        body = (j.get("body") or "").strip()
        name = (j.get("name") or "Anonymous").strip()
        assigned_to = (j.get("assigned_to") or "").strip()
        if not body:
            return jsonify({"ok": False, "error": "Prayer cannot be empty"}), 400
        assigned_name = ""
        if assigned_to:
            staff = next((s for s in data.get("staff", []) if s["id"] == assigned_to), None)
            if staff:
                assigned_name = staff.get("name","")
            else:
                assigned_to = ""
                assigned_name = ""
        prayer = {
            "id": str(uuid.uuid4()),
            "name": name,
            "body": body,
            "assigned_to": assigned_to,
            "assigned_name": assigned_name,
            "reply": "",
            "status": "open",
            "created_at": now(),
            "updated_at": now()
        }
        data["prayers"].insert(0, prayer)
        save_data(data)
        return jsonify({"ok": True, "prayer": prayer})
    return jsonify(data["prayers"])

# ------------------ admin auth ------------------

@app.route("/api/admin/exists")
def admin_exists():
    return jsonify({"exists": bool(load_data()["admins"])})

@app.route("/api/admin/register", methods=["POST"])
def admin_register():
    data = load_data()
    if data["admins"]:
        return jsonify({"ok": False, "error": "Admin already exists"}), 400
    j = request.get_json() or {}
    name = (j.get("name") or "").strip()
    password = j.get("password") or ""
    if not name or not password:
        return jsonify({"ok": False, "error": "Name & password required"}), 400
    admin = {"id": str(uuid.uuid4()), "name": name, "pass_hash": generate_password_hash(password), "created_at": now()}
    data["admins"] = [admin]
    save_data(data)
    session.clear()
    session["admin_id"] = admin["id"]
    session["admin_name"] = admin["name"]
    return jsonify({"ok": True, "admin": {"id": admin["id"], "name": admin["name"]}})

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    j = request.get_json() or {}
    name = (j.get("name") or "").strip()
    password = j.get("password") or ""
    for admin in load_data()["admins"]:
        if admin["name"] == name and check_password_hash(admin["pass_hash"], password):
            session.clear()
            session["admin_id"] = admin["id"]
            session["admin_name"] = admin["name"]
            return jsonify({"ok": True, "admin": {"id": admin["id"], "name": admin["name"]}})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return jsonify({"ok": True})

# ------------------ admin: additional admin management ------------------

@app.route("/api/admin/add_admin", methods=["POST"])
@admin_required
def admin_add_admin():
    data = load_data()
    j = request.get_json() or {}
    name = (j.get("name") or "").strip()
    password = j.get("password") or ""
    if not name or not password:
        return jsonify({"ok": False, "error": "Name & password required"}), 400
    new_admin = {"id": str(uuid.uuid4()), "name": name, "pass_hash": generate_password_hash(password), "created_at": now()}
    data.setdefault("admins", []).append(new_admin)
    save_data(data)
    return jsonify({"ok": True, "admin": {"id": new_admin["id"], "name": new_admin["name"]}})

@app.route("/api/admin/list_admins")
@admin_required
def admin_list_admins():
    data = load_data()
    return jsonify([{"id":a["id"], "name":a["name"], "created_at": a.get("created_at")} for a in data.get("admins",[])])

# ------------------ admin: members (CRUD) ------------------

@app.route("/api/admin/members", methods=["GET", "POST", "PUT", "DELETE"])
@admin_required
def admin_members():
    data = load_data()
    if request.method == "GET":
        return jsonify(data["members"])
    j = request.get_json() or {}
    if request.method == "POST":
        name = (j.get("name") or "").strip()
        if not name:
            return jsonify({"ok": False, "error": "Name required"}), 400
        m = {"id": str(uuid.uuid4()), "name": name, "gender": j.get("gender",""), "created_at": now()}
        data["members"].append(m)
        save_data(data)
        return jsonify({"ok": True, "member": m})
    if request.method == "PUT":
        mid = j.get("id")
        if not mid:
            return jsonify({"ok": False, "error": "id required"}), 400
        for m in data["members"]:
            if m["id"] == mid:
                m["name"] = j.get("name", m.get("name",""))
                m["gender"] = j.get("gender", m.get("gender",""))
                m["updated_at"] = now()
                save_data(data)
                return jsonify({"ok": True, "member": m})
        return jsonify({"ok": False, "error": "Member not found"}), 404
    # DELETE
    mid = j.get("id")
    data["members"] = [m for m in data["members"] if m["id"] != mid]
    for day in data["attendance"].values():
        day.pop(mid, None)
    save_data(data)
    return jsonify({"ok": True})

# ------------------ admin: attendance ------------------

@app.route("/api/admin/attendance", methods=["POST"])
@admin_required
def admin_attendance():
    j = request.get_json() or {}
    date = j.get("date")
    mid = j.get("id")
    status = j.get("status")
    if not date or not mid or status not in ("present", "absent"):
        return jsonify({"ok": False, "error": "Invalid data"}), 400
    data = load_data()
    data["attendance"].setdefault(date, {})[mid] = {"status": status, "edited_at": now()}
    save_data(data)
    return jsonify({"ok": True})

# ------------------ admin: content posting & editing (events, summons, bible, resources) ------------------
# Staff/admin with 'post_content' can POST and PUT; DELETE requires admin

def find_and_update(collection, item_id, updates):
    data = load_data()
    for item in data.get(collection, []):
        if item.get("id") == item_id:
            item.update(updates)
            item["updated_at"] = now()
            save_data(data)
            return item
    return None

@app.route("/api/admin/<collection>", methods=["GET", "POST"])
@staff_or_admin_allowed("post_content")
def admin_collection_post(collection):
    # allowed collections: events, summons, bible, resources
    if collection not in ("events", "summons", "bible", "resources"):
        return jsonify({"ok": False, "error": "Invalid collection"}), 400
    data = load_data()
    # GET returns collection items (staff/admin with post_content can view)
    if request.method == "GET":
        return jsonify(data.get(collection, []))
    j = request.get_json() or {}
    item = {"id": str(uuid.uuid4()), **j, "created_at": now()}
    data[collection].insert(0, item)
    save_data(data)
    return jsonify({"ok": True, collection[:-1]: item})

# allow GET (fetch single item), PUT (edit), DELETE (admin only)
@app.route("/api/admin/<collection>/<item_id>", methods=["GET", "PUT", "DELETE"])
def admin_collection_modify(collection, item_id):
    if collection not in ("events", "summons", "bible", "resources"):
        return jsonify({"ok": False, "error": "Invalid collection"}), 400

    # GET: allow staff/admin with post_content to fetch single item for editing UI
    if request.method == "GET":
        @staff_or_admin_allowed("post_content")
        def _get():
            data = load_data()
            for it in data.get(collection, []):
                if it.get("id") == item_id:
                    return jsonify(it)
            return jsonify({"ok": False, "error": "Not found"}), 404
        return _get()

    # PUT allowed for staff with post_content
    if request.method == "PUT":
        @staff_or_admin_allowed("post_content")
        def _put():
            j = request.get_json() or {}
            updates = {k: v for k, v in j.items() if k != "id"}
            updated = find_and_update(collection, item_id, updates)
            if not updated:
                return jsonify({"ok": False, "error": "Not found"}), 404
            return jsonify({"ok": True, collection[:-1]: updated})
        return _put()

    # DELETE requires admin
    if request.method == "DELETE":
        @admin_required
        def _del():
            data = load_data()
            data[collection] = [it for it in data.get(collection, []) if it.get("id") != item_id]
            save_data(data)
            return jsonify({"ok": True})
        return _del()

# ------------------ admin: donations (GET/POST/PUT/DELETE) ------------------

@app.route("/api/admin/donations", methods=["GET", "POST"])
@staff_or_admin_allowed("add_donations")
def admin_donations():
    data = load_data()
    if request.method == "GET":
        return jsonify(data["donations"])
    j = request.get_json() or {}
    name = (j.get("name") or "Anonymous").strip()
    try:
        amount = float(j.get("amount") or 0)
    except Exception:
        amount = 0
    if amount <= 0:
        return jsonify({"ok": False, "error": "Invalid amount"}), 400
    rec = {"id": str(uuid.uuid4()), "name": name, "amount": amount, "created_at": now(), "note": j.get("note","")}
    data["donations"].insert(0, rec)
    save_data(data)
    return jsonify({"ok": True, "donation": rec})

@app.route("/api/admin/donations/<did>", methods=["PUT", "DELETE"])
def admin_donation_modify(did):
    if request.method == "PUT":
        @staff_or_admin_allowed("add_donations")
        def _put():
            j = request.get_json() or {}
            data = load_data()
            for d in data["donations"]:
                if d["id"] == did:
                    d["name"] = j.get("name", d.get("name",""))
                    try:
                        if "amount" in j:
                            d["amount"] = float(j.get("amount", d.get("amount",0)))
                    except Exception:
                        pass
                    d["note"] = j.get("note", d.get("note",""))
                    d["updated_at"] = now()
                    save_data(data)
                    return jsonify({"ok": True, "donation": d})
            return jsonify({"ok": False, "error": "Not found"}), 404
        return _put()
    if request.method == "DELETE":
        @admin_required
        def _del():
            data = load_data()
            data["donations"] = [d for d in data["donations"] if d["id"] != did]
            save_data(data)
            return jsonify({"ok": True})
        return _del()

# ------------------ admin: contributions (tithes/offering/pledges) ------------------

@app.route("/api/admin/contributions", methods=["GET", "POST"])
@staff_or_admin_allowed("add_contributions")
def admin_contributions():
    data = load_data()
    if request.method == "GET":
        return jsonify(data["contributions"])
    j = request.get_json() or {}
    # expected fields: name, amount, category, date, note
    name = (j.get("name") or "Anonymous").strip()
    try:
        amount = float(j.get("amount") or 0)
    except Exception:
        amount = 0
    if amount <= 0:
        return jsonify({"ok": False, "error": "Invalid amount"}), 400
    rec = {
        "id": str(uuid.uuid4()),
        "name": name,
        "amount": amount,
        "category": (j.get("category") or "").strip(),
        "date": j.get("date") or now(),
        "note": j.get("note",""),
        "created_at": now()
    }
    data["contributions"].insert(0, rec)
    save_data(data)
    return jsonify({"ok": True, "contribution": rec})

@app.route("/api/admin/contributions/<cid>", methods=["PUT", "DELETE"])
def admin_contribution_modify(cid):
    # PUT allowed for staff with add_contributions
    if request.method == "PUT":
        @staff_or_admin_allowed("add_contributions")
        def _put():
            j = request.get_json() or {}
            data = load_data()
            for c in data["contributions"]:
                if c["id"] == cid:
                    c["name"] = j.get("name", c.get("name",""))
                    try:
                        if "amount" in j:
                            c["amount"] = float(j.get("amount", c.get("amount",0)))
                    except Exception:
                        pass
                    c["category"] = j.get("category", c.get("category",""))
                    c["date"] = j.get("date", c.get("date"))
                    c["note"] = j.get("note", c.get("note",""))
                    c["updated_at"] = now()
                    save_data(data)
                    return jsonify({"ok": True, "contribution": c})
            return jsonify({"ok": False, "error": "Not found"}), 404
        return _put()
    # DELETE requires admin to avoid accidental removal by staff
    if request.method == "DELETE":
        @admin_required
        def _del():
            data = load_data()
            data["contributions"] = [c for c in data["contributions"] if c["id"] != cid]
            save_data(data)
            return jsonify({"ok": True})
        return _del()

# ------------------ admin: prayer reply ------------------

@app.route("/api/admin/prayers/<pid>/reply", methods=["POST"])
@staff_or_admin_allowed("reply_prayers")
def reply_prayer(pid):
    j = request.get_json() or {}
    reply = (j.get("reply") or "").strip()
    data = load_data()
    for p in data["prayers"]:
        if p["id"] == pid:
            p["reply"] = reply
            p["status"] = "answered"
            p["updated_at"] = now()
            save_data(data)
            return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Not found"}), 404

# ------------------ admin: staff management ------------------

@app.route("/api/admin/staff", methods=["GET", "POST", "DELETE"])
@admin_required
def admin_staff():
    data = load_data()
    if request.method == "GET":
        return jsonify([{"id": s["id"], "name": s["name"], "role": s.get("role",""), "perms": s.get("perms",{}), "contact": s.get("contact",""), "created_at": s.get("created_at")} for s in data.get("staff",[])] )
    j = request.get_json() or {}
    if request.method == "POST":
        name = (j.get("name") or "").strip()
        password = j.get("password") or ""
        role = (j.get("role") or "").strip()
        perms = j.get("perms") or {}
        contact = (j.get("contact") or "").strip()
        if not name or not password:
            return jsonify({"ok": False, "error": "Name & password required"}), 400
        staff = {"id": str(uuid.uuid4()), "name": name, "role": role, "perms": perms, "contact": contact, "pass_hash": generate_password_hash(password), "created_at": now()}
        data.setdefault("staff", []).append(staff)
        save_data(data)
        return jsonify({"ok": True, "staff": {"id": staff["id"], "name": staff["name"], "role": staff["role"], "perms": staff["perms"], "contact": staff.get("contact","")}})
    if request.method == "DELETE":
        sid = j.get("id")
        data["staff"] = [s for s in data.get("staff",[]) if s["id"] != sid]
        save_data(data)
        return jsonify({"ok": True})

# ------------------ staff auth ------------------

@app.route("/api/staff/login", methods=["POST"])
def staff_login():
    j = request.get_json() or {}
    name = (j.get("name") or "").strip()
    password = j.get("password") or ""
    for s in load_data().get("staff", []):
        if s["name"] == name and check_password_hash(s["pass_hash"], password):
            session.clear()
            session["staff_id"] = s["id"]
            session["staff_name"] = s["name"]
            return jsonify({"ok": True, "staff": {"id": s["id"], "name": s["name"], "role": s.get("role",""), "perms": s.get("perms",{}), "contact": s.get("contact","")}})
    return jsonify({"ok": False, "error": "Invalid credentials"}), 401

@app.route("/api/staff/logout", methods=["POST"])
def staff_logout():
    session.pop("staff_id", None)
    session.pop("staff_name", None)
    return jsonify({"ok": True})

@app.route("/api/staff/me")
def staff_me():
    sid = session.get("staff_id")
    if not sid:
        return jsonify({"ok": False, "staff": None})
    for s in load_data().get("staff",[]):
        if s["id"] == sid:
            return jsonify({"ok": True, "staff": {"id": s["id"], "name": s["name"], "role": s.get("role",""), "perms": s.get("perms",{}), "contact": s.get("contact","")}})
    return jsonify({"ok": False, "staff": None})

# ------------------ PULSE RECEIVER: new endpoint for breathe -> whoiam integration ------------------

@app.route("/pulse_receiver", methods=["POST", "GET"])
def pulse_receiver():
    """
    Receive an external pulse (from breathe).
    - If PULSE_SECRET env var is set, require header X-PULSE-TOKEN or ?token=... to match.
    - Stores the pulse in data['pulses'] and updates app.config['LAST_PINGS'] for quick visibility.
    - Returns JSON {status, received_at}.
    """
    token = request.headers.get("X-PULSE-TOKEN") or request.args.get("token")
    if PULSE_SECRET:
        if not token or token != PULSE_SECRET:
            app.logger.warning("pulse_receiver: unauthorized attempt from %s", request.remote_addr)
            return jsonify({"status": "unauthorized"}), 401

    payload = request.get_json(silent=True)
    if payload is None:
        payload = request.form.to_dict() or {"message": "ping"}

    received_at = now()
    # persist pulse to data store (GitHub-aware)
    try:
        data = load_data()
        pulse = {
            "id": str(uuid.uuid4()),
            "source_ip": request.remote_addr or "unknown",
            "headers": {k: v for k, v in request.headers.items()},
            "payload": payload,
            "received_at": received_at
        }
        # prepend so newest first
        data.setdefault("pulses", [])
        data["pulses"].insert(0, pulse)
        # trim to last 200 entries to avoid runaway growth
        if len(data["pulses"]) > 200:
            data["pulses"] = data["pulses"][:200]
        save_data(data)
        # update quick in-memory last pings map
        src = request.remote_addr or "unknown"
        app.config["LAST_PINGS"][src] = {"ok": True, "at": received_at}
        app.logger.info("pulse_receiver: received pulse from %s at %s", src, received_at)
    except Exception as e:
        app.logger.exception("pulse_receiver: failed to save pulse: %s", e)
        return jsonify({"status": "error", "error": str(e)}), 500

    return jsonify({"status": "ok", "received_at": received_at}), 200

# ------------------ health & keep-alive ------------------

@app.route("/api/health")
def api_health():
    uptime = (datetime.datetime.utcnow() - START_TIME).total_seconds()
    return jsonify({
        "ok": True,
        "uptime_seconds": int(uptime),
        "github_sha": app.config.get("GITHUB_SHA"),
        "keepalive_running": app.config.get("KEEPALIVE_RUNNING", False),
        "last_pings": app.config.get("LAST_PINGS", {}),
        "pulses_count": len(load_data().get("pulses", []))
    })

def keepalive_worker(self_url, monitors, interval_seconds):
    """Background loop: ping self_url and extra monitors every interval_seconds."""
    app.logger.info(f"keepalive_worker starting: self_url={self_url} monitors={monitors} interval={interval_seconds}s")
    app.config["KEEPALIVE_RUNNING"] = True
    session_req = requests.Session()
    headers = {"User-Agent": "TomorrowAI-KeepAlive/1"}
    targets = [self_url] + monitors
    while True:
        for t in targets:
            try:
                r = session_req.get(t, headers=headers, timeout=15)
                app.config["LAST_PINGS"][t] = {"ok": r.status_code == 200, "status_code": r.status_code, "at": now()}
                app.logger.debug(f"keepalive ping {t} -> {r.status_code}")
            except Exception as e:
                app.config["LAST_PINGS"][t] = {"ok": False, "error": str(e), "at": now()}
                app.logger.warning(f"keepalive ping failed {t}: {e}")
        time.sleep(interval_seconds)

def start_keepalive_in_thread():
    try:
        enabled = os.getenv("KEEPALIVE_ENABLED", "1") == "1"
        if not enabled:
            app.logger.info("KEEPALIVE_ENABLED != 1 -> not starting keepalive.")
            return
        try:
            interval = int(os.getenv("KEEPALIVE_INTERVAL", str(1200)))
            if interval < 60:
                interval = 60
        except Exception:
            interval = 1200
        app_url = os.getenv("APP_URL")
        port = int(os.getenv("PORT", "4001"))
        if app_url:
            self_url = app_url.rstrip("/") + "/"
        else:
            self_url = f"http://127.0.0.1:{port}/"
        monitors_raw = os.getenv("UPTIME_MONITORS", "")
        monitors = [m.strip() for m in monitors_raw.split(",") if m.strip()]
        # If already started in this process, skip
        if app.config.get("KEEPALIVE_STARTED"):
            app.logger.info("Keepalive already started in this process.")
            return
        thr = threading.Thread(target=keepalive_worker, args=(self_url, monitors, interval), daemon=True)
        thr.start()
        app.config["KEEPALIVE_STARTED"] = True
        app.logger.info("keepalive thread started.")
    except Exception as e:
        app.logger.warning(f"failed to start keepalive: {e}")

# Use before_request guard so we don't rely on before_first_request (some Flask installs lack it)
@app.before_request
def _ensure_keepalive_started_on_first_request():
    run_in_workers = os.getenv("RUN_KEEPALIVE_IN_WORKERS", "1") == "1"
    if not run_in_workers:
        return
    if not app.config.get("KEEPALIVE_STARTED"):
        start_keepalive_in_thread()

# ------------------ start ------------------

if __name__ == "__main__":
    # ensure local DB exists or initialize from DEFAULT_DATA
    if not LOCAL_DATA_FILE.exists():
        save_data(DEFAULT_DATA.copy())
    # try to prime from GitHub once at startup (best-effort)
    if GITHUB_TOKEN and REPO:
        db, sha = load_from_github()
        if isinstance(db, dict):
            try:
                with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(db, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            _GITHUB_SHA = sha
            app.config["GITHUB_SHA"] = _GITHUB_SHA
    port = int(os.getenv("PORT", 4001))
    # start keepalive immediately when running directly
    start_keepalive_in_thread()
    app.run(host="0.0.0.0", port=port, debug=True)

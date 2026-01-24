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

If GITHUB_TOKEN and GITHUB_REPO are provided, the app will attempt
to read/write the DB file from GitHub. If that fails it will fall back
to a local `data.json` file in the project root.
"""
import os
import json
import base64
import datetime
import uuid
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

# stored in memory while process runs (sha returned by GitHub)
_GITHUB_SHA = None

def load_from_github():
    """
    Returns (data_dict, sha) or (None, None) if failed.
    """
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
    Saves data to the repo path. Returns (success_bool, new_sha_or_None).
    """
    if not GITHUB_TOKEN or not REPO:
        return False, None

    url = f"https://api.github.com/repos/{REPO}/contents/{DB_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}"}

    content_b64 = base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")).decode("utf-8")

    payload = {
        "message": "Update database (auto)",
        "content": content_b64,
        "branch": BRANCH
    }
    if sha:
        payload["sha"] = sha

    try:
        r = requests.put(url, headers=headers, json=payload, timeout=15)
    except Exception:
        return False, None

    if r.status_code in (200, 201):
        try:
            new_sha = r.json().get("content", {}).get("sha")
            return True, new_sha
        except Exception:
            return True, None
    else:
        return False, None

# ------------------ Flask + app setup ------------------

app = Flask(__name__, static_folder=str(ROOT))
app.secret_key = os.getenv("FLASK_SECRET", "CHANGE-THIS-TO-A-SECURE-RANDOM-STRING")

DEFAULT_DATA = {
    "admins": [],
    "staff": [],
    "members": [],
    "attendance": {},
    "events": [],
    "summons": [],
    "bible": [],
    "resources": [],
    "donations": [],
    "prayers": []
}

def now():
    return datetime.datetime.utcnow().isoformat() + "Z"

# ------------------ load/save data (GitHub-aware) ------------------

def load_data():
    """
    Loads data from GitHub if configured, otherwise from local file.
    Ensures DEFAULT_DATA keys exist and returns dict.
    """
    global _GITHUB_SHA

    # Try GitHub first
    if GITHUB_TOKEN and REPO:
        db, sha = load_from_github()
        if isinstance(db, dict):
            _GITHUB_SHA = sha
            # ensure keys
            for k, v in DEFAULT_DATA.items():
                if k not in db:
                    db[k] = v
            # Write local cache for convenience (best-effort)
            try:
                with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(db, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
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

    # ensure keys
    for k, v in DEFAULT_DATA.items():
        if k not in data:
            data[k] = v
    return data

def save_data(data):
    """
    Saves data either to GitHub (if configured) or to local file.
    When GitHub is used, we update the in-process _GITHUB_SHA.
    Returns True on success (GitHub or local), False otherwise.
    """
    global _GITHUB_SHA

    if GITHUB_TOKEN and REPO:
        success, new_sha = save_to_github(data, sha=_GITHUB_SHA)
        if success:
            _GITHUB_SHA = new_sha
            # also update local cache best-effort
            try:
                with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            return True
        else:
            # fallback to local file
            try:
                with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                return True
            except Exception:
                return False
    else:
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
    return jsonify({
        "members": d["members"],
        "attendance": d["attendance"],
        "events": d["events"],
        "summons": d["summons"],
        "bible": d["bible"],
        "resources": d["resources"],
        "donations": d["donations"],
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

    admin = {
        "id": str(uuid.uuid4()),
        "name": name,
        "pass_hash": generate_password_hash(password),
        "created_at": now()
    }

    data["admins"] = [admin]
    save_data(data)

    session.clear()
    session["admin_id"] = admin["id"]
    session["admin_name"] = admin["name"]

    return jsonify({
        "ok": True,
        "admin": {"id": admin["id"], "name": admin["name"]}
    })

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
            return jsonify({
                "ok": True,
                "admin": {"id": admin["id"], "name": admin["name"]}
            })

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
    new_admin = {
        "id": str(uuid.uuid4()),
        "name": name,
        "pass_hash": generate_password_hash(password),
        "created_at": now()
    }
    data.setdefault("admins", []).append(new_admin)
    save_data(data)
    return jsonify({"ok": True, "admin": {"id": new_admin["id"], "name": new_admin["name"]}})

@app.route("/api/admin/list_admins")
@admin_required
def admin_list_admins():
    data = load_data()
    return jsonify([{"id":a["id"], "name":a["name"], "created_at": a.get("created_at")} for a in data.get("admins",[])])

# ------------------ admin: members ------------------

@app.route("/api/admin/members", methods=["GET", "POST", "DELETE"])
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
        m = {"id": str(uuid.uuid4()), "name": name, "gender": j.get("gender","")}
        data["members"].append(m)
        save_data(data)
        return jsonify({"ok": True, "member": m})

    if request.method == "DELETE":
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
    data["attendance"].setdefault(date, {})[mid] = {
        "status": status,
        "edited_at": now()
    }
    save_data(data)
    return jsonify({"ok": True})

# ------------------ admin: content posting ------------------

def simple_post(collection):
    j = request.get_json() or {}
    data = load_data()
    item = {"id": str(uuid.uuid4()), **j, "created_at": now()}
    data[collection].insert(0, item)
    save_data(data)
    return jsonify({"ok": True, collection[:-1]: item})

@app.route("/api/admin/events", methods=["POST"])
@staff_or_admin_allowed("post_content")
def admin_events(): return simple_post("events")

@app.route("/api/admin/summons", methods=["POST"])
@staff_or_admin_allowed("post_content")
def admin_summons(): return simple_post("summons")

@app.route("/api/admin/bible", methods=["POST"])
@staff_or_admin_allowed("post_content")
def admin_bible(): return simple_post("bible")

@app.route("/api/admin/resources", methods=["POST"])
@staff_or_admin_allowed("post_content")
def admin_resources(): return simple_post("resources")

# ------------------ admin: donations ------------------

@app.route("/api/admin/donations", methods=["POST", "GET"])
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
    rec = {"id": str(uuid.uuid4()), "name": name, "amount": amount, "created_at": now()}
    data["donations"].insert(0, rec)
    save_data(data)
    return jsonify({"ok": True, "donation": rec})

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
        staff = {
            "id": str(uuid.uuid4()),
            "name": name,
            "role": role,
            "perms": perms,
            "contact": contact,
            "pass_hash": generate_password_hash(password),
            "created_at": now()
        }
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

# ------------------ start ------------------

if __name__ == "__main__":
    # ensure local DB exists or initialize from DEFAULT_DATA
    if not LOCAL_DATA_FILE.exists():
        save_data(DEFAULT_DATA.copy())
    # try to prime from GitHub once at startup (best-effort)
    if GITHUB_TOKEN and REPO:
        db, sha = load_from_github()
        if isinstance(db, dict):
            # write local cache and set sha
            try:
                with open(LOCAL_DATA_FILE, "w", encoding="utf-8") as f:
                    json.dump(db, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            _GITHUB_SHA = sha

    port = int(os.getenv("PORT", 4001))
    app.run(host="0.0.0.0", port=port, debug=True)

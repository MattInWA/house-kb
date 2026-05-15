#!/usr/bin/env python3
"""
House Knowledge Base — Flask app
"""

import sqlite3
import hashlib
import secrets
import os
import functools
import uuid
import mimetypes
from datetime import datetime
from flask import (
    Flask, g, request, session, redirect, url_for,
    render_template, jsonify, flash, abort, send_from_directory
)
from PIL import Image
import pillow_heif

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("HOUSE_DB", "/home/share/house/house.db")
SECRET_KEY = os.environ.get("HOUSE_SECRET", "change-me-in-production")
UPLOAD_DIR = os.environ.get("HOUSE_UPLOADS", "/home/matt/house/uploads")
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".pdf"}
MAX_UPLOAD_MB = 20

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

os.makedirs(UPLOAD_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db:
        db.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rv = cur.fetchall()
    return (rv[0] if rv else None) if one else rv


def execute(sql, args=()):
    db = get_db()
    cur = db.execute(sql, args)
    db.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(32)
    h = hashlib.sha256(f"{salt}{password}".encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(stored: str, password: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256(f"{salt}{password}".encode()).hexdigest() == h
    except Exception:
        return False


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = current_user()
        if not user or not user["is_admin"]:
            abort(403)
        return f(*args, **kwargs)
    return decorated


def api_key_required(f):
    """For API endpoints — accepts X-API-Key header."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if not key:
            return jsonify({"error": "Missing X-API-Key header"}), 401
        key_hash = hash_api_key(key)
        row = query("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,), one=True)
        if not row:
            return jsonify({"error": "Invalid API key"}), 401
        # Update last_used
        execute("UPDATE api_keys SET last_used = ? WHERE id = ?",
                (datetime.utcnow().isoformat(), row["id"]))
        g.api_key_row = row
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Template context
# ---------------------------------------------------------------------------

@app.context_processor
def inject_user():
    return {"current_user": current_user()}


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = query("SELECT * FROM users WHERE username = ?", (username,), one=True)
        if user and verify_password(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Main web routes
# ---------------------------------------------------------------------------


def build_location_tree():
    """Return level-1 locations (children of root) with nested children, skipping root itself."""
    all_locs = query("""
        SELECT l.id, l.name, l.parent_id,
               (SELECT COUNT(*) FROM items i WHERE i.location_id = l.id) as item_count
        FROM locations l
        ORDER BY l.name
    """)
    # Find root (no parent)
    root = next((l for l in all_locs if l["parent_id"] is None), None)
    if not root:
        return []
    by_parent = {}
    for loc in all_locs:
        pid = loc["parent_id"]
        by_parent.setdefault(pid, []).append(dict(loc))
    def attach_children(node):
        node["children"] = by_parent.get(node["id"], [])
        for child in node["children"]:
            attach_children(child)
        return node
    l1_nodes = by_parent.get(root["id"], [])
    return [attach_children(dict(n)) for n in l1_nodes]

@app.route("/")
@login_required
def index():
    item_count = query("SELECT COUNT(*) as n FROM items", one=True)["n"]
    location_count = query("SELECT COUNT(*) as n FROM locations WHERE id != 1", one=True)["n"]
    recent_items = query("""
        SELECT i.*, l.name as location_name
        FROM items i
        LEFT JOIN locations l ON l.id = i.location_id
        ORDER BY i.created_at DESC LIMIT 8
    """)
    recent_events = query("""
        SELECT e.*, i.name as item_name
        FROM events e
        JOIN items i ON i.id = e.item_id
        ORDER BY e.created_at DESC LIMIT 5
    """)
    tree = build_location_tree()
    return render_template("index.html",
        item_count=item_count,
        location_count=location_count,
        recent_items=recent_items,
        recent_events=recent_events,
        tree=tree,
    )


# --- Locations ---

@app.route("/locations")
@login_required
def locations():
    tree = build_location_tree()
    return render_template("locations.html", tree=tree)


@app.route("/locations/add", methods=["GET", "POST"])
@login_required
def add_location():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        parent_id = request.form.get("parent_id") or None
        notes = request.form.get("notes", "").strip() or None
        if not name:
            flash("Name is required.", "error")
        else:
            duplicate = query(
                "SELECT id FROM locations WHERE LOWER(name) = LOWER(?) AND parent_id IS ?",
                (name, parent_id), one=True
            )
            if duplicate:
                flash(f"A location named '{name}' already exists under that parent.", "error")
            else:
                loc_id = execute(
                    "INSERT INTO locations (name, parent_id, notes) VALUES (?, ?, ?)",
                    (name, parent_id, notes)
                )
                flash(f"Location '{name}' added.", "success")
                action = request.form.get("action", "save")
                if action == "another":
                    return redirect(url_for("add_location", parent_id=parent_id))
                return redirect(url_for("location_detail", loc_id=loc_id))
    all_locs = query("SELECT id, name FROM locations ORDER BY name")
    selected_parent = request.args.get("parent_id")
    return render_template("add_location.html", all_locations=all_locs, selected_parent=selected_parent)


@app.route("/locations/<int:loc_id>/edit", methods=["GET", "POST"])
@login_required
def edit_location(loc_id):
    loc = query("SELECT * FROM locations WHERE id = ?", (loc_id,), one=True)
    if not loc:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
        else:
            execute(
                "UPDATE locations SET name=?, parent_id=?, notes=? WHERE id=?",
                (
                    name,
                    request.form.get("parent_id") or None,
                    request.form.get("notes", "").strip() or None,
                    loc_id,
                )
            )
            flash("Location updated.", "success")
            return redirect(url_for("location_detail", loc_id=loc_id))
    all_locs = query("SELECT id, name FROM locations WHERE id != ? ORDER BY name", (loc_id,))
    return render_template("edit_location.html", loc=loc, all_locations=all_locs)


@app.route("/locations/<int:loc_id>/delete", methods=["GET", "POST"])
@login_required
def delete_location(loc_id):
    loc = query("SELECT * FROM locations WHERE id = ?", (loc_id,), one=True)
    if not loc:
        abort(404)

    # Calculate depth via recursive CTE
    depth_row = query("""
        WITH RECURSIVE ancestors(id, parent_id, depth) AS (
            SELECT id, parent_id, 0 FROM locations WHERE id = ?
            UNION ALL
            SELECT l.id, l.parent_id, a.depth + 1
            FROM locations l JOIN ancestors a ON l.id = a.parent_id
        )
        SELECT MAX(depth) as depth FROM ancestors
    """, (loc_id,), one=True)
    depth = depth_row["depth"] if depth_row else 0

    if depth <= 1:
        flash("This location is protected and cannot be deleted.", "error")
        return redirect(url_for("location_detail", loc_id=loc_id))

    children = query("SELECT id, name FROM locations WHERE parent_id = ? ORDER BY name", (loc_id,))
    items = query("SELECT id, name FROM items WHERE location_id = ? ORDER BY name", (loc_id,))

    if request.method == "POST":
        if children or items:
            flash("Cannot delete — reassign all items and sub-locations first.", "error")
            return redirect(url_for("delete_location", loc_id=loc_id))
        execute("DELETE FROM locations WHERE id = ?", (loc_id,))
        flash("Location deleted.", "success")
        return redirect(url_for("locations"))

    return render_template("delete_location.html", loc=loc, children=children, items=items)


@app.route("/locations/<int:loc_id>")
@login_required
def location_detail(loc_id):
    loc = query("SELECT l.*, p.name as parent_name FROM locations l LEFT JOIN locations p ON p.id = l.parent_id WHERE l.id = ?", (loc_id,), one=True)
    if not loc:
        abort(404)
    children = query("SELECT *, (SELECT COUNT(*) FROM items i WHERE i.location_id = l.id) as item_count FROM locations l WHERE parent_id = ? ORDER BY name", (loc_id,))
    items = query("""
        SELECT * FROM items WHERE location_id = ? ORDER BY name
    """, (loc_id,))
    return render_template("location_detail.html", loc=loc, children=children, items=items)


# --- Items ---

@app.route("/items")
@login_required
def items():
    category = request.args.get("category", "")
    search = request.args.get("q", "").strip()

    sql = """
        SELECT i.*, l.name as location_name
        FROM items i
        LEFT JOIN locations l ON l.id = i.location_id
        WHERE 1=1
    """
    args = []
    if category:
        sql += " AND i.category = ?"
        args.append(category)
    if search:
        sql += " AND (i.name LIKE ? OR i.notes LIKE ? OR i.manufacturer LIKE ? OR i.model LIKE ?)"
        args.extend([f"%{search}%"] * 4)
    sql += " ORDER BY i.name"

    rows = query(sql, args)
    categories = query("SELECT DISTINCT category FROM items WHERE category IS NOT NULL ORDER BY category")
    return render_template("items.html", items=rows, categories=categories,
                           active_category=category, search=search)


@app.route("/items/add", methods=["GET", "POST"])
@login_required
def add_item():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "error")
        else:
            user = current_user()
            item_id = execute("""
                INSERT INTO items (name, category, location_id, purchased_date, installed_date,
                                   manufacturer, model, notes, created_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                name,
                request.form.get("category", "").strip() or None,
                request.form.get("location_id") or None,
                request.form.get("purchased_date", "").strip() or None,
                request.form.get("installed_date", "").strip() or None,
                request.form.get("manufacturer", "").strip() or None,
                request.form.get("model", "").strip() or None,
                request.form.get("notes", "").strip() or None,
                user["id"] if user else None,
            ))

            # Handle dynamic attributes
            keys = request.form.getlist("attr_key")
            vals = request.form.getlist("attr_value")
            for k, v in zip(keys, vals):
                k = k.strip()
                v = v.strip()
                if k and v:
                    execute("INSERT INTO attributes (item_id, key, value) VALUES (?, ?, ?)",
                            (item_id, k, v))

            # Auto-create purchase event if date provided
            purchased_date = request.form.get("purchased_date", "").strip()
            if purchased_date:
                execute("""
                    INSERT INTO events (item_id, event_date, event_type, description, created_by)
                    VALUES (?, ?, 'purchased', 'Initial purchase', ?)
                """, (item_id, purchased_date, user["id"] if user else None))

            flash(f"'{name}' added.", "success")
            action = request.form.get("action", "save")
            if action == "another":
                return redirect(url_for("add_item"))
            return redirect(url_for("item_detail", item_id=item_id))

    all_locs = query("""
        SELECT l.id, l.name, p.name as parent_name
        FROM locations l
        LEFT JOIN locations p ON p.id = l.parent_id
        ORDER BY p.name NULLS FIRST, l.name
    """)
    categories = query("SELECT DISTINCT category FROM items WHERE category IS NOT NULL ORDER BY category")
    return render_template("add_item.html", all_locations=all_locs, categories=categories)


@app.route("/items/<int:item_id>")
@login_required
def item_detail(item_id):
    item = query("""
        SELECT i.*, l.name as location_name, u.display_name as created_by_name
        FROM items i
        LEFT JOIN locations l ON l.id = i.location_id
        LEFT JOIN users u ON u.id = i.created_by
        WHERE i.id = ?
    """, (item_id,), one=True)
    if not item:
        abort(404)
    attrs = query("SELECT * FROM attributes WHERE item_id = ? ORDER BY key", (item_id,))
    events = query("SELECT * FROM events WHERE item_id = ? ORDER BY event_date DESC, created_at DESC", (item_id,))
    rels_from = query("""
        SELECT r.*, i.name as related_name
        FROM relationships r JOIN items i ON i.id = r.to_item
        WHERE r.from_item = ?
    """, (item_id,))
    rels_to = query("""
        SELECT r.*, i.name as related_name
        FROM relationships r JOIN items i ON i.id = r.from_item
        WHERE r.to_item = ?
    """, (item_id,))
    all_items = query("SELECT id, name FROM items WHERE id != ? ORDER BY name", (item_id,))
    attachments = query("SELECT * FROM attachments WHERE item_id = ? ORDER BY created_at", (item_id,))
    return render_template("item_detail.html", item=item, attrs=attrs,
                           events=events, rels_from=rels_from, rels_to=rels_to,
                           all_items=all_items, attachments=attachments)


@app.route("/items/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def edit_item(item_id):
    item = query("SELECT * FROM items WHERE id = ?", (item_id,), one=True)
    if not item:
        abort(404)

    if request.method == "POST":
        execute("""
            UPDATE items SET name=?, category=?, location_id=?, purchased_date=?,
            installed_date=?, manufacturer=?, model=?, notes=?, updated_at=datetime('now')
            WHERE id=?
        """, (
            request.form.get("name", "").strip(),
            request.form.get("category", "").strip() or None,
            request.form.get("location_id") or None,
            request.form.get("purchased_date", "").strip() or None,
            request.form.get("installed_date", "").strip() or None,
            request.form.get("manufacturer", "").strip() or None,
            request.form.get("model", "").strip() or None,
            request.form.get("notes", "").strip() or None,
            item_id,
        ))
        flash("Item updated.", "success")
        return redirect(url_for("item_detail", item_id=item_id))

    all_locs = query("""
        SELECT l.id, l.name, p.name as parent_name
        FROM locations l LEFT JOIN locations p ON p.id = l.parent_id
        ORDER BY p.name NULLS FIRST, l.name
    """)
    categories = query("SELECT DISTINCT category FROM items WHERE category IS NOT NULL ORDER BY category")
    return render_template("edit_item.html", item=item, all_locations=all_locs, categories=categories)


@app.route("/items/<int:item_id>/add_attribute", methods=["POST"])
@login_required
def add_attribute(item_id):
    key = request.form.get("key", "").strip()
    value = request.form.get("value", "").strip()
    if key and value:
        execute("INSERT INTO attributes (item_id, key, value) VALUES (?, ?, ?)", (item_id, key, value))
        flash("Attribute added.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


@app.route("/attributes/<int:attr_id>/delete", methods=["POST"])
@login_required
def delete_attribute(attr_id):
    attr = query("SELECT * FROM attributes WHERE id = ?", (attr_id,), one=True)
    if attr:
        execute("DELETE FROM attributes WHERE id = ?", (attr_id,))
        flash("Attribute removed.", "success")
        return redirect(url_for("item_detail", item_id=attr["item_id"]))
    abort(404)


@app.route("/items/<int:item_id>/add_event", methods=["POST"])
@login_required
def add_event(item_id):
    user = current_user()
    execute("""
        INSERT INTO events (item_id, event_date, event_type, description, cost, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        item_id,
        request.form.get("event_date", "").strip() or None,
        request.form.get("event_type", "noted"),
        request.form.get("description", "").strip() or None,
        request.form.get("cost") or None,
        user["id"] if user else None,
    ))
    flash("Event logged.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


@app.route("/items/<int:item_id>/add_relationship", methods=["POST"])
@login_required
def add_relationship(item_id):
    to_item = request.form.get("to_item")
    relation = request.form.get("relation", "").strip()
    if to_item and relation:
        execute("""
            INSERT INTO relationships (from_item, to_item, relation, notes)
            VALUES (?, ?, ?, ?)
        """, (item_id, to_item, relation, request.form.get("notes", "").strip() or None))
        flash("Relationship added.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


# --- Admin ---

@app.route("/admin")
@login_required
@admin_required
def admin():
    users = query("SELECT * FROM users ORDER BY username")
    api_keys = query("""
        SELECT a.*, u.username as created_by_name
        FROM api_keys a LEFT JOIN users u ON u.id = a.created_by
        ORDER BY a.created_at DESC
    """)
    return render_template("admin.html", users=users, api_keys=api_keys)


@app.route("/admin/add_user", methods=["POST"])
@login_required
@admin_required
def add_user():
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip()
    password = request.form.get("password", "")
    is_admin = 1 if request.form.get("is_admin") else 0
    if not username or not password:
        flash("Username and password required.", "error")
    else:
        try:
            execute(
                "INSERT INTO users (username, password_hash, display_name, is_admin) VALUES (?, ?, ?, ?)",
                (username, hash_password(password), display_name or None, is_admin)
            )
            flash(f"User '{username}' created.", "success")
        except sqlite3.IntegrityError:
            flash(f"Username '{username}' already exists.", "error")
    return redirect(url_for("admin"))


@app.route("/admin/generate_api_key", methods=["POST"])
@login_required
@admin_required
def generate_api_key():
    label = request.form.get("label", "").strip()
    if not label:
        flash("Label required.", "error")
        return redirect(url_for("admin"))
    user = current_user()
    raw_key = f"hkb-{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(raw_key)
    execute(
        "INSERT INTO api_keys (key_hash, label, created_by) VALUES (?, ?, ?)",
        (key_hash, label, user["id"])
    )
    # Show the key once — it's never stored in plaintext
    flash(f"API key generated for '{label}'. Copy it now — it won't be shown again: {raw_key}", "apikey")
    return redirect(url_for("admin"))


@app.route("/admin/revoke_api_key/<int:key_id>", methods=["POST"])
@login_required
@admin_required
def revoke_api_key(key_id):
    execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    flash("API key revoked.", "success")
    return redirect(url_for("admin"))


# ---------------------------------------------------------------------------
# API routes (JSON, X-API-Key auth)
# ---------------------------------------------------------------------------

@app.route("/api/items", methods=["GET"])
@api_key_required
def api_list_items():
    category = request.args.get("category")
    location_id = request.args.get("location_id")
    search = request.args.get("q")

    sql = """
        SELECT i.*, l.name as location_name
        FROM items i LEFT JOIN locations l ON l.id = i.location_id
        WHERE 1=1
    """
    args = []
    if category:
        sql += " AND i.category = ?"
        args.append(category)
    if location_id:
        sql += " AND i.location_id = ?"
        args.append(location_id)
    if search:
        sql += " AND (i.name LIKE ? OR i.notes LIKE ?)"
        args.extend([f"%{search}%", f"%{search}%"])
    sql += " ORDER BY i.name"

    rows = query(sql, args)
    return jsonify([dict(r) for r in rows])


@app.route("/api/items/<int:item_id>", methods=["GET"])
@api_key_required
def api_get_item(item_id):
    item = query("""
        SELECT i.*, l.name as location_name
        FROM items i LEFT JOIN locations l ON l.id = i.location_id
        WHERE i.id = ?
    """, (item_id,), one=True)
    if not item:
        return jsonify({"error": "Not found"}), 404
    attrs = query("SELECT key, value FROM attributes WHERE item_id = ?", (item_id,))
    events = query("SELECT * FROM events WHERE item_id = ? ORDER BY event_date DESC", (item_id,))
    result = dict(item)
    result["attributes"] = {r["key"]: r["value"] for r in attrs}
    result["events"] = [dict(e) for e in events]
    return jsonify(result)


@app.route("/api/items", methods=["POST"])
@api_key_required
def api_create_item():
    data = request.get_json(force=True)
    if not data or not data.get("name"):
        return jsonify({"error": "name is required"}), 400

    item_id = execute("""
        INSERT INTO items (name, category, location_id, purchased_date, installed_date,
                           manufacturer, model, notes, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
    """, (
        data.get("name"),
        data.get("category"),
        data.get("location_id"),
        data.get("purchased_date"),
        data.get("installed_date"),
        data.get("manufacturer"),
        data.get("model"),
        data.get("notes"),
    ))

    # Optional attributes dict
    for k, v in (data.get("attributes") or {}).items():
        execute("INSERT INTO attributes (item_id, key, value) VALUES (?, ?, ?)", (item_id, str(k), str(v)))

    return jsonify({"id": item_id, "name": data["name"]}), 201


@app.route("/api/items/<int:item_id>", methods=["PATCH"])
@api_key_required
def api_update_item(item_id):
    item = query("SELECT * FROM items WHERE id = ?", (item_id,), one=True)
    if not item:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True) or {}
    fields = ["name", "category", "location_id", "purchased_date",
              "installed_date", "manufacturer", "model", "notes"]
    updates = {f: data[f] for f in fields if f in data}
    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        execute(f"UPDATE items SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
                list(updates.values()) + [item_id])
    if "attributes" in data:
        for k, v in data["attributes"].items():
            existing = query("SELECT id FROM attributes WHERE item_id = ? AND key = ?", (item_id, k), one=True)
            if existing:
                execute("UPDATE attributes SET value = ? WHERE id = ?", (str(v), existing["id"]))
            else:
                execute("INSERT INTO attributes (item_id, key, value) VALUES (?, ?, ?)", (item_id, k, str(v)))
    return jsonify({"id": item_id})


@app.route("/api/locations", methods=["GET"])
@api_key_required
def api_list_locations():
    rows = query("""
        SELECT l.*, p.name as parent_name
        FROM locations l LEFT JOIN locations p ON p.id = l.parent_id
        ORDER BY l.name
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/api/search", methods=["GET"])
@api_key_required
def api_search():
    """Full-text search across items + attributes."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    items = query("""
        SELECT DISTINCT i.id, i.name, i.category, l.name as location_name
        FROM items i
        LEFT JOIN locations l ON l.id = i.location_id
        LEFT JOIN attributes a ON a.item_id = i.id
        WHERE i.name LIKE ? OR i.notes LIKE ? OR i.manufacturer LIKE ?
              OR a.value LIKE ?
        ORDER BY i.name
        LIMIT 50
    """, [f"%{q}%"] * 4)
    return jsonify([dict(r) for r in items])


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

THUMB_EXTS = {".jpg", ".jpeg", ".png", ".heic"}

def make_thumb(src_path, dest_path, size=(400, 400)):
    """Convert image (including HEIC) to JPEG thumbnail."""
    pillow_heif.register_heif_opener()
    with Image.open(src_path) as img:
        img = img.convert("RGB")
        img.thumbnail(size, Image.LANCZOS)
        img.save(dest_path, "JPEG", quality=85)


@app.route("/items/<int:item_id>/upload", methods=["POST"])
@login_required
def upload_attachment(item_id):
    item = query("SELECT id FROM items WHERE id = ?", (item_id,), one=True)
    if not item:
        abort(404)
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("item_detail", item_id=item_id))
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        flash(f"File type not allowed. Accepted: jpg, png, heic, pdf.", "error")
        return redirect(url_for("item_detail", item_id=item_id))

    unique = uuid.uuid4().hex
    is_image = ext in THUMB_EXTS

    if is_image:
        # Save original temporarily, convert to JPEG
        tmp_path = os.path.join(UPLOAD_DIR, f"tmp_{unique}{ext}")
        stored_name = f"{unique}.jpg"
        stored_path = os.path.join(UPLOAD_DIR, stored_name)
        f.save(tmp_path)
        try:
            make_thumb(tmp_path, stored_path, size=(1600, 1600))
        except Exception as e:
            os.remove(tmp_path)
            flash(f"Image conversion failed: {e}", "error")
            return redirect(url_for("item_detail", item_id=item_id))
        os.remove(tmp_path)
        mime = "image/jpeg"
    else:
        stored_name = f"{unique}{ext}"
        stored_path = os.path.join(UPLOAD_DIR, stored_name)
        f.save(stored_path)
        mime = mimetypes.guess_type(f.filename)[0] or "application/octet-stream"

    execute(
        "INSERT INTO attachments (item_id, filename, original_name, mime_type) VALUES (?, ?, ?, ?)",
        (item_id, stored_name, f.filename, mime)
    )
    flash("Attachment added.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


@app.route("/uploads/<path:filename>")
@login_required
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/attachments/<int:att_id>/delete", methods=["POST"])
@login_required
def delete_attachment(att_id):
    att = query("SELECT * FROM attachments WHERE id = ?", (att_id,), one=True)
    if not att:
        abort(404)
    filepath = os.path.join(UPLOAD_DIR, att["filename"])
    if os.path.exists(filepath):
        os.remove(filepath)
    execute("DELETE FROM attachments WHERE id = ?", (att_id,))
    flash("Attachment removed.", "success")
    return redirect(url_for("item_detail", item_id=att["item_id"]))


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)

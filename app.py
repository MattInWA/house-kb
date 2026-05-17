#!/usr/bin/env python3
"""
House Knowledge Base — Flask app
"""

import sqlite3
import hashlib
import hmac
import secrets
import os
import functools
import uuid
import mimetypes
from datetime import datetime
from io import BytesIO
from flask import (
    Flask, g, request, session, redirect, url_for,
    render_template, jsonify, flash, abort, send_from_directory, send_file
)
from PIL import Image, ImageDraw, ImageFont
import pillow_heif

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("HOUSE_DB", os.path.join(os.path.dirname(__file__), "data", "house.db"))
SECRET_KEY = os.environ.get("HOUSE_SECRET", "change-me-in-production")
UPLOAD_DIR = os.environ.get("HOUSE_UPLOADS", os.path.join(os.path.dirname(__file__), "uploads"))
BASE_URL = os.environ.get("HOUSE_BASE_URL", "").rstrip("/")
QR_LABEL = os.environ.get("HOUSE_QR_LABEL", "house-kb")
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

_PBKDF2_ITERS = 260000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERS)
    return f"pbkdf2:{salt}:{dk.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        _, salt, h = stored.split(":", 2)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERS)
        return hmac.compare_digest(dk.hex(), h)
    except Exception:
        return False


def hash_api_key(key: str) -> str:
    return hmac.new(SECRET_KEY.encode(), key.encode(), hashlib.sha256).hexdigest()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return query("SELECT * FROM users WHERE id = ?", (user_id,), one=True)


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            session["_login_next"] = request.path
            return redirect(url_for("login"))
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
            next_url = session.get("_login_next") or url_for("index")
            session.clear()
            session["user_id"] = user["id"]
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
        location_id = request.form.get("location_id") or None
        if not name:
            flash("Name is required.", "error")
        elif not location_id:
            flash("Location is required.", "error")
        elif query("SELECT id FROM items WHERE LOWER(name) = LOWER(?) AND location_id IS ?",
                   (name, location_id)):
            flash(f"An item named '{name}' already exists at that location.", "error")
            all_locs = query("""
                SELECT l.id, l.name, p.name as parent_name
                FROM locations l
                LEFT JOIN locations p ON p.id = l.parent_id
                ORDER BY p.name NULLS FIRST, l.name
            """)
            categories = query("SELECT DISTINCT category FROM items WHERE category IS NOT NULL ORDER BY category")
            return render_template("add_item.html",
                                   form_data=request.form,
                                   all_locations=all_locs,
                                   categories=categories)
        else:
            user = current_user()
            item_id = execute("""
                INSERT INTO items (name, category, location_id, purchased_date, installed_date,
                                   manufacturer, model, notes, created_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                name,
                request.form.get("category", "").strip() or None,
                location_id,
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

            # Auto-create purchase event if date or price provided
            purchased_date = request.form.get("purchased_date", "").strip() or None
            purchase_price_raw = request.form.get("purchase_price", "").strip()
            purchase_price = float(purchase_price_raw) if purchase_price_raw else None
            if purchased_date or purchase_price is not None:
                execute("""
                    INSERT INTO events (item_id, event_date, event_type, description, cost, created_by)
                    VALUES (?, ?, 'purchased', 'Initial purchase', ?, ?)
                """, (item_id, purchased_date, purchase_price, user["id"] if user else None))

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
    clone_data = None
    clone_from = request.args.get("clone_from")
    if clone_from:
        src = query("SELECT * FROM items WHERE id = ?", (clone_from,), one=True)
        if src:
            src_attrs = query("SELECT key, value FROM attributes WHERE item_id = ? ORDER BY key",
                              (int(clone_from),))
            clone_data = {
                "source_name": src["name"],
                "category":    src["category"] or "",
                "location_id": str(src["location_id"] or ""),
                "manufacturer": src["manufacturer"] or "",
                "model":       src["model"] or "",
                "notes":       src["notes"] or "",
                "attrs": [{"key": a["key"], "value": a["value"]} for a in src_attrs],
            }
    return render_template("add_item.html", all_locations=all_locs, categories=categories,
                           clone_data=clone_data)


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
    all_items = query("SELECT id, name, location_id FROM items WHERE id != ? ORDER BY name", (item_id,))
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
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    if is_fetch:
        data = request.get_json(force=True) or {}
        key = (data.get("key") or "").strip()
        value = (data.get("value") or "").strip()
    else:
        key = request.form.get("key", "").strip()
        value = request.form.get("value", "").strip()
    if not (key and value):
        if is_fetch:
            return jsonify({"error": "Key and value required"}), 400
        return redirect(url_for("item_detail", item_id=item_id))
    attr_id = execute("INSERT INTO attributes (item_id, key, value) VALUES (?, ?, ?)", (item_id, key, value))
    if is_fetch:
        return jsonify({"ok": True, "id": attr_id, "key": key, "value": value})
    flash("Attribute added.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


@app.route("/attributes/<int:attr_id>/delete", methods=["POST"])
@login_required
def delete_attribute(attr_id):
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    attr = query("SELECT * FROM attributes WHERE id = ?", (attr_id,), one=True)
    if not attr:
        if is_fetch:
            return jsonify({"error": "Not found"}), 404
        abort(404)
    execute("DELETE FROM attributes WHERE id = ?", (attr_id,))
    if is_fetch:
        return jsonify({"ok": True})
    flash("Attribute removed.", "success")
    return redirect(url_for("item_detail", item_id=attr["item_id"]))


@app.route("/items/<int:item_id>/add_event", methods=["POST"])
@login_required
def add_event(item_id):
    is_fetch = request.headers.get("X-Requested-With") == "fetch"
    user = current_user()
    if is_fetch:
        data = request.get_json(force=True) or {}
        event_date = (data.get("event_date") or "").strip() or None
        event_type = data.get("event_type") or "noted"
        description = (data.get("description") or "").strip() or None
        cost = data.get("cost") or None
    else:
        event_date = request.form.get("event_date", "").strip() or None
        event_type = request.form.get("event_type", "noted")
        description = request.form.get("description", "").strip() or None
        cost = request.form.get("cost") or None
    event_id = execute("""
        INSERT INTO events (item_id, event_date, event_type, description, cost, created_by)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (item_id, event_date, event_type, description, cost, user["id"] if user else None))
    if is_fetch:
        return jsonify({
            "ok": True, "id": event_id,
            "event_date": event_date, "event_type": event_type,
            "description": description, "cost": float(cost) if cost else None,
        })
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


_API_ITEM_FIELDS = ["name", "category", "location_id", "purchased_date",
                    "installed_date", "manufacturer", "model", "notes"]
_API_ITEM_UPDATE_SQL = {
    f: f"UPDATE items SET {f} = ?, updated_at = datetime('now') WHERE id = ?"
    for f in _API_ITEM_FIELDS
}


@app.route("/api/items/<int:item_id>", methods=["PATCH"])
@api_key_required
def api_update_item(item_id):
    item = query("SELECT * FROM items WHERE id = ?", (item_id,), one=True)
    if not item:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True) or {}
    for f in _API_ITEM_FIELDS:
        if f in data:
            execute(_API_ITEM_UPDATE_SQL[f], (data[f], item_id))
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


@app.route("/check/item-name")
@login_required
def check_item_name():
    name = request.args.get("name", "").strip()
    location_id = request.args.get("location_id") or None
    exclude_id = request.args.get("exclude_id") or None
    if not name:
        return jsonify({"exists": False})
    sql = "SELECT id, name FROM items WHERE LOWER(name) = LOWER(?) AND location_id IS ?"
    args = [name, location_id]
    if exclude_id:
        sql += " AND id != ?"
        args.append(int(exclude_id))
    row = query(sql, args, one=True)
    if row:
        return jsonify({"exists": True, "item_id": row["id"], "item_name": row["name"]})
    return jsonify({"exists": False})


@app.route("/check/location-name")
@login_required
def check_location_name():
    name = request.args.get("name", "").strip()
    parent_id = request.args.get("parent_id") or None
    exclude_id = request.args.get("exclude_id") or None
    if not name:
        return jsonify({"exists": False})
    sql = "SELECT id FROM locations WHERE LOWER(name) = LOWER(?) AND parent_id IS ?"
    args = [name, parent_id]
    if exclude_id:
        sql += " AND id != ?"
        args.append(int(exclude_id))
    row = query(sql, args, one=True)
    return jsonify({"exists": bool(row)})


@app.route("/locations.json")
@login_required
def locations_json():
    rows = query("""
        SELECT l.id, l.name, p.name as parent_name
        FROM locations l
        LEFT JOIN locations p ON p.id = l.parent_id
        ORDER BY p.name NULLS FIRST, l.name
    """)
    return jsonify([dict(r) for r in rows])


@app.route("/items/search.json")
@login_required
def items_search_json():
    category = request.args.get("category", "")
    search = request.args.get("q", "").strip()
    sql = """
        SELECT i.id, i.name, i.category, i.manufacturer, i.purchased_date,
               l.name as location_name
        FROM items i LEFT JOIN locations l ON l.id = i.location_id
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
    return jsonify([dict(r) for r in rows])


@app.route("/quick-add", methods=["POST"])
@login_required
def quick_add():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    location_id = data.get("location_id") or None
    if query("SELECT id FROM items WHERE LOWER(name) = LOWER(?) AND location_id IS ?",
             (name, location_id), one=True):
        return jsonify({"error": f"'{name}' already exists at that location"}), 409
    user = current_user()
    item_id = execute("""
        INSERT INTO items (name, category, location_id, notes, created_by, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
    """, (
        name,
        (data.get("category") or "").strip() or None,
        location_id,
        (data.get("notes") or "").strip() or None,
        user["id"] if user else None,
    ))
    return jsonify({"id": item_id, "name": name, "url": url_for("item_detail", item_id=item_id)}), 201


@app.route("/attributes/<int:attr_id>/edit", methods=["POST"])
@login_required
def edit_attribute(attr_id):
    attr = query("SELECT * FROM attributes WHERE id = ?", (attr_id,), one=True)
    if not attr:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True) or {}
    value = (data.get("value") or "").strip()
    if not value:
        return jsonify({"error": "Value required"}), 400
    execute("UPDATE attributes SET value = ? WHERE id = ?", (value, attr_id))
    return jsonify({"ok": True, "value": value})


@app.route("/search.json")
@login_required
def search_json():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    like = f"%{q}%"
    items = query("""
        SELECT DISTINCT i.id, i.name, i.category, l.name as location_name
        FROM items i
        LEFT JOIN locations l ON l.id = i.location_id
        LEFT JOIN attributes a ON a.item_id = i.id
        WHERE i.name LIKE ? OR i.manufacturer LIKE ? OR i.model LIKE ? OR a.value LIKE ?
        ORDER BY i.name LIMIT 6
    """, [like] * 4)
    locs = query("""
        SELECT l.id, l.name, p.name as parent_name
        FROM locations l LEFT JOIN locations p ON p.id = l.parent_id
        WHERE l.name LIKE ? ORDER BY l.name LIMIT 3
    """, [like])
    results = []
    for item in items:
        parts = [p for p in [item["category"], item["location_name"]] if p]
        results.append({
            "type": "item", "id": item["id"], "name": item["name"],
            "subtitle": " · ".join(parts) if parts else None,
            "url": url_for("item_detail", item_id=item["id"]),
        })
    for loc in locs:
        results.append({
            "type": "location", "id": loc["id"], "name": loc["name"],
            "subtitle": loc["parent_name"],
            "url": url_for("location_detail", loc_id=loc["id"]),
        })
    return jsonify(results)


_EDITABLE_ITEM_FIELDS = {"manufacturer", "model", "purchased_date", "installed_date", "notes", "category"}
_EDIT_FIELD_SQL = {
    f: f"UPDATE items SET {f} = ?, updated_at = datetime('now') WHERE id = ?"
    for f in _EDITABLE_ITEM_FIELDS
}

@app.route("/items/<int:item_id>/edit-field", methods=["POST"])
@login_required
def edit_item_field(item_id):
    if not query("SELECT id FROM items WHERE id = ?", (item_id,), one=True):
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True) or {}
    field = data.get("field", "")
    if field not in _EDIT_FIELD_SQL:
        return jsonify({"error": "Invalid field"}), 400
    value = (data.get("value") or "").strip() or None
    execute(_EDIT_FIELD_SQL[field], (value, item_id))
    return jsonify({"ok": True, "value": value or ""})


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
# QR codes
# ---------------------------------------------------------------------------

@app.route("/items/<int:item_id>/qr.png")
@login_required
def item_qr(item_id):
    import qrcode
    item = query("SELECT id, name FROM items WHERE id = ?", (item_id,), one=True)
    if not item:
        abort(404)

    root = BASE_URL or request.url_root.rstrip("/")
    url  = root + url_for("item_detail", item_id=item_id)

    # Generate QR
    qr = qrcode.QRCode(box_size=6, border=2,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="#2a2520", back_color="#f7f4f0").convert("RGB")
    qr_w, qr_h = qr_img.size

    # Load a system font; fall back to PIL default
    font_lg = font_sm = None
    for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
               "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
               "/usr/share/fonts/truetype/freefont/FreeSans.ttf"]:
        if os.path.exists(fp):
            font_lg = ImageFont.truetype(fp, 14)
            font_sm = ImageFont.truetype(fp, 11)
            break
    if not font_lg:
        font_lg = font_sm = ImageFont.load_default()

    BG    = (247, 244, 240)   # --bg
    INK   = ( 42,  37,  32)   # --text
    MUTED = (138, 128, 120)   # --muted
    GAP   = 7
    PAD_B = 12

    name_text = item["name"] if len(item["name"]) <= 28 else item["name"][:27] + "…"
    sub_text  = QR_LABEL

    # Measure text on a throwaway canvas
    _tmp  = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    nb    = _tmp.textbbox((0, 0), name_text, font=font_lg)
    sb    = _tmp.textbbox((0, 0), sub_text,  font=font_sm)
    name_w, name_h = nb[2] - nb[0], nb[3] - nb[1]
    sub_w,  sub_h  = sb[2] - sb[0], sb[3] - sb[1]

    total_w = max(qr_w, name_w + 16, sub_w + 16)
    total_h = qr_h + GAP + name_h + GAP + sub_h + PAD_B

    # Composite
    out  = Image.new("RGB", (total_w, total_h), BG)
    out.paste(qr_img, ((total_w - qr_w) // 2, 0))
    draw = ImageDraw.Draw(out)
    draw.text(((total_w - name_w) // 2 - nb[0], qr_h + GAP - nb[1]),
              name_text, font=font_lg, fill=INK)
    draw.text(((total_w - sub_w)  // 2 - sb[0], qr_h + GAP + name_h + GAP - sb[1]),
              sub_text,  font=font_sm, fill=MUTED)

    buf = BytesIO()
    out.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png",
                     download_name=f"item-{item_id}-qr.png")


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
    raw_ext = os.path.splitext(f.filename)[1].lower()
    if raw_ext not in ALLOWED_EXTENSIONS:
        flash(f"File type not allowed. Accepted: jpg, png, heic, pdf.", "error")
        return redirect(url_for("item_detail", item_id=item_id))
    # Re-derive ext from our allowlist to break taint chain
    ext = next(e for e in ALLOWED_EXTENSIONS if e == raw_ext)

    unique = uuid.uuid4().hex
    upload_dir_real = os.path.realpath(UPLOAD_DIR)
    is_image = ext in THUMB_EXTS

    if is_image:
        # Save original temporarily, convert to JPEG
        tmp_name = f"tmp_{unique}{ext}"
        tmp_path = os.path.realpath(os.path.join(UPLOAD_DIR, tmp_name))
        if not tmp_path.startswith(upload_dir_real + os.sep):
            abort(400)
        stored_name = f"{unique}.jpg"
        stored_path = os.path.realpath(os.path.join(UPLOAD_DIR, stored_name))
        if not stored_path.startswith(upload_dir_real + os.sep):
            abort(400)
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
        stored_path = os.path.realpath(os.path.join(UPLOAD_DIR, stored_name))
        if not stored_path.startswith(upload_dir_real + os.sep):
            abort(400)
        f.save(stored_path)
        mime = mimetypes.guess_type(stored_name)[0] or "application/octet-stream"

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
    port = int(os.environ.get("HOUSE_PORT", 5000))
    app.run(host="127.0.0.1", port=port, debug=False)

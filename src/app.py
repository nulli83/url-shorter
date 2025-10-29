import os
import re
import sqlite3
import uuid
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, request, redirect, jsonify, send_file, render_template, abort

DB_PATH = os.environ.get("DB_PATH", "data.sqlite3")
DEFAULT_BASE_URL = os.environ.get("BASE_URL", None)

RESERVED_ALIASES = {"api", "stats", "static", "favicon.ico", ""}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_exists(conn, name: str) -> bool:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def init_db():
    conn = get_db()
    try:
        if not table_exists(conn, "urls"):
            conn.execute(
                """
                CREATE TABLE urls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT UNIQUE,
                    long_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    hits INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute("CREATE INDEX idx_urls_code ON urls(code);")
            conn.execute("CREATE INDEX idx_urls_long ON urls(long_url);")
        conn.commit()
    finally:
        conn.close()


def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def base_url():
    if DEFAULT_BASE_URL:
        return str(DEFAULT_BASE_URL).rstrip("/")
    return request.url_root.rstrip("/")


def generate_unique_code(cur, length: int = 8) -> str:
    for _ in range(20):
        c = uuid.uuid4().hex[:length]
        cur.execute("SELECT 1 FROM urls WHERE code=?", (c,))
        if not cur.fetchone():
            return c
    return uuid.uuid4().hex


def handle_home():
    return render_template("index.html")


def handle_shorten():
    data = request.get_json(silent=True) or request.form
    long_url = (data.get("url") or "").strip()
    alias = (data.get("alias") or "").strip() or None

    if not is_valid_url(long_url):
        return jsonify({"error": "Invalid URL. Only http(s) URLs are allowed."}), 400

    conn = get_db()
    try:
        cur = conn.cursor()

        if alias:
            allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
            if alias.lower() in RESERVED_ALIASES or not all(ch in allowed for ch in alias):
                return jsonify({"error": "Alias may only contain A-Za-z0-9-_ and not be reserved"}), 400
            cur.execute("SELECT 1 FROM urls WHERE code=?", (alias,))
            if cur.fetchone():
                return jsonify({"error": "Alias already taken."}), 409
            code = alias
        else:
            code = generate_unique_code(cur)

        cur.execute(
            "INSERT INTO urls (code, long_url, created_at, hits) VALUES (?, ?, ?, 0)",
            (code, long_url, datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()
    finally:
        conn.close()

    short = f"{base_url()}/{code}"
    return jsonify({"code": code, "short_url": short, "long_url": long_url}), 201


def handle_resolve(code: str):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, long_url, hits FROM urls WHERE code=?", (code,))
        row = cur.fetchone()
        if not row:
            abort(404)
        new_hits = int(row["hits"] or 0) + 1
        cur.execute("UPDATE urls SET hits=? WHERE id=?", (new_hits, row["id"]))
        conn.commit()
        target = row["long_url"]
    finally:
        conn.close()
    return redirect(target, code=302)


def handle_info(code: str):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT code, long_url, created_at, hits FROM urls WHERE code=?",
            (code,),
        )
        row = cur.fetchone()
        if not row:
            return jsonify({"error": "Not found"}), 404
        return jsonify({
            "code": row["code"],
            "long_url": row["long_url"],
            "created_at": row["created_at"],
            "hits": row["hits"],
            "short_url": f"{base_url()}/{row['code']}",
        })
    finally:
        conn.close()


def handle_qr(code: str):
    import io
    import qrcode

    short_url = f"{base_url()}/{code}"
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM urls WHERE code=?", (code,))
        if not cur.fetchone():
            return jsonify({"error": "Not found"}), 404
    finally:
        conn.close()

    img = qrcode.make(short_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def handle_stats_page(code: str):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT code, long_url, created_at, hits FROM urls WHERE code=?",
            (code,),
        )
        row = cur.fetchone()
        if not row:
            abort(404)
        return render_template(
            "stats.html",
            code=row["code"],
            long_url=row["long_url"],
            created_at=row["created_at"],
            hits=row["hits"],
            base_url=base_url(),
        )
    finally:
        conn.close()


class Router:
    def __init__(self):
        self.routes = []

    def add(self, method: str, pattern: str, handler):
        self.routes.append((method.upper(), re.compile(f"^{pattern}$"), handler))

    def dispatch(self, method: str, path: str):
        for m, rx, handler in self.routes:
            if m == method.upper():
                match = rx.match(path)
                if match:
                    return handler, match.groupdict()
        return None, None


router = Router()
router.add("GET", r"", handle_home)
router.add("POST", r"shorten", handle_shorten)
router.add("GET", r"api/info/(?P<code>[^/]+)", handle_info)
router.add("GET", r"api/qr/(?P<code>[^/]+)", handle_qr)
router.add("GET", r"stats/(?P<code>[^/]+)", handle_stats_page)
router.add("GET", r"(?P<code>[^/]+)", handle_resolve)


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def _catch_all(path):
    handler, params = router.dispatch(request.method, path)
    if not handler:
        abort(404)
    return handler(**(params or {}))


if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=False, use_reloader=False)

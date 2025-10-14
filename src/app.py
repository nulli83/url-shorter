import os
import sqlite3
import string
import time
import socket
from datetime import datetime
from urllib.parse import urlparse

from flask import Flask, request, redirect, jsonify, send_file, render_template, abort

# -----------------------------
# Config
# -----------------------------
DB_PATH = os.environ.get("DB_PATH", "data.sqlite3")
DEFAULT_BASE_URL = os.environ.get("BASE_URL", None)  # byggs från request om None
RATE_LIMIT_WINDOW_SEC = 60
RATE_LIMIT_MAX_REQ = 30  # per IP per fönster

RESERVED_ALIASES = {"api", "stats", "static", "favicon.ico", ""}

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# Enkel in-memory rate limiting (best effort)
_rate_bucket = {}

# -----------------------------
# Helpers
# -----------------------------
ALPHABET = string.digits + string.ascii_letters  # 0-9A-Za-z (62)


def base62_encode(n: int) -> str:
    if n == 0:
        return ALPHABET[0]
    s = []
    b = len(ALPHABET)
    while n > 0:
        n, r = divmod(n, b)
        s.append(ALPHABET[r])
    return "".join(reversed(s))


def is_valid_url(url: str) -> bool:
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            long_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            hits INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_urls_code ON urls(code);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_urls_long ON urls(long_url);")
    conn.commit()
    conn.close()


def rate_limited(ip: str) -> bool:
    now = int(time.time())
    window = now // RATE_LIMIT_WINDOW_SEC
    key = (ip, window)
    count = _rate_bucket.get(key, 0)
    if count >= RATE_LIMIT_MAX_REQ:
        return True
    _rate_bucket[key] = count + 1
    # rensa gamla buckets ibland (best effort)
    if len(_rate_bucket) > 5000:
        old_windows = {k for k in _rate_bucket if k[1] < window}
        for k in old_windows:
            _rate_bucket.pop(k, None)
    return False


def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else request.remote_addr


def base_url():
    if DEFAULT_BASE_URL:
        return DEFAULT_BASE_URL.rstrip("/")
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{scheme}://{host}".rstrip("/")


def pick_free_port(host: str, candidates=None) -> int:
    candidates = candidates or []
    for p in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    # välj OS-tilldelad port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def home():
    return render_template("index.html")


@app.post("/shorten")
def shorten():
    ip = client_ip()
    if rate_limited(ip):
        return jsonify({"error": "Rate limit exceeded. Try again later."}), 429

    data = request.get_json(silent=True) or request.form
    long_url = (data.get("url") or "").strip()
    alias = (data.get("alias") or "").strip() or None

    if not is_valid_url(long_url):
        return jsonify({"error": "Invalid URL. Only http(s) URLs are allowed."}), 400

    conn = get_db()
    cur = conn.cursor()

    # Custom alias?
    if alias:
        # basic allowlist: [0-9A-Za-z-_]
        allowed = set(string.ascii_letters + string.digits + "-_")
        if alias.lower() in RESERVED_ALIASES or not all(ch in allowed for ch in alias):
            conn.close()
            return jsonify({"error": "Alias may only contain A-Za-z0-9-_ and not be reserved"}), 400
        # check unique
        cur.execute("SELECT 1 FROM urls WHERE code=?", (alias,))
        if cur.fetchone():
            conn.close()
            return jsonify({"error": "Alias already taken."}), 409
        code = alias
        cur.execute(
            "INSERT INTO urls (code, long_url, created_at, hits) VALUES (?, ?, ?, 0)",
            (code, long_url, datetime.utcnow().isoformat() + "Z"),
        )
        conn.commit()
    else:
        # generate from ID using base62
        cur.execute(
            "INSERT INTO urls (long_url, created_at, hits) VALUES (?, ?, 0)",
            (long_url, datetime.utcnow().isoformat() + "Z"),
        )
        new_id = cur.lastrowid
        code = base62_encode(new_id)
        cur.execute("UPDATE urls SET code=? WHERE id=?", (code, new_id))
        conn.commit()

    conn.close()
    short = f"{base_url()}/{code}"
    return jsonify({"code": code, "short_url": short, "long_url": long_url}), 201


@app.get("/<code>")
def resolve(code: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, long_url FROM urls WHERE code=?", (code,))
    row = cur.fetchone()
    if not row:
        conn.close()
        abort(404)
    # Atomisk increment
    cur.execute("UPDATE urls SET hits = hits + 1 WHERE id=?", (row[0],))
    conn.commit()
    conn.close()
    return redirect(row[1], code=302)


@app.get("/api/info/<code>")
def info(code: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, long_url, created_at, hits FROM urls WHERE code=?",
        (code,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "code": row[0],
        "long_url": row[1],
        "created_at": row[2],
        "hits": row[3],
        "short_url": f"{base_url()}/{row[0]}",
    })


@app.get("/api/qr/<code>")
def qr(code: str):
    # lazy import to avoid heavy deps on cold start
    import io
    import qrcode

    short_url = f"{base_url()}/{code}"
    # verify exists
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM urls WHERE code=?", (code,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"error": "Not found"}), 404
    conn.close()

    img = qrcode.make(short_url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    resp = send_file(buf, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/stats/<code>")
def stats_page(code: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT code, long_url, created_at, hits FROM urls WHERE code=?",
        (code,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        abort(404)
    return render_template(
        "stats.html",
        code=row[0], long_url=row[1], created_at=row[2], hits=row[3], base_url=base_url()
    )


# -----------------------------
# Entrypoint (robust på Windows-portar)
# -----------------------------
if __name__ == "__main__":
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    init_db()

    host = os.environ.get("HOST", "127.0.0.1")
    # Prova specifik port först (om satt), annars vår kandidatlista
    env_port = os.environ.get("PORT")
    candidates = []
    if env_port and str(env_port).isdigit():
        candidates.append(int(env_port))
    candidates += [8000, 8080, 8888, 5001]

    try:
        port = pick_free_port(host, candidates)
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except OSError:
        # Fallback till 0.0.0.0 och ny port (t.ex. när bindning till loopback är blockerad)
        host = "0.0.0.0"
        port = pick_free_port(host, candidates)
        app.run(host=host, port=port, debug=False, use_reloader=False)

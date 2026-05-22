import json
import os
import uuid
import urllib.request
import urllib.parse
from datetime import date, datetime, timezone
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, abort, send_from_directory
)
from werkzeug.utils import secure_filename
from db import db, init_db
from auth import login_required, get_current_user, register_user, login_user

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB upload limit

# On Render, DB_PATH is /data/bookshelf.db — store uploads alongside it
_data_dir = os.path.dirname(os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "bookshelf.db")))
UPLOAD_DIR = os.path.join(_data_dir, "videos")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Serve uploaded videos (works locally; on Render the disk is at /data)
@app.route("/uploads/videos/<path:filename>")
def serve_video(filename):
    return send_from_directory(UPLOAD_DIR, filename)
ALLOWED_VIDEO_EXT = {"mp4", "mov", "webm", "m4v", "mkv"}

# ---------------------------------------------------------------------------
# Template filters & globals
# ---------------------------------------------------------------------------

@app.template_filter("fromjson")
def fromjson_filter(s):
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}

@app.template_filter("format_date")
def format_date_filter(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%b %-d, %Y")
    except Exception:
        return ""

@app.template_filter("format_number")
def format_number_filter(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)

@app.template_filter("time_ago")
def time_ago_filter(ts):
    if not ts:
        return ""
    try:
        diff = int(datetime.now(timezone.utc).timestamp()) - int(ts)
        if diff < 60:
            return "just now"
        if diff < 3600:
            m = diff // 60
            return f"{m}m ago"
        if diff < 86400:
            h = diff // 3600
            return f"{h}h ago"
        d = diff // 86400
        return f"{d}d ago"
    except Exception:
        return ""

@app.context_processor
def inject_globals():
    return {"now": datetime.now, "session": session}

# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("auth_login"))


@app.route("/login", methods=["GET", "POST"])
def auth_login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user, error = login_user(username, password)
        if error:
            flash(error, "error")
        else:
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["display_name"] = user["display_name"]
            return redirect(url_for("dashboard"))
    return render_template("auth/login.html")


@app.route("/register", methods=["GET", "POST"])
def auth_register():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()
        if len(username) < 3:
            flash("Username must be at least 3 characters", "error")
        elif len(password) < 6:
            flash("Password must be at least 6 characters", "error")
        else:
            user, error = register_user(username, password, display_name)
            if error:
                flash(error, "error")
            else:
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["display_name"] = user["display_name"]
                return redirect(url_for("dashboard"))
    return render_template("auth/register.html")


@app.route("/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("auth_login"))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session["user_id"]
    today = date.today().isoformat()
    with db() as conn:
        # Stats
        counts = conn.execute("""
            SELECT status, COUNT(*) as cnt FROM books
            WHERE user_id = ? GROUP BY status
        """, (user_id,)).fetchall()
        stats = {r["status"]: r["cnt"] for r in counts}

        total_pages = conn.execute("""
            SELECT COALESCE(SUM(pages_read), 0) as total FROM reading_log WHERE user_id = ?
        """, (user_id,)).fetchone()["total"]

        # Currently reading
        reading = conn.execute("""
            SELECT id, title, author, cover_url, current_page, total_pages, genre
            FROM books WHERE user_id = ? AND status = 'reading'
            ORDER BY updated_at DESC LIMIT 4
        """, (user_id,)).fetchall()

        # Streak calculation
        streak = _calculate_streak(conn, user_id, today)

        # Recently finished
        recently_finished = conn.execute("""
            SELECT id, title, author, cover_url, rating, finished_at
            FROM books WHERE user_id = ? AND status = 'read'
            ORDER BY finished_at DESC LIMIT 4
        """, (user_id,)).fetchall()

        # Genre breakdown
        genres = conn.execute("""
            SELECT genre, COUNT(*) as cnt FROM books
            WHERE user_id = ? AND genre IS NOT NULL AND genre != ''
            GROUP BY genre ORDER BY cnt DESC LIMIT 6
        """, (user_id,)).fetchall()

        # Friends feed (last 10 events from people you follow)
        feed = conn.execute("""
            SELECT fe.event_type, fe.payload, fe.created_at,
                   u.username, u.display_name,
                   b.title, b.author, b.cover_url, b.id as book_id
            FROM feed_events fe
            JOIN friendships f ON f.followee_id = fe.user_id AND f.follower_id = ?
            JOIN users u ON u.id = fe.user_id
            JOIN books b ON b.id = fe.book_id
            ORDER BY fe.created_at DESC LIMIT 10
        """, (user_id,)).fetchall()

    return render_template("dashboard.html",
        stats=stats,
        total_pages=total_pages,
        reading=reading,
        streak=streak,
        recently_finished=recently_finished,
        genres=list(genres),
        feed=list(feed),
        user=get_current_user()
    )


def _calculate_streak(conn, user_id, today):
    logs = conn.execute("""
        SELECT DISTINCT log_date FROM reading_log
        WHERE user_id = ? ORDER BY log_date DESC
    """, (user_id,)).fetchall()
    if not logs:
        return 0
    dates = [r["log_date"] for r in logs]
    from datetime import timedelta
    current = date.today()
    streak = 0
    for d in dates:
        log_d = date.fromisoformat(d)
        if log_d == current or log_d == current - timedelta(days=streak):
            streak += 1
            current = log_d - timedelta(days=1) if log_d != date.today() else current - timedelta(days=1)
        else:
            break
    return streak


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------

GENRES = [
    "Fiction", "Non-Fiction", "Fantasy", "Science Fiction", "Mystery",
    "Thriller", "Romance", "Historical Fiction", "Biography", "Self-Help",
    "Science", "Philosophy", "Poetry", "Horror", "Adventure", "Graphic Novel",
    "Children's", "Young Adult", "Classic", "Other"
]


@app.route("/books")
@login_required
def books_index():
    user_id = session["user_id"]
    status_filter = request.args.get("status", "all")
    sort = request.args.get("sort", "updated")

    sort_map = {
        "updated": "b.updated_at DESC",
        "title": "b.title ASC",
        "author": "b.author ASC",
        "added": "b.created_at DESC",
    }
    order = sort_map.get(sort, "b.updated_at DESC")

    where = "WHERE b.user_id = ?"
    params = [user_id]
    if status_filter != "all":
        where += " AND b.status = ?"
        params.append(status_filter)

    with db() as conn:
        books = conn.execute(f"""
            SELECT id, title, author, cover_url, status, current_page,
                   total_pages, genre, rating, updated_at
            FROM books b {where} ORDER BY {order}
        """, params).fetchall()

        counts = conn.execute("""
            SELECT status, COUNT(*) as cnt FROM books
            WHERE user_id = ? GROUP BY status
        """, (user_id,)).fetchall()

    status_counts = {r["status"]: r["cnt"] for r in counts}
    status_counts["all"] = sum(status_counts.values())

    return render_template("books/index.html",
        books=books,
        status_filter=status_filter,
        sort=sort,
        status_counts=status_counts,
        user=get_current_user()
    )


@app.route("/books/add", methods=["GET", "POST"])
@login_required
def books_add():
    user_id = session["user_id"]
    prefill = {
        "title": request.args.get("title", ""),
        "author": request.args.get("author", ""),
        "cover_url": request.args.get("cover_url", ""),
        "ol_key": request.args.get("ol_key", ""),
        "total_pages": request.args.get("total_pages", ""),
        "genre": request.args.get("genre", ""),
    }

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        cover_url = request.form.get("cover_url", "").strip() or None
        ol_key = request.form.get("ol_key", "").strip() or None
        total_pages = request.form.get("total_pages", "").strip()
        genre = request.form.get("genre", "").strip() or None
        status = request.form.get("status", "want_to_read")

        if not title or not author:
            flash("Title and author are required", "error")
        else:
            total_pages = int(total_pages) if total_pages.isdigit() else None
            started_at = int(datetime.now(timezone.utc).timestamp()) if status == "reading" else None
            finished_at = int(datetime.now(timezone.utc).timestamp()) if status == "read" else None

            with db() as conn:
                cur = conn.execute("""
                    INSERT INTO books (user_id, title, author, cover_url, ol_key,
                        total_pages, genre, status, started_at, finished_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (user_id, title, author, cover_url, ol_key,
                      total_pages, genre, status, started_at, finished_at))
                book_id = cur.lastrowid
                conn.execute("""
                    INSERT INTO feed_events (user_id, book_id, event_type)
                    VALUES (?, ?, 'added')
                """, (user_id, book_id))

            flash(f'"{title}" added to your shelf!', "success")
            return redirect(url_for("books_detail", book_id=book_id))

    return render_template("books/add.html",
        prefill=prefill, genres=GENRES, user=get_current_user())


@app.route("/books/<int:book_id>")
@login_required
def books_detail(book_id):
    user_id = session["user_id"]
    with db() as conn:
        book = conn.execute("""
            SELECT * FROM books WHERE id = ? AND user_id = ?
        """, (book_id, user_id)).fetchone()
        if not book:
            abort(404)

        # Reading log for this book (last 30 days)
        log = conn.execute("""
            SELECT log_date, pages_read FROM reading_log
            WHERE user_id = ? AND book_id = ?
            ORDER BY log_date DESC LIMIT 30
        """, (user_id, book_id)).fetchall()

    return render_template("books/detail.html",
        book=book, log=list(log), genres=GENRES, user=get_current_user())


@app.route("/books/<int:book_id>/edit", methods=["POST"])
@login_required
def books_edit(book_id):
    user_id = session["user_id"]
    with db() as conn:
        book = conn.execute(
            "SELECT id FROM books WHERE id = ? AND user_id = ?", (book_id, user_id)
        ).fetchone()
        if not book:
            abort(404)

        title = request.form.get("title", "").strip()
        author = request.form.get("author", "").strip()
        cover_url = request.form.get("cover_url", "").strip() or None
        genre = request.form.get("genre", "").strip() or None
        total_pages_raw = request.form.get("total_pages", "").strip()
        total_pages = int(total_pages_raw) if total_pages_raw.isdigit() else None
        notes = request.form.get("notes", "").strip() or None
        rating_raw = request.form.get("rating", "").strip()
        rating = int(rating_raw) if rating_raw.isdigit() and 1 <= int(rating_raw) <= 5 else None

        if not title or not author:
            flash("Title and author are required", "error")
            return redirect(url_for("books_detail", book_id=book_id))

        conn.execute("""
            UPDATE books SET title=?, author=?, cover_url=?, genre=?,
                total_pages=?, notes=?, rating=?, updated_at=unixepoch()
            WHERE id = ? AND user_id = ?
        """, (title, author, cover_url, genre, total_pages, notes, rating, book_id, user_id))

        if rating:
            conn.execute("""
                INSERT INTO feed_events (user_id, book_id, event_type, payload)
                VALUES (?, ?, 'rated', ?)
            """, (user_id, book_id, json.dumps({"rating": rating})))

    flash("Book updated", "success")
    return redirect(url_for("books_detail", book_id=book_id))


@app.route("/books/<int:book_id>/delete", methods=["POST"])
@login_required
def books_delete(book_id):
    user_id = session["user_id"]
    with db() as conn:
        conn.execute("DELETE FROM books WHERE id = ? AND user_id = ?", (book_id, user_id))
    flash("Book removed from shelf", "success")
    return redirect(url_for("books_index"))


@app.route("/books/<int:book_id>/progress", methods=["POST"])
@login_required
def books_progress(book_id):
    user_id = session["user_id"]
    data = request.get_json() or {}
    current_page = data.get("current_page")
    new_status = data.get("status")

    if current_page is None and new_status is None:
        return jsonify({"error": "current_page or status required"}), 400

    today = date.today().isoformat()
    now = int(datetime.now(timezone.utc).timestamp())

    with db() as conn:
        book = conn.execute(
            "SELECT * FROM books WHERE id = ? AND user_id = ?", (book_id, user_id)
        ).fetchone()
        if not book:
            return jsonify({"error": "Not found"}), 404

        old_page = book["current_page"]
        total_pages = book["total_pages"] or 0
        status = new_status or book["status"]

        if current_page is not None:
            current_page = max(0, int(current_page))
            if total_pages and current_page >= total_pages:
                current_page = total_pages
                status = "read"

        # Auto-set timestamps
        started_at = book["started_at"]
        finished_at = book["finished_at"]
        if status == "reading" and not started_at:
            started_at = now
        if status == "read" and not finished_at:
            finished_at = now
            current_page = total_pages if total_pages else (current_page or old_page)

        update_page = current_page if current_page is not None else old_page

        conn.execute("""
            UPDATE books SET current_page=?, status=?, started_at=?,
                finished_at=?, updated_at=? WHERE id=?
        """, (update_page, status, started_at, finished_at, now, book_id))

        # Log pages read today
        pages_today = max(0, update_page - old_page)
        if pages_today > 0:
            conn.execute("""
                INSERT INTO reading_log (user_id, book_id, log_date, pages_read)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, book_id, log_date)
                DO UPDATE SET pages_read = pages_read + excluded.pages_read
            """, (user_id, book_id, today, pages_today))

        # Feed event
        if status == "read" and book["status"] != "read":
            event_type = "finished"
            payload = json.dumps({"page": update_page})
        elif status == "reading" and book["status"] != "reading":
            event_type = "started"
            payload = None
        else:
            event_type = "progress"
            pct = int(update_page / total_pages * 100) if total_pages else 0
            payload = json.dumps({"page": update_page, "pct": pct})

        conn.execute("""
            INSERT INTO feed_events (user_id, book_id, event_type, payload)
            VALUES (?, ?, ?, ?)
        """, (user_id, book_id, event_type, payload))

        pct = int(update_page / total_pages * 100) if total_pages else 0

    return jsonify({"current_page": update_page, "status": status, "pct": pct})


# ---------------------------------------------------------------------------
# Friends
# ---------------------------------------------------------------------------

@app.route("/friends")
@login_required
def friends_index():
    user_id = session["user_id"]
    with db() as conn:
        # People I follow
        following = conn.execute("""
            SELECT u.id, u.username, u.display_name,
                   b.title as current_book, b.author as current_author,
                   b.cover_url, b.current_page, b.total_pages, b.id as book_id
            FROM friendships f
            JOIN users u ON u.id = f.followee_id
            LEFT JOIN books b ON b.user_id = u.id AND b.status = 'reading'
                AND b.updated_at = (
                    SELECT MAX(b2.updated_at) FROM books b2
                    WHERE b2.user_id = u.id AND b2.status = 'reading'
                )
            WHERE f.follower_id = ?
            ORDER BY u.display_name
        """, (user_id,)).fetchall()

        # My followers
        followers = conn.execute("""
            SELECT u.id, u.username, u.display_name
            FROM friendships f
            JOIN users u ON u.id = f.follower_id
            WHERE f.followee_id = ?
        """, (user_id,)).fetchall()

        following_ids = {r["id"] for r in following}

        # Activity feed from people I follow
        feed = conn.execute("""
            SELECT fe.event_type, fe.payload, fe.created_at,
                   u.username, u.display_name,
                   b.title, b.author, b.cover_url, b.id as book_id
            FROM feed_events fe
            JOIN friendships f ON f.followee_id = fe.user_id AND f.follower_id = ?
            JOIN users u ON u.id = fe.user_id
            JOIN books b ON b.id = fe.book_id
            ORDER BY fe.created_at DESC LIMIT 30
        """, (user_id,)).fetchall()

    return render_template("friends/index.html",
        following=following,
        followers=list(followers),
        following_ids=following_ids,
        feed=feed,
        user=get_current_user()
    )


@app.route("/friends/add", methods=["POST"])
@login_required
def friends_add():
    user_id = session["user_id"]
    username = request.form.get("username", "").strip().lower()
    with db() as conn:
        target = conn.execute(
            "SELECT id, display_name FROM users WHERE username = ?", (username,)
        ).fetchone()
        if not target:
            flash(f'User "{username}" not found', "error")
        elif target["id"] == user_id:
            flash("You can't follow yourself", "error")
        else:
            try:
                conn.execute(
                    "INSERT INTO friendships (follower_id, followee_id) VALUES (?, ?)",
                    (user_id, target["id"])
                )
                flash(f'Now following {target["display_name"]}!', "success")
            except Exception:
                flash("You're already following this person", "error")
    return redirect(url_for("friends_index"))


@app.route("/friends/remove/<username>", methods=["POST"])
@login_required
def friends_remove(username):
    user_id = session["user_id"]
    with db() as conn:
        target = conn.execute(
            "SELECT id FROM users WHERE username = ?", (username.lower(),)
        ).fetchone()
        if target:
            conn.execute(
                "DELETE FROM friendships WHERE follower_id = ? AND followee_id = ?",
                (user_id, target["id"])
            )
    flash("Unfollowed", "success")
    return redirect(url_for("friends_index"))


@app.route("/friends/<username>")
@login_required
def friends_profile(username):
    user_id = session["user_id"]
    with db() as conn:
        profile_user = conn.execute(
            "SELECT id, username, display_name FROM users WHERE username = ?",
            (username.lower(),)
        ).fetchone()
        if not profile_user:
            abort(404)

        is_following = conn.execute("""
            SELECT 1 FROM friendships WHERE follower_id = ? AND followee_id = ?
        """, (user_id, profile_user["id"])).fetchone() is not None

        reading = conn.execute("""
            SELECT id, title, author, cover_url, current_page, total_pages, genre
            FROM books WHERE user_id = ? AND status = 'reading'
            ORDER BY updated_at DESC
        """, (profile_user["id"],)).fetchall()

        finished = conn.execute("""
            SELECT id, title, author, cover_url, rating, finished_at
            FROM books WHERE user_id = ? AND status = 'read'
            ORDER BY finished_at DESC LIMIT 12
        """, (profile_user["id"],)).fetchall()

        want_to_read = conn.execute("""
            SELECT id, title, author, cover_url
            FROM books WHERE user_id = ? AND status = 'want_to_read'
            ORDER BY created_at DESC LIMIT 8
        """, (profile_user["id"],)).fetchall()

        stats = conn.execute("""
            SELECT status, COUNT(*) as cnt FROM books
            WHERE user_id = ? GROUP BY status
        """, (profile_user["id"],)).fetchall()

    return render_template("friends/profile.html",
        profile=profile_user,
        is_following=is_following,
        reading=reading,
        finished=finished,
        want_to_read=want_to_read,
        stats={r["status"]: r["cnt"] for r in stats},
        user=get_current_user()
    )


# ---------------------------------------------------------------------------
# API: Open Library search
# ---------------------------------------------------------------------------

@app.route("/api/search")
@login_required
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        encoded = urllib.parse.quote(q)
        url = f"https://openlibrary.org/search.json?q={encoded}&limit=10&fields=key,title,author_name,cover_i,number_of_pages_median,subject"
        req = urllib.request.Request(url, headers={"User-Agent": "Bookshelf/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        results = []
        for doc in data.get("docs", []):
            cover_id = doc.get("cover_i")
            results.append({
                "ol_key": doc.get("key", ""),
                "title": doc.get("title", ""),
                "author": (doc.get("author_name") or ["Unknown"])[0],
                "cover_url": f"https://covers.openlibrary.org/b/id/{cover_id}-M.jpg" if cover_id else None,
                "total_pages": doc.get("number_of_pages_median"),
                "genre": (doc.get("subject") or [None])[0],
            })
        return jsonify(results)
    except Exception:
        return jsonify([])


# ---------------------------------------------------------------------------
# API: Recommendations
# ---------------------------------------------------------------------------

@app.route("/api/recommendations")
@login_required
def api_recommendations():
    user_id = session["user_id"]
    with db() as conn:
        top_genres = conn.execute("""
            SELECT genre, COUNT(*) as cnt FROM books
            WHERE user_id = ? AND genre IS NOT NULL AND genre != ''
            GROUP BY genre ORDER BY cnt DESC LIMIT 2
        """, (user_id,)).fetchall()

        existing_keys = {r["ol_key"] for r in conn.execute(
            "SELECT ol_key FROM books WHERE user_id = ? AND ol_key IS NOT NULL", (user_id,)
        ).fetchall()}

    if not top_genres:
        return jsonify([])

    results = []
    seen_keys = set(existing_keys)

    for genre_row in top_genres:
        genre = genre_row["genre"]
        try:
            encoded = urllib.parse.quote(genre.lower().replace(" ", "_"))
            url = f"https://openlibrary.org/subjects/{encoded}.json?limit=10"
            req = urllib.request.Request(url, headers={"User-Agent": "Bookshelf/1.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            for work in data.get("works", []):
                key = work.get("key", "")
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                cover = work.get("cover_id")
                authors = work.get("authors", [])
                results.append({
                    "ol_key": key,
                    "title": work.get("title", ""),
                    "author": authors[0]["name"] if authors else "Unknown",
                    "cover_url": f"https://covers.openlibrary.org/b/id/{cover}-M.jpg" if cover else None,
                    "genre": genre,
                })
                if len(results) >= 8:
                    break
        except Exception:
            continue
        if len(results) >= 8:
            break

    return jsonify(results)


# ---------------------------------------------------------------------------
# Reels
# ---------------------------------------------------------------------------

# Page-range bucket size for book clubs (every N pages = one room)
CLUB_BUCKET = 50


def _reel_feed(conn, user_id, offset=0, limit=10):
    """
    Scoring algorithm:
      base = 1.0
      +3.0  if reel's genre matches a genre from user's shelf
      +5.0  if reel's book matches a title on user's shelf
      +0.5  per like on the reel (capped at 5)
      -100  if user already reacted (exclude)
      recency decay: score * (1 / (1 + days_old * 0.1))
    Returns rows ordered by score DESC.
    """
    now_ts = int(datetime.now(timezone.utc).timestamp())

    user_genres = {r["genre"] for r in conn.execute(
        "SELECT DISTINCT genre FROM books WHERE user_id=? AND genre IS NOT NULL", (user_id,)
    ).fetchall()}
    user_titles = {r["title"].lower() for r in conn.execute(
        "SELECT title FROM books WHERE user_id=?", (user_id,)
    ).fetchall()}
    reacted_ids = {r["reel_id"] for r in conn.execute(
        "SELECT reel_id FROM reel_reactions WHERE user_id=?", (user_id,)
    ).fetchall()}

    # Genres boosted by what user has liked
    liked_genres = {r["book_genre"] for r in conn.execute("""
        SELECT r.book_genre FROM reels r
        JOIN reel_reactions rr ON rr.reel_id = r.id
        WHERE rr.user_id = ? AND rr.reaction = 'like' AND r.book_genre IS NOT NULL
    """, (user_id,)).fetchall()}
    boosted_genres = user_genres | liked_genres

    rows = conn.execute("""
        SELECT r.id, r.user_id, r.book_title, r.book_author, r.book_cover_url,
               r.book_genre, r.content, r.like_count, r.created_at,
               u.username, u.display_name,
               rr.reaction as my_reaction
        FROM reels r
        JOIN users u ON u.id = r.user_id
        LEFT JOIN reel_reactions rr ON rr.reel_id = r.id AND rr.user_id = ?
        WHERE r.user_id != ?
        ORDER BY r.created_at DESC
        LIMIT 200
    """, (user_id, user_id)).fetchall()

    scored = []
    for row in rows:
        if row["id"] in reacted_ids:
            continue
        score = 1.0
        if row["book_genre"] and row["book_genre"] in boosted_genres:
            score += 3.0
        if row["book_title"] and row["book_title"].lower() in user_titles:
            score += 5.0
        score += min(row["like_count"] * 0.5, 5.0)
        days_old = (now_ts - (row["created_at"] or now_ts)) / 86400
        score *= 1.0 / (1.0 + days_old * 0.1)
        scored.append((score, dict(row)))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[offset: offset + limit]]


@app.route("/reels")
@login_required
def reels_index():
    user_id = session["user_id"]
    with db() as conn:
        reels = _reel_feed(conn, user_id, offset=0, limit=12)
        # Books from shelf for quick "create reel" dropdown
        my_books = conn.execute("""
            SELECT id, title, author, cover_url, genre FROM books
            WHERE user_id = ? ORDER BY updated_at DESC LIMIT 30
        """, (user_id,)).fetchall()
        # Saved reels
        saved_ids = {r["reel_id"] for r in conn.execute(
            "SELECT reel_id FROM reel_reactions WHERE user_id=? AND reaction='save'", (user_id,)
        ).fetchall()}
    return render_template("reels/index.html",
        reels=reels,
        my_books=list(my_books),
        saved_ids=saved_ids,
        user=get_current_user()
    )


@app.route("/api/reels/feed")
@login_required
def api_reels_feed():
    user_id = session["user_id"]
    offset = int(request.args.get("offset", 0))
    with db() as conn:
        reels = _reel_feed(conn, user_id, offset=offset, limit=8)
    return jsonify(reels)


@app.route("/api/reels/react", methods=["POST"])
@login_required
def api_reels_react():
    user_id = session["user_id"]
    data = request.get_json() or {}
    reel_id = data.get("reel_id")
    reaction = data.get("reaction")  # like | dislike | save
    if not reel_id or reaction not in ("like", "dislike", "save"):
        return jsonify({"error": "invalid"}), 400

    with db() as conn:
        # Check existing
        existing = conn.execute(
            "SELECT reaction FROM reel_reactions WHERE user_id=? AND reel_id=?",
            (user_id, reel_id)
        ).fetchone()

        if existing:
            if existing["reaction"] == reaction:
                # Toggle off
                conn.execute("DELETE FROM reel_reactions WHERE user_id=? AND reel_id=?",
                             (user_id, reel_id))
                if reaction == "like":
                    conn.execute("UPDATE reels SET like_count = MAX(0, like_count-1) WHERE id=?", (reel_id,))
                return jsonify({"action": "removed"})
            else:
                old = existing["reaction"]
                conn.execute(
                    "UPDATE reel_reactions SET reaction=? WHERE user_id=? AND reel_id=?",
                    (reaction, user_id, reel_id)
                )
                if old == "like":
                    conn.execute("UPDATE reels SET like_count=MAX(0,like_count-1) WHERE id=?", (reel_id,))
                if reaction == "like":
                    conn.execute("UPDATE reels SET like_count=like_count+1 WHERE id=?", (reel_id,))
        else:
            conn.execute(
                "INSERT INTO reel_reactions (user_id, reel_id, reaction) VALUES (?,?,?)",
                (user_id, reel_id, reaction)
            )
            if reaction == "like":
                conn.execute("UPDATE reels SET like_count=like_count+1 WHERE id=?", (reel_id,))

        # Return new like count
        new_count = conn.execute("SELECT like_count FROM reels WHERE id=?", (reel_id,)).fetchone()["like_count"]

    return jsonify({"action": "set", "reaction": reaction, "like_count": new_count})


@app.route("/reels/create", methods=["POST"])
@login_required
def reels_create():
    user_id = session["user_id"]
    caption = request.form.get("content", "").strip() or None
    book_title = request.form.get("book_title", "").strip()
    book_author = request.form.get("book_author", "").strip()
    book_cover_url = request.form.get("book_cover_url", "").strip() or None
    book_genre = request.form.get("book_genre", "").strip() or None

    if not book_title:
        flash("Book title is required", "error")
        return redirect(url_for("reels_index"))

    video_path = None
    video_file = request.files.get("video")
    if video_file and video_file.filename:
        ext = video_file.filename.rsplit(".", 1)[-1].lower()
        if ext not in ALLOWED_VIDEO_EXT:
            flash("Unsupported video format. Use MP4, MOV, or WebM.", "error")
            return redirect(url_for("reels_index"))
        filename = f"{uuid.uuid4().hex}.{ext}"
        save_path = os.path.join(UPLOAD_DIR, filename)
        video_file.save(save_path)
        video_path = filename  # stored as bare filename; served via /uploads/videos/<filename>

    if not video_path and not caption:
        flash("Upload a video or add a caption.", "error")
        return redirect(url_for("reels_index"))

    if caption and len(caption) > 400:
        caption = caption[:400]

    with db() as conn:
        conn.execute("""
            INSERT INTO reels (user_id, book_title, book_author, book_cover_url, book_genre, content, video_path)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, book_title, book_author, book_cover_url, book_genre, caption, video_path))

    flash("Reel posted!", "success")
    return redirect(url_for("reels_index"))


@app.route("/api/reels/saved")
@login_required
def api_reels_saved():
    user_id = session["user_id"]
    with db() as conn:
        rows = conn.execute("""
            SELECT r.id, r.user_id, r.book_title, r.book_author, r.book_cover_url,
                   r.book_genre, r.content, r.like_count, r.created_at,
                   u.username, u.display_name,
                   'save' as my_reaction
            FROM reel_reactions rr
            JOIN reels r ON r.id = rr.reel_id
            JOIN users u ON u.id = r.user_id
            WHERE rr.user_id = ? AND rr.reaction = 'save'
            ORDER BY rr.created_at DESC
        """, (user_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/reels/mine")
@login_required
def api_reels_mine():
    user_id = session["user_id"]
    with db() as conn:
        rows = conn.execute("""
            SELECT r.id, r.user_id, r.book_title, r.book_author, r.book_cover_url,
                   r.book_genre, r.content, r.like_count, r.created_at,
                   u.username, u.display_name, NULL as my_reaction
            FROM reels r
            JOIN users u ON u.id = r.user_id
            WHERE r.user_id = ?
            ORDER BY r.created_at DESC
        """, (user_id,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/reels/<int:reel_id>/delete", methods=["POST"])
@login_required
def reels_delete(reel_id):
    user_id = session["user_id"]
    with db() as conn:
        conn.execute("DELETE FROM reels WHERE id=? AND user_id=?", (reel_id, user_id))
    return redirect(url_for("reels_index"))


# ---------------------------------------------------------------------------
# Book Clubs
# ---------------------------------------------------------------------------

def _page_bucket_label(page_start, page_end):
    if page_end:
        return f"Pages {page_start}–{page_end}"
    return f"Pages {page_start}+"


def _fetch_ol_milestones(book_title, ol_key):
    """Try to get table of contents from Open Library."""
    if not ol_key:
        return []
    try:
        clean_key = ol_key.split("/works/")[-1].split("/")[0] if "/works/" in ol_key else ol_key
        url = f"https://openlibrary.org/works/{clean_key}.json"
        req = urllib.request.Request(url, headers={"User-Agent": "Bookshelf/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read())
        toc = data.get("table_of_contents", [])
        results = []
        for i, entry in enumerate(toc):
            label = (entry.get("title") or entry.get("value") or "").strip()
            if label and len(label) > 2:
                results.append((label, None, i))
        return results[:20]
    except Exception:
        return []


def _ensure_milestones(conn, book_title, ol_key):
    existing = conn.execute(
        "SELECT id FROM book_milestones WHERE book_title=? LIMIT 1", (book_title,)
    ).fetchone()
    if existing:
        return
    milestones = _fetch_ol_milestones(book_title, ol_key)
    for label, page, order in milestones:
        conn.execute("""
            INSERT OR IGNORE INTO book_milestones
                (book_title, ol_key, label, page_approx, event_order, source)
            VALUES (?, ?, ?, ?, ?, 'openlibrary')
        """, (book_title, ol_key, label, page, order))


def _milestone_for_page(conn, book_title, current_page):
    """Return the milestone bracket the reader is currently in."""
    if current_page is None:
        current_page = 0
    return conn.execute("""
        SELECT id, label, page_approx, event_order FROM book_milestones
        WHERE book_title=? AND (page_approx IS NULL OR page_approx <= ?)
        ORDER BY event_order DESC LIMIT 1
    """, (book_title, current_page)).fetchone()


def _next_milestone(conn, book_title, current_order):
    """Return the milestone after the current one (the spoiler boundary)."""
    return conn.execute("""
        SELECT label, page_approx, event_order FROM book_milestones
        WHERE book_title=? AND event_order > ?
        ORDER BY event_order ASC LIMIT 1
    """, (book_title, current_order)).fetchone()


def _get_or_create_club(conn, book_title, book_author, cover_url, ol_key, current_page):
    """Find or create the right club for a given reading position.
    Uses story milestones when available, falls back to page buckets."""
    _ensure_milestones(conn, book_title, ol_key)
    milestone = _milestone_for_page(conn, book_title, current_page or 0)

    if milestone:
        next_m = _next_milestone(conn, book_title, milestone["event_order"])
        label = milestone["label"]
        page_start = milestone["page_approx"] or 0
        page_end = next_m["page_approx"] if next_m else None
        milestone_label = label
        milestone_type = "event"
    else:
        # Fall back to page buckets
        page_start = ((current_page or 0) // CLUB_BUCKET) * CLUB_BUCKET
        page_end = page_start + CLUB_BUCKET
        milestone_label = _page_bucket_label(page_start, page_end)
        milestone_type = "page"

    # Look for existing club
    if milestone_type == "event":
        club = conn.execute("""
            SELECT * FROM book_clubs
            WHERE book_title=? AND milestone_label=? AND milestone_type='event'
        """, (book_title, milestone_label)).fetchone()
    else:
        club = conn.execute("""
            SELECT * FROM book_clubs
            WHERE book_title=? AND page_start=? AND page_end=?
        """, (book_title, page_start, page_end)).fetchone()

    if not club:
        if milestone_type == "event" and page_end:
            desc = f'Discuss "{book_title}" up through "{milestone_label}" — no spoilers past this point!'
        elif milestone_type == "event":
            desc = f'Discuss "{book_title}" up through "{milestone_label}".'
        else:
            desc = f'Spoiler-free zone for {book_title} ({milestone_label})'

        conn.execute("""
            INSERT INTO book_clubs
                (book_title, book_author, book_cover_url, ol_key,
                 page_start, page_end, milestone_label, milestone_type, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (book_title, book_author, cover_url, ol_key,
              page_start, page_end, milestone_label, milestone_type, desc))

        if milestone_type == "event":
            club = conn.execute("""
                SELECT * FROM book_clubs WHERE book_title=? AND milestone_label=? AND milestone_type='event'
            """, (book_title, milestone_label)).fetchone()
        else:
            club = conn.execute("""
                SELECT * FROM book_clubs WHERE book_title=? AND page_start=? AND page_end=?
            """, (book_title, page_start, page_end)).fetchone()

    return club


@app.route("/clubs")
@login_required
def clubs_index():
    user_id = session["user_id"]
    with db() as conn:
        # All clubs ordered by recent activity + member count
        clubs = conn.execute("""
            SELECT bc.*, bcm_me.club_id as joined,
                   COUNT(DISTINCT bcm.user_id) as member_count,
                   MAX(bcmsg.created_at) as last_msg
            FROM book_clubs bc
            LEFT JOIN book_club_members bcm ON bcm.club_id = bc.id
            LEFT JOIN book_club_members bcm_me ON bcm_me.club_id = bc.id AND bcm_me.user_id = ?
            LEFT JOIN book_club_messages bcmsg ON bcmsg.club_id = bc.id
            GROUP BY bc.id
            ORDER BY last_msg DESC, member_count DESC
            LIMIT 50
        """, (user_id,)).fetchall()

        # Suggested clubs based on user's current reading
        reading_books = conn.execute("""
            SELECT title, author, cover_url, ol_key, current_page FROM books
            WHERE user_id=? AND status='reading' AND current_page > 0
        """, (user_id,)).fetchall()

        suggested = []
        for book in reading_books:
            club = _get_or_create_club(
                conn, book["title"], book["author"],
                book["cover_url"], book["ol_key"], book["current_page"]
            )
            if club:
                d = dict(club)
                # Attach next milestone as the spoiler boundary label
                if club["milestone_type"] == "event" and club["milestone_label"]:
                    cur_m = conn.execute("""
                        SELECT event_order FROM book_milestones
                        WHERE book_title=? AND label=? LIMIT 1
                    """, (club["book_title"], club["milestone_label"])).fetchone()
                    if cur_m:
                        nxt = _next_milestone(conn, club["book_title"], cur_m["event_order"])
                        d["next_milestone"] = nxt["label"] if nxt else None
                suggested.append(d)

        # Trending: books with most active readers
        trending = conn.execute("""
            SELECT title as book_title, author as book_author, cover_url as book_cover_url,
                   COUNT(*) as reader_count,
                   SUM(CASE WHEN status='reading' THEN 1 ELSE 0 END) as currently_reading
            FROM books
            GROUP BY title, author
            HAVING reader_count >= 1
            ORDER BY currently_reading DESC, reader_count DESC
            LIMIT 6
        """).fetchall()

    return render_template("clubs/index.html",
        clubs=list(clubs),
        suggested=suggested,
        trending=list(trending),
        user=get_current_user()
    )


@app.route("/clubs/<int:club_id>")
@login_required
def clubs_room(club_id):
    user_id = session["user_id"]
    with db() as conn:
        club = conn.execute("SELECT * FROM book_clubs WHERE id=?", (club_id,)).fetchone()
        if not club:
            abort(404)

        # Auto-join if visiting
        try:
            conn.execute(
                "INSERT INTO book_club_members (club_id, user_id) VALUES (?,?)",
                (club_id, user_id)
            )
            conn.execute(
                "UPDATE book_clubs SET member_count=member_count+1 WHERE id=?", (club_id,)
            )
        except Exception:
            pass  # Already a member

        # Update last_active
        conn.execute(
            "UPDATE book_clubs SET last_active=unixepoch() WHERE id=?", (club_id,)
        )

        messages = conn.execute("""
            SELECT bcm.id, bcm.content, bcm.created_at,
                   u.username, u.display_name
            FROM book_club_messages bcm
            JOIN users u ON u.id = bcm.user_id
            WHERE bcm.club_id=?
            ORDER BY bcm.created_at ASC
            LIMIT 200
        """, (club_id,)).fetchall()

        members = conn.execute("""
            SELECT u.username, u.display_name,
                   b.current_page, b.total_pages
            FROM book_club_members bcm
            JOIN users u ON u.id = bcm.user_id
            LEFT JOIN books b ON b.user_id = bcm.user_id
                AND LOWER(b.title) = LOWER(?) AND b.status='reading'
            WHERE bcm.club_id=?
            ORDER BY bcm.joined_at DESC
        """, (club["book_title"], club_id)).fetchall()

        # All milestone-based clubs for this book (for navigation)
        adjacent = conn.execute("""
            SELECT bc.id, bc.page_start, bc.page_end, bc.milestone_label, bc.milestone_type,
                   (SELECT COUNT(*) FROM book_club_members WHERE club_id=bc.id) as mc
            FROM book_clubs bc
            WHERE bc.book_title=? AND bc.id != ?
            ORDER BY bc.page_start ASC
        """, (club["book_title"], club_id)).fetchall()

        # All milestones for this book (for sidebar nav)
        milestones = conn.execute("""
            SELECT label, page_approx, event_order FROM book_milestones
            WHERE book_title=? ORDER BY event_order ASC
        """, (club["book_title"],)).fetchall()

        # Next milestone (spoiler boundary)
        next_milestone = None
        if club["milestone_type"] == "event" and club["milestone_label"]:
            cur_m = conn.execute("""
                SELECT event_order FROM book_milestones
                WHERE book_title=? AND label=? LIMIT 1
            """, (club["book_title"], club["milestone_label"])).fetchone()
            if cur_m:
                next_milestone = _next_milestone(conn, club["book_title"], cur_m["event_order"])

    return render_template("clubs/room.html",
        club=club,
        messages=list(messages),
        members=list(members),
        adjacent=list(adjacent),
        milestones=list(milestones),
        next_milestone=next_milestone,
        user=get_current_user()
    )


@app.route("/clubs/<int:club_id>/message", methods=["POST"])
@login_required
def clubs_message(club_id):
    user_id = session["user_id"]
    content = request.form.get("content", "").strip()
    if not content:
        return redirect(url_for("clubs_room", club_id=club_id))
    if len(content) > 1000:
        content = content[:1000]
    with db() as conn:
        conn.execute(
            "INSERT INTO book_club_messages (club_id, user_id, content) VALUES (?,?,?)",
            (club_id, user_id, content)
        )
        conn.execute(
            "UPDATE book_clubs SET last_active=unixepoch() WHERE id=?", (club_id,)
        )
    return redirect(url_for("clubs_room", club_id=club_id))


@app.route("/api/clubs/<int:club_id>/messages")
@login_required
def api_club_messages(club_id):
    after = int(request.args.get("after", 0))
    with db() as conn:
        rows = conn.execute("""
            SELECT bcm.id, bcm.content, bcm.created_at,
                   u.username, u.display_name
            FROM book_club_messages bcm
            JOIN users u ON u.id = bcm.user_id
            WHERE bcm.club_id=? AND bcm.id > ?
            ORDER BY bcm.created_at ASC
            LIMIT 50
        """, (club_id, after)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/clubs/milestones")
@login_required
def api_club_milestones():
    """Return available milestones for a book (for club creation UI)."""
    book_title = request.args.get("book_title", "").strip()
    ol_key = request.args.get("ol_key", "").strip() or None
    if not book_title:
        return jsonify([])
    with db() as conn:
        _ensure_milestones(conn, book_title, ol_key)
        rows = conn.execute("""
            SELECT label, page_approx, event_order FROM book_milestones
            WHERE book_title=? ORDER BY event_order ASC
        """, (book_title,)).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/clubs/create", methods=["POST"])
@login_required
def clubs_create():
    user_id = session["user_id"]
    book_title = request.form.get("book_title", "").strip()
    book_author = request.form.get("book_author", "").strip()
    book_cover_url = request.form.get("book_cover_url", "").strip() or None
    ol_key = request.form.get("ol_key", "").strip() or None
    milestone_label = request.form.get("milestone_label", "").strip() or None
    page_start_raw = request.form.get("page_start", "0").strip()
    page_start = int(page_start_raw) if page_start_raw.isdigit() else 0
    page_end_raw = request.form.get("page_end", "").strip()
    page_end = int(page_end_raw) if page_end_raw.isdigit() else page_start + CLUB_BUCKET

    if not book_title:
        flash("Book title is required", "error")
        return redirect(url_for("clubs_index"))

    with db() as conn:
        if milestone_label:
            # Event-based club
            existing = conn.execute("""
                SELECT id FROM book_clubs
                WHERE book_title=? AND milestone_label=? AND milestone_type='event'
            """, (book_title, milestone_label)).fetchone()
            if existing:
                flash("A club for this story point already exists!", "error")
                return redirect(url_for("clubs_room", club_id=existing["id"]))
            # Find page range for this milestone
            cur_m = conn.execute("""
                SELECT page_approx, event_order FROM book_milestones
                WHERE book_title=? AND label=? LIMIT 1
            """, (book_title, milestone_label)).fetchone()
            if cur_m:
                page_start = cur_m["page_approx"] or 0
                nxt = _next_milestone(conn, book_title, cur_m["event_order"])
                page_end = nxt["page_approx"] if nxt else None
            desc = f'Discuss "{book_title}" up through "{milestone_label}" — no spoilers past this point!'
            cur = conn.execute("""
                INSERT INTO book_clubs
                    (book_title, book_author, book_cover_url, ol_key,
                     page_start, page_end, milestone_label, milestone_type, description)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (book_title, book_author, book_cover_url, ol_key,
                  page_start, page_end, milestone_label, "event", desc))
        else:
            existing = conn.execute("""
                SELECT id FROM book_clubs WHERE book_title=? AND page_start=? AND page_end=?
            """, (book_title, page_start, page_end)).fetchone()
            if existing:
                return redirect(url_for("clubs_room", club_id=existing["id"]))
            cur = conn.execute("""
                INSERT INTO book_clubs
                    (book_title, book_author, book_cover_url, ol_key,
                     page_start, page_end, milestone_type, description)
                VALUES (?,?,?,?,?,?,?,?)
            """, (book_title, book_author, book_cover_url, ol_key,
                  page_start, page_end, "page",
                  f"Discussion for {book_title} ({_page_bucket_label(page_start, page_end)})"))

        club_id = cur.lastrowid
        conn.execute(
            "INSERT INTO book_club_members (club_id, user_id) VALUES (?,?)",
            (club_id, user_id)
        )

    flash(f'"{book_title}" book club created!', "success")
    return redirect(url_for("clubs_room", club_id=club_id))




@app.route("/import-sql-x7k9q2", methods=["GET", "POST"])
def import_sql():
    if request.method == "GET":
        return '''<form method=post enctype=multipart/form-data>
            <input type=file name=sql> <button type=submit>Upload SQL</button></form>'''
    f = request.files.get("sql")
    if not f:
        return "no file", 400
    sql = f.read().decode("utf-8")
    db_path = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "bookshelf.db"))
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db_path)
    conn.executescript(sql)
    conn.close()
    return "Done! All data imported."


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5001)

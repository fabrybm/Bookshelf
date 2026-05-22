import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "data", "bookshelf.db"))


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                username     TEXT    NOT NULL UNIQUE,
                password_hash TEXT   NOT NULL,
                display_name TEXT,
                created_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            CREATE TABLE IF NOT EXISTS books (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                title        TEXT    NOT NULL,
                author       TEXT    NOT NULL,
                cover_url    TEXT,
                ol_key       TEXT,
                total_pages  INTEGER,
                genre        TEXT,
                status       TEXT    NOT NULL DEFAULT 'want_to_read'
                             CHECK(status IN ('want_to_read','reading','read')),
                current_page INTEGER NOT NULL DEFAULT 0,
                rating       INTEGER CHECK(rating BETWEEN 1 AND 5),
                notes        TEXT,
                started_at   INTEGER,
                finished_at  INTEGER,
                created_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                updated_at   INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            CREATE TABLE IF NOT EXISTS reading_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                log_date    TEXT    NOT NULL,
                pages_read  INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                UNIQUE(user_id, book_id, log_date)
            );

            CREATE TABLE IF NOT EXISTS friendships (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                followee_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                UNIQUE(follower_id, followee_id)
            );

            CREATE TABLE IF NOT EXISTS feed_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                event_type  TEXT    NOT NULL
                            CHECK(event_type IN ('started','progress','finished','rated','added')),
                payload     TEXT,
                created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            CREATE INDEX IF NOT EXISTS idx_books_user       ON books(user_id, status);
            CREATE INDEX IF NOT EXISTS idx_reading_log_user ON reading_log(user_id, log_date);
            CREATE INDEX IF NOT EXISTS idx_feed_user        ON feed_events(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_friends_pair     ON friendships(follower_id, followee_id);

            -- Reels
            CREATE TABLE IF NOT EXISTS reels (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                book_title    TEXT    NOT NULL,
                book_author   TEXT    NOT NULL,
                book_cover_url TEXT,
                book_genre    TEXT,
                content       TEXT,
                video_path    TEXT,
                like_count    INTEGER NOT NULL DEFAULT 0,
                created_at    INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            CREATE TABLE IF NOT EXISTS reel_reactions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reel_id    INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
                reaction   TEXT    NOT NULL CHECK(reaction IN ('like','dislike','save')),
                created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                UNIQUE(user_id, reel_id)
            );

            -- Book clubs
            CREATE TABLE IF NOT EXISTS book_clubs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                book_title      TEXT    NOT NULL,
                book_author     TEXT    NOT NULL,
                book_cover_url  TEXT,
                ol_key          TEXT,
                page_start      INTEGER NOT NULL DEFAULT 0,
                page_end        INTEGER,
                milestone_label TEXT,
                milestone_type  TEXT DEFAULT 'page',
                description     TEXT,
                member_count    INTEGER NOT NULL DEFAULT 0,
                created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                last_active     INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            -- Story milestones / chapter events for book clubs
            CREATE TABLE IF NOT EXISTS book_milestones (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                book_title  TEXT    NOT NULL,
                ol_key      TEXT,
                label       TEXT    NOT NULL,
                page_approx INTEGER,
                event_order INTEGER NOT NULL DEFAULT 0,
                source      TEXT DEFAULT 'manual',
                created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
            CREATE INDEX IF NOT EXISTS idx_milestones_book ON book_milestones(book_title);

            CREATE TABLE IF NOT EXISTS book_club_members (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                club_id   INTEGER NOT NULL REFERENCES book_clubs(id) ON DELETE CASCADE,
                user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                UNIQUE(club_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS book_club_messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                club_id    INTEGER NOT NULL REFERENCES book_clubs(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                content    TEXT    NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );

            CREATE INDEX IF NOT EXISTS idx_reels_created      ON reels(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_reel_reactions_user ON reel_reactions(user_id, reel_id);
            CREATE INDEX IF NOT EXISTS idx_club_messages      ON book_club_messages(club_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_club_members       ON book_club_members(club_id, user_id);

            CREATE TABLE IF NOT EXISTS notifications (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                type        TEXT    NOT NULL,
                from_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                read        INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
            );
        """)

        # Migrate reel_reactions to allow multiple reactions per reel (like + save independent)
        rr_idx = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='sqlite_autoindex_reel_reactions_1'").fetchone()
        # Check if unique constraint is on (user_id, reel_id) — old schema — by checking the index sql
        old_unique = conn.execute("""
            SELECT sql FROM sqlite_master WHERE type='table' AND name='reel_reactions'
        """).fetchone()
        if old_unique and "UNIQUE(user_id, reel_id)" in (old_unique["sql"] or "").replace(" ", "").replace("\n",""):
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reel_reactions_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
                    reaction TEXT NOT NULL CHECK(reaction IN ('like','dislike','save')),
                    created_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
                    UNIQUE(user_id, reel_id, reaction)
                );
                INSERT OR IGNORE INTO reel_reactions_new (id,user_id,reel_id,reaction,created_at)
                    SELECT id,user_id,reel_id,reaction,created_at FROM reel_reactions;
                DROP TABLE reel_reactions;
                ALTER TABLE reel_reactions_new RENAME TO reel_reactions;
            """)

        # Add video_path column to existing reels table if missing
        cols = [r[1] for r in conn.execute("PRAGMA table_info(reels)").fetchall()]
        if "video_path" not in cols:
            conn.execute("ALTER TABLE reels ADD COLUMN video_path TEXT")
        if "content" in cols:
            # Make content nullable by accepting empty — SQLite can't DROP NOT NULL
            pass

        # Add age and gender columns to users if missing
        user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "age" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN age INTEGER")
        if "gender" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN gender TEXT")

        # Add milestone columns to book_clubs if missing
        club_cols = [r[1] for r in conn.execute("PRAGMA table_info(book_clubs)").fetchall()]
        if "milestone_label" not in club_cols:
            conn.execute("ALTER TABLE book_clubs ADD COLUMN milestone_label TEXT")
        if "milestone_type" not in club_cols:
            conn.execute("ALTER TABLE book_clubs ADD COLUMN milestone_type TEXT DEFAULT 'page'")

        _seed_milestones(conn)


# Curated story milestones for popular books (spoiler-safe labels)
_SEEDED_MILESTONES = [
    # Dune
    ("Dune", "/works/OL27448W", [
        ("Before the Journey", 0, 0),
        ("Arrival on Arrakis", 70, 1),
        ("The Water of Life", 170, 2),
        ("Desert Power", 280, 3),
        ("The Reckoning", 380, 4),
    ]),
    # Harry Potter and the Sorcerer's Stone
    ("Harry Potter and the Sorcerer's Stone", "/works/OL82563W", [
        ("Before Hogwarts", 0, 0),
        ("First Year Begins", 90, 1),
        ("The Forbidden Corridor", 150, 2),
        ("The Final Challenge", 250, 3),
    ]),
    # The Hunger Games
    ("The Hunger Games", "/works/OL8399091W", [
        ("Reaping Day", 0, 0),
        ("The Capitol", 60, 1),
        ("The Arena Begins", 130, 2),
        ("Alliance", 200, 3),
        ("The Final Battle", 280, 4),
    ]),
    # The Great Gatsby
    ("The Great Gatsby", "/works/OL468431W", [
        ("West Egg", 0, 0),
        ("The Party", 40, 1),
        ("The Reunion", 70, 2),
        ("Confrontation", 110, 3),
        ("The Aftermath", 140, 4),
    ]),
    # 1984
    ("1984", "/works/OL1168007W", [
        ("Oceania", 0, 0),
        ("The Brotherhood", 80, 1),
        ("Room 101", 190, 2),
        ("After the Ministry", 250, 3),
    ]),
    # To Kill a Mockingbird
    ("To Kill a Mockingbird", "/works/OL3279901W", [
        ("Maycomb Summers", 0, 0),
        ("The Trial Begins", 90, 1),
        ("The Verdict", 180, 2),
        ("Halloween Night", 230, 3),
    ]),
    # The Hobbit
    ("The Hobbit", "/works/OL262758W", [
        ("An Unexpected Party", 0, 0),
        ("Riddles in the Dark", 70, 1),
        ("Mirkwood", 130, 2),
        ("Lake-town", 185, 3),
        ("The Battle of Five Armies", 240, 4),
    ]),
    # Pride and Prejudice
    ("Pride and Prejudice", "/works/OL1068148W", [
        ("Netherfield Ball", 0, 0),
        ("First Proposal", 95, 1),
        ("Pemberley", 175, 2),
        ("The Letter", 230, 3),
        ("Resolution", 290, 4),
    ]),
    # The Alchemist
    ("The Alchemist", "/works/OL8062235W", [
        ("The Dream Begins", 0, 0),
        ("The Oasis", 70, 1),
        ("The Pyramids", 130, 2),
    ]),
    # Atomic Habits
    ("Atomic Habits", "/works/OL17930490W", [
        ("The Fundamentals", 0, 0),
        ("The 1st Law", 50, 1),
        ("The 2nd Law", 90, 2),
        ("The 3rd Law", 130, 3),
        ("The 4th Law", 170, 4),
        ("Advanced Tactics", 210, 5),
    ]),
]


def _seed_milestones(conn):
    for book_title, ol_key, events in _SEEDED_MILESTONES:
        existing = conn.execute(
            "SELECT id FROM book_milestones WHERE book_title=? LIMIT 1", (book_title,)
        ).fetchone()
        if existing:
            continue
        for label, page, order in events:
            conn.execute("""
                INSERT INTO book_milestones (book_title, ol_key, label, page_approx, event_order, source)
                VALUES (?, ?, ?, ?, ?, 'seed')
            """, (book_title, ol_key, label, page, order))

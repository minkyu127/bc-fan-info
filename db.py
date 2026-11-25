"""데이터베이스(SQLite) 관리.

모든 데이터는 data/blackcombat.db 파일 하나에 저장됩니다.
- 자동 수집된 데이터: fighters, rankings, fighter_fights, events, event_fights, signings
- 직접 수정한 내용: overrides 테이블 (자동 수집이 덮어쓰지 않음)
"""
import os
import shutil
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "blackcombat.db")

# Vercel(서버리스)에서는 프로그램 폴더에 쓸 수 없어서, 함께 올린 DB를 /tmp로 복사해 읽기 전용처럼 씀
READ_ONLY = bool(os.environ.get("VERCEL"))
if READ_ONLY:
    _bundled = DB_PATH
    import tempfile
    DATA_DIR = os.path.join(tempfile.gettempdir(), "bcdata")  # Vercel에서는 /tmp/bcdata
    DB_PATH = os.path.join(DATA_DIR, "blackcombat.db")
    if not os.path.exists(DB_PATH) and os.path.exists(_bundled):
        os.makedirs(DATA_DIR, exist_ok=True)
        shutil.copyfile(_bundled, DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fighters (
    id TEXT PRIMARY KEY,          -- 공식 사이트 선수 번호, 없으면 'n:나무위키문서명'
    name TEXT,                    -- 본명 (한국어)
    name_en TEXT,                 -- 공식 사이트 표기 이름
    ring_name TEXT,               -- 링네임
    weight_class TEXT,
    country TEXT,                 -- 국가 코드 (kr, br ...)
    age INTEGER,
    birth TEXT,
    height TEXT,
    team TEXT,
    wins INTEGER, losses INTEGER, draws INTEGER,
    record_text TEXT,             -- 통산 전적 원문 (예: 9전 9승)
    bc_record_text TEXT,          -- 블랙컴뱃 전적 원문
    note TEXT,                    -- 비고 (예: 블랙 테이블 영입)
    official_url TEXT,
    namu_title TEXT,
    photo_url TEXT,
    updated_at TEXT,
    summary TEXT                  -- 선수 서사 요약 (나무위키 개요)
);
CREATE TABLE IF NOT EXISTS rankings (
    weight_class TEXT,
    sort_order INTEGER,           -- 체급 표시 순서
    rank INTEGER,                 -- 0 = 챔피언
    fighter_id TEXT,
    change TEXT,
    PRIMARY KEY (weight_class, rank, fighter_id)
);
CREATE TABLE IF NOT EXISTS fighter_fights (
    fighter_id TEXT,
    seq INTEGER,
    date TEXT,
    event TEXT,
    opponent TEXT,
    result_text TEXT,
    outcome TEXT,                 -- W / L / D / NC
    method TEXT,                  -- KO/TKO, 서브미션, 판정, 기타
    note TEXT,
    source TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    name TEXT,
    date TEXT,                    -- YYYY-MM-DD
    venue TEXT,
    main_event TEXT,
    series TEXT,
    namu_title TEXT,
    source TEXT                   -- namu / manual
);
CREATE TABLE IF NOT EXISTS event_fights (
    event_id TEXT,
    bout_order INTEGER,
    bout_label TEXT,
    fighter_a TEXT, fighter_a_id TEXT,
    fighter_b TEXT, fighter_b_id TEXT,
    result_text TEXT,
    winner TEXT,
    scorecard TEXT,               -- 판정 채점 (예: 30-27, 29-28, 30-27)
    video_url TEXT,               -- 블랙컴뱃 공식 유튜브 경기 영상
    story TEXT,                   -- 경기 이야기 요약 (나무위키 대회 문서)
    story_ref TEXT                -- 원문 위치 (나무위키 문단 번호)
);
CREATE TABLE IF NOT EXISTS signings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    year TEXT,
    name TEXT,
    ring_name TEXT,
    fighter_id TEXT,
    program TEXT,                 -- 블랙 테이블 / 직접 입력 ...
    description TEXT,
    source TEXT                   -- namu / manual
);
CREATE TABLE IF NOT EXISTS fighter_stories (   -- 선수 문서의 'vs. 상대' 경기 이야기
    fighter_id TEXT,
    opponent TEXT,
    ref TEXT,
    text TEXT
);
CREATE TABLE IF NOT EXISTS overrides (
    entity TEXT, entity_id TEXT, field TEXT, value TEXT,
    PRIMARY KEY (entity, entity_id, field)
);
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,          -- 기사/영상 주소
    kind TEXT,                    -- article / video
    title TEXT,
    source TEXT,
    url TEXT,
    published TEXT,               -- ISO 날짜시간
    category TEXT,                -- 도발·신경전 / 기자회견 / 이슈 / 영입 / 경기 결과 / 일반
    thumb TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript(SCHEMA)
        # 예전에 만든 DB에 새 칸 추가 (업그레이드)
        cols = [r["name"] for r in db.execute("PRAGMA table_info(event_fights)")]
        if "scorecard" not in cols:
            db.execute("ALTER TABLE event_fights ADD COLUMN scorecard TEXT")
        for col in ("video_url", "story", "story_ref"):
            if col not in cols:
                db.execute(f"ALTER TABLE event_fights ADD COLUMN {col} TEXT")
        fcols = [r["name"] for r in db.execute("PRAGMA table_info(fighters)")]
        if "summary" not in fcols:
            db.execute("ALTER TABLE fighters ADD COLUMN summary TEXT")


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


# ---------- 직접 수정(overrides) ----------

def load_overrides(db, entity):
    out = {}
    for r in db.execute("SELECT entity_id, field, value FROM overrides WHERE entity=?", (entity,)):
        out.setdefault(r["entity_id"], {})[r["field"]] = r["value"]
    return out


def apply_overrides(rows, overrides, key="id"):
    """DB 행 목록에 직접 수정한 값을 덮어씌워 dict 목록으로 돌려줌."""
    result = []
    for r in rows:
        d = dict(r)
        ov = overrides.get(str(d.get(key)))
        if ov:
            d.update(ov)
            d["_edited"] = sorted(ov.keys())
        result.append(d)
    return result


def save_override(db, entity, entity_id, field, value):
    if value is None or value == "":
        db.execute("DELETE FROM overrides WHERE entity=? AND entity_id=? AND field=?",
                   (entity, entity_id, field))
    else:
        db.execute("INSERT OR REPLACE INTO overrides VALUES (?, ?, ?, ?)",
                   (entity, entity_id, field, value))

"""블랙컴뱃 팬 정보 사이트 (비공식).

내 PC에서 실행: 시작.bat 더블클릭 → 브라우저에서 http://127.0.0.1:5000
서버에서 실행: gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 app:app
  환경 변수 ADMIN_PASSWORD (관리 비밀번호), SECRET_KEY, AUTO_UPDATE_HOURS (자동 업데이트 주기)
"""
import functools
import hmac
import os
import re
import secrets
import threading
import time
import webbrowser
from datetime import date
from threading import Timer

from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for

import db as dbm
import scraper

app = Flask(__name__)

# ---------------------------------------------------------------- 관리자 보호
# 내 PC에서 시작.bat으로 실행하면 LOCAL_MODE (로그인 없이 관리 가능).
# 서버에서는 ADMIN_PASSWORD 환경 변수로 정한 비밀번호를 알아야 관리할 수 있음.
LOCAL_MODE = False
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
PUBLIC_UPDATE_COOLDOWN_MIN = 30  # 다른 사람이 누르는 업데이트는 30분에 한 번만


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    os.makedirs(dbm.DATA_DIR, exist_ok=True)
    path = os.path.join(dbm.DATA_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def is_admin():
    return LOCAL_MODE or bool(session.get("admin"))


def admin_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if dbm.READ_ONLY:  # Vercel 같은 읽기 전용 서버에서는 관리 기능을 쓰지 않음
            return render_template("readonly.html"), 200
        if not is_admin():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def public_update_allowed(db):
    return dbm.get_meta(db, "allow_public_update", "0") == "1"

COUNTRIES = {
    "kr": "대한민국", "br": "브라질", "jp": "일본", "mn": "몽골", "ru": "러시아", "kz": "카자흐스탄",
    "kg": "키르기스스탄", "us": "미국", "cn": "중국", "uz": "우즈베키스탄", "tj": "타지키스탄",
    "ua": "우크라이나", "ph": "필리핀", "th": "태국", "fr": "프랑스", "gb": "영국", "ie": "아일랜드",
    "pe": "페루", "ar": "아르헨티나", "mx": "멕시코", "ca": "캐나다", "au": "호주", "az": "아제르바이잔",
    "ge": "조지아", "am": "아르메니아", "tr": "튀르키예", "ir": "이란", "in": "인도", "id": "인도네시아",
    "vn": "베트남", "tw": "대만", "ve": "베네수엘라", "co": "콜롬비아", "cl": "칠레", "it": "이탈리아",
    "de": "독일", "es": "스페인", "pl": "폴란드", "nz": "뉴질랜드", "za": "남아공", "ng": "나이지리아",
    "cm": "카메룬", "hk": "홍콩", "sg": "싱가포르", "my": "말레이시아", "pt": "포르투갈", "nl": "네덜란드",
    "se": "스웨덴", "af": "아프가니스탄", "kp": "북한", "by": "벨라루스", "md": "몰도바", "tm": "투르크메니스탄",
}

FIGHTER_FIELDS = [  # (필드, 화면 이름) — 관리자 수정 화면에 쓰임
    ("name", "이름"), ("ring_name", "링네임"), ("name_en", "영문/공식 표기"),
    ("weight_class", "체급"), ("country", "국가 코드 (kr, br ...)"), ("age", "나이"),
    ("birth", "생년월일"), ("height", "키"), ("team", "소속팀"),
    ("wins", "승"), ("losses", "패"), ("draws", "무"),
    ("record_text", "통산 전적 (글)"), ("bc_record_text", "블랙컴뱃 전적 (글)"),
    ("note", "비고"), ("photo_url", "사진 주소"),
]
EVENT_FIELDS = [("name", "대회 이름"), ("date", "날짜 (YYYY-MM-DD)"), ("venue", "장소"),
                ("main_event", "메인 이벤트"), ("series", "시리즈")]
SERIES = ["넘버링", "블랙컵", "대항전", "라이즈", "챔피언스 리그", "비긴즈", "기타"]
HIDDEN_SERIES = {"프로 오디션"}  # 경기 일정에서 빼는 종류


# ---------------------------------------------------------------- 데이터 읽기

def fighters_all(db):
    rows = db.execute("SELECT * FROM fighters").fetchall()
    return {f["id"]: f for f in dbm.apply_overrides(rows, dbm.load_overrides(db, "fighter"))}


def events_all(db):
    rows = db.execute("SELECT * FROM events").fetchall()
    evs = dbm.apply_overrides(rows, dbm.load_overrides(db, "event"))
    evs = [e for e in evs if e.get("series") not in HIDDEN_SERIES]
    return sorted(evs, key=lambda e: e.get("date") or "9999-99-99")


def ranking_map(db):
    """fighter_id → (체급, 순위)"""
    out = {}
    for r in db.execute("SELECT * FROM rankings ORDER BY sort_order, rank"):
        out.setdefault(r["fighter_id"], (r["weight_class"], r["rank"]))
    return out


def event_fights(db, event_id):
    return [dict(r) for r in db.execute(
        "SELECT * FROM event_fights WHERE event_id=? ORDER BY bout_order", (event_id,))]


def fight_stats(fights):
    s = {"W": 0, "L": 0, "D": 0, "NC": 0,
         "win_by": {"KO/TKO": 0, "서브미션": 0, "판정": 0, "기타": 0},
         "loss_by": {"KO/TKO": 0, "서브미션": 0, "판정": 0, "기타": 0}}
    for f in fights:
        o = f.get("outcome")
        if o in s:
            s[o] += 1
        if o == "W":
            s["win_by"][f.get("method") or "기타"] += 1
        elif o == "L":
            s["loss_by"][f.get("method") or "기타"] += 1
    return s


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def streak(fights):
    """최근 연승/연패"""
    seq = [f["outcome"] for f in fights if f.get("outcome") in ("W", "L", "D")]
    if not seq:
        return ""
    last, n = seq[-1], 0
    for o in reversed(seq):
        if o != last:
            break
        n += 1
    return {"W": f"{n}연승", "L": f"{n}연패", "D": f"{n}연속 무승부"}[last]


def country_name(code):
    return COUNTRIES.get((code or "").lower(), (code or "").upper())


def rank_label(rank):
    return "챔피언" if rank == 0 else f"#{rank}"


def ddays(iso):
    if not iso:
        return None
    return (date.fromisoformat(iso) - date.today()).days


def fmt_date(iso):
    if not iso:
        return "날짜 미정"
    d = date.fromisoformat(iso)
    return f"{d.year}.{d.month:02d}.{d.day:02d} ({'월화수목금토일'[d.weekday()]})"


# 화면(템플릿) 어디서나 쓸 수 있는 함수
app.jinja_env.globals.update(country_name=country_name, rank_label=rank_label,
                             ddays=ddays, fmt_date=fmt_date)


@app.context_processor
def page_info():
    with dbm.get_db() as db:
        last = dbm.get_meta(db, "last_update")
        allow = public_update_allowed(db)
    return dict(last_update=last, update_status=scraper.STATUS,
                is_admin=is_admin() and not dbm.READ_ONLY,
                public_update=allow and not dbm.READ_ONLY, read_only=dbm.READ_ONLY)


# ---------------------------------------------------------------- 페이지

@app.route("/")
def home():
    today = date.today().isoformat()
    with dbm.get_db() as db:
        evs = events_all(db)
        upcoming = [e for e in evs if is_upcoming(e, today)]
        past = [e for e in evs if e.get("date") and e["date"] < today]
        next_event = upcoming[0] if upcoming else None
        next_fights = event_fights(db, next_event["id"]) if next_event else []
        recent = None
        for e in reversed(past):
            fights = event_fights(db, e["id"])
            if any(f["result_text"] for f in fights):
                recent = (e, fights)
                break
        fighters = fighters_all(db)
        champs = [(r["weight_class"], fighters.get(r["fighter_id"])) for r in
                  db.execute("SELECT * FROM rankings WHERE rank=0 ORDER BY sort_order")]
        signings = [dict(r) for r in db.execute(
            "SELECT * FROM signings ORDER BY id DESC LIMIT 6")]
        latest_news = [dict(r) for r in db.execute(
            "SELECT * FROM news WHERE kind='article' ORDER BY published DESC LIMIT 6")]
        empty = not fighters
    return render_template("home.html", next_event=next_event, next_fights=next_fights,
                           upcoming=upcoming[1:4], recent=recent, champs=champs,
                           signings=signings, empty=empty, fighters=fighters,
                           latest_news=latest_news, news_cats=NEWS_CATS)


def is_upcoming(e, today):
    """날짜가 없는 대항전처럼 이미 끝난 행사는 예정으로 보지 않음"""
    if e.get("date"):
        return e["date"] >= today
    return e.get("series") != "대항전"


@app.route("/schedule")
def schedule():
    series = request.args.get("series", "")
    today = date.today().isoformat()
    with dbm.get_db() as db:
        evs = events_all(db)
        counts = {r["event_id"]: r["n"] for r in db.execute(
            "SELECT event_id, COUNT(*) n FROM event_fights GROUP BY event_id")}
    if series:
        evs = [e for e in evs if e.get("series") == series]
    upcoming = [e for e in evs if is_upcoming(e, today)]
    past = list(reversed([e for e in evs if not is_upcoming(e, today)]))
    by_year = {}
    for e in past:
        if e.get("date"):
            by_year.setdefault(e["date"][:4], []).append(e)
    undated = [e for e in past if not e.get("date")]
    if undated:
        by_year["날짜 미상"] = undated
    return render_template("schedule.html", upcoming=upcoming, by_year=by_year,
                           series=series, series_list=SERIES, counts=counts)


@app.route("/event/<path:eid>")
def event(eid):
    with dbm.get_db() as db:
        evs = {e["id"]: e for e in events_all(db)}
        e = evs.get(eid)
        if not e:
            abort(404)
        fights = event_fights(db, eid)
        fighters = fighters_all(db)
    return render_template("event.html", e=e, fights=list(reversed(fights)), fighters=fighters,
                           done=not is_upcoming(e, date.today().isoformat()))


@app.route("/results")
def results():
    today = date.today().isoformat()
    with dbm.get_db() as db:
        fighters = fighters_all(db)
        groups = []
        for e in reversed(events_all(db)):
            if is_upcoming(e, today):
                continue
            fights = [x for x in reversed(event_fights(db, e["id"])) if x["result_text"]]
            if fights:
                groups.append((e, fights))
    years = sorted({e["date"][:4] for e, _ in groups if e.get("date")}, reverse=True)
    return render_template("results.html", groups=groups, fighters=fighters, years=years,
                           series_list=SERIES, q=request.args.get("q", ""),
                           year=request.args.get("year", ""), series=request.args.get("series", ""))


NEWS_CATS = ["도발·신경전", "기자회견", "영입·계약", "경기 결과", "이슈·논란", "일반"]


def fmt_when(iso):
    """'2026-11-24T10:00+09:00' → '11.24 10:00' (올해가 아니면 연도 표시)"""
    if not iso:
        return ""
    try:
        from datetime import datetime
        d = datetime.fromisoformat(iso)
    except ValueError:
        return iso[:10]
    return d.strftime("%m.%d %H:%M") if d.year == date.today().year else d.strftime("%Y.%m.%d")


app.jinja_env.globals.update(fmt_when=fmt_when)


@app.route("/news")
def news():
    cat, kind = request.args.get("c", ""), request.args.get("k", "")
    sql, args = "SELECT * FROM news WHERE 1=1", []
    if cat:
        sql += " AND category=?"
        args.append(cat)
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    with dbm.get_db() as db:
        items = [dict(r) for r in db.execute(sql + " ORDER BY published DESC LIMIT 150", args)]
        counts = {r["category"]: r["n"] for r in db.execute(
            "SELECT category, COUNT(*) n FROM news GROUP BY category")}
    return render_template("news.html", items=items, cats=NEWS_CATS, cat=cat, kind=kind, counts=counts)


@app.route("/rankings")
def rankings():
    with dbm.get_db() as db:
        fighters = fighters_all(db)
        groups = {}
        for r in db.execute("SELECT * FROM rankings ORDER BY sort_order, rank"):
            f = fighters.get(r["fighter_id"])
            if f:
                groups.setdefault(r["weight_class"], []).append((r["rank"], r["change"], f))
    current = request.args.get("w") or (next(iter(groups)) if groups else "")
    return render_template("rankings.html", groups=groups, current=current)


@app.route("/fighters")
def fighters():
    with dbm.get_db() as db:
        fs = list(fighters_all(db).values())
        ranks = ranking_map(db)
    weights = []
    for f in fs:
        f["rank"] = ranks.get(f["id"], (None, None))[1]
        if f.get("weight_class") and f["weight_class"] not in weights:
            weights.append(f["weight_class"])
    order = scraper.WEIGHT_ORDER + ["언더그라운드", "여성부"]
    weights.sort(key=lambda w: order.index(w) if w in order else 99)
    fs.sort(key=lambda f: (order.index(f["weight_class"]) if f.get("weight_class") in order else 99,
                           f["rank"] if f["rank"] is not None else 999, f.get("name") or ""))
    countries = sorted({f.get("country") for f in fs if f.get("country")})
    return render_template("fighters.html", fighters=fs, weights=weights, countries=countries)


def fighter_bundle(db, fid):
    fighters = fighters_all(db)
    f = fighters.get(fid)
    if not f:
        return None
    fights = [dict(r) for r in db.execute(
        "SELECT * FROM fighter_fights WHERE fighter_id=? ORDER BY seq", (fid,))]
    bc = []
    evs = {e["id"]: e for e in events_all(db)}
    for r in db.execute("SELECT * FROM event_fights WHERE fighter_a_id=? OR fighter_b_id=?", (fid, fid)):
        r = dict(r)
        mine = r["fighter_a"] if r["fighter_a_id"] == fid else r["fighter_b"]
        opp = r["fighter_b"] if r["fighter_a_id"] == fid else r["fighter_a"]
        opp_id = r["fighter_b_id"] if r["fighter_a_id"] == fid else r["fighter_a_id"]
        if not r["result_text"]:
            outcome = ""
        elif r["winner"] and r["winner"] == mine:
            outcome = "W"
        elif r["winner"] and r["winner"] == opp:
            outcome = "L"
        else:
            outcome = scraper.outcome_of(r["result_text"]) if "무" in r["result_text"] else ""
        e = evs.get(r["event_id"], {})
        bc.append({**r, "opp": opp, "opp_id": opp_id if opp_id in fighters else None,
                   "outcome": outcome, "event": e, "date": e.get("date") or ""})
    bc.sort(key=lambda x: x["date"], reverse=True)
    rank = ranking_map(db).get(fid)
    stats = fight_stats(fights)
    if not fights:  # 나무위키 전적이 없으면 공식 승/패 사용
        stats.update(W=to_int(f.get("wins")), L=to_int(f.get("losses")), D=to_int(f.get("draws")))
    return {"f": f, "fights": fights, "bc": bc, "rank": rank, "stats": stats,
            "streak": streak(fights)}


@app.route("/fighter/<path:fid>")
def fighter(fid):
    with dbm.get_db() as db:
        b = fighter_bundle(db, fid)
        signing = db.execute("SELECT * FROM signings WHERE fighter_id=?", (fid,)).fetchone()
    if not b:
        abort(404)
    return render_template("fighter.html", **b, signing=signing)


@app.route("/compare")
def compare():
    a_id, b_id = request.args.get("a", ""), request.args.get("b", "")
    with dbm.get_db() as db:
        everyone = sorted(fighters_all(db).values(), key=lambda f: f.get("name") or "")
        a = fighter_bundle(db, a_id) if a_id else None
        b = fighter_bundle(db, b_id) if b_id else None
    return render_template("compare.html", everyone=everyone, a=a, b=b, a_id=a_id, b_id=b_id)


@app.route("/signings")
def signings():
    program = request.args.get("p", "")
    with dbm.get_db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM signings ORDER BY id")]
        fighters = fighters_all(db)
    programs = []
    for s in rows:
        if s["program"] not in programs:
            programs.append(s["program"])
    if program:
        rows = [s for s in rows if s["program"] == program]

    def group_key(g):  # '2026' / '4차' → 숫자 큰 것 먼저
        m = re.search(r"\d+", g or "")
        return int(m.group()) if m else -1

    sections = {}
    for s in rows:
        sections.setdefault(s["program"], {}).setdefault(s["year"] or "기타", []).append(s)
    for p in sections:
        sections[p] = dict(sorted(sections[p].items(), key=lambda kv: group_key(kv[0]), reverse=True))
    return render_template("signings.html", sections=sections, fighters=fighters,
                           programs=programs, program=program)


@app.route("/favorites")
def favorites():
    return render_template("favorites.html")


@app.route("/api/fighters")
def api_fighters():
    ids = [i for i in request.args.get("ids", "").split(",") if i]
    with dbm.get_db() as db:
        fs = fighters_all(db)
        ranks = ranking_map(db)
    out = []
    for i in ids:
        f = fs.get(i)
        if f:
            w, r = ranks.get(i, (f.get("weight_class"), None))
            out.append({"id": i, "name": f.get("name"), "ring_name": f.get("ring_name"),
                        "weight_class": f.get("weight_class"), "rank": r,
                        "record": f"{to_int(f.get('wins'))}승 {to_int(f.get('losses'))}패 {to_int(f.get('draws'))}무",
                        "photo_url": f.get("photo_url"), "team": f.get("team"),
                        "url": url_for("fighter", fid=i)})
    return jsonify(out)


# ---------------------------------------------------------------- 관리자

@app.route("/login", methods=["GET", "POST"])
def login():
    if dbm.READ_ONLY:
        return render_template("readonly.html")
    if LOCAL_MODE:
        return redirect(url_for("admin"))
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            session.clear()
            session["admin"] = True
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin"))
        time.sleep(1.5)  # 비밀번호 마구 넣어보기 방지
        error = "비밀번호가 틀렸어요." if ADMIN_PASSWORD else "서버에 관리 비밀번호(ADMIN_PASSWORD)가 설정되지 않았어요."
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.post("/update")
def public_update():
    """관리자가 허용한 경우 누구나 누를 수 있는 빠른 업데이트"""
    if dbm.READ_ONLY:
        abort(403)
    with dbm.get_db() as db:
        if not (is_admin() or public_update_allowed(db)):
            abort(403)
        last_ts = float(dbm.get_meta(db, "last_update_ts", "0") or 0)
    if not is_admin() and time.time() - last_ts < PUBLIC_UPDATE_COOLDOWN_MIN * 60:
        return redirect(request.referrer or url_for("home"))
    scraper.run_update("quick")
    return redirect(request.referrer or url_for("home"))


@app.post("/admin/settings")
@admin_required
def admin_settings():
    with dbm.get_db() as db:
        dbm.set_meta(db, "allow_public_update", "1" if request.form.get("allow_public_update") else "0")
    return redirect(url_for("admin"))


@app.route("/admin")
@admin_required
def admin():
    with dbm.get_db() as db:
        counts = {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("fighters", "rankings", "events", "event_fights", "signings", "overrides")}
        edits = [dict(r) for r in db.execute("SELECT * FROM overrides ORDER BY entity, entity_id")]
        manual_events = [dict(r) for r in db.execute("SELECT * FROM events WHERE source='manual'")]
        manual_signings = [dict(r) for r in db.execute("SELECT * FROM signings WHERE source='manual'")]
        fighters = sorted(fighters_all(db).values(), key=lambda f: f.get("name") or "")
    return render_template("admin.html", counts=counts, edits=edits, fighters=fighters,
                           manual_events=manual_events, manual_signings=manual_signings,
                           series_list=SERIES)


@app.post("/admin/update")
@admin_required
def admin_update():
    scraper.run_update(request.form.get("mode", "quick"))
    return redirect(url_for("admin"))


@app.route("/api/status")
def api_status():
    return jsonify(scraper.STATUS)


@app.route("/admin/fighter/<path:fid>", methods=["GET", "POST"])
@admin_required
def admin_fighter(fid):
    with dbm.get_db() as db:
        raw = db.execute("SELECT * FROM fighters WHERE id=?", (fid,)).fetchone()
        if not raw:
            abort(404)
        if request.method == "POST":
            for field, _ in FIGHTER_FIELDS:
                val = request.form.get(field, "").strip()
                original = "" if raw[field] is None else str(raw[field])
                dbm.save_override(db, "fighter", fid, field, None if val == original else val)
            return redirect(url_for("fighter", fid=fid))
        ov = dbm.load_overrides(db, "fighter").get(fid, {})
    return render_template("admin_edit.html", kind="선수", fields=FIGHTER_FIELDS, raw=dict(raw),
                           ov=ov, back=url_for("fighter", fid=fid))


@app.route("/admin/event/<path:eid>", methods=["GET", "POST"])
@admin_required
def admin_event(eid):
    with dbm.get_db() as db:
        raw = db.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
        if not raw:
            abort(404)
        if request.method == "POST":
            if request.form.get("delete") and raw["source"] == "manual":
                db.execute("DELETE FROM events WHERE id=?", (eid,))
                db.execute("DELETE FROM event_fights WHERE event_id=?", (eid,))
                return redirect(url_for("schedule"))
            for field, _ in EVENT_FIELDS:
                val = request.form.get(field, "").strip()
                if raw["source"] == "manual":
                    db.execute(f"UPDATE events SET {field}=? WHERE id=?", (val, eid))
                else:
                    original = raw[field] or ""
                    dbm.save_override(db, "event", eid, field, None if val == original else val)
            # 직접 추가한 대진
            a, b = request.form.get("new_a", "").strip(), request.form.get("new_b", "").strip()
            if a and b:
                n = db.execute("SELECT COALESCE(MAX(bout_order),0)+1 FROM event_fights WHERE event_id=?",
                               (eid,)).fetchone()[0]
                ids = {f.get("name"): i for i, f in fighters_all(db).items()}
                db.execute(scraper.FIGHT_INSERT,
                           (eid, n, request.form.get("new_label", "").strip(), a, ids.get(a), b, ids.get(b),
                            request.form.get("new_result", "").strip(),
                            request.form.get("new_winner", "").strip(),
                            request.form.get("new_score", "").strip()))
            return redirect(url_for("event", eid=eid))
        ov = dbm.load_overrides(db, "event").get(eid, {})
    return render_template("admin_edit.html", kind="대회", fields=EVENT_FIELDS, raw=dict(raw), ov=ov,
                           back=url_for("event", eid=eid), event_mode=True,
                           manual=raw["source"] == "manual", series_list=SERIES)


@app.post("/admin/event-new")
@admin_required
def admin_event_new():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("admin"))
    eid = "manual:" + re.sub(r"\s+", "-", name)
    with dbm.get_db() as db:
        db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?)", (
            eid, name, request.form.get("date") or None, request.form.get("venue", ""),
            request.form.get("main_event", ""), request.form.get("series") or scraper.series_of(name),
            None, "manual"))
    return redirect(url_for("event", eid=eid))


@app.post("/admin/signing-new")
@admin_required
def admin_signing_new():
    name = request.form.get("name", "").strip()
    if name:
        with dbm.get_db() as db:
            fid = request.form.get("fighter_id") or None
            db.execute("INSERT INTO signings(year,name,ring_name,fighter_id,program,description,source) "
                       "VALUES (?,?,?,?,?,?,?)", (
                           request.form.get("year") or str(date.today().year), name,
                           request.form.get("ring_name", ""), fid,
                           request.form.get("program") or "직접 입력",
                           request.form.get("description", ""), "manual"))
    return redirect(url_for("signings"))


@app.post("/admin/signing-delete/<int:sid>")
@admin_required
def admin_signing_delete(sid):
    with dbm.get_db() as db:
        db.execute("DELETE FROM signings WHERE id=? AND source='manual'", (sid,))
    return redirect(url_for("admin"))


@app.post("/admin/override-delete")
@admin_required
def admin_override_delete():
    with dbm.get_db() as db:
        db.execute("DELETE FROM overrides WHERE entity=? AND entity_id=? AND field=?",
                   (request.form["entity"], request.form["entity_id"], request.form["field"]))
    return redirect(url_for("admin"))


@app.errorhandler(404)
def not_found(_):
    return render_template("base.html", not_found=True), 404


@app.errorhandler(403)
def forbidden(_):
    return redirect(url_for("home"))


# ---------------------------------------------------------------- 시작 & 자동 업데이트

def _first_run_update():
    dbm.init_db()
    with dbm.get_db() as db:
        empty = db.execute("SELECT COUNT(*) FROM fighters").fetchone()[0] == 0
    if empty:
        scraper.run_update("full")  # 처음 실행이면 자동으로 데이터 수집 시작


def _auto_update_loop(hours):
    """서버용: 정해진 시간마다 빠른 업데이트, 7일마다 전체 업데이트"""
    while True:
        time.sleep(600)
        try:
            with dbm.get_db() as db:
                last = float(dbm.get_meta(db, "last_update_ts", "0") or 0)
                last_full = float(dbm.get_meta(db, "last_full_ts", "0") or 0)
            now = time.time()
            if now - last_full > 7 * 86400:
                scraper.run_update("full")
            elif now - last > hours * 3600:
                scraper.run_update("quick")
        except Exception as e:  # 자동 업데이트가 실패해도 사이트는 계속 동작
            print("자동 업데이트 오류:", e, flush=True)


if os.environ.get("AUTO_UPDATE_HOURS"):  # 서버(gunicorn)에서 실행될 때
    _first_run_update()
    threading.Thread(target=_auto_update_loop, args=(float(os.environ["AUTO_UPDATE_HOURS"]),),
                     daemon=True).start()


if __name__ == "__main__":
    LOCAL_MODE = True  # 내 PC에서는 로그인 없이 관리
    _first_run_update()
    Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
